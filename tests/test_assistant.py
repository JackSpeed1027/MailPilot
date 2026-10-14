"""Offline evidence, scope, budget and browser protection regressions."""
import json
import re
import threading
import time
import uuid
import urllib.request
import urllib.error
from urllib.parse import urlencode
from unittest.mock import patch
from mailpilot.assistant import MailAssistant
from mailpilot.config import example, load
from mailpilot.web import make_server
from test_mailpilot import BaseCase, ACCOUNT, LLM, message, raw_mail, RESULT
import test_reading_ui as configuration_tests


def call_response(name, args, identity='call'):
    return {'choices': [{'finish_reason': 'tool_calls', 'message': {'tool_calls': [
        {'id': identity, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}}]}


def final_response(**changes):
    result = dict(overview='需要确认会议。', items=[dict(message_id=1, finding='周五需要确认会议。', evidence="Please confirm Friday's meeting.")])
    result.update(changes)
    return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(result)}}]}


def sequence(final=None):
    return iter([call_response('search_mail', dict(query='', action_only=False, limit=10), 'search'),
                 call_response('read_mail', dict(message_id=1), 'read'), final or final_response()])


class AssistantTests(BaseCase):
    def agent(self, transport, **scope):
        cfg = example(); cfg['accounts'] = [dict(ACCOUNT)]; cfg['llm'] = dict(LLM, daily_attempt_limit=10)
        return MailAssistant(cfg, self.store, 'fake-key', transport=transport, **scope)

    def seed(self):
        self.store.ingest(message())
        self.store.complete(1, RESULT, 'offline', 'offline', False)

    def test_search_read_answer_with_verified_citation(self):
        self.seed(); responses = sequence(); payloads = []
        def transport(url, payload, headers):
            payloads.append(json.dumps(payload)); return next(responses)
        agent = self.agent(transport)
        answer = agent.answer('最近有什么待办？')
        self.assertEqual(answer['items'][0]['message_id'], 1)
        self.assertFalse(answer['items'][0]['partial'])
        self.assertEqual(self.store.status()['model']['attempts'], 3)
        self.assertNotIn('owner@qq.com', ''.join(payloads))
        self.assertNotIn('alice@example.com', ''.join(payloads))
        self.assertNotIn('fake-key', json.dumps(agent.trace))
        self.assertEqual(agent.trace[-1]['step'], 'citation_validation')

    def test_scope_filters_consent_sensitive_account_and_time_before_model(self):
        self.seed()
        self.store.ingest(message(2, account=dict(ACCOUNT, allow_cloud=False)))
        self.store.ingest(message(3, raw=raw_mail('Your verification code is 123456.')))
        self.store.ingest(message(4, account=dict(ACCOUNT, id='other')))
        self.store.ingest(message(5))
        with self.store.connect() as db:db.execute('UPDATE messages SET created=? WHERE id=5',(time.time()-10*86400,))
        agent = self.agent(lambda *a: None, account='qq', days=7)
        result = agent.search(dict(query='',action_only=False,limit=10))
        self.assertEqual([r['message_id'] for r in result['results']],[1])
        agent.cfg['accounts'][0]['email']='different@qq.com'
        self.assertEqual(agent.store.search_assistant(agent.allowed_accounts(),agent.since),[])

    def test_keyword_body_search_literal_wildcards_and_action_scope(self):
        self.store.ingest(message(raw=raw_mail('Receipt 100%_ confirmed.', subject='Special')))
        self.store.ingest(message(2, raw=raw_mail('Plain receipt', subject='Other')))
        agent = self.agent(lambda *a:None)
        rows = agent.search(dict(query='%_',action_only=False,limit=10))
        self.assertEqual([r['message_id'] for r in rows['results']],[1])
        self.assertEqual(self.store.search_assistant([ACCOUNT],agent.since,"' OR 1=1 --"),[])
        self.assertEqual(self.store.search_assistant([ACCOUNT],agent.since,action_only=True),[])

    def test_cannot_read_guessed_mail_or_call_sending_tool(self):
        self.seed()
        for response in [call_response('read_mail',dict(message_id=1)),call_response('smtp_send',{})]:
            with self.subTest(response=response):
                with self.assertRaises(ValueError):self.agent(lambda *a: response).answer('读邮件')

    def test_invalid_types_extra_fields_and_limits_rejected(self):
        self.seed()
        args = [dict(query='',action_only=False,limit=11),dict(query='',action_only=False,limit=True),
                dict(query='',action_only=False,limit=1,account='other'),dict(query='x'*81,action_only=False,limit=1)]
        for fields in args:
            with self.subTest(fields=fields):
                with self.assertRaises(ValueError):self.agent(lambda *a:call_response('search_mail',fields)).answer('问题')

    def test_fabricated_evidence_or_unread_citation_rejected(self):
        self.seed()
        for item in [dict(message_id=1,finding='虚构',evidence='Never appeared in the email'),
                     dict(message_id=999,finding='虚构',evidence="Please confirm Friday's meeting.")]:
            with self.subTest(item=item):
                responses=sequence(final_response(items=[item]))
                with self.assertRaises(ValueError):self.agent(lambda *a:next(responses)).answer('问题')

    def test_empty_results_replace_unsupported_model_claim_with_local_message(self):
        responses=iter([call_response('search_mail',dict(query='',action_only=False,limit=10)),final_response(overview='你有100封欠款邮件')])
        answer=self.agent(lambda *a:next(responses)).answer('问题')
        self.assertEqual(answer['items'],[])
        self.assertIn('未找到',answer['overview'])
        self.assertNotIn('欠款',answer['overview'])

    def test_must_read_and_truncated_model_output_cannot_be_accepted(self):
        self.seed()
        responses=iter([call_response('search_mail',dict(query='',action_only=False,limit=10)),final_response()])
        with self.assertRaises(ValueError):self.agent(lambda *a:next(responses)).answer('问题')
        response=final_response();response['choices'][0]['finish_reason']='length'
        responses=sequence(response)
        with self.assertRaises(ValueError):self.agent(lambda *a:next(responses)).answer('问题')

    def test_revocation_after_search_prevents_further_upload(self):
        self.seed(); responses=sequence(); agent=self.agent(lambda *a:None); calls=[]
        def transport(*args):
            calls.append(1)
            response=next(responses)
            if len(calls)==2:agent.cfg['accounts'][0]['allow_cloud']=False
            return response
        agent.transport=transport
        with self.assertRaises(ValueError):agent.answer('问题')
        self.assertEqual(len(calls),2)

    def test_reading_limit_partial_flag_and_repeat_search_bound(self):
        self.store.ingest(message(raw=raw_mail('English source text. '*400)))
        agent=self.agent(lambda *a:None)
        args=dict(query='',action_only=False,limit=10)
        agent.search(args)
        value=agent.read(1)
        self.assertTrue(value['partial'])
        self.assertLessEqual(len(value['body']),6000)
        with self.assertRaises(ValueError):agent.read(1)
        with self.assertRaises(ValueError):agent.search(args)

    def test_budget_is_shared_and_request_loop_is_bounded(self):
        self.seed();responses=sequence();agent=self.agent(lambda *a:next(responses));agent.cfg['llm']['daily_attempt_limit']=1
        with self.assertRaises(ValueError):agent.answer('问题')
        self.assertEqual(self.store.status()['model']['attempts'],1)


class AssistantHTTPTests(BaseCase):
    controller=configuration_tests.ConfigurationTests.controller
    fields=configuration_tests.ConfigurationTests.fields
    def prepared(self):
        controller=self.controller()
        controller.save(dict(self.fields(controller),new_cloud='on',llm_enabled='on',model_key='fake-key'))
        with controller.guard:pass
        cfg=load(controller.path)
        controller.store.ingest(message(account=cfg['accounts'][0]))
        return controller

    def test_controller_persists_success_and_rejects_duplicate_without_second_charge(self):
        controller=self.prepared();responses=sequence();identity=uuid.uuid4().hex
        fields=dict(csrf='x',question_id=identity,question='会议待办',account='',days='7')
        controller.ask_assistant(fields,lambda *a:next(responses))
        row=controller.store.assistant_run(identity)
        self.assertEqual(row['state'],'complete')
        with self.assertRaises(ValueError):controller.ask_assistant(fields,lambda *a:self.fail('duplicate call'))
        self.assertEqual(controller.store.status()['model']['attempts'],3)

    def test_failed_model_stored_safely_and_interrupted_task_recovers(self):
        controller=self.prepared();identity=uuid.uuid4().hex
        def fail(*args):raise RuntimeError('private-provider-response fake-secret')
        controller.ask_assistant(dict(csrf='x',question_id=identity,question='问题',account='',days='7'),fail)
        row=controller.store.assistant_run(identity)
        self.assertEqual(row['state'],'failed')
        self.assertNotIn('fake-secret',row['error'])
        interrupted=uuid.uuid4().hex
        controller.store.start_assistant(interrupted,'待恢复',dict(days=7))
        controller.store.recover_replies()
        self.assertEqual(controller.store.assistant_run(interrupted)['state'],'failed')

    def test_gui_can_revoke_consent_during_slow_assistant_without_next_request(self):
        controller=self.prepared(); started=threading.Event(); release=threading.Event(); calls=[]
        identity=uuid.uuid4().hex
        def slow(*args):
            calls.append(1);started.set();release.wait(5)
            return call_response('search_mail',dict(query='',action_only=False,limit=10))
        thread=threading.Thread(target=lambda:controller.ask_assistant(dict(csrf='x',question_id=identity,question='问题',account='',days='7'),slow))
        thread.start()
        try:
            self.assertTrue(started.wait(2))
            with self.assertRaises(ValueError):
                controller.ask_assistant(dict(csrf='x',question_id=uuid.uuid4().hex,question='并发问题',account='',days='7'),slow)
            values=self.fields(controller);values['new_email']='';values['llm_enabled']='on'
            before=time.perf_counter();controller.save(values)
            self.assertLess(time.perf_counter()-before,2)
            self.assertFalse(load(controller.path)['accounts'][0]['allow_cloud'])
        finally:
            release.set();thread.join(6)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls,[1])
        self.assertEqual(controller.store.assistant_run(identity)['state'],'failed')

    def test_http_csrf_origin_nonce_script_and_redirect_result(self):
        controller=self.prepared()
        server=make_server(controller.store,0,config_path=controller.path,controller=controller)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        base=f'http://127.0.0.1:{server.server_port}'
        with urllib.request.urlopen(base+'/assistant') as response:
            page=response.read().decode();csp=response.headers['Content-Security-Policy']
        nonce=re.search('<script nonce="([^"]+)"',page).group(1)
        self.assertIn("script-src 'nonce-"+nonce+"'",csp)
        self.assertNotIn("script-src 'unsafe-inline'",csp)
        csrf=re.search('name="csrf" value="([^"]+)"',page).group(1)
        fields=dict(csrf=csrf,question_id=uuid.uuid4().hex,question='会议待办<script>x</script>',account='',days='7')
        def post(origin):return urllib.request.urlopen(urllib.request.Request(base+'/assistant',data=urlencode(fields).encode(),headers={'Origin':origin,'Content-Type':'application/x-www-form-urlencoded'}))
        with self.assertRaises(urllib.error.HTTPError) as err:post('https://evil.example')
        self.assertEqual(err.exception.code,403)
        original=controller.ask_assistant;responses=sequence()
        with patch.object(controller,'ask_assistant',side_effect=lambda value:original(value,lambda *a:next(responses))):
            with post(base) as response:
                page=response.read().decode();self.assertIn('/assistant/'+fields['question_id'],response.url)
        self.assertIn('/messages/1',page)
        self.assertNotIn('<script>x</script>',page)
        self.assertIn('&lt;script&gt;',page)
