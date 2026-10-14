"""Parse untrusted mail as data. Sensitive messages never enter model input."""

import hashlib
import html as html_module
import re
import secrets
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from html.parser import HTMLParser

AUTH_CODE = re.compile(
    r"验证码|校验码|动态码|登录码|一次性密码|認証コード|認証番号|確認コード|ワンタイムパスワード|"
    r"verification\s+code|security\s+code|one[ -]?time\s+(?:password|code)|passcode|\bOTP\b|\b2FA\b|"
    r"login\s+code|PIN\s+code", re.I)
CODE_VALUE = re.compile(r"(?<![\w])(?:\d{4,8}|(?=[A-Za-z\d]{6,16}(?!\w))(?=[A-Za-z\d]*\d)[A-Za-z\d]+)(?![\w])")
AUTH_LINK = re.compile(
    r"重置密码|密码重置|登录链接|验证链接|パスワード.{0,12}リセット|ログインリンク|"
    r"reset\s+your\s+password|magic\s+link|sign[ -]?in\s+link|login\s+link|"
    r"click.{0,50}(?:sign[ -]?in|verify\s+(?:your\s+)?(?:account|email))", re.I)


def sensitive_reason(subject, alternatives):
    # Inspect decoded text, including hidden alternatives, not CSS class names or URLs alone.
    source_text = html_module.unescape("\n".join([subject, *alternatives]))
    text = re.sub(r"<[^>]*>", " ", source_text)
    for match in AUTH_CODE.finditer(text):
        nearby = text[max(0, match.start() - 80):match.end() + 160]
        if CODE_VALUE.search(nearby):
            return "auth_code"
    if AUTH_LINK.search(text) and re.search(r"https?://", source_text, re.I):
        return "auth_link"
    # Explicit authentication request phrasing also protects image-only code mail.
    if re.search(r"(?:your|use\s+this)\s+(?:login\s+)?(?:verification\s+code|passcode)|"
                 r"(?:本次|您的|あなたの).{0,12}(?:登录码|验证码|認証コード)", text, re.I):
        return "auth_request"
    return ""


class TextHTML(HTMLParser):
    """Discard active/hidden elements; never fetch external resources."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden_depth = 0
        self.stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        style = re.sub(r"\s+", "", attrs.get("style", "")).lower()
        hidden = tag in ("script", "style", "head", "iframe", "object") or (
            "hidden" in attrs or "display:none" in style or "visibility:hidden" in style
        )
        if tag not in ("br", "img", "hr", "meta", "link", "input", "source", "wbr"):
            self.stack.append((tag, hidden))
            self.hidden_depth += int(hidden)
        if not self.hidden_depth and tag in ("p", "div", "br", "li", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                self.hidden_depth -= sum(int(v[1]) for v in self.stack[i:])
                del self.stack[i:]
                break

    def handle_data(self, text):
        if not self.hidden_depth:
            self.parts.append(text)


def clean(text):
    text = text.replace("\x00", "")
    return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", text).strip()


def addresses(msg, name):
    return [addr for _, addr in getaddresses([str(h) for h in msg.get_all(name, [])]) if addr]


def parse(raw, account, uidvalidity, uid):
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    subject = str(msg.get("Subject", "（无主题）"))
    plain, html, attachments = [], [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        if part.get_content_disposition() == "attachment" or part.get_filename():
            attachments.append(part.get_filename() or "未命名附件")
            continue
        if part.get_content_type() not in ("text/plain", "text/html"):
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError):
            content = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        if isinstance(content, str):
            (plain if part.get_content_type() == "text/plain" else html).append(content)
    html_text = []
    for content in html:
        parser = TextHTML()
        parser.feed(content)
        html_text.append("".join(parser.parts))
    body = clean("\n".join(plain or html_text))
    # Inspect all textual alternatives, including hidden HTML, before persistence.
    reason = sensitive_reason(subject, [*plain, *html])
    sensitive = bool(reason)
    from_addrs = addresses(msg, "From")
    to_addrs = addresses(msg, "To")
    cc_addrs = addresses(msg, "Cc")
    mid = str(msg.get("Message-ID", "")).strip()
    fingerprint = hashlib.sha256(raw).hexdigest()
    return {
        "account_id": account["id"], "account_label": account["label"],
        "received_account": account["email"], "folder": account["folder"],
        "uidvalidity": str(uidvalidity), "uid": int(uid), "message_id": mid,
        "fingerprint": fingerprint, "from_addrs": from_addrs,
        "to_addrs": to_addrs, "cc_addrs": cc_addrs,
        "subject": "验证／登录类邮件（敏感内容已隐藏）" if sensitive else subject[:500],
        "body": "" if sensitive else body[:100000],
        "body_truncated": not sensitive and len(body) > 100000,
        "body_chars": 0 if sensitive else len(body),
        "date_header": str(msg.get("Date", ""))[:100],
        "attachments": [] if sensitive else attachments[:30],
        "sensitive": sensitive, "allow_cloud": account.get("allow_cloud", False),
        "sensitive_reason": reason,
    }


def guess_language(text):
    """A routing hint; model language detection can refine it, not a universal detector."""
    kana = len(re.findall(r"[\u3040-\u30ff]", text))
    hangul = len(re.findall(r"[\uac00-\ud7af]", text))
    han = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if kana >= 3:
        return "ja"
    if hangul >= 3:
        return "ko"
    if han and latin > max(40, han * 2):
        return "mixed"
    if han:
        return "zh"
    if latin >= 10:
        return "en"
    if any(char.isalpha() for char in text):
        return "other"
    return "unknown"


def protect_translation(text):
    """Send opaque placeholders instead of common private identifiers; restore locally."""
    prefix = 'MPPRIVATE' + secrets.token_hex(6).upper()
    mapping = {}
    pattern = r"https?://\S+|\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b|(?<!\d)\d{11,19}(?!\d)"
    def replace(match):
        key = f'[{prefix}_{len(mapping)}]'
        mapping[key] = match.group(0)
        return key
    return re.sub(pattern, replace, text), mapping


def restore_translation(text, mapping):
    if any(text.count(key) != 1 for key in mapping):
        raise ValueError("翻译未完整保留私密字段占位符")
    for key, value in mapping.items():
        text = text.replace(key, value)
    return text


def model_text(text):
    """Reduce common PII; this is not a guarantee of full anonymization."""
    text = re.sub(r"https?://\S+", "[链接已省略]", text)
    text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[邮箱已隐藏]", text)
    text = re.sub(r"(?<!\d)\d{11,19}(?!\d)", "[长号码已隐藏]", text)
    return text[:12000]
