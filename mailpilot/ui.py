"""Browser configuration and a restartable local worker, without console secrets."""
import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from .config import example, load, required_secrets, PROVIDERS
from .credentials import Credentials, WindowsVault, bindings
from .runtime import run_forever, worker_lock
from .store import Store


class UIController:
    def __init__(self, path, runner=run_forever):
        self.path = Path(path).resolve()
        self.runner = runner
        self.guard = threading.Lock()
        self.settings_lock = threading.Lock()
        self.stop = threading.Event()
        self.worker = None
        self.managed_names = set()
        self.closed = False
        self.status = '请在设置页添加邮箱并填写凭据。'
        try:
            vault = WindowsVault()
        except (ValueError, OSError):
            vault = None
        self.credentials = Credentials(self.path, vault)
        cfg, _ = self.read()
        data = cfg.get('data_dir', 'data')
        self.store = Store((self.path.parent / data).resolve())
        try:
            with worker_lock((self.path.parent / data).resolve()):
                self.store.recover_replies()
        except ValueError:
            pass
        if self.path.exists():
            cfg = load(self.path)
            for name, binding in bindings(cfg).items():
                if os.environ.get(name):
                    self.credentials.session[name, binding] = os.environ[name]
            self.reload()

    def read(self):
        if self.path.exists():
            raw = self.path.read_bytes()
            return load(self.path), hashlib.sha256(raw).hexdigest()
        cfg = example()
        cfg['accounts'] = []
        cfg['llm']['enabled'] = False
        cfg['llm']['model'] = 'deepseek-flash'
        cfg['channel']['enabled'] = False
        cfg['channel']['uid'] = ''
        return cfg, 'new'

    def save(self, fields):
        with self.settings_lock:
            cfg, revision = self.read()
            if fields.get('revision') != revision:
                raise ValueError('配置已变更，请刷新页面重新填写。')
            allowed = {'csrf', 'revision', 'poll_seconds', 'daily_attempt_limit', 'summary_style',
                       'llm_enabled', 'base_url', 'model', 'model_key', 'channel_enabled', 'uid',
                       'app_token', 'remember', 'translation_enabled', 'translation_max_chars',
                       'new_provider', 'new_email', 'new_label', 'new_secret', 'new_cloud', 'new_smtp',
                       'reply_tone', 'reply_length', 'reply_language', 'reply_signature', 'reply_instructions'}
            secret_values = {}
            for acc in cfg['accounts']:
                identity = acc['id']
                allowed.update({'label_' + identity, 'cloud_' + identity, 'secret_' + identity, 'smtp_' + identity})
                acc['label'] = fields.get('label_' + identity, '').strip()
                if not 1 <= len(acc['label']) <= 60:
                    raise ValueError('邮箱显示名称请填写 1～60 个字符，例如工作邮箱。')
                acc['allow_cloud'] = fields.get('cloud_' + identity) == 'on'
                acc['smtp_enabled'] = fields.get('smtp_' + identity) == 'on'
                secret_values[acc['secret_env']] = fields.get('secret_' + identity, '').strip()
            if set(fields) - allowed:
                raise ValueError('页面包含不支持的设置，请刷新后重试。')
            for key in allowed:
                if key.endswith('_enabled') or key in {'remember', 'new_cloud', 'new_smtp'} or key.startswith(('cloud_', 'smtp_')):
                    if fields.get(key, 'off') not in {'on', 'off'}:
                        raise ValueError('开关格式无效，请刷新页面。')
            if fields.get('new_email', '').strip():
                provider = fields.get('new_provider', '')
                if provider not in PROVIDERS:
                    raise ValueError('请选择 QQ、163 或 Gmail。')
                email = fields['new_email'].strip()
                if any(a['email'].lower() == email.lower() for a in cfg['accounts']):
                    raise ValueError('这个邮箱已经添加，请在上方更新授权码。')
                number = 1
                while any(a['id'] == f'{provider}_{number}' for a in cfg['accounts']):
                    number += 1
                identity = f'{provider}_{number}'
                name = 'MAILPILOT_' + identity.upper() + '_SECRET'
                label = fields.get('new_label', '').strip() or email
                if len(label) > 60:
                    raise ValueError('新邮箱显示名称不能超过 60 个字符。')
                cfg['accounts'].append(dict(id=identity, provider=provider, email=email,
                                            label=label, secret_env=name, allow_cloud=fields.get('new_cloud') == 'on',
                                            smtp_enabled=fields.get('new_smtp') == 'on'))
                secret_values[name] = fields.get('new_secret', '').strip()
            try:
                cfg['poll_seconds'] = int(fields['poll_seconds'])
                cfg['llm']['daily_attempt_limit'] = int(fields['daily_attempt_limit'])
                cfg['llm']['translation_max_chars'] = int(fields['translation_max_chars'])
            except (KeyError, ValueError):
                raise ValueError('检查收件间隔、每日调用上限和翻译长度，均需填写整数。') from None
            llm, channel = cfg['llm'], cfg['channel']
            llm.update(enabled=fields.get('llm_enabled') == 'on', base_url=fields.get('base_url', '').strip(),
                       model=fields.get('model', '').strip(), summary_style=fields.get('summary_style', 'balanced'),
                       translation_enabled=fields.get('translation_enabled') == 'on')
            channel.update(enabled=fields.get('channel_enabled') == 'on', uid=fields.get('uid', '').strip())
            if 'reply_tone' in fields:
                cfg['reply_preferences'] = dict(tone=fields.get('reply_tone'), length=fields.get('reply_length'),
                    language=fields.get('reply_language'), signature=fields.get('reply_signature', '').strip(),
                    instructions=fields.get('reply_instructions', '').strip())
            secret_values[llm['key_env']] = fields.get('model_key', '').strip()
            secret_values[channel['token_env']] = fields.get('app_token', '').strip()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.json',
                                                 prefix='.settings-', dir=self.path.parent, delete=False) as output:
                    temporary = Path(output.name)
                    json.dump(cfg, output, ensure_ascii=False, indent=2)
                    output.flush()
                    os.fsync(output.fileno())
                validated = load(temporary)
                scoped = bindings(validated)
                if any(len(v.encode('utf-8')) > 2560 for v in secret_values.values()):
                    raise ValueError('输入的密钥过长，请检查是否误填了其他内容。')
                if self.read()[1] != revision:
                    raise ValueError('配置已被其他程序修改，请刷新。')
                undo = []
                try:
                    for name, value in secret_values.items():
                        if value:
                            binding = scoped[name]
                            target = self.credentials.target(name, binding)
                            vault = self.credentials.vault
                            undo.append((name, binding, self.credentials.session.get((name, binding)),
                                         vault.read(target) if vault else ''))
                            self.credentials.put(name, binding, value, fields.get('remember') == 'on')
                    os.replace(temporary, self.path)
                except Exception:
                    for name, binding, old_session, old_saved in reversed(undo):
                        target = self.credentials.target(name, binding)
                        if self.credentials.vault:
                            try:
                                if old_saved:
                                    self.credentials.vault.write(target, old_saved)
                                else:
                                    self.credentials.vault.delete(target)
                            except (ValueError, OSError):
                                pass
                        if old_session is None:
                            self.credentials.session.pop((name, binding), None)
                        else:
                            self.credentials.session[name, binding] = old_session
                    raise
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
        self.reload()

    def reload(self):
        self.status = '正在应用配置；等待当前收件或模型请求结束，请稍后刷新。'
        self.stop.set()
        threading.Thread(target=self._restart, daemon=True).start()

    def _restart(self):
        with self.guard:
            if self.worker:
                self.stop.set()
                self.worker.join()
            if self.closed:
                return
            try:
                cfg = load(self.path)
                for name, binding in bindings(cfg).items():
                    self.managed_names.add(name)
                    value = self.credentials.get(name, binding)
                    if value:
                        os.environ[name] = value
                    else:
                        os.environ.pop(name, None)
                missing = [n for n in required_secrets(cfg) if not os.environ.get(n)]
                if missing:
                    self.status = '配置已保存，尚未开始收件：请补充邮箱授权码、启用的模型 Key 或 WxPusher AppToken。'
                    return
                self.stop = threading.Event()
                self.status = '收件已启动。首次连接只建立基线；请随后发送新邮件测试。'
                def work():
                    try:
                        self.runner(cfg, self.store, self.stop)
                    except Exception:
                        self.status = '收件未启动或已停止。检查是否已有另一窗口运行、邮箱设置与网络，再保存重试。'
                self.worker = threading.Thread(target=work, daemon=True)
                self.worker.start()
            except (ValueError, OSError):
                self.status = '配置或 Windows 凭据读取失败，请检查设置后重新保存。'

    def close(self):
        self.closed = True
        self.stop.set()
        with self.guard:
            if self.worker:
                self.worker.join()
        for name in self.managed_names | {name for name, _ in self.credentials.session}:
            os.environ.pop(name, None)
        self.credentials.session.clear()

    def send_reply(self, identity, revision, sender=None):
        from .reply import account_for, target, smtp_send
        with self.guard:
            if self.closed:
                raise ValueError('程序正在关闭，请重新启动后操作。')
            cfg = load(self.path)
            draft = self.store.reply(identity)
            if not draft:
                raise ValueError('草稿不存在。')
            row = self.store.detail(draft['message_id'])
            original = json.loads(row['payload'])
            account = account_for(cfg, original)
            if draft['sender'] != account['email'] or draft['recipient'] != target(original):
                raise ValueError('回复来源或目标已变化，请重新生成草稿。')
            password = self.credentials.get(account['secret_env'], bindings(cfg)[account['secret_env']])
            if not password:
                raise ValueError('缺少此邮箱的授权码，请到设置页补充。')
            claimed = self.store.claim_reply(identity, revision)
            try:
                state, error = (sender or smtp_send)(account, password, claimed, original)
            except Exception:
                state, error = 'unknown', '发送流程异常，请核对收件方，勿直接重发。'
            self.store.reply_result(identity, state, error)

    def make_ai_draft(self, message_id, fields, transport=None):
        from .agent import ReplyAgent
        from .reply import account_for, target, save_draft
        import re
        if set(fields) != {'csrf', 'draft_id'} or not re.fullmatch('[0-9a-f]{32}', fields.get('draft_id', '')):
            raise ValueError('智能起草表单无效，请刷新。')
        with self.guard:
            if self.closed:
                raise ValueError('程序正在关闭。')
            cfg = load(self.path)
            row = self.store.detail(message_id)
            if not row:
                raise ValueError('原邮件不存在。')
            message = json.loads(row['payload'])
            account = account_for(cfg, message)
            target(message)
            message['allow_cloud'] = bool(message.get('allow_cloud') and account['allow_cloud'])
            key = self.credentials.get(cfg['llm']['key_env'], bindings(cfg)[cfg['llm']['key_env']])
            agent = ReplyAgent(cfg['llm'], self.store, key, **({'transport': transport} if transport else {}))
            identity = fields['draft_id']
            if self.store.reply(identity):
                raise ValueError('草稿编号已存在，请返回邮件查看草稿或重新打开起草页。')
            self.store.start_agent(identity, message_id)
            try:
                result = agent.draft(message, cfg['reply_preferences'])
                signature = cfg['reply_preferences']['signature']
                if signature:
                    result['body'] += '\n\n' + signature
                identity = save_draft(self.store, cfg, message_id, dict(csrf=fields['csrf'], draft_id=identity, revision='0', **result))
                self.store.finish_agent(identity, 'complete', agent.trace)
                return identity
            except ValueError as exc:
                self.store.finish_agent(identity, 'failed', agent.trace, str(exc))
                raise
            except Exception:
                self.store.finish_agent(identity, 'failed', agent.trace, '智能起草处理失败，请检查本机状态。')
                raise ValueError('智能起草未完成；请检查本机状态后手动写回复。') from None


def configuration_page(controller, token, error='', submitted=None):
    from .web import esc, layout
    cfg, revision = controller.read()
    # Keep editable, non-secret values after validation errors. Never echo passwords.
    submitted = submitted if error and submitted is not None else None
    scoped = bindings(cfg)
    def credential_hint(name):
        try:
            return '已配置，留空保留。' if controller.credentials.get(name, scoped[name]) else '尚未配置，请填写。'
        except (ValueError, OSError):
            return '无法读取已保存凭据，请重新填写。'
    def text(name, label, value='', hint='', kind='text'):
        if submitted is not None and kind != 'password':
            value = submitted.get(name, value)
        autocomplete = 'autocomplete="new-password"' if kind == 'password' else ''
        return (f'<div class="field"><label for="{esc(name)}">{esc(label)}</label>'
                f'<input id="{esc(name)}" name="{esc(name)}" type="{kind}" value="{esc(value)}" '
                f'{autocomplete}>'
                f'<p class="small">{esc(hint)}</p></div>')
    def check(name, label, value):
        if submitted is not None:
            value = submitted.get(name) == 'on'
        return f'<label class="check"><input type="checkbox" name="{name}" {"checked" if value else ""}><span>{esc(label)}</span></label>'
    content = f'<div class="notice">{esc(controller.status)}</div>'
    if error:
        content += f'<div class="notice warn" role="alert">{esc(error)}</div>'
    content += '<p class="small">初次使用：先添加邮箱，再配置摘要模型；微信推送和智能回复可以按需开启。点击下方分组展开设置，最后统一保存。</p>'
    content += f'<form class="settings" method="post" action="/settings"><input type="hidden" name="csrf" value="{esc(token)}"><input type="hidden" name="revision" value="{esc(revision)}">'
    content += '<section class="panel" id="settings-mailboxes"><h2>1. 我的邮箱</h2><p class="small">显示名称是自己起的备注，例如“工作邮箱”。地址用于登录；邮件详情还会分别显示发件人、To / Cc 与实际收件账号。</p>'
    for acc in cfg['accounts']:
        identity = acc['id']
        content += f'<div class="mailbox"><h3>{esc(acc["email"])}</h3>'
        content += text('label_' + identity, '显示名称', acc['label'])
        content += text('secret_' + identity, '邮箱授权码 / 应用专用密码', hint=credential_hint(acc['secret_env']) + ' 不会回显。QQ、163 使用 IMAP 授权码，Gmail 使用应用专用密码。', kind='password')
        content += check('cloud_' + identity, '允许普通邮件正文交给模型摘要、翻译（不含附件）', acc['allow_cloud'])
        content += check('smtp_' + identity, '允许在本机手动发送回复（SMTP）', acc.get('smtp_enabled', False)) + '</div>'
    content += '<details class="add-mailbox"' + (' open' if not cfg['accounts'] else '') + '><summary>＋ 添加一个邮箱</summary><div class="field"><label for="new_provider">邮箱服务商</label><select id="new_provider" name="new_provider"><option value="qq">QQ</option><option value="163">163</option><option value="gmail">Gmail</option></select></div>'
    content += text('new_email', '邮箱地址', hint='例如 yourname@qq.com。不添加时留空。', kind='email')
    content += text('new_label', '显示名称', hint='例如个人邮箱。留空使用邮箱地址。')
    content += text('new_secret', '邮箱授权码 / 应用专用密码', hint='需先在邮箱官网启用 IMAP。不是邮箱的普通登录密码。', kind='password')
    content += check('new_cloud', '允许这个新邮箱的普通邮件云摘要与翻译', False)
    content += check('new_smtp', '允许这个新邮箱在本机手动发送回复（SMTP）', False)
    content += '<p class="small">回复从实际收到邮件的账号发送。请在邮箱官网同时开启 SMTP，使用对应授权码/应用专用密码；这里只启用能力，不会自动发送。第一版仅单人文字回复，不抄送、不附附件。默认使用官方 SMTP 与 TLS 465。</p>'
    content += '<p class="small">设置入口：<a href="https://mail.qq.com/" target="_blank" rel="noreferrer">QQ 邮箱</a> · <a href="https://mail.163.com/" target="_blank" rel="noreferrer">163 邮箱</a> · <a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noreferrer">Google 应用专用密码</a>。Gmail 需开启两步验证，部分组织账号不可用；连接还需本机网络能够访问 Gmail。</p></details></section>'
    llm = cfg['llm']
    content += '<details class="panel setting-group" id="settings-model"><summary><span class="step-number">2</span><span><span class="group-title">摘要与外文阅读</span><span class="group-hint">API、全文翻译和调用上限</span></span></summary><div class="group-body">'
    diagnostics = controller.store.model_diagnostics()
    content += f'<div class="notice">本机今日（{esc(diagnostics["day"])}）模型尝试 {diagnostics["attempts"]} 次，收到接口响应 {diagnostics["responses"]} 次。摘要、翻译和智能起草共享统计。这里不是服务商账单，也不代表每次输出都通过校验。</div>'
    content += check('llm_enabled', '启用模型（邮箱也要勾选云处理授权）', llm['enabled'])
    content += text('base_url', 'API 基础地址', llm['base_url'], '使用兼容 OpenAI Chat Completions 的 HTTPS 基础地址，例如 https://api.deepseek.com/v1；不要填写完整 /chat/completions 路径。')
    content += text('model', '模型名称', llm['model'], '填写服务商支持的名称。DeepSeek 当前可选 deepseek-flash；已有模型名称可以保留。')
    content += text('model_key', 'API Key', hint=credential_hint(llm['key_env']) + ' 在模型服务商后台获取；更换 API 地址需重新填 Key。', kind='password')
    content += '<p class="small"><a href="https://platform.deepseek.com/" target="_blank" rel="noreferrer">DeepSeek 官方控制台</a>：创建 API Key 并确认余额。模型调用按服务商规则计费。</p><div class="field"><label for="summary_style">摘要详细程度</label><select id="summary_style" name="summary_style">'
    for key, label in [('brief', '简短'), ('balanced', '均衡'), ('detailed', '详细')]:
        selected = submitted.get('summary_style', llm['summary_style']) if submitted is not None else llm['summary_style']
        content += f'<option value="{key}" {"selected" if selected == key else ""}>{label}</option>'
    content += '</select></div>'
    content += check('translation_enabled', '外文邮件生成全文中文翻译，并保留原文', llm.get('translation_enabled', True))
    content += text('translation_max_chars', '单封全文翻译字符上限', llm.get('translation_max_chars', 30000), '范围 1000～100000。长邮件分段翻译，会增加调用次数；超过上限明确提示，不伪装成全文。', 'number')
    content += text('daily_attempt_limit', '每日模型调用尝试上限', llm['daily_attempt_limit'], '范围 1～10000；摘要、翻译分段和重试共享上限，不是金额预算。', 'number') + '</div></details>'
    channel = cfg['channel']
    content += '<details class="panel setting-group" id="settings-push"><summary><span class="step-number">3</span><span><span class="group-title">微信消息推送</span><span class="group-hint">可选，把新邮件重点推送到手机</span></span></summary><div class="group-body">'
    content += check('channel_enabled', '启用 WxPusher（关闭时邮件仍保存在本机）', channel['enabled'])
    content += text('uid', '接收人 UID', channel['uid'], '以 UID_ 开头。扫描你创建的应用订阅二维码后，在后台用户列表获取自己的 UID。')
    content += text('app_token', '应用 AppToken', hint=credential_hint(channel['token_env']) + ' 在应用管理后台获取；不是 UID。', kind='password')
    content += '<p class="small"><a href="https://wxpusher.zjiecode.com/" target="_blank" rel="noreferrer">WxPusher 官网</a> → 微信扫码登录 → 创建应用 → 复制 AppToken → 扫描应用订阅二维码 → 用户列表复制本人 UID。两个值分别填写在上方。开启推送会把消息内容交给 WxPusher；“通道已接受”还需在手机确认。</p></div></details>'
    content += '<details class="panel setting-group" id="settings-privacy"><summary><span class="step-number">4</span><span><span class="group-title">收件与隐私</span><span class="group-hint">检查频率、密钥保存和处理范围</span></span></summary><div class="group-body">'
    content += text('poll_seconds', '检查新邮件间隔（秒）', cfg['poll_seconds'], '建议 60；范围 15～3600。第一次连接跳过旧邮件，只收之后的新邮件。', 'number')
    content += check('remember', '将本次新输入的密钥保存到 Windows 凭据管理器', False)
    content += '<p class="small">默认仅本次运行使用；关闭程序后需重填。勾选后下次可自动读取，保存在当前 Windows 用户凭据中；不会写入 config.json，也不会在页面显示。留空不会修改已有凭据。若要将已保存密钥改为仅本次，请重新输入并取消勾选。也可在 Windows“凭据管理器 → Windows 凭据”中删除 MailPilot/ 条目。</p><p class="small">当前附件只显示名称，不解析图片或文件。配置保存不会自动重算旧邮件；已发出的请求无法撤回。</p></div></details>'
    preferences = cfg.get('reply_preferences', dict(tone='professional', length='balanced', language='same', signature='', instructions=''))
    content += '<details class="panel setting-group" id="settings-reply"><summary><span class="step-number">5</span><span><span class="group-title">智能回复偏好</span><span class="group-hint">语气、语言和惯用署名</span></span></summary><div class="group-body"><p class="small">这是你明确设置的习惯，不会自动扫描历史邮件学习。仅点击“按我的偏好智能起草”时调用模型，最多3轮，与摘要/翻译共享每日上限。模型只有读取当前邮件和读取偏好两个工具，没有发信工具。</p>'
    for name, label, values in [('tone', '回复语气', [('professional', '专业礼貌'), ('friendly', '亲切自然')]),
                                ('length', '回复长度', [('brief', '简短'), ('balanced', '均衡'), ('detailed', '详细')]),
                                ('language', '回复语言', [('same', '跟随原邮件'), ('zh', '中文'), ('en', '英文')])]:
        content += f'<div class="field"><label for="reply_{name}">{label}</label><select id="reply_{name}" name="reply_{name}">'
        for value, caption in values:
            selected = submitted.get('reply_' + name, preferences[name]) if submitted is not None else preferences[name]
            content += f'<option value="{value}" {"selected" if selected == value else ""}>{caption}</option>'
        content += '</select></div>'
    content += text('reply_signature', '惯用署名', preferences['signature'], '最多200字，本机添加到智能草稿末尾，不发送给模型。')
    content += text('reply_instructions', '其他回复偏好', preferences['instructions'], '最多500字，例如“先回应对方问题，未确认的时间不要承诺”。不要填写密钥。此说明会交给模型。')
    content += '</div></details><div class="settings-actions"><p class="small">修改后统一保存。密钥留空会保留已有凭据。</p><button class="primary" type="submit">保存并应用配置</button></div></form>'
    if error:
        content = content.replace('<details ', '<details open ')
        if submitted is not None:
            content = content.replace('<p class="small">初次使用：', '<p class="small">未保存的普通字段已保留；新输入的授权码和密钥请重新填写。初次使用：', 1)
            provider = submitted.get('new_provider', 'qq')
            if provider in PROVIDERS:
                content = content.replace(f'<option value="{provider}">', f'<option value="{provider}" selected>', 1)
    return layout(content, title='设置', active='settings', subtitle='在页面完成配置；密钥不回显，保存后自动应用。')
