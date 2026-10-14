"""Workbench regression coverage: retrieval, consent, atomic edits and HTTP."""
import hashlib
import json
import re
import threading
import urllib.error
import urllib.request
from urllib.parse import urlencode
from pathlib import Path
from unittest.mock import patch

from mailpilot.config import example, load
from mailpilot.runtime import process_pending
from mailpilot.services import Summarizer, validate_result
from mailpilot.settings import Settings
from mailpilot.web import dashboard, make_server
from test_mailpilot import ACCOUNT, CFG, LLM, RESULT, BaseCase, message, raw_mail, model_transport


class WorkbenchTests(BaseCase):
    def seed(self, count=61):
        for uid in range(1, count + 1):
            self.store.ingest(message(uid, raw=raw_mail(subject=f"合同 {uid}")))

    def test_search_reaches_beyond_old_recent_fifty_and_paginates(self):
        self.seed()
        page = self.store.browse(page=2)
        self.assertEqual(page['total'], 61)
        self.assertEqual(page['pages'], 4)
        self.assertEqual(len(page['rows']), 20)
        self.assertEqual(page['rows'][0]['id'], 41)
        self.assertEqual(self.store.browse(query='合同 1')['total'], 11)
        self.assertEqual(self.store.browse(page=999)['page'], 4)

    def test_filters_compose_and_search_wildcards_are_literal(self):
        msg = message(raw=raw_mail(subject='报价 100%_确认'))
        msg['attachments'] = ['报价.pdf']
        self.store.ingest(msg)
        self.store.complete(1, validate_result(RESULT), 'test', 'test', False)
        self.store.ingest(message(2, account=dict(ACCOUNT, id='gmail')))
        self.assertEqual(self.store.browse(account='qq', view='action')['total'], 1)
        self.assertEqual(self.store.browse(view='attachments')['total'], 1)
        self.assertEqual(self.store.browse(query='%_')['total'], 1)
        self.assertEqual(self.store.browse(query="' OR 1=1 --")['total'], 0)
        self.assertEqual(self.store.browse(query='12,800')['total'], 1)

    def test_attention_includes_fallback_and_ambiguous_delivery(self):
        self.store.ingest(message())
        self.store.complete(1, dict(RESULT, mode='fallback'), 'test', 'test', True)
        self.assertEqual(self.store.browse(view='attention')['total'], 1)
        task = self.store.next_push()
        self.store.push_result(task, 'unknown')
        self.assertEqual(self.store.browse(view='attention')['total'], 1)

    def test_filter_values_and_subject_are_escaped(self):
        markup = dashboard(self.store, filters={'q': '\"><script>bad()</script>', 'page': 'oops'})
        self.assertNotIn('<script>', markup)
        self.assertIn('&lt;script&gt;', markup)

    def test_queued_mail_respects_new_opt_out_after_restart(self):
        self.store.ingest(message())
        cfg = dict(CFG, accounts=[dict(ACCOUNT, allow_cloud=False)])
        def never(*args):
            self.fail('revoked consent must not make an external request')
        process_pending(cfg, self.store, Summarizer(LLM, self.store, never))
        result = json.loads(self.store.detail(1)['result'])
        self.assertEqual(result['mode'], 'fallback')
        self.assertEqual(self.store.status()['model']['attempts'], 0)

    def test_later_opt_in_does_not_upload_previously_unconsented_mail(self):
        self.store.ingest(message(account=dict(ACCOUNT, allow_cloud=False)))
        process_pending(dict(CFG, accounts=[ACCOUNT]), self.store,
                        Summarizer(LLM, self.store, lambda *a: self.fail('retroactive upload')))
        self.assertEqual(self.store.status()['model']['attempts'], 0)

    def test_stopping_does_not_start_next_message_in_batch(self):
        self.seed(3)
        stop = threading.Event()
        class StopAfterFirst:
            def summarize(self, msg):
                stop.set()
                return validate_result(RESULT)
        self.assertEqual(process_pending(CFG, self.store, StopAfterFirst(), stop), 1)
        self.assertEqual(len(self.store.pending()), 2)

    def test_style_changes_actual_model_instructions(self):
        prompts = []
        def inspect(url, payload, headers):
            prompts.append(payload['messages'][0]['content'])
            return model_transport(url, payload, headers)
        for style in ('brief', 'balanced', 'detailed'):
            Summarizer(dict(LLM, summary_style=style), self.store, inspect).summarize(message())
        self.assertEqual(len(set(prompts)), 3)
        self.assertIn('80字', prompts[0])
        self.assertIn('300字', prompts[2])


class SettingsTests(BaseCase):
    def setUp(self):
        super().setUp()
        self.path = Path(self.temp.name) / 'config.json'
        cfg = example()
        cfg['accounts'] = [dict(ACCOUNT)]
        cfg['channel']['uid'] = 'UID_test'
        cfg['custom_extension'] = {'keep': True}
        self.path.write_text(json.dumps(cfg, ensure_ascii=False), encoding='utf-8')
        self.settings = Settings(self.path)

    def fields(self):
        return {'revision': self.settings.read()[1], 'label_qq': '新的备注', 'cloud_qq': 'on',
                'llm_enabled': 'on', 'poll_seconds': '90', 'daily_attempt_limit': '30', 'summary_style': 'brief'}

    def test_atomic_save_preserves_other_config_and_references(self):
        before = json.loads(self.path.read_text(encoding='utf-8'))
        self.settings.save(self.fields())
        after = load(self.path)
        self.assertEqual(after['accounts'][0]['label'], '新的备注')
        self.assertEqual(after['accounts'][0]['secret_env'], ACCOUNT['secret_env'])
        self.assertEqual(after['llm']['summary_style'], 'brief')
        self.assertEqual(after['llm']['base_url'], before['llm']['base_url'])
        self.assertEqual(after['custom_extension'], {'keep': True})
        self.assertEqual(list(Path(self.temp.name).glob('.settings-*.json')), [])

    def test_invalid_save_does_not_damage_config_or_leave_temp_file(self):
        original = self.path.read_bytes()
        for changes in ({'poll_seconds': '0'}, {'summary_style': 'invalid'}, {'label_qq': ''},
                        {'secret_env': 'fake-secret'}, {'daily_attempt_limit': '0'}):
            with self.assertRaises(ValueError):
                self.settings.save(dict(self.fields(), **changes))
            self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(Path(self.temp.name).glob('.settings-*.json')), [])

    def test_stale_form_cannot_overwrite_new_config(self):
        form = self.fields()
        self.settings.save(self.fields())
        newer = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.settings.save(form)
        self.assertEqual(self.path.read_bytes(), newer)

    def test_unchecked_boxes_disable_consent_and_model(self):
        form = self.fields()
        del form['cloud_qq']
        del form['llm_enabled']
        self.settings.save(form)
        cfg = load(self.path)
        self.assertFalse(cfg['accounts'][0]['allow_cloud'])
        self.assertFalse(cfg['llm']['enabled'])

    def serve(self, editable=True):
        stopped = threading.Event()
        server = make_server(self.store, 0, config_path=self.path,
                             on_settings_saved=stopped.set if editable else None)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_port}', stopped

    def form_from_page(self, url):
        with urllib.request.urlopen(url + '/settings') as response:
            text = response.read().decode('utf-8')
        token = re.search(r'name="csrf" value="([^"]+)"', text).group(1)
        revision = re.search(r'name="revision" value="([^"]+)"', text).group(1)
        return dict(self.fields(), csrf=token, revision=revision)

    def request(self, url, fields, origin=None):
        return urllib.request.Request(url + '/settings', data=urlencode(fields).encode(),
                                      headers={'Content-Type': 'application/x-www-form-urlencoded',
                                               'Origin': origin or url})

    def test_http_valid_form_saves_and_requests_worker_stop(self):
        url, stopped = self.serve()
        with urllib.request.urlopen(self.request(url, self.form_from_page(url))) as response:
            self.assertIn('设置已保存', response.read().decode('utf-8'))
        self.assertTrue(stopped.is_set())
        self.assertEqual(load(self.path)['accounts'][0]['label'], '新的备注')

    def test_http_rejects_foreign_origin_and_invalid_csrf_without_writes(self):
        url, stopped = self.serve()
        form = self.form_from_page(url)
        original = self.path.read_bytes()
        for request in (self.request(url, form, 'https://evil.example'),
                        self.request(url, dict(form, csrf='wrong'))):
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 403)
        self.assertFalse(stopped.is_set())
        self.assertEqual(self.path.read_bytes(), original)

    def test_browse_only_server_cannot_edit(self):
        url, stopped = self.serve(False)
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.request(url, self.form_from_page(url)))
        self.assertEqual(error.exception.code, 403)
        self.assertFalse(stopped.is_set())
