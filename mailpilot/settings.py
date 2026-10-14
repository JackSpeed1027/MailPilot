"""Allowlisted, validated configuration edits; never handles credentials."""
import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from .config import load


class Settings:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.lock = threading.Lock()

    def read(self):
        raw = self.path.read_bytes()
        return load(self.path), hashlib.sha256(raw).hexdigest()

    def save(self, fields):
        with self.lock:
            original = self.path.read_bytes()
            if fields.get("revision") != hashlib.sha256(original).hexdigest():
                raise ValueError("配置已被其他窗口修改，请刷新设置页后重新填写。")
            cfg = json.loads(original.decode("utf-8-sig"))
            expected = {"revision", "csrf", "poll_seconds", "daily_attempt_limit", "summary_style", "llm_enabled"}
            for account in cfg["accounts"]:
                identity = account["id"]
                expected.update({"label_" + identity, "cloud_" + identity})
                label = fields.get("label_" + identity, "").strip()
                if not label or len(label) > 60:
                    raise ValueError("每个邮箱备注请填写 1～60 个字符。")
                account["label"] = label
                cloud = fields.get("cloud_" + identity, "off")
                if cloud not in ("on", "off"):
                    raise ValueError("云摘要开关格式不正确。")
                account["allow_cloud"] = cloud == "on"
            if set(fields) - expected:
                raise ValueError("设置中包含不支持的字段，请刷新页面。")
            try:
                cfg["poll_seconds"] = int(fields["poll_seconds"])
                cfg["llm"]["daily_attempt_limit"] = int(fields["daily_attempt_limit"])
                cfg["llm"]["summary_style"] = fields["summary_style"]
            except (KeyError, ValueError):
                raise ValueError("请填写有效的间隔、调用上限和摘要风格。") from None
            enabled = fields.get("llm_enabled", "off")
            if enabled not in ("on", "off"):
                raise ValueError("模型开关格式不正确。")
            cfg["llm"]["enabled"] = enabled == "on"
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json",
                                                 prefix=".settings-", dir=self.path.parent, delete=False) as output:
                    temporary = Path(output.name)
                    json.dump(cfg, output, ensure_ascii=False, indent=2)
                    output.write("\n")
                    output.flush()
                    os.fsync(output.fileno())
                load(temporary)
                if self.path.read_bytes() != original:
                    raise ValueError("配置已被其他程序修改，请刷新设置页。")
                os.replace(temporary, self.path)
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
