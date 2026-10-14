"""Local workbench with paged search, escaped mail and protected settings."""
import html
import json
import re
import secrets
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
from .settings import Settings
from .mail import readable
from .styles import STYLE

STATES = {"received": "待摘要", "processed": "已处理", "duplicate": "重复邮件", "queued": "待推送", "sending": "发送中", "accepted": "通道已接受", "failed": "推送失败", "unknown": "需确认投递", "preview": "仅本机预览"}
VIEWS = {"all": "全部邮件", "action": "需要行动", "attention": "需要检查", "attachments": "含附件"}



def esc(value):
    return html.escape(readable(value), quote=True)


def stamp(value):
    return datetime.fromtimestamp(value).astimezone().strftime("%m-%d %H:%M") if value else "尚未同步"


def link(filters=None, **changes):
    values = dict(filters or {})
    values.update(changes)
    return "/?" + urlencode({k: v for k, v in values.items() if v and v != "all"})


def layout(content, demo=False, active="all", title="邮件简报", subtitle=""):
    paths = {
        'all': '<rect x="3" y="5" width="18" height="14" rx="3"/><path d="m3 6 9 7 9-7"/>',
        'action': '<rect x="4" y="3" width="16" height="18" rx="3"/><path d="m8 12 3 3 5-6"/>',
        'attention': '<circle cx="12" cy="12" r="9"/><path d="M12 7v6m0 4h.01"/>',
        'attachments': '<path d="m8 12 6-6a3 3 0 0 1 4 4l-8 8a5 5 0 0 1-7-7l8-8"/>',
        'settings': '<path d="M4 6h16M4 12h16M4 18h16"/><circle cx="9" cy="6" r="2" fill="currentColor"/><circle cx="15" cy="12" r="2" fill="currentColor"/><circle cx="9" cy="18" r="2" fill="currentColor"/>',
    }
    names = dict(VIEWS, settings='设置')
    nav = ''
    for key, name in names.items():
        href = '/settings' if key == 'settings' else link(view=key)
        current = ' aria-current="page"' if active == key else ''
        nav += f'<a class="nav-{key} {"active" if active == key else ""}" href="{esc(href)}"{current}><svg viewBox="0 0 24 24" aria-hidden="true">{paths[key]}</svg><span>{name}</span></a>'
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#173e33"><title>MailPilot · {esc(title)}</title><style>{STYLE}</style></head><body><a class="skip" href="#content">跳到主要内容</a><div class="shell"><aside class="sidebar"><a class="brand" href="/" aria-label="MailPilot 邮件首页"><span class="brand-mark" aria-hidden="true">✉</span><span class="logo">MailPilot</span></a><nav class="nav" aria-label="主导航">{nav}</nav></aside><main id="content"><header><div><h1>{esc(title)}</h1>{('<div class="muted">' + esc(subtitle) + '</div>') if subtitle else ''}</div><a class="button refresh" href="" aria-label="刷新当前页面">刷新 ↻</a></header>{'<div class="notice warn">演示模式：固定合成内容，不会联网或发送通知。</div>' if demo else ''}{content}<footer>MailPilot · 本机邮件阅读</footer></main></div></body></html>'''


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
    names = {a['id']: a['label'] for a in (cfg or {}).get('accounts', [])}
    options = {a['account']: a['label'] for a in store.mailbox_options()}
    options.update(names)
    selected = {'q': query, 'account': account, 'view': view}
    issues = sum(status['delivery'].get(k, 0) for k in ('failed', 'unknown'))
    connection_errors = sum(not a['ok'] for a in status['accounts'])
    content = f'''<form class="toolbar" method="get" action="/" role="search"><input type="search" name="q" aria-label="搜索邮件" placeholder="搜索邮件…" value="{esc(query)}"><select name="account" aria-label="筛选邮箱"><option value="">所有邮箱</option>'''
    for identity, name in options.items():
        content += f'<option value="{esc(identity)}" {"selected" if identity == account else ""}>{esc(name)}</option>'
    content += f'</select><input type="hidden" name="view" value="{esc(view)}"><button class="primary" type="submit">搜索</button>'
    if query or account:
        content += f'<a class="button" href="{esc(link(view=view))}">清除筛选</a>'
    content += '</form>'
    if issues or connection_errors:
        content += '<div class="notice warn">连接或推送需要检查。<a href="#connection-status">查看状态</a></div>'
    content += f'<div class="list-caption"><span>{browse["total"]} 封邮件 · 最新在前</span><a class="mobile-attachment-filter" href="{esc(link(selected, view="all" if view == "attachments" else "attachments", page=1))}">{"全部邮件" if view == "attachments" else "只看含附件"}</a></div>'
    if not browse['rows']:
        text = '换一个关键词或邮箱试试。' if query or account or view != 'all' else '连接邮箱后，这里会显示新收到的邮件。首次连接会跳过旧邮件。'
        content += f'<section class="empty"><span class="empty-icon" aria-hidden="true">✉</span><h2>暂时没有邮件</h2><p class="muted">{text}</p><a class="button" href="/settings">邮箱设置</a></section>'
    content += '<section class="mail-list" aria-label="邮件列表">'
    for row in browse['rows']:
        msg = json.loads(row['payload'])
        result = json.loads(row['result']) if row['result'] else {}
        detail_link = '/messages/' + str(row['id']) + '?' + urlencode({**selected, 'page': browse['page']})
        content += f'<article class="mail"><div class="mail-head"><div class="tags"><span class="tag mailbox-tag">{esc(msg["account_label"])}</span>'
        if result.get('needs_action'):
            content += '<span class="tag action">待处理</span>'
        if result.get('mode') == 'fallback':
            content += '<span class="tag warning">摘要未生成</span>'
        attachments = len(msg.get('attachments', []))
        if attachments:
            content += f'<span class="tag">附件 {attachments}</span>'
        content += f'</div><time class="small">{stamp(row["created"])}</time></div><h2><a href="{esc(detail_link)}">{esc(msg["subject"])}</a></h2>'
        content += f'<div class="meta"><span><span class="meta-label">发件人</span>{esc(", ".join(msg["from_addrs"]) or "未提供")}</span><span><span class="meta-label">收件邮箱</span>{esc(msg["received_account"])}</span></div><p class="summary">{esc(result.get("summary_zh", "正在生成摘要…"))}</p>'
        if result.get('needs_action') and result.get('action_summary'):
            content += f'<p class="mail-action"><span>待办</span>{esc(result["action_summary"])}</p>'
        if row['delivery_error']:
            content += f'<p class="bad small">推送需检查：{esc(row["delivery_error"])}</p>'
        content += '</article>'
    content += '</section>'
    if browse['pages'] > 1:
        content += '<div class="pagination">'
        if browse['page'] > 1:
            content += f'<a class="button" href="{esc(link(selected, page=browse["page"] - 1))}">← 上一页</a>'
        content += f'<span class="small">第 {browse["page"]} / {browse["pages"]} 页</span>'
        if browse['page'] < browse['pages']:
            content += f'<a class="button" href="{esc(link(selected, page=browse["page"] + 1))}">下一页 →</a>'
        content += '</div>'
    expand = ' open' if issues or any(not a['ok'] for a in status['accounts']) else ''
    content += f'<details class="diagnostics" id="connection-status"{expand}><summary>连接状态与用量</summary><div class="health">'
    seen = set()
    for item in status['accounts']:
        seen.add(item['name'])
        content += f'<span class="chip {"good" if item["ok"] else "bad"}">{esc(names.get(item["name"], item["name"]))} · {"正常" if item["ok"] else "需检查"} · {stamp(item["updated"])}</span>'
        if item['error']:
            content += f'<p class="bad small">{esc(item["error"])}</p>'
    for identity, name in names.items():
        if identity not in seen:
            content += f'<span class="chip">{esc(name)} · 尚未同步</span>'
    model = status['model']
    content += f'</div><p class="small">推送待检查 {issues} 条 · 模型尝试 {model["attempts"]} 次 · 输入 {model["input_tokens"]} / 输出 {model["output_tokens"]} token（非账单）。</p><p class="small">推送通道接受任务不代表手机已收到；请在手机确认。</p></details>'
    return layout(content, demo, view, VIEWS[view])


def detail_page(row, demo=False, can_reply=False, filters=None):
    msg = json.loads(row['payload'])
    result = json.loads(row['result']) if row['result'] else {}
    metadata = [('发件人', ', '.join(msg['from_addrs']) or '未提供'), ('邮件头收件地址（To）', ', '.join(msg['to_addrs']) or '未公开，可能密送或转发'), ('抄送（Cc）', ', '.join(msg['cc_addrs']) or '无'), ('实际收件账号', f"{msg['account_label']} <{msg['received_account']}>"), ('邮件标注时间', msg['date_header'] or '未提供'), ('本机接收时间', stamp(row['created'])), ('推送状态', STATES.get(row['delivery_state'], row['delivery_state'] or '尚未生成'))]
    filters = {k: str(v)[:200] for k, v in (filters or {}).items() if k in {'q', 'account', 'view', 'page'}}
    language = result.get('source_language', 'unknown')
    navigation = '<a href="#summary">重点摘要</a>'
    if language != 'zh':
        navigation += '<a href="#translation">中文译文</a>'
    navigation += '<a href="#original">邮件原文</a>'
    content = f'<a class="back" href="{esc(link(filters))}">← 返回列表</a>'
    content += f'<div class="source-strip"><strong>发件人</strong> {esc(", ".join(msg["from_addrs"]) or "未提供")}<br><strong>收件邮箱</strong> {esc(msg["account_label"])} &lt;{esc(msg["received_account"])}&gt;</div>'
    if can_reply:
        content += f'<p><a class="button primary" href="/messages/{row["id"]}/reply">回复这封邮件</a></p>'
    content += f'<nav class="reading-nav" aria-label="阅读内容">{navigation}</nav><div class="detail-grid"><div class="reading-body"><section class="panel" id="summary"><h2>重点摘要</h2>'

    content += f'<p class="summary">{esc(result.get("summary_zh", "正在等待摘要"))}</p>'
    source = {'model': '大模型生成', 'demo': '合成示例摘要（未调用模型）'}.get(result.get('mode'), '本机提示，尚无有效模型摘要')
    content += f'<p class="small">结果来源：{source}</p>'
    if result.get('key_points'):
        content += '<ul class="points">' + ''.join(f'<li>{esc(p)}</li>' for p in result['key_points']) + '</ul>'
    if result.get('needs_action'):
        content += f'<div class="todo"><strong>下一步：</strong>{esc(result.get("action_summary", ""))}</div>'
    elif result.get('mode') == 'fallback' or not result:
        content += '<p class="bad small">尚无有效摘要，请核对原文。</p>'
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
        content += f'<section class="panel" id="translation"><h2>中文全文翻译</h2><p class="small">原文语言：{esc(names.get(language, "其他外文"))} · {esc(labels.get(status, status))}</p>'
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
    content += '<section class="panel" id="original"><h2>邮件原文</h2><pre>' + esc(msg['body'] or '无可展示正文／敏感内容已隐藏') + '</pre>'
    if msg.get('body_truncated'):
        content += '<div class="notice warn">正文超过本机保留上限，这里不是完整原文。请到邮箱查看原邮件。</div>'
    if msg.get('sensitive'):
        content += '<p class="small">本机隐私规则已保护验证或登录内容，未交给模型。旧版本误判的记录没有保存正文，请重新发送测试邮件。</p>'
    content += '</section><details class="panel source-details" id="mail-source"><summary>查看完整来源与投递状态</summary><dl>'
    content += ''.join(f'<dt>{esc(name)}</dt><dd>{esc(value)}</dd>' for name, value in metadata)
    content += '</dl><p class="small">转发、别名或密送时，To 地址可能与实际收件账号不同。</p>'
    if row.get('delivery_error'):
        content += f'<p class="bad">{esc(row["delivery_error"])}</p>'
    content += '</details>'
    if msg.get('attachments'):
        content += '<section class="panel"><h2>附件</h2><ul class="file-list">' + ''.join(f'<li>{esc(name)}</li>' for name in msg['attachments']) + '</ul><p class="small">只显示文件名，尚未解析内容。</p></section>'
    if can_reply and row.get('replies'):
        content += '<details class="panel source-details"><summary>回复记录</summary>'
        from .reply import REPLY_STATES
        for draft in row['replies']:
            content += f'<p><a href="/replies/{esc(draft["id"])}">{esc(REPLY_STATES[draft["state"]])} · {esc(draft["subject"])}</a></p>'
        content += '</details>'
    return layout(content + '</div></div>', demo, active=filters.get('view', 'all') if filters.get('view', 'all') in VIEWS else 'all', title=msg['subject'])


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

        def settings_view(self, error='', status=200, submitted=None):
            if controller:
                from .ui import configuration_page
                self.respond(configuration_page(controller, token, error, submitted), 'text/html; charset=utf-8', status)
                return
            try:
                cfg, revision = settings.read() if settings else (None, '')
            except (ValueError, OSError):
                self.respond(layout('<div class="notice warn">配置无法读取，请检查 config.json 格式。</div>'), 'text/html; charset=utf-8', 400)
                return
            self.respond(settings_page(cfg, revision, token, editable, demo, error), 'text/html; charset=utf-8', status)

        def reply_view(self, path, error='', status=200, submitted=None):
            if not controller:
                self.send_error(403)
                return
            from .reply import compose_page, review_page
            match = re.fullmatch(r'/messages/(\d+)/reply(?:/ai)?', path)
            if match:
                row = store.detail(int(match[1]))
                if not row:
                    self.send_error(404)
                    return
                draft = None
                if submitted and re.fullmatch('[0-9a-f]{32}', submitted.get('draft_id', '')):
                    draft = {'id': submitted['draft_id'], 'revision': submitted.get('revision', '0'),
                             'subject': submitted.get('subject', ''), 'body': submitted.get('body', '')}
                self.respond(compose_page(row, controller.read()[0], token, draft=draft, error=error), 'text/html; charset=utf-8', status)
                return
            match = re.fullmatch(r'/replies/([0-9a-f]{32})(?:/send)?', path)
            draft = store.reply(match[1]) if match else None
            if not draft:
                self.send_error(404)
                return
            if parse_qs(urlparse(self.path).query).get('edit') == ['1'] and draft['state'] == 'draft':
                row = store.detail(draft['message_id'])
                page = compose_page(row, controller.read()[0], token, draft=draft, error=error)
            else:
                run = store.agent_run(draft['id'])
                if run:
                    draft['agent_trace'] = json.loads(run['trace'])
                page = review_page(draft, token, error)
            self.respond(page, 'text/html; charset=utf-8', status)

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
                    problem = any(word in controller.status for word in ('失败', '未启动', '尚未开始', '请在设置页'))
                    runtime = '<div class="notice warn runtime">' if problem else '<details class="diagnostics"><summary>运行信息</summary>'
                    runtime += esc(controller.status) + ' <a href="/settings">邮箱设置</a>' + ('</div>' if problem else '</details>')
                    page = page.replace('</header>', '</header>' + runtime, 1) if problem else page.replace('<footer>', runtime + '<footer>', 1)
                self.respond(page, 'text/html; charset=utf-8')
            elif target.path == '/settings':
                self.settings_view()
            elif target.path == '/api/status':
                state = store.status()
                if controller:
                    state['runtime'] = controller.status
                self.respond(json.dumps(state, ensure_ascii=False), 'application/json; charset=utf-8')
            elif re.fullmatch(r'/messages/\d+/reply(?:/ai)?|/replies/[0-9a-f]{32}', target.path):
                self.reply_view(target.path)
            elif target.path.startswith('/messages/') and target.path[10:].isdigit() and len(target.path) < 30:
                row = store.detail(int(target.path[10:]))
                if row:
                    row['replies'] = store.replies_for(row['id']) if controller else []
                    self.respond(detail_page(row, demo, bool(controller), {k: v[0] for k, v in parse_qs(target.query).items()}), 'text/html; charset=utf-8')
                else:
                    self.send_error(404)
            else:
                self.send_error(404)

        def do_POST(self):
            if not self.local_host() or not editable:
                self.send_error(403)
                return
            path = urlparse(self.path).path
            reply_route = bool(controller and re.fullmatch(r'/messages/\d+/reply(?:/ai)?|/replies/[0-9a-f]{32}/send', path))
            if path != '/settings' and not reply_route:
                self.send_error(404)
                return
            if self.headers.get('Origin') != 'http://' + self.headers.get('Host', ''):
                self.send_error(403)
                return
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/x-www-form-urlencoded':
                self.send_error(415)
                return
            fields = None
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 1 <= length <= (262144 if reply_route else 32768):
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
                if reply_route:
                    from .reply import save_draft
                    compose = re.fullmatch(r'/messages/(\d+)/reply', path)
                    ai = re.fullmatch(r'/messages/(\d+)/reply/ai', path)
                    if compose:
                        identity = save_draft(store, controller.read()[0], int(compose[1]), fields)
                    elif ai:
                        identity = controller.make_ai_draft(int(ai[1]), fields)
                    else:
                        identity = path.split('/')[2]
                        if set(fields) != {'csrf', 'revision'}:
                            raise ValueError('发送确认中包含无效字段。')
                        controller.send_reply(identity, int(fields['revision']))
                    self.send_response(303)
                    self.send_header('Location', '/replies/' + identity)
                    self.send_header('Content-Length', '0')
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    return
                if controller:
                    controller.save(fields)
                else:
                    settings.save(fields)
            except (ValueError, UnicodeError) as exc:
                if reply_route:
                    self.reply_view(path, '操作未完成：' + (str(exc) if isinstance(exc, ValueError) and not isinstance(exc, UnicodeError) else '字段编码无效，请重新填写。'), 400, submitted=fields if path.endswith('/reply') else None)
                    return
                self.settings_view(('未保存：' + str(exc)) if controller and isinstance(exc, ValueError) and not isinstance(exc, UnicodeError) else '未保存：字段格式无效或配置已被修改。请刷新后检查备注、间隔、上限和摘要风格。', 400, submitted=fields)
                return
            except OSError:
                if reply_route:
                    self.reply_view(path, '本机保存失败，请检查磁盘权限或空间；不要重复发送，先核对草稿状态。', 500)
                    return
                self.settings_view('未保存：配置无法写入，请检查目录权限与磁盘空间。', 500)
                return
            except Exception:
                if reply_route:
                    self.reply_view(path, '处理异常。请核对草稿与发送状态；结果不确定时不要直接重发。', 500)
                else:
                    self.settings_view('配置处理异常，请检查本机状态后重试。', 500)
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
