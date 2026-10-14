"""Explicit configuration; credentials are referenced, never saved by setup."""
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

PROVIDERS = {"qq": "imap.qq.com", "163": "imap.163.com", "gmail": "imap.gmail.com"}
SMTP_HOSTS = {'qq': 'smtp.qq.com', '163': 'smtp.163.com', 'gmail': 'smtp.gmail.com'}


def example():
    return {
        "accounts": [
            {"id": provider, "provider": provider, "label": label, "email": email,
             "secret_env": env, "allow_cloud": False, "smtp_enabled": False, 'attachment_preview': False}
            for provider, label, email, env in [
                ("qq", "QQ 主邮箱", "you@qq.com", "MAILPILOT_QQ_SECRET"),
                ("163", "163 邮箱", "you@163.com", "MAILPILOT_163_SECRET"),
                ("gmail", "Gmail 邮箱", "you@gmail.com", "MAILPILOT_GMAIL_SECRET"),
            ]
        ],
        "poll_seconds": 60, "data_dir": "data", "max_message_bytes": 2_000_000,
        'reply_preferences': {'tone': 'professional', 'length': 'balanced', 'language': 'same', 'signature': '', 'instructions': ''},
        "llm": {"enabled": True, "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat", "key_env": "MAILPILOT_LLM_KEY",
                "daily_attempt_limit": 100, "summary_style": "balanced", "translation_enabled": True,
                "translation_chunk_chars": 2000, "translation_max_chars": 30000},
        "channel": {"enabled": True, "uid": "请填写本人UID",
                    "token_env": "MAILPILOT_WXPUSHER_TOKEN"},
    }


def load(path):
    path = Path(path).resolve()
    cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(cfg, dict):
        raise ValueError("配置必须是 JSON 对象")
    accounts = cfg.get("accounts", [])
    if not isinstance(accounts, list) or not accounts:
        raise ValueError("请至少配置一个邮箱账号")
    ids = set()
    for acc in accounts:
        if not isinstance(acc, dict):
            raise ValueError("accounts 必须是邮箱配置列表")
        for field in ("id", "email", "secret_env"):
            if not isinstance(acc.get(field), str) or not acc[field].strip():
                raise ValueError(f"邮箱配置缺少 {field}")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", acc["id"]):
            raise ValueError("账号 id 只支持字母、数字、下划线和横线")
        if acc["id"] in ids:
            raise ValueError("账号 id 不能重复")
        ids.add(acc["id"])
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", acc["email"]):
            raise ValueError("邮箱地址格式有误")
        acc.setdefault("label", acc["id"])
        acc.setdefault("folder", "INBOX")
        acc.setdefault("port", 993)
        acc.setdefault("allow_cloud", False)
        acc.setdefault('smtp_enabled', False)
        acc.setdefault('attachment_preview', False)
        if type(acc['attachment_preview']) is not bool:
            raise ValueError('attachment_preview 应为 true 或 false')
        acc.setdefault('smtp_host', SMTP_HOSTS.get(acc.get('provider'), ''))
        if type(acc['smtp_enabled']) is not bool:
            raise ValueError('smtp_enabled 应为 true 或 false')
        if acc['smtp_enabled'] and (acc.get('provider') not in SMTP_HOSTS or acc['smtp_host'] != SMTP_HOSTS.get(acc.get('provider'))):
            raise ValueError('当前回复只支持 QQ、163、Gmail 的官方 SMTP 服务器')
        if type(acc["allow_cloud"]) is not bool:
            raise ValueError("allow_cloud 应为 true 或 false")
        if not all(isinstance(acc.get(k), str) and acc[k] for k in ("label", "folder")):
            raise ValueError("邮箱 label 和 folder 必须是非空文本")
        if not acc.get("host"):
            acc["host"] = PROVIDERS.get(acc.get("provider"))
        if not isinstance(acc.get("host"), str) or not acc["host"]:
            raise ValueError("未知邮箱需填写 host")
        if int(acc["port"]) != 993:
            raise ValueError("第一版只支持端口 993 的 IMAP TLS")
    cfg.setdefault("poll_seconds", 60)
    if type(cfg["poll_seconds"]) is not int or not 15 <= cfg["poll_seconds"] <= 3600:
        raise ValueError("poll_seconds 应是 15～3600 的整数")
    cfg.setdefault("max_message_bytes", 2_000_000)
    if type(cfg["max_message_bytes"]) is not int or not 1024 <= cfg["max_message_bytes"] <= 10_000_000:
        raise ValueError("max_message_bytes 应在 1024～10000000 之间")
    for name in ("llm", "channel"):
        cfg.setdefault(name, {})
        if not isinstance(cfg[name], dict):
            raise ValueError(f"{name} 必须是对象")
        cfg[name].setdefault("enabled", True)
        if type(cfg[name]["enabled"]) is not bool:
            raise ValueError(f"{name}.enabled 应为布尔值")
    llm = cfg["llm"]
    for key, value in {"daily_attempt_limit": 100, "model": "", "base_url": "", "summary_style": "balanced",
                       "key_env": "MAILPILOT_LLM_KEY"}.items():
        llm.setdefault(key, value)
    if llm["summary_style"] not in ("brief", "balanced", "detailed"):
        raise ValueError("summary_style 应是 brief、balanced 或 detailed")
    llm.setdefault("translation_enabled", True)
    if type(llm["translation_enabled"]) is not bool:
        raise ValueError("translation_enabled 应为 true 或 false")
    for field, default, minimum, maximum in [("translation_chunk_chars", 2000, 500, 3000),
                                              ("translation_max_chars", 30000, 1000, 100000)]:
        llm.setdefault(field, default)
        if type(llm[field]) is not int or not minimum <= llm[field] <= maximum:
            raise ValueError(f"{field} 应在 {minimum}～{maximum} 之间")
    if llm["enabled"]:
        if not isinstance(llm["model"], str) or not llm["model"]:
            raise ValueError("启用摘要模型时需指定 llm.model")
        url = urlparse(llm["base_url"])
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("llm.base_url 必须是无内嵌凭据和查询参数的 HTTPS 地址")
    if type(llm["daily_attempt_limit"]) is not int or not 1 <= llm["daily_attempt_limit"] <= 10000:
        raise ValueError("daily_attempt_limit 应在 1～10000 之间")
    channel = cfg["channel"]
    channel.setdefault("token_env", "MAILPILOT_WXPUSHER_TOKEN")
    channel.setdefault("uid", "")
    if channel["enabled"] and (not isinstance(channel["uid"], str) or not channel["uid"].startswith("UID_")):
        raise ValueError("启用 WxPusher 时需配置本人 UID（以 UID_ 开头）")
    names = [a['secret_env'] for a in accounts] + [llm['key_env'], channel['token_env']]
    if any(not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,99}', name) for name in names):
        raise ValueError('凭据名称只能使用字母、数字和下划线，不能以数字开头')
    if len(names) != len(set(names)):
        raise ValueError('每个邮箱、模型和推送必须使用独立的凭据名称')
    cfg["data_dir"] = str((path.parent / cfg.get("data_dir", "data")).resolve())
    defaults = {'tone': 'professional', 'length': 'balanced', 'language': 'same', 'signature': '', 'instructions': ''}
    preferences = cfg.setdefault('reply_preferences', defaults.copy())
    if not isinstance(preferences, dict):
        raise ValueError('回复偏好格式无效')
    for name, value in defaults.items():
        preferences.setdefault(name, value)
    if preferences['tone'] not in ('professional', 'friendly') or preferences['length'] not in ('brief', 'balanced', 'detailed') or preferences['language'] not in ('same', 'zh', 'en'):
        raise ValueError('回复语气、长度或语言无效')
    if any(not isinstance(preferences[key], str) or len(preferences[key]) > limit for key, limit in [('signature', 200), ('instructions', 500)]):
        raise ValueError('回复署名最多200字，偏好说明最多500字')
    return cfg


def secret(name):
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"缺少环境变量 {name}")
    return value


def required_secrets(cfg):
    names = [acc["secret_env"] for acc in cfg["accounts"]]
    if cfg["llm"]["enabled"] and any(a["allow_cloud"] for a in cfg["accounts"]):
        names.append(cfg["llm"]["key_env"])
    if cfg["channel"]["enabled"]:
        names.append(cfg["channel"]["token_env"])
    return list(dict.fromkeys(names))
