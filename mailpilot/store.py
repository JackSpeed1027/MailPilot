"""Durable intake and delivery; connections have short, explicit transactions."""
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class Store:
    def __init__(self, directory):
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.path = str(Path(directory) / "mailpilot.sqlite3")
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS cursors (
                    account TEXT, folder TEXT, validity TEXT, uid INTEGER,
                    PRIMARY KEY(account,folder));
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY, account TEXT, folder TEXT,
                    validity TEXT, uid INTEGER, fingerprint TEXT, payload TEXT,
                    state TEXT DEFAULT 'received', result TEXT,
                    created REAL, UNIQUE(account,folder,validity,uid));
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY, message_id INTEGER UNIQUE,
                    content TEXT, preview TEXT, state TEXT DEFAULT 'queued',
                    attempts INTEGER DEFAULT 0, next_try REAL DEFAULT 0,
                    error TEXT, record_id TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS model_usage (
                    id INTEGER PRIMARY KEY, day TEXT, input_tokens INTEGER,
                    output_tokens INTEGER, ok INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS health (
                    name TEXT PRIMARY KEY, ok INTEGER, updated REAL, error TEXT);
                CREATE INDEX IF NOT EXISTS messages_account_id ON messages(account,id DESC);
                CREATE TABLE IF NOT EXISTS replies (
                    id TEXT PRIMARY KEY, message_id INTEGER, account TEXT, sender TEXT,
                    recipient TEXT, subject TEXT, body TEXT, mail_id TEXT,
                    revision INTEGER DEFAULT 1, state TEXT DEFAULT 'draft', error TEXT DEFAULT '',
                    created REAL, updated REAL);
                CREATE TABLE IF NOT EXISTS agent_runs (
                    id TEXT PRIMARY KEY, message_id INTEGER, state TEXT, trace TEXT, error TEXT,
                    created REAL, updated REAL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover_inflight(self):
        # Only the lock-owning worker recovers. Opening the dashboard never alters tasks.
        with self.connect() as db:
            db.execute("""UPDATE outbox SET state='unknown',
                error='进程在发送期间停止，需人工检查'
                WHERE state='sending'""")

    def cursor(self, account, folder):
        with self.connect() as db:
            row = db.execute("SELECT * FROM cursors WHERE account=? AND folder=?", (account, folder)).fetchone()
            return dict(row) if row else None

    def baseline(self, account, folder, validity, uid):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO cursors VALUES (?,?,?,?)", (account, folder, str(validity), uid))

    def ingest(self, message):
        with self.connect() as db:
            cur = db.execute("""INSERT OR IGNORE INTO messages
                (account,folder,validity,uid,fingerprint,payload,created) VALUES (?,?,?,?,?,?,?)""",
                (message["account_id"], message["folder"], message["uidvalidity"], message["uid"],
                 message["fingerprint"], json.dumps(message, ensure_ascii=False), time.time()))
            # Input and cursor advance atomically; replaying older input cannot regress the cursor.
            db.execute("""INSERT INTO cursors VALUES (?,?,?,?)
                ON CONFLICT(account,folder) DO UPDATE SET
                uid=CASE WHEN cursors.validity=excluded.validity
                         THEN MAX(cursors.uid,excluded.uid) ELSE excluded.uid END,
                validity=excluded.validity""",
                (message["account_id"], message["folder"], message["uidvalidity"], message["uid"]))
            return cur.rowcount == 1

    def pending(self):
        with self.connect() as db:
            rows = db.execute("SELECT id,payload FROM messages WHERE state='received' ORDER BY id LIMIT 10").fetchall()
            return [(r["id"], json.loads(r["payload"])) for r in rows]

    def previous_result(self, message):
        with self.connect() as db:
            row = db.execute("""SELECT result FROM messages WHERE account=? AND fingerprint=?
                AND state='processed' AND result IS NOT NULL LIMIT 1""",
                (message["account_id"], message["fingerprint"])).fetchone()
            return json.loads(row["result"]) if row else None

    def complete(self, message_id, result, content, preview, channel_enabled):
        with self.connect() as db:
            msg = db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if not msg or msg["state"] != "received":
                return
            # Preserve cross-account attribution; suppress exact duplicates only in one account.
            previous = db.execute("""SELECT 1 FROM messages WHERE account=? AND fingerprint=?
                AND id<>? AND state='processed' LIMIT 1""",
                (msg["account"], msg["fingerprint"], message_id)).fetchone()
            db.execute("UPDATE messages SET state=?,result=? WHERE id=?",
                       ("duplicate" if previous else "processed", json.dumps(result, ensure_ascii=False), message_id))
            if not previous:
                db.execute("""INSERT OR IGNORE INTO outbox
                    (message_id,content,preview,state,created) VALUES (?,?,?,?,?)""",
                    (message_id, content, preview, "queued" if channel_enabled else "preview", time.time()))

    def reserve_model(self, limit):
        day = datetime.now().astimezone().strftime("%Y-%m-%d")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT COUNT(*) FROM model_usage WHERE day=?", (day,)).fetchone()[0]
            if count >= limit:
                return None
            return db.execute("INSERT INTO model_usage(day) VALUES (?)", (day,)).lastrowid

    def record_model(self, attempt, usage):
        def number(name):
            value = usage.get(name, 0) if isinstance(usage, dict) else 0
            return value if type(value) is int and value >= 0 else 0
        with self.connect() as db:
            db.execute("UPDATE model_usage SET ok=1,input_tokens=?,output_tokens=? WHERE id=?",
                       (number("prompt_tokens"), number("completion_tokens"), attempt))

    def next_push(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT * FROM outbox WHERE state='queued' AND next_try<=?
                ORDER BY id LIMIT 1""", (time.time(),)).fetchone()
            if not row:
                return None
            db.execute("UPDATE outbox SET state='sending',attempts=attempts+1 WHERE id=?", (row["id"],))
            result = dict(row)
            result["attempts"] += 1
            return result

    def push_result(self, task, state, error="", record_id=""):
        if state == "retry":
            state = "queued" if task["attempts"] < 3 else "failed"
        if state not in ("queued", "accepted", "unknown", "failed"):
            raise ValueError("未知推送状态")
        with self.connect() as db:
            db.execute("UPDATE outbox SET state=?,error=?,record_id=?,next_try=? WHERE id=?",
                       (state, error, str(record_id), time.time() + min(300, 10 ** task["attempts"]), task["id"]))

    def retry_push(self, task_id, accept_duplicate=False):
        with self.connect() as db:
            row = db.execute("SELECT state FROM outbox WHERE id=?", (task_id,)).fetchone()
            if not row or row["state"] not in ("failed", "unknown", "preview"):
                raise ValueError("仅可重试 failed、unknown 或 preview 的任务")
            if row["state"] == "unknown" and not accept_duplicate:
                raise ValueError("结果不确定，先在手机检查；确认重发需 --accept-duplicate-risk")
            db.execute("UPDATE outbox SET state='queued',attempts=0,next_try=0,error='' WHERE id=?", (task_id,))

    def health(self, name, ok, error=""):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO health VALUES (?,?,?,?)", (name, int(ok), time.time(), error))

    def status(self):
        with self.connect() as db:
            return {
                "accounts": [dict(r) for r in db.execute("SELECT * FROM health ORDER BY name")],
                "messages": db.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                "delivery": {r[0]: r[1] for r in db.execute("SELECT state,COUNT(*) FROM outbox GROUP BY state")},
                "model": dict(db.execute("""SELECT COUNT(*) attempts,COALESCE(SUM(input_tokens),0) input_tokens,
                    COALESCE(SUM(output_tokens),0) output_tokens FROM model_usage""").fetchone()),
            }

    def recent(self, limit=50):
        with self.connect() as db:
            return [dict(r) for r in db.execute("""SELECT m.id,m.payload,m.result,m.state,m.created,
                o.id AS outbox_id,o.state AS delivery_state,o.error AS delivery_error
                FROM messages m LEFT JOIN outbox o ON o.message_id=m.id
                ORDER BY m.id DESC LIMIT ?""", (limit,))]

    def browse(self, query="", account="", view="all", page=1, size=20):
        """Search all stored mail, with bound SQL parameters and bounded page size."""
        page = max(1, min(int(page), 1_000_000))
        size = max(1, min(int(size), 100))
        conditions, params = [], []
        if account:
            conditions.append("m.account=?")
            params.append(account)
        if query:
            # Treat %, _ and backslash as literal search characters, not LIKE syntax.
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append("""(COALESCE(json_extract(m.payload,'$.subject'),'') || ' ' ||
                COALESCE(json_extract(m.payload,'$.from_addrs'),'') || ' ' ||
                COALESCE(json_extract(m.payload,'$.received_account'),'') || ' ' ||
                COALESCE(json_extract(m.result,'$.summary_zh'),'') || ' ' ||
                COALESCE(json_extract(m.result,'$.action_summary'),'')) LIKE ? ESCAPE '\\'""")
            params.append("%" + escaped + "%")
        if view == "action":
            conditions.append("json_extract(m.result,'$.needs_action')=1")
        elif view == "attention":
            conditions.append("(o.state IN ('failed','unknown') OR json_extract(m.result,'$.mode')='fallback')")
        elif view == "attachments":
            conditions.append("json_array_length(json_extract(m.payload,'$.attachments'))>0")
        clause = " WHERE " + " AND ".join(conditions) if conditions else ""
        join = " FROM messages m LEFT JOIN outbox o ON o.message_id=m.id"
        with self.connect() as db:
            total = db.execute("SELECT COUNT(*)" + join + clause, params).fetchone()[0]
            pages = max(1, (total + size - 1) // size)
            page = min(page, pages)
            rows = db.execute("""SELECT m.id,m.payload,m.result,m.state,m.created,
                o.id AS outbox_id,o.state AS delivery_state,o.error AS delivery_error""" + join + clause +
                " ORDER BY m.id DESC LIMIT ? OFFSET ?", params + [size, (page - 1) * size]).fetchall()
        return {"rows": [dict(r) for r in rows], "total": total, "page": page, "pages": pages}

    def mailbox_options(self):
        with self.connect() as db:
            rows = db.execute("""SELECT m.account,json_extract(m.payload,'$.account_label') label
                FROM messages m JOIN (SELECT account,MAX(id) id FROM messages GROUP BY account) latest
                ON m.id=latest.id ORDER BY m.account""").fetchall()
            return [dict(r) for r in rows]

    def detail(self, message_id):
        with self.connect() as db:
            row = db.execute("""SELECT m.*,o.id AS outbox_id,o.state AS delivery_state,o.error AS delivery_error
                FROM messages m LEFT JOIN outbox o ON o.message_id=m.id WHERE m.id=?""", (message_id,)).fetchone()
            return dict(row) if row else None

    def save_reply(self, identity, message_id, sender, recipient, subject, body, account, revision=0):
        from email.utils import make_msgid
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM replies WHERE id=?', (identity,)).fetchone()
            if old:
                if old['state'] != 'draft' or old['revision'] != revision or old['message_id'] != message_id:
                    raise ValueError('草稿已被修改或发送，请重新打开邮件确认。')
                db.execute('UPDATE replies SET subject=?,body=?,revision=revision+1,updated=? WHERE id=?',
                           (subject, body, now, identity))
            else:
                if revision != 0:
                    raise ValueError('草稿不存在，请重新打开邮件。')
                db.execute('INSERT INTO replies(id,message_id,account,sender,recipient,subject,body,mail_id,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?)',
                           (identity, message_id, account, sender, recipient, subject, body,
                            make_msgid(domain=sender.split('@')[-1]), now, now))

    def reply(self, identity):
        with self.connect() as db:
            row = db.execute('SELECT * FROM replies WHERE id=?', (identity,)).fetchone()
            return dict(row) if row else None

    def replies_for(self, message_id):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM replies WHERE message_id=? ORDER BY created DESC LIMIT 20', (message_id,))]

    def claim_reply(self, identity, revision):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM replies WHERE id=?', (identity,)).fetchone()
            if not row or row['state'] != 'draft' or row['revision'] != revision:
                raise ValueError('这份草稿已变化或已提交发送，不能重复发送。')
            db.execute("UPDATE replies SET state='sending',updated=? WHERE id=?", (time.time(), identity))
            return dict(row)

    def reply_result(self, identity, state, error=''):
        if state not in ('accepted', 'failed', 'unknown'):
            raise ValueError('未知回复状态')
        with self.connect() as db:
            db.execute("UPDATE replies SET state=?,error=?,updated=? WHERE id=? AND state='sending'",
                       (state, error, time.time(), identity))

    def recover_replies(self):
        with self.connect() as db:
            db.execute("UPDATE replies SET state='unknown',error='程序在发送期间停止，请到原邮箱和收件方核对，勿直接重发。' WHERE state='sending'")
            db.execute("UPDATE agent_runs SET state='failed',error='程序停止，起草未完成，请重新起草。' WHERE state='running'")

    def start_agent(self, identity, message_id):
        with self.connect() as db:
            inserted = db.execute("INSERT OR IGNORE INTO agent_runs VALUES (?,?,'running','[]','',?,?)",
                                  (identity, message_id, time.time(), time.time())).rowcount
            if not inserted:
                raise ValueError('这次智能起草已提交。请回原邮件查看草稿，或重新打开起草页。')

    def finish_agent(self, identity, state, trace, error=''):
        with self.connect() as db:
            db.execute('UPDATE agent_runs SET state=?,trace=?,error=?,updated=? WHERE id=?',
                       (state, json.dumps(trace, ensure_ascii=False), error, time.time(), identity))

    def agent_run(self, identity):
        with self.connect() as db:
            row = db.execute('SELECT * FROM agent_runs WHERE id=?', (identity,)).fetchone()
            return dict(row) if row else None

    def model_diagnostics(self):
        day = datetime.now().astimezone().strftime('%Y-%m-%d')
        with self.connect() as db:
            row = db.execute('SELECT COUNT(*) attempts,COALESCE(SUM(ok),0) responses FROM model_usage WHERE day=?', (day,)).fetchone()
            return dict(day=day, **dict(row))
