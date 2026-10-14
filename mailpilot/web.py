"""Local workbench with paged search, escaped mail and protected settings."""
import html
import json
import secrets
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
from .settings import Settings

STATES = {"received": "待摘要", "processed": "已处理", "duplicate": "重复邮件", "queued": "待推送", "sending": "发送中", "accepted": "通道已接受", "failed": "推送失败", "unknown": "需确认投递", "preview": "仅本机预览"}
VIEWS = {"all": "全部邮件", "action": "需要行动", "attention": "需要检查", "attachments": "含附件"}
STYLE = """
:root{font-family:'Segoe UI','Microsoft YaHei',sans-serif;color:#24332f;background:#f3f5f2;--green:#216653;--muted:#697770;--line:#dfe6df}*{box-sizing:border-box}body{margin:0}a{color:var(--green);text-decoration:none}a:hover{text-decoration:underline}
.shell{display:grid;grid-template-columns:220px minmax(0,1fr);min-height:100vh}.sidebar{background:#173f34;color:#e8f1ec;padding:36px 22px;position:sticky;top:0;height:100vh}.logo{font-size:23px;font-weight:750;letter-spacing:1px}.sidebar p{font-size:13px;line-height:1.8;color:#b8cec2}.nav{display:grid;gap:7px;margin-top:35px}.nav a{color:#d5e6dd;padding:12px;border-radius:8px;font-size:14px}.nav a.active{background:#2b5748;color:white}.sidebar .small{margin-top:45px}
main{max-width:1260px;width:100%;margin:auto;padding:40px 42px}header{display:flex;justify-content:space-between;gap:20px;align-items:center;margin-bottom:28px}h1{font-size:30px;letter-spacing:-.8px;margin:6px 0 10px}h2{font-size:19px;margin:10px 0 14px}h3{font-size:15px}.eyebrow{color:var(--green);font-size:12px;font-weight:700;letter-spacing:2px}.muted,.small{color:var(--muted);font-size:13px;line-height:1.7}.button,button{display:inline-block;border:1px solid var(--line);border-radius:8px;padding:11px 17px;background:white;color:var(--green);font:inherit;font-size:14px;cursor:pointer}.primary{background:var(--green);color:white;border-color:var(--green)}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:22px}.stat{background:white;border:1px solid var(--line);border-radius:12px;padding:20px}.stat strong{display:block;font-size:30px;margin-top:10px}.stat span{font-size:13px;color:var(--muted)}.health{display:flex;flex-wrap:wrap;gap:9px;margin:16px 0 22px}.chip{font-size:12px;border:1px solid var(--line);border-radius:7px;background:white;padding:8px 11px}.good{color:#216653}.bad{color:#a84731}.health .bad{background:#fff4ef}.notice{background:#e8f1ea;border:1px solid #cfdfd1;padding:15px 18px;border-radius:10px;font-size:14px;line-height:1.8;margin:16px 0}.warn{background:#fff4e6;border-color:#e8d6b7}
.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:25px 0 18px}input,select{font:inherit;font-size:14px;border:1px solid #cbd8ce;background:white;border-radius:8px;padding:11px 12px;color:#24332f;max-width:100%}input[type=search]{flex:1;min-width:180px}input:focus,select:focus,button:focus,a:focus{outline:3px solid #98bca8;outline-offset:2px}.tabs{display:flex;gap:9px;flex-wrap:wrap;margin:18px 0}.tabs a{padding:8px 13px;border-radius:20px;font-size:13px;border:1px solid var(--line);background:white}.tabs a.active{background:#216653;color:white;border-color:#216653}.section-head{display:flex;justify-content:space-between;align-items:center;margin:24px 0 12px}
.mail{background:white;border:1px solid var(--line);border-radius:12px;padding:23px 25px;margin-bottom:14px;overflow-wrap:anywhere}.mail-head{display:flex;gap:10px;align-items:center;justify-content:space-between}.tags{display:flex;gap:7px;flex-wrap:wrap}.tag{display:inline-block;font-size:11px;background:#eef2ee;color:#567061;border-radius:5px;padding:4px 8px}.tag.action{background:#fff1d8;color:#866018}.tag.warning{background:#fff0e8;color:#a84731}.mail h2{font-size:19px;line-height:1.5;margin:13px 0 9px}.meta{font-size:13px;color:var(--muted);line-height:1.9;overflow-wrap:anywhere}.summary{font-size:15px;line-height:1.85;margin:17px 0 10px;white-space:pre-wrap}.points{padding-left:20px;font-size:14px;line-height:1.85;color:#52665b}.todo{background:#f7f4ea;border-left:3px solid #cba95b;padding:11px 14px;font-size:14px;line-height:1.75;margin-top:15px}.mail-bottom{border-top:1px solid #eef1eb;padding-top:14px;margin-top:17px;display:flex;justify-content:space-between;gap:12px;font-size:13px;color:var(--muted)}
.empty{text-align:center;padding:55px 24px;border:1px dashed #bdcfc1;border-radius:12px;background:white}.pagination{display:flex;justify-content:center;align-items:center;gap:18px;margin:26px 0}footer{font-size:12px;color:var(--muted);line-height:1.8;margin-top:28px}.detail-grid{display:grid;grid-template-columns:1.7fr 1fr;gap:18px;align-items:start}.panel{background:white;border:1px solid var(--line);border-radius:12px;padding:24px;margin-bottom:18px;overflow-wrap:anywhere}.panel dl{margin:0}.panel dt{color:var(--muted);font-size:12px;margin-top:17px}.panel dd{margin:5px 0;font-size:14px;line-height:1.7}.panel pre{white-space:pre-wrap;font:inherit;font-size:14px;line-height:1.9;background:#f7f9f5;padding:17px;border-radius:8px;overflow-wrap:anywhere}.panel details summary{cursor:pointer;font-size:15px;color:var(--green)}.panel ul{line-height:1.9;padding-left:20px}.back{display:inline-block;margin-bottom:18px;font-size:14px}.setting-row{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin:20px 0}.field label{display:block;font-size:14px;margin-bottom:9px}.field input:not([type=checkbox]),.field select{width:100%}.check{display:flex;align-items:flex-start;gap:10px;font-size:14px;line-height:1.7}.check input{accent-color:var(--green);width:18px;height:18px;flex-shrink:0}.settings .mailbox{padding-top:20px;border-top:1px solid var(--line);margin-top:20px}
@media(max-width:1000px){main{padding:28px 24px}.shell{grid-template-columns:185px minmax(0,1fr)}.sidebar{padding:30px 16px}.detail-grid{grid-template-columns:1fr}}@media(max-width:700px){.shell{display:block}.sidebar{position:static;height:auto;padding:18px 20px}.sidebar p,.sidebar .small{display:none}.nav{display:flex;flex-wrap:wrap;margin-top:12px;gap:4px}.nav a{padding:8px 10px}.logo{font-size:20px}main{padding:24px 15px}header{align-items:flex-start}h1{font-size:25px}.stats{gap:8px}.stat{padding:15px 11px}.stat strong{font-size:24px}.stat span{font-size:11px}.mail{padding:18px}.mail-head{align-items:flex-start}.setting-row{grid-template-columns:1fr}.toolbar input[type=search]{width:100%}.toolbar select{flex:1}.panel{padding:19px}}
"""


def esc(value):
    return html.escape(str(value), quote=True)


def stamp(value):
    return datetime.fromtimestamp(value).astimezone().strftime("%m-%d %H:%M") if value else "尚未同步"


def link(filters=None, **changes):
    values = dict(filters or {})
    values.update(changes)
    return "/?" + urlencode({k: v for k, v in values.items() if v and v != "all"})


def layout(content, demo=False, active="all", title="邮件简报", subtitle="让来源清楚，让重点一眼可见。"):
    nav = ''.join(f'<a class="{"active" if active == key else ""}" href="{esc(link(view=key))}">{name}</a>' for key, name in VIEWS.items())
    nav += f'<a class="{"active" if active == "settings" else ""}" href="/settings">偏好设置</a>'
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MailPilot · {esc(title)}</title><style>{STYLE}</style></head><body><div class="shell"><aside class="sidebar"><div class="logo">MailPilot.</div><p>把邮件，读成重点。<br>留更多时间给重要的事。</p><nav class="nav" aria-label="主导航">{nav}</nav><p class="small">邮箱来源独立显示<br>隐私选择由你决定</p></aside><main><header><div><div class="eyebrow">YOUR MAIL, SIMPLIFIED</div><h1>{esc(title)}</h1><div class="muted">{esc(subtitle)}</div></div><a class="button" href="">刷新 ↻</a></header>{'<div class="notice warn">演示模式：固定合成内容，不会联网或发送通知。</div>' if demo else ''}{content}<footer>个人邮件工作台 · 仅在本机开放<br>“通道已接受”表示任务被接受，手机是否收到以设备为准。页面不会加载邮件里的远程图片。</footer></main></div></body></html>'''


def dashboard(store, demo=False, filters=None, cfg=None):
    filters = filters or {}
    query, account = filters.get('q', '')[:200], filters.get('account', '')[:80]
    view = filters.get('view', 'all')
    if view not in VIEWS:
        view = 'all'
    try:
        page = int(filters.get('page', 1))
    except ValueError:
        page = 1
    browse = store.browse(query, account, view, page)
    status = store.status()
    delivery = status['delivery']
    issues = sum(delivery.get(k, 0) for k in ('failed', 'unknown'))
    content = f'''<section class="stats" aria-label="运行概览"><div class="stat"><span>累计接收</span><strong>{status['messages']}</strong></div><div class="stat"><span>通道已接受</span><strong>{delivery.get('accepted', 0)}</strong></div><div class="stat"><span>待检查的推送</span><strong>{issues}</strong></div></section>'''
    names = {a['id']: a['label'] for a in (cfg or {}).get('accounts', [])}
    options = {a['account']: a['label'] for a in store.mailbox_options()}
    options.update(names)
    content += '<div class="health" aria-label="服务状态">'
    seen = set()
    for item in status['accounts']:
        seen.add(item['name'])
        content += f'<span class="chip {"good" if item["ok"] else "bad"}">{esc(names.get(item["name"], item["name"]))} · {"正常" if item["ok"] else "需检查"} · {stamp(item["updated"])}</span>'
        if item['error']:
            content += f'<span class="chip bad">{esc(item["error"])}</span>'
    for identity, name in names.items():
        if identity not in seen:
            content += f'<span class="chip">{esc(name)} · 尚未同步</span>'
    if not status['accounts'] and not names:
        content += '<span class="chip">尚无运行状态；演示或仅浏览模式不会连接邮箱。</span>'
    content += f'</div><form class="toolbar" method="get" action="/" role="search"><input type="search" name="q" aria-label="搜索邮件" placeholder="搜索主题、发件人、摘要或待办…" value="{esc(query)}"><select name="account" aria-label="筛选邮箱"><option value="">所有邮箱</option>'
    for identity, name in options.items():
        content += f'<option value="{esc(identity)}" {"selected" if identity == account else ""}>{esc(name)}</option>'
    content += f'</select><input type="hidden" name="view" value="{esc(view)}"><button class="primary" type="submit">搜索</button><a class="button" href="/">重置</a></form>'
    selected = {'q': query, 'account': account, 'view': view}
    content += '<div class="tabs">' + ''.join(f'<a class="{"active" if key == view else ""}" href="{esc(link(selected, view=key, page=1))}">{name}</a>' for key, name in VIEWS.items()) + '</div>'
    content += f'<div class="section-head"><h2>{VIEWS[view]}</h2><span class="muted">{browse["total"]} 封 · 新邮件在前</span></div>'
    if not browse['rows']:
        text = '试试其他关键词、邮箱或切回全部邮件。' if query or account or view != 'all' else '首次成功连接只建立起点，之后的新邮件才会出现在这里。请在邮箱连接正常后发一封测试邮件。'
        content += f'<section class="empty"><div class="eyebrow">A LITTLE MORE ROOM</div><h2>这里暂时没有邮件</h2><p class="muted">{text}</p></section>'
    for row in browse['rows']:
        msg = json.loads(row['payload'])
        result = json.loads(row['result']) if row['result'] else {}
        state = row['delivery_state'] or row['state']
        content += f'<article class="mail"><div class="mail-head"><div class="tags"><span class="tag">{esc(msg["account_label"])}</span>'
        if result.get('needs_action'):
            content += '<span class="tag action">需要行动</span>'
        if result.get('mode') == 'fallback':
            content += '<span class="tag warning">摘要未生成</span>'
        content += f'</div><span class="small">{stamp(row["created"])}</span></div><h2><a href="/messages/{row["id"]}">{esc(msg["subject"])}</a></h2><div class="meta">发件人：{esc(", ".join(msg["from_addrs"]) or "未提供")}<br>实际收件账号：{esc(msg["account_label"])} &lt;{esc(msg["received_account"])}&gt;</div><p class="summary">{esc(result.get("summary_zh", "正在等待生成摘要…"))}</p>'
        if result.get('key_points'):
            content += '<ul class="points">' + ''.join(f'<li>{esc(p)}</li>' for p in result['key_points']) + '</ul>'
        if result.get('needs_action'):
            content += f'<div class="todo"><strong>下一步：</strong>{esc(result.get("action_summary", ""))}</div>'
        if row['delivery_error']:
            content += f'<p class="bad small">{esc(row["delivery_error"])} · 推送任务 {row["outbox_id"]}</p>'
        attachments = len(msg.get('attachments', []))
        content += f'<div class="mail-bottom"><span>{esc(STATES.get(state, state))}{" · 附件 " + str(attachments) + " 个（未解析）" if attachments else ""}</span><a href="/messages/{row["id"]}">阅读详情 →</a></div></article>'
    content += '<div class="pagination">'
    if browse['page'] > 1:
        content += f'<a class="button" href="{esc(link(selected, page=browse["page"] - 1))}">← 上一页</a>'
    content += f'<span class="small">第 {browse["page"]} / {browse["pages"]} 页</span>'
    if browse['page'] < browse['pages']:
        content += f'<a class="button" href="{esc(link(selected, page=browse["page"] + 1))}">下一页 →</a>'
    model = status['model']
    content += f'</div><p class="small">累计模型尝试 {model["attempts"]} 次 · 已记录输入 {model["input_tokens"]} / 输出 {model["output_tokens"]} token</p>'
    return layout(content, demo, view, VIEWS[view])


def detail_page(row, demo=False):
    msg = json.loads(row['payload'])
    result = json.loads(row['result']) if row['result'] else {}
    metadata = [('发件人', ', '.join(msg['from_addrs']) or '未提供'), ('邮件头收件地址（To）', ', '.join(msg['to_addrs']) or '未公开，可能密送或转发'), ('抄送（Cc）', ', '.join(msg['cc_addrs']) or '无'), ('实际收件账号', f"{msg['account_label']} <{msg['received_account']}>"), ('邮件标注时间', msg['date_header'] or '未提供'), ('本机接收时间', stamp(row['created'])), ('推送状态', STATES.get(row['delivery_state'], row['delivery_state'] or '尚未生成'))]
    content = '<a class="back" href="/">← 返回邮件列表</a><div class="detail-grid"><div><section class="panel"><div class="eyebrow">SUMMARY</div><h2>中文摘要</h2>'
    content += f'<p class="summary">{esc(result.get("summary_zh", "正在等待摘要"))}</p>'
    if result.get('key_points'):
        content += '<ul class="points">' + ''.join(f'<li>{esc(p)}</li>' for p in result['key_points']) + '</ul>'
    if result.get('needs_action'):
        content += f'<div class="todo"><strong>下一步：</strong>{esc(result.get("action_summary", ""))}</div>'
    else:
        content += f'<p class="small">{"尚未完成摘要分析，不能据此判断没有待办。" if result.get("mode") == "fallback" or not result else "未识别到明确待办；重要事项请核对原邮件。"}</p>'
    if result.get('summary_scope') == 'partial':
        content += '<div class="notice warn">摘要只覆盖部分正文，请结合原文阅读。</div>'
    content += '</section>'
    language = result.get('source_language', 'unknown')
    names = {'en': '英语', 'ja': '日语', 'ko': '韩语', 'mixed': '混合语言', 'other': '其他外文', 'zh': '中文', 'unknown': '尚未确认'}
    status = result.get('translation_status', 'legacy')
    if language != 'zh':
        labels = {'complete': '全文翻译已完成', 'partial': '翻译未完成：仅展示已完成部分', 'failed': '翻译失败',
                  'disabled': '全文翻译未开启', 'blocked': '当前未进行翻译', 'too_long': '超过翻译长度上限',
                  'unknown_language': '语言尚未确认', 'legacy': '旧版本记录，尚无全文翻译'}
        content += f'<section class="panel"><h2>中文全文翻译</h2><p class="small">原文语言：{esc(names.get(language, "其他外文"))} · {esc(labels.get(status, status))}</p>'
        if result.get('subject_zh'):
            content += f'<h3>译文主题：{esc(result["subject_zh"])}</h3>'
        if result.get('translation_zh'):
            content += '<pre>' + esc(result['translation_zh']) + '</pre>'
        else:
            content += '<p class="muted">暂无全文译文。检查云处理授权、API Key、全文翻译开关与每日调用上限。</p>'
        if result.get('translation_note'):
            content += f'<div class="notice warn">{esc(result["translation_note"])}</div>'
        if result.get('translation_chunks_total'):
            content += f'<p class="small">已完成 {result.get("translation_chunks_done", 0)} / {result["translation_chunks_total"]} 段。机器翻译供阅读参考，重要信息请核对原文。</p>'
        content += '</section>'
    content += '<section class="panel"><h2>邮件原文（纯文本）</h2><p class="small">保留清洗后的原文，不加载远程图片，不执行邮件中的内容。</p><pre>' + esc(msg['body'] or '无可展示正文／敏感内容已隐藏') + '</pre>'
    if msg.get('body_truncated'):
        content += '<div class="notice warn">正文超过本机保留上限，这里不是完整原文。请到邮箱查看原邮件。</div>'
    if msg.get('sensitive'):
        content += '<p class="small">本机隐私规则已保护验证或登录内容，未交给模型。旧版本误判的记录没有保存正文，请重新发送测试邮件。</p>'
    content += '</section></div><div><section class="panel"><h2>邮件来源</h2><dl>'
    content += ''.join(f'<dt>{esc(name)}</dt><dd>{esc(value)}</dd>' for name, value in metadata)
    content += '</dl><p class="small">转发、别名或密送时，To 地址可能与实际收件账号不同。</p></section><section class="panel"><h2>附件</h2>'
    if msg.get('attachments'):
        content += '<ul>' + ''.join(f'<li>{esc(name)}</li>' for name in msg['attachments']) + '</ul><p class="small">当前只显示附件名称，不解析内容，也不会将附件送给模型。</p>'
    else:
        content += '<p class="small">没有附件或未获取到附件信息。</p>'
    if row.get('delivery_error'):
        content += f'<p class="bad">{esc(row["delivery_error"])} · 推送任务 {row["outbox_id"]}</p>'
    return layout(content + '</section></div></div>', demo, title=msg['subject'], subtitle='中文摘要、全文译文和原文分开展示，来源信息清楚可查。')


def settings_page(cfg=None, revision='', token='', editable=False, demo=False, error=''):
    if not cfg:
        return layout('<section class="empty"><h2>演示模式没有个人配置</h2><p class="muted">正式运行后可调整邮箱备注和摘要偏好。</p></section>', demo, 'settings', '偏好设置')
    content = '<div class="notice">设置不保存授权码或 API Key。保存后本次进程会停止，等终端显示“已停止”后重新启动。已发出的请求无法撤回，旧摘要不会自动重算。</div>'
    if not editable:
        content += '<div class="notice warn">当前仅浏览。通过启动脚本或 run 命令运行后才能保存设置。</div>'
    if error:
        content += f'<div class="notice warn" role="alert">{esc(error)}</div>'
    content += f'<form class="settings" method="post" action="/settings"><input type="hidden" name="csrf" value="{esc(token)}"><input type="hidden" name="revision" value="{esc(revision)}"><fieldset style="border:0;padding:0;margin:0" {"disabled" if not editable else ""}><section class="panel"><h2>邮箱与隐私</h2><p class="small">每个邮箱独立授权。关闭后，重启后的处理不会将该邮箱正文交给模型；之后再开启也不会补传原先未授权的邮件。</p>'
    for a in cfg['accounts']:
        identity = a['id']
        content += f'<div class="mailbox"><h3>{esc(a["email"])}</h3><div class="setting-row"><div class="field"><label for="label_{esc(identity)}">显示名称（自己起的备注）</label><input id="label_{esc(identity)}" name="label_{esc(identity)}" value="{esc(a["label"])}" required maxlength="60"></div><div class="field"><label>普通邮件云摘要</label><label class="check"><input type="checkbox" name="cloud_{esc(identity)}" {"checked" if a["allow_cloud"] else ""}><span>允许将清洗后的普通邮件主题与正文发送给模型服务商</span></label></div></div></div>'
    content += f'</section><section class="panel"><h2>摘要偏好</h2><label class="check"><input type="checkbox" name="llm_enabled" {"checked" if cfg["llm"]["enabled"] else ""}><span>启用摘要模型（对应邮箱也需要授权）</span></label><div class="setting-row"><div class="field"><label for="summary_style">摘要详细程度</label><select id="summary_style" name="summary_style">'
    for value, name in [('brief', '简短 · 最重要的事'), ('balanced', '均衡 · 重点与行动兼顾'), ('detailed', '详细 · 保留更多关键事项')]:
        content += f'<option value="{value}" {"selected" if cfg["llm"]["summary_style"] == value else ""}>{name}</option>'
    content += f'</select><p class="small">这是输出偏好，效果以模型返回为准。</p></div><div class="field"><label for="daily_attempt_limit">每日模型调用尝试上限</label><input id="daily_attempt_limit" name="daily_attempt_limit" type="number" min="1" max="10000" required value="{cfg["llm"]["daily_attempt_limit"]}"><p class="small">按本机日期计算，次数上限不是金额预算。</p></div></div><p class="small">当前模型：{esc(cfg["llm"]["model"])}</p></section><section class="panel"><h2>收件频率</h2><div class="field"><label for="poll_seconds">每隔多少秒检查新邮件</label><input id="poll_seconds" name="poll_seconds" type="number" min="15" max="3600" required value="{cfg["poll_seconds"]}"><p class="small">建议 60 秒，范围 15～3600 秒。</p></div></section><section class="panel"><h2>图片与文件解析：后续功能</h2><p class="small">当前没有附件解析。后续按邮箱、按类型选择，默认关闭，分别控制本机提取和云端处理；这里暂不提供无效开关。</p></section><button class="primary" type="submit">保存设置并停止本次运行</button></fieldset></form>'
    return layout(content, demo, 'settings', '偏好设置', '让每个邮箱按照你的习惯工作。')


def make_server(store, port=8788, demo=False, config_path=None, on_settings_saved=None, controller=None):
    settings = Settings(config_path) if config_path and not demo else None
    token = secrets.token_urlsafe(32)
    editable = bool(controller or (settings and on_settings_saved))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def local_host(self):
            return self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}

        def settings_view(self, error='', status=200):
            if controller:
                from .ui import configuration_page
                self.respond(configuration_page(controller, token, error), 'text/html; charset=utf-8', status)
                return
            try:
                cfg, revision = settings.read() if settings else (None, '')
            except (ValueError, OSError):
                self.respond(layout('<div class="notice warn">配置无法读取，请检查 config.json 格式。</div>'), 'text/html; charset=utf-8', 400)
                return
            self.respond(settings_page(cfg, revision, token, editable, demo, error), 'text/html; charset=utf-8', status)

        def do_GET(self):
            if not self.local_host():
                self.send_error(403)
                return
            target = urlparse(self.path)
            if target.path == '/':
                params = {k: v[0] for k, v in parse_qs(target.query).items() if v}
                try:
                    cfg = settings.read()[0] if settings else None
                except (ValueError, OSError):
                    cfg = None
                page = dashboard(store, demo, params, cfg)
                if controller:
                    page = page.replace('<main>', '<main><div class="notice">' + esc(controller.status) + ' <a href="/settings">打开邮箱与 API 配置</a></div>', 1)
                self.respond(page, 'text/html; charset=utf-8')
            elif target.path == '/settings':
                self.settings_view()
            elif target.path == '/api/status':
                state = store.status()
                if controller:
                    state['runtime'] = controller.status
                self.respond(json.dumps(state, ensure_ascii=False), 'application/json; charset=utf-8')
            elif target.path.startswith('/messages/') and target.path[10:].isdigit() and len(target.path) < 30:
                row = store.detail(int(target.path[10:]))
                if row:
                    self.respond(detail_page(row, demo), 'text/html; charset=utf-8')
                else:
                    self.send_error(404)
            else:
                self.send_error(404)

        def do_POST(self):
            if not self.local_host() or not editable:
                self.send_error(403)
                return
            if urlparse(self.path).path != '/settings':
                self.send_error(404)
                return
            if self.headers.get('Origin') != 'http://' + self.headers.get('Host', ''):
                self.send_error(403)
                return
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/x-www-form-urlencoded':
                self.send_error(415)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 1 <= length <= 32768:
                    self.send_error(413)
                    return
                values = parse_qs(self.rfile.read(length).decode('utf-8'), keep_blank_values=True, max_num_fields=200)
                if any(len(v) != 1 for v in values.values()):
                    self.send_error(400)
                    return
                fields = {key: value[0] for key, value in values.items()}
                if not secrets.compare_digest(fields.get('csrf', ''), token):
                    self.send_error(403)
                    return
                if controller:
                    controller.save(fields)
                else:
                    settings.save(fields)
            except (ValueError, UnicodeError) as exc:
                self.settings_view(('未保存：' + str(exc)) if controller and isinstance(exc, ValueError) and not isinstance(exc, UnicodeError) else '未保存：字段格式无效或配置已被修改。请刷新后检查备注、间隔、上限和摘要风格。', 400)
                return
            except OSError:
                self.settings_view('未保存：配置无法写入，请检查目录权限与磁盘空间。', 500)
                return
            if controller:
                self.settings_view('配置已保存。系统正在应用；请稍后刷新查看运行状态。密钥不会回显。')
                return
            on_settings_saved()
            self.respond(layout('<section class="panel"><h2>设置已保存</h2><p>收件进程正在停止。请等终端显示“已停止”后重新运行启动脚本并输入凭据。</p><p class="small">进行中的请求会等待结束。已发送内容无法撤回，新设置不自动重算历史邮件。</p></section>', title='设置已保存'), 'text/html; charset=utf-8')

        def respond(self, content, mime, status=200):
            body = content.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)
