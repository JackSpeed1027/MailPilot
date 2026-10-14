"""User-reviewed single-recipient SMTP replies, no autonomous sending or retries."""
import html
import json
import re
import smtplib
import ssl
import uuid
from email.message import EmailMessage
from email.utils import formatdate
from .mail import readable

REPLY_STATES = {'draft': '草稿，尚未发送', 'sending': '发送中，请勿重复点击',
                'accepted': '邮件服务器已接受', 'failed': '发送失败', 'unknown': '发送结果不确定，请核对'}


def email_address(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", value) or len(value) > 254:
        raise ValueError('回复地址不完整或格式不支持，请到原邮箱确认。')
    return value


def target(message):
    if message.get('sensitive'):
        raise ValueError('受保护邮件未保留完整内容，请到原邮箱处理和回复。')
    candidates = message.get('reply_to_addrs') or message.get('from_addrs', [])
    if len(candidates) != 1:
        raise ValueError('邮件未提供唯一的回复地址，请到原邮箱确认。')
    return email_address(candidates[0])


def account_for(cfg, message):
    account = next((a for a in cfg['accounts'] if a['id'] == message['account_id']), None)
    if not account or account['email'].lower() != message['received_account'].lower():
        raise ValueError('原收件账号已改变或不存在，请检查邮箱配置。')
    if not account.get('smtp_enabled', False):
        raise ValueError('这个邮箱尚未开启手动回复，请到设置页启用 SMTP。')
    email_address(account['email'])
    return account


def save_draft(store, cfg, message_id, fields):
    if set(fields) - {'csrf', 'draft_id', 'revision', 'subject', 'body'}:
        raise ValueError('草稿中包含不支持的字段。')
    identity = fields.get('draft_id', '')
    if not re.fullmatch('[0-9a-f]{32}', identity):
        raise ValueError('草稿编号无效，请重新打开邮件。')
    row = store.detail(message_id)
    if not row:
        raise ValueError('原邮件不存在。')
    message = json.loads(row['payload'])
    account = account_for(cfg, message)
    recipient = target(message)
    subject = fields.get('subject', '').strip()
    body = fields.get('body', '')
    if not subject or len(subject) > 300 or any(char in subject for char in '\r\n\x00'):
        raise ValueError('回复主题请填写 1～300 个字符，不能包含换行。')
    if not body.strip() or len(body) > 16000 or '\x00' in body:
        raise ValueError('请填写回复正文，最多 16000 个字符。')
    try:
        revision = int(fields.get('revision', '0'))
    except ValueError:
        raise ValueError('草稿版本无效，请刷新。') from None
    store.save_reply(identity, message_id, account['email'], recipient, subject, body, account['id'], revision)
    return identity


def build_message(draft, original):
    mail = EmailMessage()
    mail['From'] = email_address(draft['sender'])
    mail['To'] = email_address(draft['recipient'])
    mail['Subject'] = draft['subject']
    mail['Date'] = formatdate(localtime=False)
    mail['Message-ID'] = draft['mail_id']
    # Retain only valid bounded message IDs; never paste untrusted raw headers.
    pattern = r'<[^<>\s\x00-\x1f]{1,200}@[^<>\s\x00-\x1f]{1,200}>'
    mid = original.get('message_id', '')
    if re.fullmatch(pattern, mid):
        mail['In-Reply-To'] = mid
        references = re.findall(pattern, original.get('references', ''))[-19:]
        if mid not in references:
            references.append(mid)
        mail['References'] = ' '.join(references)
    mail.set_content(draft['body'])
    return mail


def smtp_send(account, password, draft, original, factory=smtplib.SMTP_SSL):
    client, attempted = None, False
    try:
        client = factory(account['smtp_host'], 465, timeout=30, context=ssl.create_default_context())
        client.login(account['email'], password)
        mail = build_message(draft, original)
        attempted = True
        refused = client.send_message(mail, from_addr=account['email'], to_addrs=[draft['recipient']])
        if refused:
            return 'failed', '收件地址被服务器拒绝，请确认地址。'
        return 'accepted', ''
    except smtplib.SMTPAuthenticationError:
        return 'failed', 'SMTP 登录失败，请检查 SMTP 开关、授权码或应用专用密码。'
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError, smtplib.SMTPNotSupportedError):
        return 'failed', '邮件服务器拒绝本次发送，请检查地址、SMTP 权限及服务商限制。'
    except Exception:
        if attempted:
            return 'unknown', '发送期间连接中断，服务器可能已接收。请核对原邮箱或收件方，勿直接重发。'
        return 'failed', '尚未提交邮件：连接或凭据读取失败，请检查网络与邮箱配置。'
    finally:
        if client:
            try:
                client.quit()
            except Exception:
                pass


def compose_page(row, cfg, token, draft=None, error=''):
    from .web import layout, esc
    message = json.loads(row['payload'])
    try:
        account = account_for(cfg, message)
        recipient = target(message)
    except ValueError as exc:
        return layout(f'<div class="notice warn">{esc(exc)}</div><a href="/settings">打开设置</a>', title='暂时无法回复')
    subject = readable(message['subject'])
    if not re.match(r'(?i)^re\s*:', subject):
        subject = 'Re: ' + subject
    identity = draft['id'] if draft else uuid.uuid4().hex
    revision = draft['revision'] if draft else 0
    subject = draft['subject'] if draft else subject[:300]
    body = draft['body'] if draft else ''
    # Use raw escaping for editable user drafts, preserving exactly what was typed.
    escape = lambda value: html.escape(str(value), quote=True)
    content = f'<a class="back" href="/messages/{row["id"]}">← 返回原邮件</a><section class="panel"><h2>写回复</h2><div class="source-strip"><strong>发件邮箱</strong>{esc(account["label"])} &lt;{esc(account["email"])}&gt;<br><strong>收件人</strong>{esc(recipient)}</div><details><summary>回复范围</summary><p class="small">优先使用原邮件 Reply-To，否则回复 From。只回复这一位，不自动抄送或引用原文，不发送附件。</p></details>'
    if 'reply_to_addrs' not in message:
        content += '<div class="notice warn">这是旧版本邮件，未保存 Reply-To，当前按 From 回复。请核对收件地址，必要时到原邮箱回复。</div>'
    if error:
        content += f'<div class="notice warn">{esc(error)}</div>'
    if not draft and cfg['llm']['enabled'] and account['allow_cloud'] and message.get('allow_cloud') and len(message.get('body', '')) <= 12000 and not message.get('body_truncated'):
        content += f'<form method="post" action="/messages/{row["id"]}/reply/ai" data-loading-text="正在智能起草…"><input type="hidden" name="csrf" value="{esc(token)}"><input type="hidden" name="draft_id" value="{identity}"><p class="small">智能起草将这封已授权的普通邮件交给模型。最多3轮，有调用费用，生成结果需核对，不会发送。</p><button class="button" type="submit">按我的偏好智能起草</button></form><hr>'
    elif not draft:
        content += '<p class="small">智能起草需要启用模型、邮箱当前与收件时均已授权云处理，且完整原文不超过12000字符。当前可直接手动写回复。</p>'
    content += f'<form class="settings" method="post" action="/messages/{row["id"]}/reply"><input type="hidden" name="csrf" value="{esc(token)}"><input type="hidden" name="draft_id" value="{identity}"><input type="hidden" name="revision" value="{escape(revision)}"><div class="field"><label for="reply_subject">回复主题</label><input id="reply_subject" name="subject" required maxlength="300" value="{escape(subject)}"></div><div class="field"><label for="reply_body">回复正文</label><textarea id="reply_body" name="body" rows="12" maxlength="16000" required>{escape(body)}</textarea><p class="small">发送的是这里填写的原文，不会自动翻译或由模型改写。草稿保存在本机。</p></div><button class="primary" type="submit">保存草稿并预览</button></form></section>'
    return layout(content, title='回复邮件', subtitle='先编辑、再预览，最后由你点击发送。')


def review_page(draft, token, error=''):
    from .web import layout, esc
    escape = lambda value: html.escape(str(value), quote=True)
    identity = draft['id']
    content = f'<a href="/messages/{draft["message_id"]}">← 返回原邮件</a><section class="panel"><h2>{esc(REPLY_STATES[draft["state"]])}</h2><dl><dt>发件邮箱</dt><dd>{esc(draft["sender"])}</dd><dt>收件人</dt><dd>{esc(draft["recipient"])}</dd><dt>主题</dt><dd>{escape(draft["subject"])}</dd></dl><pre>{escape(draft["body"])}</pre>'
    if error or draft.get('error'):
        content += f'<div class="notice warn">{esc(error or draft["error"])}</div>'
    if draft.get('agent_trace'):
        labels = {'model_request': '请求模型', 'get_email_context': '读取当前邮件',
                  'get_reply_preferences': '读取回复偏好', 'draft_validation': '草稿格式检查'}
        content += '<details><summary>查看智能起草处理步骤</summary><ol>'
        for step in draft['agent_trace']:
            caption = labels.get(step['step'], step['step'])
            content += f'<li>{esc(caption)}'
            if 'elapsed_ms' in step:
                content += f' · {step["elapsed_ms"]} 毫秒'
            content += '</li>'
        content += '</ol><p class="small">这里是实际工具与请求记录，不是模型内部思维。</p></details>'
    if draft['state'] == 'draft':
        content += f'<div class="notice">请核对发件邮箱、收件人和全文。点击下面按钮会真实发送邮件。</div><a class="button" href="/replies/{identity}?edit=1">修改草稿</a><form method="post" action="/replies/{identity}/send" data-loading-text="正在提交回复…"><input type="hidden" name="csrf" value="{esc(token)}"><input type="hidden" name="revision" value="{draft["revision"]}"><button class="primary" type="submit">确认发送这封回复</button></form>'
    else:
        content += '<p class="small">同一草稿不能再次提交。服务器接受不代表对方已读或必然投递到收件箱；本机保存发送记录，不承诺原邮箱“已发送”文件夹一定同步。</p>'
    return layout(content + '</section>', title='回复预览与发送状态', subtitle='核对草稿；只有你点击确认发送，才会提交给邮箱服务器。')
