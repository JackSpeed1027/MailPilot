"""Local-only bounded text previews; filenames never become filesystem paths."""
from pathlib import PurePosixPath

MAX_BYTES = 256_000
MAX_CHARS = 12_000
MAX_TOTAL_CHARS = 30_000
MAX_FILES = 10
EXTENSIONS = {'.txt', '.csv', '.md', '.markdown'}
MIME_TYPES = {'text/plain', 'text/csv', 'text/markdown', 'text/x-markdown', 'application/octet-stream'}


def preview(part, enabled, remaining=MAX_CHARS):
    from .mail import clean, sensitive_reason
    name = str(part.get_filename() or '未命名附件')[:300]
    value = {'name': name, 'state': 'disabled', 'text': '', 'note': '未开启本机附件预览。', 'chars': 0}
    if not enabled:
        return value
    extension = PurePosixPath(name.replace('\\', '/')).suffix.lower()
    if extension not in EXTENSIONS or part.get_content_type() not in MIME_TYPES:
        value.update(state='unsupported', note='本版仅支持 TXT、CSV、Markdown 文字附件；PDF、图片和其他类型暂不解析。')
        return value
    if remaining <= 0:
        value.update(state='limit', note='本封邮件附件预览已达到总字符上限。')
        return value
    # Bound the transfer-encoded input before allocating decoded bytes.
    encoded = part.get_payload()
    if not isinstance(encoded, (str, bytes)) or len(encoded) > MAX_BYTES * 2:
        value.update(state='too_large', note='附件超过本机预览大小上限（256KB）。')
        return value
    data = part.get_payload(decode=True) or b''
    value['bytes'] = len(data)
    if len(data) > MAX_BYTES:
        value.update(state='too_large', note='附件超过本机预览大小上限（256KB）。')
        return value
    text = None
    # Prefer reliable BOM detection, then a declared charset and conservative fallbacks.
    encodings = ['utf-8-sig']
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        encodings = ['utf-16']
    elif data.startswith(b'\xef\xbb\xbf'):
        encodings = ['utf-8-sig']
    else:
        declared = part.get_content_charset()
        if declared:
            encodings.insert(0, declared)
        encodings += ['gb18030']
    for encoding in dict.fromkeys(encodings):
        try:
            text = data.decode(encoding)
            break
        except (LookupError, UnicodeError):
            continue
    if text is None or '\x00' in text or any(ord(c) < 32 and c not in '\r\n\t' for c in text):
        value.update(state='unreadable', note='内容不是可识别的文字编码，或包含二进制控制字符。')
        return value
    if sensitive_reason(name, [text]):
        value.update(state='protected', note='附件包含验证或登录信息，未保存预览内容。')
        return value
    text = clean(text)
    limit = min(MAX_CHARS, remaining)
    partial = len(text) > limit
    value.update(state='partial' if partial else 'ready', text=text[:limit], chars=min(len(text), limit),
                 note='仅显示前段文字，请到原邮箱阅读完整附件。' if partial else '在本机提取，未上传模型；原文件未保存。')
    if not text:
        value.update(state='empty', note='附件没有可展示文字。')
    return value
