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

    def detail(self, message_id):
        with self.connect() as db:
            row = db.execute("""SELECT m.*,o.id AS outbox_id,o.state AS delivery_state,o.error AS delivery_error
                FROM messages m LEFT JOIN outbox o ON o.message_id=m.id WHERE m.id=?""", (message_id,)).fetchone()
            return dict(row) if row else None
