"""Workers are isolated by account; a local OS lock prevents duplicate runners."""
import contextlib
import imaplib
import logging
import os
import threading
from pathlib import Path

from .services import Summarizer, WxPusher, render
from .source import scan

LOG = logging.getLogger("mailpilot")


@contextlib.contextmanager
def worker_lock(directory):
    Path(directory).mkdir(parents=True, exist_ok=True)
    handle = open(Path(directory) / ".worker.lock", "a+b")
    handle.seek(0, 2)
    if not handle.tell():
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        yield
    except OSError as exc:
        if not locked:
            raise ValueError("这个数据目录已有 MailPilot 收件进程运行") from exc
        raise
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def safe_error(exc):
    if isinstance(exc, imaplib.IMAP4.error):
        return "邮箱认证或协议失败；检查授权码、IMAP 开关及服务商权限"
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return "网络或本地读写失败；检查网络连接、磁盘空间和目录权限"
    if isinstance(exc, ValueError):
        # Internal ValueError messages are controlled; don't echo server payloads.
        return "配置或邮件响应格式有误，请检查配置并运行 doctor"
    return "处理异常；邮件将在下次循环重试"


def process_pending(cfg, store, summarizer=None, stop=None):
    summarizer = summarizer or (Summarizer(cfg["llm"], store, should_stop=stop.is_set) if stop else Summarizer(cfg["llm"], store))
    count = 0
    for message_id, message in store.pending():
        if stop is not None and stop.is_set():
            break
        if "accounts" in cfg:
            account = next((a for a in cfg["accounts"] if a["id"] == message["account_id"]), None)
            # Opting in later never grants consent retroactively to older intake.
            message["allow_cloud"] = bool(message.get("allow_cloud") and account and account["allow_cloud"])
        previous = store.previous_result(message)
        if previous and previous.get("analysis_version") != "0.3":
            previous = None
        result = previous or summarizer.summarize(message)
        content, preview = render(message, result)
        store.complete(message_id, result, content, preview, cfg["channel"]["enabled"])
        count += 1
    return count


def dispatch_one(cfg, store, channel=None):
    if not cfg["channel"]["enabled"]:
        return False
    task = store.next_push()
    if task is None:
        return False
    channel = channel or WxPusher(cfg["channel"])
    try:
        state, error, record = channel.send(task["content"], task["preview"])
    except Exception:
        state, error, record = "unknown", "通道调用异常，发送结果不确定", ""
    store.push_result(task, state, error, record)
    store.health("推送通道", state == "accepted", error)
    LOG.info("推送任务 %s：%s", task["id"], state)
    return True


def scan_account(cfg, store, account):
    try:
        count = scan(account, store, cfg["max_message_bytes"])
        if count:
            LOG.info("账号 %s 新接收 %s 封", account["id"], count)
        return count
    except Exception as exc:
        message = safe_error(exc)
        store.health(account["id"], False, message)
        LOG.warning("账号 %s：%s", account["id"], message)
        return 0


def run_once(cfg, store):
    with worker_lock(cfg["data_dir"]):
        store.recover_inflight()
        threads = [threading.Thread(target=scan_account, args=(cfg, store, acc)) for acc in cfg["accounts"]]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        while process_pending(cfg, store):
            pass
        while dispatch_one(cfg, store):
            pass


def run_forever(cfg, store, stop):
    with worker_lock(cfg["data_dir"]):
        store.recover_inflight()

        def collector(account):
            while not stop.is_set():
                scan_account(cfg, store, account)
                stop.wait(cfg["poll_seconds"])

        def processor():
            while not stop.is_set():
                try:
                    count = process_pending(cfg, store, stop=stop)
                    store.health("摘要处理", True)
                except Exception as exc:
                    store.health("摘要处理", False, safe_error(exc))
                    count = 0
                if not count:
                    stop.wait(2)

        def dispatcher():
            while not stop.is_set():
                try:
                    active = dispatch_one(cfg, store)
                except Exception as exc:
                    store.health("推送通道", False, safe_error(exc))
                    active = False
                stop.wait(1 if active else 2)

        threads = [threading.Thread(target=collector, args=(acc,), daemon=True)
                   for acc in cfg["accounts"]]
        threads.extend([threading.Thread(target=processor, daemon=True),
                        threading.Thread(target=dispatcher, daemon=True)])
        for thread in threads:
            thread.start()
        try:
            while not stop.wait(1):
                pass
        finally:
            stop.set()
            # HTTP and IMAP calls have finite timeouts; don't leave live workers sharing a released lock.
            for thread in threads:
                thread.join()
