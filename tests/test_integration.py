"""Full offline runner and setup contracts, including real Windows lock/HTTP code."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mailpilot.__main__ import setup
from mailpilot.config import load
from mailpilot.runtime import run_once
from mailpilot.services import Summarizer, render, validate_result
from mailpilot.source import connect
from mailpilot.store import Store
from test_mailpilot import ACCOUNT, CHANNEL, LLM, RESULT, message, model_transport


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.environment = patch.dict(os.environ, {"TEST_LLM_KEY": "fake-key",
                                                   "TEST_MAIL_SECRET": "fake-mail-secret"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_runner_delivers_two_accounts_end_to_end(self):
        accounts = [ACCOUNT, dict(ACCOUNT, id="gmail", email="owner@gmail.com", label="Gmail")]
        cfg = {"accounts": accounts, "llm": LLM, "channel": CHANNEL,
               "data_dir": self.temp.name, "max_message_bytes": 2000000}
        def fake_scan(account, store, max_bytes):
            store.ingest(message(account=account))
            store.health(account["id"], True)
            return 1
        class Accept:
            def __init__(self, cfg):
                pass
            def send(self, content, preview):
                self_account_present = "owner@" in content
                if not self_account_present:
                    raise AssertionError("missing account")
                return "accepted", "", "fake-record"
        with patch("mailpilot.runtime.scan", side_effect=fake_scan), \
             patch("mailpilot.runtime.Summarizer", side_effect=lambda cfg, db: Summarizer(cfg, db, model_transport)), \
             patch("mailpilot.runtime.WxPusher", Accept):
            run_once(cfg, self.store)
            run_once(cfg, self.store)
        self.assertEqual(self.store.status()["messages"], 2)
        self.assertEqual(self.store.status()["delivery"], {"accepted": 2})

    def test_one_failed_mailbox_does_not_block_other(self):
        accounts = [ACCOUNT, dict(ACCOUNT, id="gmail", email="owner@gmail.com", label="Gmail")]
        cfg = {"accounts": accounts, "llm": LLM, "channel": dict(CHANNEL, enabled=False),
               "data_dir": self.temp.name, "max_message_bytes": 2000000}
        def fake_scan(account, store, max_bytes):
            if account["id"] == "qq":
                raise OSError("DO_NOT_LEAK_SECRET")
            store.ingest(message(account=account))
            store.health(account["id"], True)
            return 1
        with patch("mailpilot.runtime.scan", side_effect=fake_scan), \
             patch("mailpilot.runtime.Summarizer", side_effect=lambda cfg, db: Summarizer(cfg, db, model_transport)):
            run_once(cfg, self.store)
        self.assertEqual(self.store.status()["messages"], 1)
        self.assertEqual(self.store.status()["delivery"], {"preview": 1})
        self.assertNotIn("DO_NOT_LEAK_SECRET", json.dumps(self.store.status()))

    def test_setup_saves_choice_but_never_requests_or_stores_password(self):
        path = Path(self.temp.name) / "config.json"
        answers = iter(["qq", "owner@qq.com", "主邮箱", "y", "",
                        "https://model.example/v1", "example-model", "UID_test"])
        with patch("builtins.input", side_effect=lambda p: next(answers)), redirect_stdout(io.StringIO()):
            setup(path)
        cfg = load(path)
        self.assertTrue(cfg["accounts"][0]["allow_cloud"])
        self.assertEqual(cfg["llm"]["model"], "example-model")
        self.assertIn("secret_env", cfg["accounts"][0])
        self.assertNotIn("fake-mail-secret", path.read_text(encoding="utf-8"))
        self.assertFalse(path.with_suffix(".validation.json").exists())

    def test_setup_invalid_uid_leaves_no_partial_configuration(self):
        path = Path(self.temp.name) / "config.json"
        answers = iter(["qq", "owner@qq.com", "主邮箱", "n", "", "not-a-uid"])
        with patch("builtins.input", side_effect=lambda p: next(answers)), redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError):
                setup(path)
        self.assertFalse(path.exists())
        self.assertFalse(path.with_suffix(".validation.json").exists())

    def test_long_recipient_list_keeps_summary_and_actual_account_in_push(self):
        msg = message()
        msg["to_addrs"] = [f"recipient{i}@example.com" for i in range(500)]
        content, _ = render(msg, validate_result(RESULT))
        self.assertIn(RESULT["summary_zh"], content)
        self.assertIn("owner@qq.com", content)
        self.assertIn(RESULT["action_summary"], content)
        self.assertLessEqual(len(content), 6000)

    def test_imap_login_selects_readonly_and_163_sends_id(self):
        calls = []
        class FakeConnection:
            capabilities = ("ID",)
            def login(self, user, password):
                calls.append(("login", user))
            def _simple_command(self, command, arguments):
                calls.append(("command", command))
                return "OK", []
            def select(self, folder, readonly=False):
                calls.append(("select", folder, readonly))
                return "OK", [b"0"]
            def response(self, name):
                return name, [b"123"]
            def logout(self):
                calls.append(("logout",))
        with patch("mailpilot.source.imaplib.IMAP4_SSL", return_value=FakeConnection()) as ssl:
            client, validity = connect(dict(ACCOUNT, provider="163"))
            self.assertEqual(validity, "123")
            self.assertIn(("select", "INBOX", True), calls)
            self.assertIn(("command", "ID"), calls)
            ssl.assert_called_once_with("imap.qq.com", 993, timeout=20)
            client.logout()

    def test_login_failure_closes_connection(self):
        calls = []
        class FakeConnection:
            def login(self, user, password):
                raise OSError("failed")
            def logout(self):
                calls.append("logout")
        with patch("mailpilot.source.imaplib.IMAP4_SSL", return_value=FakeConnection()):
            with self.assertRaises(OSError):
                connect(ACCOUNT)
        self.assertEqual(calls, ["logout"])


if __name__ == "__main__":
    unittest.main()
