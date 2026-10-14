"""Read-only TLS IMAP polling with per-account durable UID cursors."""

import imaplib
import re

from .config import secret
from .mail import parse


def fetch_bytes(response):
    for item in response:
        if isinstance(item, tuple) and isinstance(item[1], bytes):
            return item[1]
    raise ValueError("邮件响应不含正文")


def connect(account):
    client = imaplib.IMAP4_SSL(account["host"], account["port"], timeout=20)
    try:
        client.login(account["email"], secret(account["secret_env"]))
        if account.get("provider") == "163":
            # imaplib has no public ID API; use a guarded extension command.
            if b"ID" in client.capabilities or "ID" in client.capabilities:
                imaplib.Commands.setdefault("ID", ("AUTH", "SELECTED"))
                client._simple_command("ID", '("name" "MailPilot" "version" "0.1.0")')
        status, _ = client.select(account["folder"], readonly=True)
        if status != "OK":
            raise ValueError("无法打开邮箱文件夹")
        validity = client.response("UIDVALIDITY")[1]
        if not validity or not validity[0]:
            raise ValueError("服务器未提供 UIDVALIDITY")
        return client, validity[0].decode("ascii")
    except Exception:
        try:
            client.logout()
        except Exception:
            pass
        raise


def scan(account, store, max_bytes=2_000_000, reset=False, connector=connect):
    client, validity = connector(account)
    try:
        cursor = store.cursor(account["id"], account["folder"])
        if cursor is None or reset:
            status, data = client.uid("search", None, "ALL")
            if status != "OK":
                raise ValueError("无法建立邮件基线")
            uids = [int(x) for x in data[0].split()]
            store.baseline(account["id"], account["folder"], validity, max(uids, default=0))
            store.health(account["id"], True, "已建立基线：只处理此后收到的新邮件")
            return 0
        if cursor["validity"] != validity:
            store.health(account["id"], False, "UIDVALIDITY 已变化，已暂停采集；请检查邮箱并运行 baseline")
            return 0
        status, data = client.uid("search", None, f"UID {cursor['uid'] + 1}:*")
        if status != "OK":
            raise ValueError("增量查询失败")
        # IMAP range endpoints can reverse; filter UIDs ourselves.
        uids = sorted(int(x) for x in data[0].split() if int(x) > cursor["uid"])
        count = 0
        for uid in uids[:20]:
            status, metadata = client.uid("fetch", str(uid), "(RFC822.SIZE)")
            sizes = re.findall(rb"RFC822\.SIZE\s+(\d+)", b" ".join(x for x in metadata if isinstance(x, bytes)))
            if status != "OK" or not sizes:
                raise ValueError("读取邮件大小失败")
            oversized = int(sizes[0]) > max_bytes
            selector = "(BODY.PEEK[HEADER])" if oversized else "(BODY.PEEK[])"
            status, data = client.uid("fetch", str(uid), selector)
            if status != "OK":
                raise ValueError("读取邮件失败")
            raw = fetch_bytes(data)
            if len(raw) > max_bytes:
                raise ValueError("服务器返回邮件超过配置大小限制")
            message = parse(raw, account, validity, uid)
            if oversized:
                message["local_reason"] = "邮件超过大小限制，仅读取邮件头，请到邮箱查看正文和附件。"
            count += int(store.ingest(message))
        store.health(account["id"], True)
        return count
    finally:
        try:
            client.logout()
        except Exception:
            pass
