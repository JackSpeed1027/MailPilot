"""Loopback-only, read-only dashboard with escaped mail and no remote resources."""
import html
import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

STATES = {
    "received": "待摘要", "processed": "已处理", "duplicate": "同账号重复邮件",
    "queued": "待推送", "sending": "发送中", "accepted": "通道已接受",
    "failed": "发送失败", "unknown": "结果不确定", "preview": "仅预览",
}
STYLE = """
:root{font-family:Segoe UI,Microsoft YaHei,sans-serif;color:#233244;background:#f4f6fb}
*{box-sizing:border-box}body{margin:0}main{max-width:1120px;margin:42px auto;padding:0 24px}
header{display:flex;justify-content:space-between;align-items:center;gap:16px}
h1{font-size:30px;margin:8px 0}h2{font-size:20px}p{line-height:1.7}
.brand{color:#4962be;font-weight:700;letter-spacing:2px}a{color:#3e5dc2;text-decoration:none}
.muted{color:#6c7a8e;font-size:14px}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:28px 0}
.card,article{background:white;border:1px solid #e1e6f0;border-radius:14px;padding:22px}
.card strong{display:block;font-size:28px;margin-top:10px}.badge{display:inline-block;padding:4px 10px;background:#edf1ff;border-radius:20px;font-size:12px;color:#4259ac}
article{margin-bottom:16px}.metadata{font-size:13px;color:#6c7a8e;line-height:1.8;overflow-wrap:anywhere}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-family:inherit;line-height:1.8;background:#f7f8fc;border-radius:10px;padding:18px}
.health{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0 24px}.good{color:#18795f}.bad{color:#b25731}
footer{margin:30px 0;font-size:13px;color:#6c7a8e}
@media(max-width:650px){main{margin:24px auto;padding:0 14px}.cards{grid-template-columns:1fr;gap:10px}.card{padding:14px}header{align-items:flex-start}h1{font-size:24px}}
"""


def esc(value):
    return html.escape(str(value), quote=True)


def stamp(value):
    return datetime.fromtimestamp(value).astimezone().strftime("%m-%d %H:%M") if value else "尚未同步"


def layout(content, demo=False):
    note = "演示数据 · 摘要为固定示例，不代表真实模型质量" if demo else "本机只读控制台 · 页面显示最近 50 封邮件"
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>MailPilot 邮件简报</title><style>{STYLE}</style></head><body><main>
    <header><div><div class="brand">MAILPILOT</div><h1>把邮件，读成重点。</h1>
    <p class="muted">{note}</p></div><a href="/">刷新列表 ↻</a></header>
    {content}<footer>“通道已接受”表示服务商创建了任务；手机是否收到仍需在设备上确认。</footer>
    </main></body></html>"""


def dashboard(store, demo=False):
    status = store.status()
    delivery = status["delivery"]
    warnings = sum(delivery.get(k, 0) for k in ("failed", "unknown"))
    content = f"""<section class="cards"><div class="card">已接收邮件<strong>{status['messages']}</strong></div>
    <div class="card">通道已接受<strong>{delivery.get('accepted', 0)}</strong></div>
    <div class="card">需要检查的推送<strong>{warnings}</strong></div></section><div class="health">"""
    for item in status["accounts"]:
        cls = "good" if item["ok"] else "bad"
        content += f"<span class='{cls}'>{esc(item['name'])} · {stamp(item['updated'])} · {esc(item['error'] or '正常')}</span>"
    content += "</div><h2>最近邮件</h2>"
    rows = store.recent()
    if not rows:
        content += "<article>还没有新邮件。首次连接只建立基线；请在连接成功后发送一封测试邮件。</article>"
    for row in rows:
        msg = json.loads(row["payload"])
        result = json.loads(row["result"]) if row["result"] else {}
        state = row["delivery_state"] or row["state"]
        content += f"""<article><span class="badge">{esc(STATES.get(state, state))}</span>
        <h2><a href="/messages/{row['id']}">{esc(msg['subject'])}</a></h2>
        <div class="metadata">发件人：{esc(', '.join(msg['from_addrs']) or '未提供')}<br>
        实际收件账号：{esc(msg['account_label'])} &lt;{esc(msg['received_account'])}&gt; · 本机接收 {stamp(row['created'])}</div>
        <p>{esc(result.get('summary_zh', '正在等待摘要'))}</p>"""
        if result.get("needs_action"):
            content += f"<p><strong>待办：</strong>{esc(result['action_summary'])}</p>"
        if row["delivery_error"]:
            content += f"<p class='bad'>{esc(row['delivery_error'])}（任务 {row['outbox_id']}）</p>"
        content += "</article>"
    model = status["model"]
    content += f"<p class='muted'>模型请求 {model['attempts']} 次 · 已记录输入 {model['input_tokens']} token / 输出 {model['output_tokens']} token</p>"
    return layout(content, demo)


def detail_page(row, demo=False):
    msg = json.loads(row["payload"])
    result = json.loads(row["result"]) if row["result"] else {}
    metadata = [
        ("发件人邮箱", ", ".join(msg["from_addrs"]) or "未提供"),
        ("邮件头收件地址（To）", ", ".join(msg["to_addrs"]) or "未公开，可能密送或转发"),
        ("抄送（Cc）", ", ".join(msg["cc_addrs"]) or "无"),
        ("实际收件账号", f"{msg['account_label']} <{msg['received_account']}>"),
        ("邮件标注时间", msg["date_header"] or "未提供"),
        ("本机接收时间", stamp(row["created"])),
        ("附件名", ", ".join(msg["attachments"]) or "无／未解析"),
        ("推送状态", STATES.get(row["delivery_state"], row["delivery_state"] or "尚未生成")),
    ]
    content = f"<article><h2>{esc(msg['subject'])}</h2>"
    for name, value in metadata:
        content += f"<p class='metadata'>{name}：{esc(value)}</p>"
    content += "<h2>中文摘要</h2><pre>" + esc(result.get("summary_zh", "待处理")) + "</pre>"
    for point in result.get("key_points", []):
        content += f"<p>• {esc(point)}</p>"
    content += "<h2>清洗后正文</h2><pre>" + esc(msg["body"] or "无可展示正文／敏感内容已隐藏") + "</pre></article>"
    return layout(content, demo)


def make_server(store, port=8788, demo=False):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            # Reject foreign Host headers to limit DNS rebinding against localhost data.
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if self.headers.get("Host") not in allowed:
                self.send_error(403)
                return
            path = urlparse(self.path).path
            if path == "/":
                self.respond(dashboard(store, demo), "text/html; charset=utf-8")
            elif path == "/api/status":
                self.respond(json.dumps(store.status(), ensure_ascii=False), "application/json; charset=utf-8")
            elif path.startswith("/messages/") and path[10:].isdigit():
                row = store.detail(int(path[10:]))
                if row:
                    self.respond(detail_page(row, demo), "text/html; charset=utf-8")
                else:
                    self.send_error(404)
            else:
                self.send_error(404)

        def respond(self, content, mime):
            body = content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
