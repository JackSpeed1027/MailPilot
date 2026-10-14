"""Offline regression tests: no real credentials, no external network calls."""
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stdout
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from mailpilot.__main__ import demo, main
from mailpilot.config import example, load
from mailpilot.mail import parse
from mailpilot.runtime import dispatch_one, process_pending, worker_lock
from mailpilot.services import Summarizer, WxPusher, render, validate_result
from mailpilot.source import scan
from mailpilot.store import Store
from mailpilot.web import dashboard, make_server

ACCOUNT = {"id": "qq", "label": "QQ 主邮箱", "email": "owner@qq.com",
           "folder": "INBOX", "allow_cloud": True, "provider": "qq",
           "host": "imap.qq.com", "port": 993, "secret_env": "TEST_MAIL_SECRET"}
LLM = {"enabled": True, "model": "test-model", "base_url": "https://model.example/v1",
       "key_env": "TEST_LLM_KEY", "daily_attempt_limit": 5}
CHANNEL = {"enabled": True, "uid": "UID_test", "token_env": "TEST_PUSH_TOKEN"}
RESULT = {"summary_zh": "请确认周五会议，报价为 12,800 元。",
          "key_points": ["周五开会", "报价 12,800 元"], "needs_action": True,
          "action_summary": "确认会议。"}
CFG = {"llm": LLM, "channel": CHANNEL}


def raw_mail(body="Please confirm Friday's meeting. Quote: CNY 12,800.", subject="Meeting",
             to="team@example.com", message_id="<meeting@example.com>"):
    msg = EmailMessage()
    msg["From"] = "Alice <alice@example.com>"
    if to is not None:
        msg["To"] = to
    msg["Subject"] = subject
    if message_id:
        msg["Message-ID"] = message_id
    msg["Date"] = "Mon, 12 Oct 2026 10:00:00 +0800"
    msg.set_content(body)
    return msg.as_bytes()


def message(uid=1, raw=None, account=None):
    return parse(raw or raw_mail(), account or ACCOUNT, "1", uid)


def model_transport(url, payload, headers=None):
    return {"choices": [{"message": {"content": json.dumps(RESULT, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 123, "completion_tokens": 45}}


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.env = patch.dict(os.environ, {"TEST_LLM_KEY": "fake-key",
                                          "TEST_PUSH_TOKEN": "fake-token"}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)


class ParsingTests(BaseCase):
    def test_actual_account_is_not_to_header(self):
        msg = message()
        self.assertEqual(msg["received_account"], "owner@qq.com")
        self.assertEqual(msg["to_addrs"], ["team@example.com"])

    def test_bcc_keeps_account_when_to_missing(self):
        msg = message(raw=raw_mail(to=None))
        body, _ = render(msg, validate_result(RESULT))
        self.assertIn("未公开", body)
        self.assertIn("owner@qq.com", body)

    def test_encoded_chinese_subject_and_gbk_body(self):
        raw = (b"From: alice@example.com\r\nTo: owner@qq.com\r\n"
               b"Subject: =?utf-8?b?5Lya6K6u6YCa55+l?=\r\n"
               b"Content-Type: text/plain; charset=gbk\r\n\r\n" + "请确认会议".encode("gbk"))
        msg = message(raw=raw)
        self.assertEqual(msg["subject"], "会议通知")
        self.assertIn("请确认会议", msg["body"])

    def test_html_drops_script_hidden_text_and_never_fetches(self):
        mail = EmailMessage()
        mail["Subject"] = "Report"
        mail.set_content('<p>Hello</p><script>steal()</script><div style="display:none">hidden</div><img src="https://evil.example/a">', subtype="html")
        msg = message(raw=mail.as_bytes())
        self.assertIn("Hello", msg["body"])
        self.assertNotIn("steal", msg["body"])
        self.assertNotIn("hidden", msg["body"])

    def test_sensitive_plain_and_subject_are_not_persisted(self):
        msg = message(raw=raw_mail("Your verification code is 867530", "Code 867530"))
        self.store.ingest(msg)
        with self.store.connect() as db:
            payload = db.execute("SELECT payload FROM messages").fetchone()[0]
        self.assertNotIn("867530", payload)
        self.assertEqual(msg["body"], "")
        self.assertTrue(msg["sensitive"])

    def test_sensitive_alternative_html_is_checked_even_when_plain_exists(self):
        mail = EmailMessage()
        mail.set_content("Hello")
        mail.add_alternative("<div>Your OTP is 867530</div>", subtype="html")
        msg = message(raw=mail.as_bytes())
        self.assertTrue(msg["sensitive"])

    def test_same_message_id_changed_amount_not_duplicate(self):
        first = message(raw=raw_mail("Quote 1000"))
        second = message(raw=raw_mail("Quote 2000"))
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])

    def test_missing_id_different_body_not_duplicate(self):
        self.assertNotEqual(message(raw=raw_mail("First", message_id=None))["fingerprint"],
                            message(raw=raw_mail("Second", message_id=None))["fingerprint"])

    def test_attachment_is_metadata_only(self):
        mail = EmailMessage()
        mail.set_content("Read the report.")
        mail.add_attachment(b"SECRET_FILE_CONTENT", maintype="application", subtype="pdf", filename="report.pdf")
        msg = message(raw=mail.as_bytes())
        self.assertEqual(msg["attachments"], ["report.pdf"])
        self.assertNotIn("SECRET_FILE_CONTENT", msg["body"])


class StoreTests(BaseCase):
    def test_replay_does_not_regress_cursor(self):
        self.store.ingest(message(3))
        self.store.ingest(message(1))
        self.store.ingest(message(1))
        self.assertEqual(self.store.cursor("qq", "INBOX")["uid"], 3)
        self.assertEqual(self.store.status()["messages"], 2)

    def test_same_uid_new_validity_is_separate(self):
        one = message()
        two = dict(one, uidvalidity="2")
        self.store.ingest(one)
        self.store.ingest(two)
        self.assertEqual(self.store.status()["messages"], 2)

    def test_failed_input_write_does_not_advance_cursor(self):
        self.store.baseline("qq", "INBOX", "1", 0)
        msg = message()
        msg["body"] = object()
        with self.assertRaises(TypeError):
            self.store.ingest(msg)
        self.assertEqual(self.store.status()["messages"], 0)
        self.assertEqual(self.store.cursor("qq", "INBOX")["uid"], 0)

    def test_exact_duplicate_only_notifies_once(self):
        self.store.ingest(message(1))
        self.store.ingest(message(2))
        summarizer = Summarizer(LLM, self.store, model_transport)
        process_pending(CFG, self.store, summarizer)
        process_pending(CFG, self.store, summarizer)
        self.assertEqual(self.store.status()["delivery"], {"queued": 1})
        self.assertEqual(self.store.status()["model"]["attempts"], 1)

    def test_cross_account_deliveries_both_keep_source_and_notify(self):
        self.store.ingest(message())
        self.store.ingest(message(account=dict(ACCOUNT, id="gmail", label="Gmail", email="owner@gmail.com")))
        process_pending(CFG, self.store, Summarizer(LLM, self.store, model_transport))
        self.assertEqual(self.store.status()["delivery"], {"queued": 2})
        with self.store.connect() as db:
            text = " ".join(row[0] for row in db.execute("SELECT content FROM outbox"))
        self.assertIn("owner@qq.com", text)
        self.assertIn("owner@gmail.com", text)

    def test_opening_dashboard_store_does_not_reset_live_send(self):
        self.store.ingest(message())
        process_pending(CFG, self.store, Summarizer(LLM, self.store, model_transport))
        self.store.next_push()
        other = Store(self.temp.name)
        self.assertEqual(other.status()["delivery"], {"sending": 1})

    def test_worker_recovery_does_not_resend_ambiguous_task(self):
        self.store.ingest(message())
        process_pending(CFG, self.store, Summarizer(LLM, self.store, model_transport))
        self.store.next_push()
        self.store.recover_inflight()
        self.assertEqual(self.store.status()["delivery"], {"unknown": 1})
        self.assertIsNone(self.store.next_push())

    def test_retry_unknown_requires_duplicate_ack(self):
        self.store.ingest(message())
        process_pending(CFG, self.store, Summarizer(LLM, self.store, model_transport))
        task = self.store.next_push()
        self.store.push_result(task, "unknown")
        with self.assertRaises(ValueError):
            self.store.retry_push(task["id"])
        self.store.retry_push(task["id"], True)
        self.assertIsNotNone(self.store.next_push())

    def test_model_daily_limit_reserves_before_request(self):
        self.assertIsNotNone(self.store.reserve_model(1))
        self.assertIsNone(self.store.reserve_model(1))

    def test_worker_lock_prevents_two_runners(self):
        with worker_lock(self.temp.name):
            with self.assertRaises(ValueError):
                with worker_lock(self.temp.name):
                    pass


class ModelTests(BaseCase):
    def test_valid_model_summary_records_usage(self):
        result = Summarizer(LLM, self.store, model_transport).summarize(message())
        self.assertEqual(result["mode"], "model")
        self.assertEqual(self.store.status()["model"]["input_tokens"], 123)

    def test_sensitive_mail_never_calls_model(self):
        with patch("mailpilot.services.secret", side_effect=AssertionError("must not read model key")):
            result = Summarizer(LLM, self.store).summarize(message(raw=raw_mail("验证码 867530")))
        self.assertEqual(result["mode"], "fallback")
        self.assertEqual(self.store.status()["model"]["attempts"], 0)

    def test_cloud_opt_out_never_calls_model(self):
        result = Summarizer(LLM, self.store,
                            lambda *a: self.fail("external call")).summarize(
                                message(account=dict(ACCOUNT, allow_cloud=False)))
        self.assertEqual(result["mode"], "fallback")
        self.assertEqual(self.store.status()["model"]["attempts"], 0)

    def test_invalid_json_falls_back_without_losing_mail(self):
        def bad(*args):
            return {"choices": [{"message": {"content": "not JSON"}}]}
        self.store.ingest(message())
        process_pending(CFG, self.store, Summarizer(LLM, self.store, bad))
        self.assertEqual(self.store.status()["messages"], 1)
        self.assertEqual(self.store.status()["delivery"], {"queued": 1})
        self.assertEqual(json.loads(self.store.recent()[0]["result"])["mode"], "fallback")

    def test_model_timeout_does_not_block_delivery_fallback(self):
        def timeout(*args):
            raise TimeoutError()
        self.assertEqual(Summarizer(LLM, self.store, timeout).summarize(message())["mode"], "fallback")

    def test_schema_rejects_string_boolean_and_forged_action(self):
        with self.assertRaises(ValueError):
            validate_result(dict(RESULT, needs_action="true"))
        with self.assertRaises(ValueError):
            validate_result(dict(RESULT, key_points=["a"] * 10))

    def test_model_cannot_replace_source_addresses(self):
        result = validate_result(dict(RESULT, received_account="attacker@evil.example"))
        content, _ = render(message(), result)
        self.assertIn("owner@qq.com", content)
        self.assertNotIn("attacker@evil.example", content)

    def test_pii_redacted_in_model_request(self):
        calls = []
        def inspect(url, payload, headers):
            calls.append(json.dumps(payload))
            return model_transport(url, payload, headers)
        Summarizer(LLM, self.store, inspect).summarize(
            message(raw=raw_mail("Contact person@example.com at 13800138000; https://evil.example/token?secret=xyz")))
        self.assertNotIn("person@example.com", calls[0])
        self.assertNotIn("13800138000", calls[0])
        self.assertNotIn("secret=xyz", calls[0])


class PushTests(BaseCase):
    def test_checks_per_recipient_failure(self):
        def response(*args):
            return {"code": 1000, "data": [{"uid": "UID_test", "code": 1001}]}
        self.assertEqual(WxPusher(CHANNEL, response).send("test", "test")[0], "retry")

    def test_acceptance_is_not_phone_delivery(self):
        def response(*args):
            return {"code": 1000, "data": [{"uid": "UID_test", "code": 1000, "sendRecordId": 42}]}
        self.assertEqual(WxPusher(CHANNEL, response).send("test", "test"), ("accepted", "", 42))

    def test_timeout_is_unknown_not_auto_retry(self):
        def timeout(*args):
            raise TimeoutError()
        self.assertEqual(WxPusher(CHANNEL, timeout).send("test", "test")[0], "unknown")

    def test_exactly_one_uid_and_plaintext_payload(self):
        def inspect(url, payload, headers=None):
            self.assertEqual(payload["uids"], ["UID_test"])
            self.assertEqual(payload["contentType"], 1)
            self.assertNotIn("topicIds", payload)
            return {"code": 1000, "data": [{"uid": "UID_test", "code": 1000}]}
        WxPusher(CHANNEL, inspect).send("test", "test")

    def test_three_explicit_failures_end_failed(self):
        self.store.ingest(message())
        process_pending(CFG, self.store, Summarizer(LLM, self.store, model_transport))
        class Reject:
            def send(self, *args):
                return "retry", "rejected", ""
        for _ in range(3):
            dispatch_one(CFG, self.store, Reject())
            with self.store.connect() as db:
                db.execute("UPDATE outbox SET next_try=0")
        self.assertEqual(self.store.status()["delivery"], {"failed": 1})


class FakeIMAP:
    def __init__(self, uids, mails=None, fail_fetch=False):
        self.uids = uids
        self.mails = mails or {uid: raw_mail() for uid in uids}
        self.calls = []
        self.fail_fetch = fail_fetch

    def uid(self, operation, *args):
        self.calls.append((operation, args))
        if operation == "search":
            return "OK", [" ".join(map(str, self.uids)).encode()]
        uid = int(args[0])
        if args[1] == "(RFC822.SIZE)":
            return "OK", [f"1 (RFC822.SIZE {len(self.mails[uid])})".encode()]
        if self.fail_fetch:
            return "NO", [b"not fetched"]
        if args[1] == "(BODY.PEEK[HEADER])":
            return "OK", [(b"meta", self.mails[uid].split(b"\n\n", 1)[0])]
        return "OK", [(b"meta", self.mails[uid])]

    def logout(self):
        self.calls.append(("logout", ()))


class SourceTests(BaseCase):
    def test_first_connection_skips_history(self):
        fake = FakeIMAP([1, 2, 3])
        scan(ACCOUNT, self.store, connector=lambda a: (fake, "1"))
        self.assertEqual(self.store.cursor("qq", "INBOX")["uid"], 3)
        self.assertEqual(self.store.status()["messages"], 0)

    def test_imap_reverse_uid_range_is_filtered(self):
        self.store.baseline("qq", "INBOX", "1", 3)
        fake = FakeIMAP([3])
        self.assertEqual(scan(ACCOUNT, self.store, connector=lambda a: (fake, "1")), 0)
        self.assertFalse(any(call[0] == "fetch" for call in fake.calls))

    def test_fetch_uses_peek_and_uid_replay_is_idempotent(self):
        self.store.baseline("qq", "INBOX", "1", 0)
        fake = FakeIMAP([1])
        scan(ACCOUNT, self.store, connector=lambda a: (fake, "1"))
        scan(ACCOUNT, self.store, connector=lambda a: (fake, "1"))
        self.assertEqual(self.store.status()["messages"], 1)
        self.assertTrue(any(call[1][-1] == "(BODY.PEEK[])" for call in fake.calls if call[0] == "fetch"))

    def test_fetch_failure_does_not_move_cursor(self):
        self.store.baseline("qq", "INBOX", "1", 0)
        fake = FakeIMAP([1], fail_fetch=True)
        with self.assertRaises(ValueError):
            scan(ACCOUNT, self.store, connector=lambda a: (fake, "1"))
        self.assertEqual(self.store.cursor("qq", "INBOX")["uid"], 0)

    def test_uidvalidity_change_pauses_without_skipping_silently(self):
        self.store.baseline("qq", "INBOX", "1", 3)
        fake = FakeIMAP([1])
        scan(ACCOUNT, self.store, connector=lambda a: (fake, "2"))
        self.assertEqual(self.store.cursor("qq", "INBOX")["validity"], "1")
        self.assertEqual(self.store.status()["accounts"][0]["ok"], 0)

    def test_oversized_mail_fetches_only_headers(self):
        self.store.baseline("qq", "INBOX", "1", 0)
        fake = FakeIMAP([1], mails={1: raw_mail("x" * 4000)})
        scan(ACCOUNT, self.store, max_bytes=1024, connector=lambda a: (fake, "1"))
        self.assertTrue(any(call[1][-1] == "(BODY.PEEK[HEADER])" for call in fake.calls if call[0] == "fetch"))
        self.assertIn("local_reason", self.store.pending()[0][1])


class ConfigWebCLITests(BaseCase):
    def test_config_relative_data_directory_and_privacy_default(self):
        cfg = example()
        cfg["channel"]["uid"] = "UID_test"
        path = Path(self.temp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        loaded = load(path)
        self.assertEqual(loaded["data_dir"], str(Path(self.temp.name) / "data"))
        self.assertFalse(loaded["accounts"][0]["allow_cloud"])

    def test_config_rejects_plain_http_and_broadcast_uid(self):
        cfg = example()
        cfg["llm"]["base_url"] = "http://model.example/v1"
        cfg["channel"]["uid"] = "UID_test"
        path = Path(self.temp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        with self.assertRaises(ValueError):
            load(path)
        cfg["llm"]["base_url"] = "https://model.example/v1"
        cfg["channel"]["uid"] = "@all"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        with self.assertRaises(ValueError):
            load(path)

    def test_html_escape_prevents_stored_script_injection(self):
        self.store.ingest(message(raw=raw_mail("<script>alert(1)</script>", "<img src=x onerror=alert(1)>")))
        process_pending(CFG, self.store, Summarizer(LLM, self.store, model_transport))
        markup = dashboard(self.store)
        self.assertNotIn("<img src=x", markup)
        self.assertIn("&lt;img", markup)

    def test_local_http_serves_page_status_detail_and_rejects_foreign_host(self):
        self.store.ingest(message())
        server = make_server(self.store, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            with urllib.request.urlopen(url) as response:
                self.assertIn("MailPilot", response.read().decode())
                self.assertEqual(response.headers["Cache-Control"], "no-store")
            with urllib.request.urlopen(url + "/api/status") as response:
                self.assertEqual(json.load(response)["messages"], 1)
            with urllib.request.urlopen(url + "/messages/1") as response:
                self.assertIn("owner@qq.com", response.read().decode())
            request = urllib.request.Request(url, headers={"Host": "evil.example"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request)
            self.assertEqual(caught.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_cli_demo_is_repeatable_without_network(self):
        with redirect_stdout(io.StringIO()), patch("socket.create_connection", side_effect=AssertionError("external network")):
            demo(self.temp.name)
            demo(self.temp.name)
        self.assertEqual(self.store.status()["messages"], 3)
        self.assertEqual(self.store.status()["delivery"], {"preview": 3})

    def test_init_does_not_overwrite_existing_config(self):
        path = Path(self.temp.name) / "config.json"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--config", str(path), "init"]), 0)
        original = path.read_bytes()
        with patch("sys.stderr", io.StringIO()):
            self.assertEqual(main(["--config", str(path), "init"]), 1)
        self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
