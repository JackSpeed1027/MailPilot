"""Runnable CLI. Online test commands send synthetic, explicitly labelled data."""
import argparse
import getpass
import json
import logging
import os
import sys
import threading
from email.message import EmailMessage
from pathlib import Path

from .config import PROVIDERS, example, load, required_secrets
from .mail import parse
from .runtime import dispatch_one, process_pending, run_forever, run_once, safe_error, worker_lock
from .services import Summarizer, WxPusher, validate_result
from .source import connect, scan
from .store import Store
from .web import make_server


def write_config(path, value):
    path = Path(path)
    if path.exists():
        raise ValueError("配置文件已存在，请直接编辑；不会覆盖现有配置")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def setup(path):
    if Path(path).exists():
        raise ValueError("配置已存在；请编辑配置文件，或用 --config 指定另一个路径")
    cfg = example()
    accounts = []
    print("配置向导：凭据稍后在启动时隐藏输入，不写入配置。")
    print("普通邮件正文会发给你选择的模型服务商进行摘要，请按邮箱决定是否允许。")
    while True:
        provider = input("邮箱类型 qq / 163 / gmail（留空结束）：").strip().lower()
        if not provider:
            break
        if provider not in PROVIDERS:
            print("第一版先支持 qq、163、gmail。")
            continue
        email = input("邮箱地址：").strip()
        identity = provider + str(len(accounts) + 1)
        label = input(f"显示名称 [{provider}]：").strip() or provider
        cloud = input("允许普通邮件正文发送到摘要模型？[y/N]：").strip().lower() == "y"
        accounts.append({"id": identity, "provider": provider, "email": email, "label": label,
                         "secret_env": "MAILPILOT_" + identity.upper() + "_SECRET",
                         "allow_cloud": cloud})
    if not accounts:
        raise ValueError("未添加邮箱")
    cfg["accounts"] = accounts
    cfg["llm"]["enabled"] = any(a["allow_cloud"] for a in accounts)
    if cfg["llm"]["enabled"]:
        cfg["llm"]["base_url"] = input("模型 API 地址 [https://api.deepseek.com/v1]：").strip() or cfg["llm"]["base_url"]
        cfg["llm"]["model"] = input("模型名称 [deepseek-chat]：").strip() or cfg["llm"]["model"]
    cfg["channel"]["uid"] = input("WxPusher 本人 UID（以 UID_ 开头）：").strip()
    # Validate before writing a partially invalid configuration.
    temporary = Path(path).with_suffix(".validation.json")
    if temporary.exists():
        raise ValueError("配置校验临时文件已存在，请检查该文件")
    try:
        temporary.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        load(temporary)
    finally:
        temporary.unlink(missing_ok=True)
    write_config(path, cfg)
    print(f"已保存 {Path(path).resolve()}。运行 run --prompt-secrets，按提示输入授权码和 API Key。")


def prompt_secrets(cfg):
    for name in required_secrets(cfg):
        if not os.environ.get(name):
            os.environ[name] = getpass.getpass(f"{name}（输入隐藏）：").strip()


def ensure_secrets(cfg):
    missing = [name for name in required_secrets(cfg) if not os.environ.get(name)]
    if missing:
        raise ValueError("缺少凭据：" + "、".join(missing) + "；可加 --prompt-secrets 隐藏输入")


def sample(account, uid=1, subject="Project meeting", body=None):
    msg = EmailMessage()
    msg["From"] = "Alice <alice@example.com>"
    msg["To"] = "team@example.com"
    msg["Cc"] = "manager@example.com"
    msg["Subject"] = subject
    msg["Date"] = "Mon, 12 Oct 2026 10:00:00 +0800"
    msg["Message-ID"] = f"<mailpilot-demo-{uid}@example.com>"
    msg.set_content(body or "Please confirm the meeting on Friday at 10:00. The revised quote is CNY 12,800.")
    return parse(msg.as_bytes(), account, "demo", uid)


def demo(directory):
    store = Store(directory)
    cfg = {"llm": {"enabled": False}, "channel": {"enabled": False}}
    for provider, label, email in [("qq", "QQ 主邮箱", "demo@qq.com"),
                                   ("163", "163 邮箱", "demo@163.com"),
                                   ("gmail", "Gmail 邮箱", "demo@gmail.com")]:
        account = {"id": provider, "label": label, "email": email, "folder": "INBOX", "allow_cloud": False}
        store.ingest(sample(account))
    class DemoSummarizer:
        def summarize(self, message):
            value = validate_result({
                "summary_zh": "供应商要求确认周五上午 10 点的会议，并提供了人民币 12,800 元的修订报价。",
                "key_points": ["会议时间：周五 10:00", "修订报价：¥12,800"],
                "needs_action": True, "action_summary": "确认会议安排并核对报价。",
            })
            value["mode"] = "demo"
            return value
    while process_pending(cfg, store, DemoSummarizer()):
        pass
    print("演示已生成（固定示例摘要；未联网、未推送）。")
    print(f"数据目录：{Path(directory).resolve()}")
    print("运行 python -m mailpilot web --demo 查看。")
    return store


def doctor(cfg, online=False):
    failed = False
    print("配置：有效；首次连接仅建立新邮件基线")
    for name in required_secrets(cfg):
        present = bool(os.environ.get(name))
        print(f"凭据 {name}：{'已设置' if present else '缺失'}")
        failed |= not present
    for account in cfg["accounts"]:
        print(f"邮箱 {account['label']}：云摘要 {'允许' if account['allow_cloud'] else '关闭'}")
        if online:
            try:
                client, _ = connect(account)
                client.logout()
                print(f"  {account['id']}：TLS IMAP 登录成功（只读；未改变采集基线）")
            except Exception as exc:
                print(f"  {account['id']}：{safe_error(exc)}")
                failed = True
    print("模型真实性能和手机通知效果：请运行 test-model、test-push 及人工验收。")
    return 1 if failed else 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="MailPilot 多邮箱中文摘要助理")
    parser.add_argument("--config", default="config.json", help="配置文件；放在子命令之前")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="创建配置模板；不会覆盖已有配置")
    sub.add_parser("setup", help="配置向导，不保存密码")
    doc = sub.add_parser("doctor", help="配置和凭据自检")
    doc.add_argument("--online", action="store_true", help="实际测试 IMAP 只读登录")
    doc.add_argument("--prompt-secrets", action="store_true")
    run = sub.add_parser("run", help="运行收件、摘要、推送与本机控制台")
    run.add_argument("--once", action="store_true")
    run.add_argument("--prompt-secrets", action="store_true")
    run.add_argument("--no-web", action="store_true")
    run.add_argument("--port", type=int, default=8788)
    web = sub.add_parser("web", help="只打开本机只读控制台")
    web.add_argument("--port", type=int, default=8788)
    web.add_argument("--demo", action="store_true")
    sub.add_parser("demo", help="生成三个邮箱的离线演示")
    sub.add_parser("status", help="查看邮件、任务及 token 统计")
    push = sub.add_parser("test-push", help="真实发送一条标注为测试的通知")
    push.add_argument("--prompt-secrets", action="store_true")
    model = sub.add_parser("test-model", help="真实模型分析合成邮件，不发送真实邮件正文")
    model.add_argument("--prompt-secrets", action="store_true")
    baseline = sub.add_parser("baseline", help="跳过当前所有邮件，重新建立新邮件采集基线")
    baseline.add_argument("--account", required=True)
    baseline.add_argument("--prompt-secrets", action="store_true")
    retry = sub.add_parser("retry", help="将指定推送任务重新入队；不立即发送")
    retry.add_argument("task_id", type=int)
    retry.add_argument("--accept-duplicate-risk", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        if args.command == "init":
            write_config(args.config, example())
            print("配置模板已创建；请填写邮箱、本人 UID，并按邮箱决定 allow_cloud。")
            return 0
        if args.command == "setup":
            setup(args.config)
            return 0
        config_parent = Path(args.config).resolve().parent
        if args.command == "demo":
            demo(config_parent / "data-demo")
            return 0
        if args.command == "web" and args.demo:
            store = Store(config_parent / "data-demo")
            cfg = None
        else:
            cfg = load(args.config)
            store = Store(cfg["data_dir"])
        if getattr(args, "prompt_secrets", False):
            if args.command == "test-push":
                name = cfg["channel"]["token_env"]
                if not os.environ.get(name):
                    os.environ[name] = getpass.getpass(f"{name}（输入隐藏）：").strip()
            elif args.command == "test-model":
                name = cfg["llm"]["key_env"]
                if not os.environ.get(name):
                    os.environ[name] = getpass.getpass(f"{name}（输入隐藏）：").strip()
            else:
                prompt_secrets(cfg)
        if args.command == "doctor":
            return doctor(cfg, args.online)
        if args.command == "status":
            print(json.dumps(store.status(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "test-push":
            if not cfg["channel"]["enabled"]:
                raise ValueError("请先启用 channel.enabled")
            state, error, _ = WxPusher(cfg["channel"]).send(
                "MailPilot 通道测试\n这是合成测试消息，不含真实邮件或验证码。\n请确认手机可见。",
                "MailPilot 测试通知")
            print(f"结果：{state}；{error or '服务商已接受，请在手机确认'}")
            return 0 if state == "accepted" else 1
        if args.command == "test-model":
            if not cfg["llm"]["enabled"]:
                raise ValueError("请先启用 llm.enabled")
            account = dict(cfg["accounts"][0], allow_cloud=True)
            result = Summarizer(cfg["llm"], store).summarize(sample(account))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["mode"] == "model" else 1
        if args.command == "baseline":
            account = next((a for a in cfg["accounts"] if a["id"] == args.account), None)
            if not account:
                raise ValueError("找不到这个账号 id")
            with worker_lock(cfg["data_dir"]):
                scan(account, store, cfg["max_message_bytes"], reset=True)
            print("基线已重建；当前已有邮件被跳过，只处理此后新邮件。")
            return 0
        if args.command == "retry":
            with worker_lock(cfg["data_dir"]):
                store.retry_push(args.task_id, args.accept_duplicate_risk)
            print("任务已重新入队，下次 run 时发送。")
            return 0
        if args.command == "web":
            server = make_server(store, args.port, args.demo)
            print(f"控制台：http://127.0.0.1:{server.server_port}（Ctrl+C 关闭）")
            try:
                server.serve_forever()
            finally:
                server.server_close()
            return 0
        if args.command == "run":
            ensure_secrets(cfg)
            if args.once:
                run_once(cfg, store)
                print(json.dumps(store.status(), ensure_ascii=False, indent=2))
                return 0 if all(row["ok"] for row in store.status()["accounts"]) else 1
            stop = threading.Event()
            server = None
            if not args.no_web:
                server = make_server(store, args.port)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                print(f"控制台：http://127.0.0.1:{server.server_port}")
            print("已启动：默认每 60 秒轮询；Ctrl+C 停止。首次连接跳过历史邮件。")
            try:
                run_forever(cfg, store, stop)
            except KeyboardInterrupt:
                stop.set()
                print("\n已停止。")
            finally:
                stop.set()
                if server:
                    server.shutdown()
                    server.server_close()
            return 0
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"运行失败：{safe_error(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
