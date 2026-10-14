"""Parse untrusted mail as data. Sensitive messages never enter model input."""

import hashlib
import re
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from html.parser import HTMLParser

SENSITIVE = re.compile(
    r"验证码|校验码|动态码|登录码|一次性密码|密码重置|重置密码|登录链接|验证链接|"
    r"verification|security\s+code|one[ -]?time|passcode|\bOTP\b|\b2FA\b|"
    r"password\s+reset|reset.{0,20}password|magic\s+link|sign[ -]?in\s+link|"
    r"login\s+(code|link)|verify.{0,30}(account|email)|PIN\s+code",
    re.I,
)


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
    sensitive = bool(SENSITIVE.search("\n".join([subject, *plain, *html])))
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
        "body": "" if sensitive else body[:30000],
        "date_header": str(msg.get("Date", ""))[:100],
        "attachments": [] if sensitive else attachments[:30],
        "sensitive": sensitive, "allow_cloud": account.get("allow_cloud", False),
    }


def model_text(text):
    """Reduce common PII; this is not a guarantee of full anonymization."""
    text = re.sub(r"https?://\S+", "[链接已省略]", text)
    text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[邮箱已隐藏]", text)
    text = re.sub(r"(?<!\d)\d{11,19}(?!\d)", "[长号码已隐藏]", text)
    return text[:12000]
