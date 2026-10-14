"""Multilingual reading, credential isolation and local configuration regression."""
import json
import os
import re
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode
from mailpilot.credentials import Credentials, WindowsVault, bindings
from mailpilot.mail import protect_translation, restore_translation, guess_language
from mailpilot.services import Summarizer, render, ModelFailure
from mailpilot.translation import split_text, translate
from mailpilot.ui import UIController, configuration_page
from mailpilot.web import detail_page, make_server
from test_mailpilot import BaseCase, ACCOUNT, LLM, RESULT, message, raw_mail

JAPANESE = '''お世話になっております。
本メールは、メール送受信システムの正常動作を確認するためのテスト送信となります。業務内容は含まれませんので、お手数ですが無視していただいて問題ございません。
今後ともよろしくお願いいたします。
テストユーザー
2026年10月12日'''


class ReadingTests(BaseCase):
    def cfg(self, **changes):
        return dict(LLM, translation_enabled=True, translation_chunk_chars=500,
                    translation_max_chars=30000, **changes)

    def test_business_verification_and_one_time_payment_not_sensitive(self):
        for body in ['Please review the verification report for Friday. Budget: 1200 dollars.',
                     'This is a one-time payment of 12800 CNY for the approved contract.',
                     'Verification complete. Please review the deployment plan.']:
            with self.subTest(body=body):
                self.assertFalse(message(raw=raw_mail(body))['sensitive'])

    def test_actual_codes_and_auth_links_still_protected(self):
        for body in ['Your verification code is 123456.', '認証コード：123456',
                     '验证码 123456，请勿透露。', 'Click to verify your email: https://example.com/verify?token=secret']:
            with self.subTest(body=body):
                msg = message(raw=raw_mail(body))
                self.assertTrue(msg['sensitive'])
                self.assertEqual(msg['body'], '')

    def test_japanese_test_is_non_sensitive_and_language_routed(self):
        msg = message(raw=raw_mail(JAPANESE, subject='システム動作確認用テストメール'))
        self.assertFalse(msg['sensitive'])
        self.assertEqual(guess_language(msg['body']), 'ja')

    def test_chunking_preserves_all_text(self):
        original = ('Hello world.\n' * 400) + JAPANESE
        chunks = split_text(original, 500)
        self.assertEqual(''.join(chunks), original)
        self.assertTrue(all(0 < len(chunk) <= 500 for chunk in chunks))
        marker = '[MPPRIVATE' + 'untrusted-body' * 500 + ']'
        self.assertEqual(''.join(split_text(marker, 500)), marker)

    def test_private_identifiers_round_trip_and_missing_placeholder_rejected(self):
        original = 'Contact person@example.com https://example.com/path 13800000000'
        masked, mapping = protect_translation(original)
        self.assertNotIn('person@example.com', masked)
        self.assertNotIn('https://example.com', masked)
        self.assertEqual(restore_translation(masked, mapping), original)
        with self.assertRaises(ValueError):
            restore_translation('译文没有保留占位符', mapping)

    def test_full_translation_separate_from_summary(self):
        calls = []
        def fake(url, payload, headers):
            calls.append(payload)
            value = dict(RESULT, summary_zh='这是系统收发测试邮件，可忽略。', source_language='ja') if len(calls) == 1 else {
                'translation_zh': '承蒙关照。此邮件用于确认邮件收发系统运行正常，不包含业务内容，可以忽略。今后也请多多关照。测试用户，2026年10月12日。',
                'subject_zh': '系统运行确认测试邮件', 'chunk_summary_zh': '系统收发测试，无业务内容，可忽略。'}
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(value)}}]}
        msg = message(raw=raw_mail(JAPANESE))
        result = Summarizer(self.cfg(), self.store, fake).summarize(msg)
        self.assertEqual(result['translation_status'], 'complete')
        self.assertEqual(len(calls), 2)
        self.assertIn('2026年10月12日', result['translation_zh'])
        self.assertNotEqual(result['summary_zh'], result['translation_zh'])
        content, _ = render(msg, result)
        self.assertIn('全文中文翻译', content)
        self.assertIn(JAPANESE.splitlines()[0], content)

    def test_translation_budget_shared_with_summary(self):
        def fake(url, payload, headers):
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(dict(RESULT, source_language='ja'))}}]}
        result = Summarizer(self.cfg(daily_attempt_limit=1), self.store, fake).summarize(message(raw=raw_mail(JAPANESE)))
        self.assertEqual(result['translation_status'], 'failed')
        self.assertIn('上限', result['translation_note'])
        self.assertEqual(self.store.status()['model']['attempts'], 1)

    def test_length_finish_reason_never_claims_full_translation(self):
        calls = []
        def fake(url, payload, headers):
            calls.append(payload)
            value = dict(RESULT, source_language='ja') if len(calls) == 1 else {
                'translation_zh': '不完整', 'subject_zh': '测试', 'chunk_summary_zh': '测试'}
            return {'choices': [{'finish_reason': 'stop' if len(calls) == 1 else 'length', 'message': {'content': json.dumps(value)}}]}
        result = Summarizer(self.cfg(), self.store, fake).summarize(message(raw=raw_mail(JAPANESE)))
        self.assertEqual(result['translation_status'], 'failed')
        self.assertEqual(result['translation_zh'], '')

    def test_partial_chunks_keep_completed_text_with_explicit_status(self):
        count = 0
        def request(prompt, payload, *args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise ModelFailure('api_error')
            return {'translation_zh': '第一段完整译文', 'subject_zh': '主题', 'chunk_summary_zh': '第一段事实'}
        result = translate(message(raw=raw_mail(JAPANESE * 10)), self.cfg(), request)
        self.assertEqual(result['translation_status'], 'partial')
        self.assertEqual(result['translation_chunks_done'], 1)
        self.assertIn('第一段', result['translation_zh'])

    def test_too_long_no_translation_call(self):
        def never(*args, **kwargs):
            self.fail('over limit must not make translation call')
        result = translate(message(raw=raw_mail(JAPANESE * 20)), dict(self.cfg(), translation_max_chars=1000), never)
        self.assertEqual(result['translation_status'], 'too_long')

    def test_subject_placeholder_failure_cannot_mark_one_chunk_complete(self):
        msg = message(raw=raw_mail('English body text for translation.', subject='Reply to person@example.com'))
        result = translate(msg, self.cfg(), lambda *args, **kwargs: dict(translation_zh='英文正文译文',
                            subject_zh='回复某人', chunk_summary_zh='需要回复'))
        self.assertEqual(result['translation_status'], 'failed')
        self.assertEqual(result['translation_chunks_done'], 0)

    def test_private_address_crossing_boundary_is_masked_and_restored(self):
        body = 'A' * 490 + ' contact@example.com ' + 'English end.' * 50
        seen = []
        def fake(prompt, payload, *args, **kwargs):
            seen.append(payload['body'])
            return dict(translation_zh=payload['body'], subject_zh=payload['subject'], chunk_summary_zh='本段事实')
        result = translate(message(raw=raw_mail(body)), self.cfg(), fake)
        self.assertEqual(result['translation_status'], 'complete')
        self.assertNotIn('contact@example.com', ''.join(seen))
        self.assertIn('contact@example.com', result['translation_zh'])

    def test_sensitive_and_nonconsent_do_not_call_model(self):
        def never(*args, **kwargs):
            self.fail('must not send to model')
        for msg in [message(raw=raw_mail('Your verification code is 123456')),
                    message(account=dict(ACCOUNT, allow_cloud=False))]:
            self.assertEqual(Summarizer(self.cfg(), self.store, never).summarize(msg)['mode'], 'fallback')

    def test_translation_detail_escapes_both_languages(self):
        msg = message(raw=raw_mail('<script>bad()</script> English body text.'))
        self.store.ingest(msg)
        self.store.complete(1, dict(RESULT, source_language='en', translation_status='partial', translation_zh='<script>翻译</script>'), 'test', 'test', False)
        page = detail_page(self.store.detail(1))
        self.assertNotIn('<script>', page)
        self.assertIn('&lt;script&gt;', page)
        self.assertIn('翻译未完成', page)
        self.assertIn('邮件原文', page)

    def test_push_long_reading_is_explicitly_excerpt(self):
        msg = message(raw=raw_mail('English original body. ' * 500))
        result = dict(RESULT, source_language='en', translation_status='complete', translation_zh='全文中文译文。' * 1200)
        content, _ = render(msg, result)
        self.assertLessEqual(len(content), 6000)
        self.assertIn('节选', content)


class FakeVault:
    def __init__(self):
        self.values = {}
    def write(self, target, value):
        self.values[target] = value
    def read(self, target):
        return self.values.get(target, '')
    def delete(self, target):
        self.values.pop(target, None)


class ConfigurationTests(BaseCase):
    def controller(self):
        def runner(cfg, store, stop):
            stop.wait()
        controller = UIController(Path(self.temp.name) / 'config.json', runner)
        controller.credentials.vault = FakeVault()
        self.addCleanup(controller.close)
        return controller

    def fields(self, controller):
        cfg, revision = controller.read()
        values = dict(revision=revision, poll_seconds='60', daily_attempt_limit='100', summary_style='detailed',
                      base_url='https://api.deepseek.com/v1', model='deepseek-flash', translation_enabled='on',
                      translation_max_chars='30000', new_email='synthetic@qq.com', new_provider='qq',
                      new_label='个人邮箱', new_secret='fake-mail-secret', uid='')
        for acc in cfg['accounts']:
            values['label_' + acc['id']] = acc['label']
        return values

    def test_first_configuration_created_without_console_or_plaintext_secrets(self):
        controller = self.controller()
        controller.save(self.fields(controller))
        saved = controller.path.read_text(encoding='utf-8')
        self.assertNotIn('fake-mail-secret', saved)
        self.assertIn('synthetic@qq.com', saved)
        page = configuration_page(controller, 'csrf')
        self.assertNotIn('fake-mail-secret', page)
        self.assertIn('API Key', page)
        self.assertIn('AppToken', page)
        self.assertIn('WxPusher 官网', page)

    def test_stale_revision_rejected(self):
        controller = self.controller()
        values = self.fields(controller)
        controller.save(values)
        with self.assertRaises(ValueError):
            controller.save(values)

    def test_unknown_fields_do_not_write_config(self):
        controller = self.controller()
        with self.assertRaises(ValueError):
            controller.save(dict(self.fields(controller), data_dir='elsewhere'))
        self.assertFalse(controller.path.exists())

    def test_invalid_url_rejected_before_credentials_saved(self):
        controller = self.controller()
        values = dict(self.fields(controller), llm_enabled='on', new_cloud='on', base_url='http://example.com', model_key='fake-key')
        with self.assertRaises(ValueError):
            controller.save(values)
        self.assertFalse(controller.path.exists())
        self.assertEqual(controller.credentials.session, {})

    def test_credential_scope_prevents_old_key_to_new_api(self):
        vault = FakeVault()
        credentials = Credentials('test-project', vault)
        credentials.put('KEY', 'model:https://a.example', 'fake-key', True)
        self.assertEqual(credentials.get('KEY', 'model:https://a.example'), 'fake-key')
        self.assertEqual(credentials.get('KEY', 'model:https://b.example'), '')
        self.assertEqual(Credentials('other-project', vault).get('KEY', 'model:https://a.example'), '')

    def test_session_only_replaces_persistent_credential(self):
        vault = FakeVault()
        credentials = Credentials('test-project', vault)
        credentials.put('KEY', 'binding', 'remembered', True)
        credentials.put('KEY', 'binding', 'session-only', False)
        self.assertEqual(credentials.get('KEY', 'binding'), 'session-only')
        self.assertEqual(Credentials('test-project', vault).get('KEY', 'binding'), '')

    def test_missing_credentials_saves_config_but_does_not_start(self):
        controller = self.controller()
        values = dict(self.fields(controller), new_secret='')
        controller.save(values)
        with controller.guard:
            pass
        self.assertIn('尚未开始收件', controller.status)
        self.assertIsNone(controller.worker)

    def test_restart_waits_for_old_worker_before_new_config(self):
        controller = self.controller()
        calls = []
        started = threading.Event()
        def runner(cfg, store, stop):
            calls.append('start:' + cfg['llm']['model'])
            started.set()
            stop.wait()
            calls.append('end:' + cfg['llm']['model'])
        controller.runner = runner
        controller.save(self.fields(controller))
        self.assertTrue(started.wait(2))
        started.clear()
        values = self.fields(controller)
        values.update(new_email='', model='second-model')
        controller.save(values)
        self.assertTrue(started.wait(2))
        self.assertEqual(calls[:3], ['start:deepseek-flash', 'end:deepseek-flash', 'start:second-model'])

    def test_credential_write_failure_rolls_back_before_config_replace(self):
        controller = self.controller()
        class FailSecond(FakeVault):
            def write(self, target, value):
                if value == 'fail-this-write':
                    raise ValueError('合成存储失败')
                super().write(target, value)
        controller.credentials.vault = FailSecond()
        values = dict(self.fields(controller), remember='on', model_key='fail-this-write')
        with self.assertRaises(ValueError):
            controller.save(values)
        self.assertFalse(controller.path.exists())
        self.assertEqual(controller.credentials.session, {})
        self.assertEqual(controller.credentials.vault.values, {})

    def test_http_form_csrf_origin_and_secret_non_echo(self):
        controller = self.controller()
        server = make_server(controller.store, 0, config_path=controller.path, controller=controller)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'
        with urllib.request.urlopen(base + '/settings') as response:
            page = response.read().decode()
        csrf = re.search('name="csrf" value="([^"]+)"', page).group(1)
        values = dict(self.fields(controller), csrf=csrf)
        def post(origin):
            return urllib.request.urlopen(urllib.request.Request(base + '/settings',
                data=urlencode(values).encode(), headers={'Origin': origin, 'Content-Type': 'application/x-www-form-urlencoded'}))
        with self.assertRaises(urllib.error.HTTPError) as error:
            post('https://evil.example')
        self.assertEqual(error.exception.code, 403)
        values['csrf'] = 'wrong'
        with self.assertRaises(urllib.error.HTTPError):
            post(base)
        values['csrf'] = csrf
        with post(base) as response:
            page = response.read().decode()
        self.assertNotIn('fake-mail-secret', page)
        self.assertIn('配置已保存', page)

    @unittest.skipUnless(os.name == 'nt', 'Windows only')
    def test_windows_vault_synthetic_roundtrip_and_delete(self):
        import uuid
        vault = WindowsVault()
        target = 'MailPilot/test/' + uuid.uuid4().hex
        try:
            vault.write(target, 'synthetic-only-假密钥')
            self.assertEqual(vault.read(target), 'synthetic-only-假密钥')
        finally:
            vault.delete(target)
        self.assertEqual(vault.read(target), '')
