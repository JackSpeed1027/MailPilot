"""Offline reply transport and bounded tool Agent; never sends real mail."""
import json
import re
import smtplib
import threading
import urllib.request
import urllib.error
import uuid
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode
from mailpilot.agent import ReplyAgent
from mailpilot.config import load
from mailpilot.credentials import bindings
from mailpilot.mail import parse, clean, readable
from mailpilot.reply import target, build_message, smtp_send, save_draft, compose_page
from mailpilot.ui import UIController
from mailpilot.web import make_server, detail_page
from test_mailpilot import BaseCase, ACCOUNT, LLM, RESULT, message, raw_mail
import test_reading_ui as configurations


def tool_response(names=('get_email_context', 'get_reply_preferences')):
    return {'choices': [{'finish_reason': 'tool_calls', 'message': {'tool_calls': [
        {'id': 'call_' + str(index), 'type': 'function', 'function': {'name': name, 'arguments': '{}'}}
        for index, name in enumerate(names)]}}], 'usage': {'prompt_tokens': 40, 'completion_tokens': 10}}


def draft_response(**changes):
    value = dict(subject='Re: Meeting', body='感谢邀请。关于时间安排，我确认后回复。')
    value.update(changes)
    return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(value)}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 40}}


class ReplyTests(BaseCase):
    def cfg(self):
        return dict(accounts=[dict(ACCOUNT, smtp_enabled=True, smtp_host='smtp.qq.com')])

    def saved(self):
        msg = message()
        msg['reply_to_addrs'] = ['reply@example.com']
        msg['references'] = '<parent@example.com>'
        self.store.ingest(msg)
        identity = uuid.uuid4().hex
        save_draft(self.store, self.cfg(), 1, dict(draft_id=identity, revision='0', subject='Re: Meeting', body='收到，稍后确认。'))
        return self.store.reply(identity), msg

    def test_nbsp_and_nested_entities_are_readable_without_rendering_html(self):
        self.assertEqual(clean('A&nbsp;B &amp;nbsp; C\xa0D'), 'A B   C D')
        msg = message(raw=raw_mail('Hello&nbsp;world &amp;nbsp;', subject='Meeting&nbsp;news'))
        self.assertNotIn('&nbsp;', msg['body'])
        self.assertEqual(msg['subject'], 'Meeting news')
        self.store.ingest(msg)
        page = detail_page(self.store.detail(1))
        self.assertNotIn('&amp;nbsp;', page)

    def test_legacy_entities_fixed_at_display_and_script_still_escaped(self):
        msg = message()
        msg['body'] = 'Old &nbsp; text &lt;script&gt;bad()&lt;/script&gt;'
        self.store.ingest(msg)
        self.store.complete(1, dict(RESULT, summary_zh='重点&nbsp;说明'), 'x', 'x', False)
        page = detail_page(self.store.detail(1))
        self.assertNotIn('&amp;nbsp;', page)
        self.assertNotIn('<script>', page)
        self.assertIn('&lt;script&gt;', page)

    def test_nested_encoded_code_still_sensitive(self):
        msg = message(raw=raw_mail('Verification code: &amp;#49;&amp;#50;&amp;#51;&amp;#52;&amp;#53;&amp;#54;'))
        self.assertTrue(msg['sensitive'])
        self.assertEqual(msg['body'], '')

    def test_reply_to_precedes_from(self):
        msg = message()
        msg['reply_to_addrs'] = ['reply@example.com']
        self.assertEqual(target(msg), 'reply@example.com')
        msg['reply_to_addrs'] = []
        self.assertEqual(target(msg), 'alice@example.com')

    def test_multiple_invalid_and_sensitive_recipients_blocked(self):
        for msg in [dict(message(), reply_to_addrs=['one@example.com', 'two@example.com']),
                    dict(message(), reply_to_addrs=['evil@example.com\r\nBcc: other@example.com']),
                    dict(message(), sensitive=True)]:
            with self.assertRaises(ValueError):
                target(msg)

    def test_parse_stores_reply_headers(self):
        from email import policy
        from email.parser import BytesParser
        mail = BytesParser(policy=policy.default).parsebytes(raw_mail())
        mail['Reply-To'] = 'Reply Person <reply@example.com>'
        mail['References'] = '<parent@example.com>'
        msg = parse(mail.as_bytes(), ACCOUNT, '1', 1)
        self.assertEqual(msg['reply_to_addrs'], ['reply@example.com'])
        self.assertEqual(msg['references'], '<parent@example.com>')

    def test_thread_headers_source_account_and_single_recipient(self):
        draft, msg = self.saved()
        email = build_message(draft, msg)
        self.assertEqual(email['From'], ACCOUNT['email'])
        self.assertEqual(email['To'], 'reply@example.com')
        self.assertEqual(email['In-Reply-To'], '<meeting@example.com>')
        self.assertIn('<parent@example.com>', email['References'])
        self.assertIsNone(email['Cc'])
        self.assertEqual(email.get_content().strip(), '收到，稍后确认。')

    def test_draft_edit_version_and_duplicate_send_claim(self):
        draft, msg = self.saved()
        save_draft(self.store, self.cfg(), 1, dict(draft_id=draft['id'], revision='1', subject='Re: Updated', body='更新正文'))
        with self.assertRaises(ValueError):
            self.store.claim_reply(draft['id'], 1)
        self.store.claim_reply(draft['id'], 2)
        with self.assertRaises(ValueError):
            self.store.claim_reply(draft['id'], 2)
        self.store.reply_result(draft['id'], 'accepted')
        self.assertEqual(self.store.reply(draft['id'])['state'], 'accepted')

    def test_smtp_disabled_and_header_injection_do_not_save(self):
        self.store.ingest(message())
        fields = dict(draft_id=uuid.uuid4().hex, revision='0', subject='Re: x\r\nBcc: evil@example.com', body='test')
        with self.assertRaises(ValueError):
            save_draft(self.store, self.cfg(), 1, fields)
        fields['subject'] = 'Re: x'
        with self.assertRaises(ValueError):
            save_draft(self.store, dict(accounts=[dict(ACCOUNT, smtp_enabled=False)]), 1, fields)
        self.assertEqual(self.store.replies_for(1), [])

    def test_smtp_acceptance_not_overwritten_by_quit_failure(self):
        draft, msg = self.saved()
        class Fake:
            def login(self, email, password):
                self.email = email
            def send_message(self, mail, **kwargs):
                self.mail = mail
                return {}
            def quit(self):
                raise OSError('synthetic close failure')
        client = Fake()
        self.assertEqual(smtp_send(self.cfg()['accounts'][0], 'fake-secret', draft, msg,
                                 lambda *args, **kwargs: client)[0], 'accepted')

    def test_smtp_failure_before_data_vs_unknown_after_data(self):
        draft, msg = self.saved()
        class Fake:
            def login(self, *args):
                pass
            def send_message(self, *args, **kwargs):
                raise TimeoutError('secret-server-text')
            def quit(self):
                pass
        unknown = smtp_send(self.cfg()['accounts'][0], 'fake', draft, msg, lambda *a, **k: Fake())
        self.assertEqual(unknown[0], 'unknown')
        self.assertNotIn('secret-server-text', unknown[1])
        def offline(*a, **k):
            raise TimeoutError()
        self.assertEqual(smtp_send(self.cfg()['accounts'][0], 'fake', draft, msg, offline)[0], 'failed')

    def test_restart_recovery_marks_sending_unknown_never_resends(self):
        draft, msg = self.saved()
        self.store.claim_reply(draft['id'], 1)
        self.store.recover_replies()
        self.assertEqual(self.store.reply(draft['id'])['state'], 'unknown')


class AgentTests(BaseCase):
    def test_model_selects_read_tools_then_returns_draft(self):
        requests = []
        def fake(url, payload, headers):
            requests.append(payload)
            return tool_response() if len(requests) == 1 else draft_response()
        agent = ReplyAgent(LLM, self.store, 'fake-key', fake)
        result = agent.draft(message(), {'tone': 'professional', 'signature': 'private@example.com'})
        self.assertIn('感谢', result['body'])
        self.assertEqual([x['step'] for x in agent.trace], ['model_request', 'get_email_context', 'get_reply_preferences', 'model_request', 'draft_validation'])
        self.assertEqual(self.store.status()['model']['attempts'], 2)
        tools = requests[0]['tools']
        self.assertEqual({tool['function']['name'] for tool in tools}, {'get_email_context', 'get_reply_preferences'})
        self.assertNotIn('private@example.com', json.dumps(requests))
        self.assertNotIn('alice@example.com', json.dumps(requests))

    def test_forbidden_tool_stops_without_execution(self):
        agent = ReplyAgent(LLM, self.store, 'fake', lambda *a: tool_response(('send_mail',)))
        with self.assertRaises(ValueError):
            agent.draft(message(), {})
        self.assertEqual(self.store.status()['model']['attempts'], 1)

    def test_model_draft_entities_are_cleaned_before_user_reads(self):
        responses = iter([tool_response(), draft_response(body='您好&nbsp;谢谢&amp;nbsp;')])
        result = ReplyAgent(LLM, self.store, 'fake', lambda *a: next(responses)).draft(message(), {})
        self.assertNotIn('&nbsp;', result['body'])

    def test_nonempty_tool_arguments_rejected(self):
        response = tool_response()
        response['choices'][0]['message']['tool_calls'][0]['function']['arguments'] = '{"mail_id":999}'
        agent = ReplyAgent(LLM, self.store, 'fake', lambda *a: response)
        with self.assertRaises(ValueError):
            agent.draft(message(), {})

    def test_no_cloud_permission_no_model_call(self):
        def never(*args):
            self.fail('not authorized')
        agent = ReplyAgent(LLM, self.store, 'fake', never)
        with self.assertRaises(ValueError):
            agent.draft(dict(message(), allow_cloud=False), {})

    def test_model_cannot_skip_tools_or_add_recipient(self):
        agent = ReplyAgent(LLM, self.store, 'fake', lambda *a: draft_response())
        with self.assertRaises(ValueError):
            agent.draft(message(), {})
        responses = iter([tool_response(), draft_response(recipient='evil@example.com')])
        agent = ReplyAgent(LLM, self.store, 'fake', lambda *a: next(responses))
        with self.assertRaises(ValueError):
            agent.draft(message(), {})

    def test_budget_shared_and_trace_contains_no_body_or_credentials(self):
        agent = ReplyAgent(dict(LLM, daily_attempt_limit=1), self.store, 'fake-secret', lambda *a: tool_response())
        with self.assertRaises(ValueError):
            agent.draft(message(), {})
        trace = json.dumps(agent.trace)
        self.assertNotIn('fake-secret', trace)
        self.assertNotIn('meeting', trace)

    def test_truncated_draft_is_rejected(self):
        response = draft_response()
        response['choices'][0]['finish_reason'] = 'length'
        responses = iter([tool_response(), response])
        with self.assertRaises(ValueError):
            ReplyAgent(LLM, self.store, 'fake', lambda *a: next(responses)).draft(message(), {})

    def test_long_body_cannot_be_silently_truncated_for_reply(self):
        def never(*args):
            self.fail('partial source must not reach reply agent')
        with self.assertRaises(ValueError):
            ReplyAgent(LLM, self.store, 'fake', never).draft(dict(message(), body='English body ' * 1200), {})

    def test_repeated_tool_calls_cannot_loop(self):
        agent = ReplyAgent(LLM, self.store, 'fake', lambda *a: tool_response())
        with self.assertRaises(ValueError):
            agent.draft(message(), {})
        self.assertEqual(self.store.status()['model']['attempts'], 2)


class ReplyControllerTests(BaseCase):
    controller = configurations.ConfigurationTests.controller
    fields = configurations.ConfigurationTests.fields
    def prepared(self):
        controller = self.controller()
        values = dict(self.fields(controller), new_smtp='on', new_cloud='on', llm_enabled='on', model_key='fake-key')
        controller.save(values)
        with controller.guard:
            pass
        cfg = load(controller.path)
        account = cfg['accounts'][0]
        msg = message(account=account)
        msg['reply_to_addrs'] = ['reply@example.com']
        controller.store.ingest(msg)
        return controller, cfg, msg

    def test_controller_manual_send_once_and_model_not_needed(self):
        controller, cfg, msg = self.prepared()
        identity = uuid.uuid4().hex
        save_draft(controller.store, cfg, 1, dict(draft_id=identity, revision='0', subject='Re: x', body='确认收到'))
        sent = []
        def fake(account, key, draft, original):
            sent.append(draft['id'])
            return 'accepted', ''
        controller.send_reply(identity, 1, fake)
        with self.assertRaises(ValueError):
            controller.send_reply(identity, 1, fake)
        self.assertEqual(sent, [identity])

    def test_controller_ai_creates_draft_and_appends_signature_locally(self):
        controller, cfg, msg = self.prepared()
        raw = json.loads(controller.path.read_text(encoding='utf-8'))
        raw['reply_preferences']['signature'] = '我的测试署名'
        controller.path.write_text(json.dumps(raw), encoding='utf-8')
        responses = iter([tool_response(), draft_response()])
        identity = uuid.uuid4().hex
        controller.make_ai_draft(1, dict(csrf='test', draft_id=identity), lambda *a: next(responses))
        draft = controller.store.reply(identity)
        self.assertEqual(draft['state'], 'draft')
        self.assertTrue(draft['body'].endswith('我的测试署名'))
        self.assertEqual(controller.store.agent_run(identity)['state'], 'complete')
        with self.assertRaises(ValueError):
            controller.make_ai_draft(1, dict(csrf='test', draft_id=identity), lambda *a: self.fail('duplicate request'))

    def test_ai_respects_revoked_cloud_consent(self):
        controller, cfg, msg = self.prepared()
        raw = json.loads(controller.path.read_text(encoding='utf-8'))
        raw['accounts'][0]['allow_cloud'] = False
        controller.path.write_text(json.dumps(raw), encoding='utf-8')
        with self.assertRaises(ValueError):
            controller.make_ai_draft(1, dict(csrf='test', draft_id=uuid.uuid4().hex), lambda *a: self.fail('revoked'))

    def test_http_draft_review_confirm_and_cross_origin(self):
        controller, cfg, msg = self.prepared()
        server = make_server(controller.store, 0, config_path=controller.path, controller=controller)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'
        with urllib.request.urlopen(base + '/messages/1/reply') as response:
            page = response.read().decode()
        csrf = re.search('name="csrf" value="([^"]+)"', page).group(1)
        identity = re.search('name="draft_id" value="([^"]+)"', page).group(1)
        def post(path, fields, origin=base):
            return urllib.request.urlopen(urllib.request.Request(base + path, data=urlencode(fields).encode(),
                headers={'Content-Type': 'application/x-www-form-urlencoded', 'Origin': origin}))
        fields = dict(csrf=csrf, draft_id=identity, revision='0', subject='Re: x', body='测试正文<script>x</script>')
        with post('/messages/1/reply', fields) as response:
            page = response.read().decode()
        self.assertIn('确认发送这封回复', page)
        self.assertNotIn('<script>', page)
        self.assertEqual(controller.store.reply(identity)['state'], 'draft')
        with self.assertRaises(urllib.error.HTTPError) as err:
            post('/replies/' + identity + '/send', dict(csrf=csrf, revision='1'), 'https://evil.example')
        self.assertEqual(err.exception.code, 403)
        with patch('mailpilot.reply.smtp_send', return_value=('accepted', '')) as sender:
            with post('/replies/' + identity + '/send', dict(csrf=csrf, revision='1')) as response:
                self.assertIn('邮件服务器已接受', response.read().decode())
            with self.assertRaises(urllib.error.HTTPError):
                post('/replies/' + identity + '/send', dict(csrf=csrf, revision='1'))
            self.assertEqual(sender.call_count, 1)
