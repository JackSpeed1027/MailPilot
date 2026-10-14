"""Local extraction limits and proof that previews never enter model contexts."""
import json
import uuid
from pathlib import Path
from email.message import EmailMessage
from unittest.mock import patch
from mailpilot.attachments import MAX_BYTES, MAX_TOTAL_CHARS, preview
from mailpilot.mail import parse
from mailpilot.config import example, load
from mailpilot.web import detail_page
from mailpilot.agent import ReplyAgent
from mailpilot.assistant import MailAssistant
from mailpilot.services import Summarizer
from test_mailpilot import BaseCase, ACCOUNT, LLM, RESULT, model_transport
import test_reading_ui as configurations
from test_reply_agent import tool_response, draft_response


def raw_with_files(files, body='Please confirm Friday meeting.'):
    msg=EmailMessage();msg['From']='sender@example.com';msg['To']='owner@qq.com';msg['Subject']='附件测试'
    msg.set_content(body)
    for name,data,kind in files:
        main,sub=kind.split('/')
        msg.add_attachment(data,maintype=main,subtype=sub,filename=name)
    return msg.as_bytes()


class AttachmentTests(BaseCase):
    def parse(self, files, enabled=True, body='Please confirm Friday meeting.'):
        return parse(raw_with_files(files,body),dict(ACCOUNT,attachment_preview=enabled),'1',1)

    def test_disabled_does_not_decode_attachment_payload(self):
        part=EmailMessage();part.add_header('Content-Disposition','attachment',filename='secret.txt')
        with patch.object(part,'get_payload',side_effect=AssertionError('must not decode')):
            self.assertEqual(preview(part,False)['state'],'disabled')
        msg=self.parse([('file.txt',b'PRIVATE_ATTACHMENT','text/plain')],False)
        self.assertEqual(msg['attachment_previews'][0]['text'],'')
        self.assertNotIn('PRIVATE_ATTACHMENT',json.dumps(msg))

    def test_utf8_csv_markdown_gb18030_and_utf16(self):
        for name,data in [('a.txt','预算12800元'.encode()),('a.csv',b'name,amount\nquote,12800'),
                          ('a.md',b'# Project\nConfirm Friday'),('a.txt','报价确认'.encode('gb18030')),
                          ('a.txt','请确认会议'.encode('utf-16'))]:
            with self.subTest(name=name,data=data):
                value=self.parse([(name,data,'application/octet-stream')])['attachment_previews'][0]
                self.assertEqual(value['state'],'ready');self.assertTrue(value['text'])

    def test_binary_pdf_html_and_images_are_not_executed_or_parsed(self):
        for name,data,kind in [('a.pdf',b'%PDF-invalid','application/pdf'),('a.png',b'\x89PNG','image/png'),
                               ('a.txt',b'<script>x</script>','text/html')]:
            with self.subTest(name=name):
                self.assertEqual(self.parse([(name,data,kind)])['attachment_previews'][0]['state'],'unsupported')
        self.assertEqual(self.parse([('a.txt',b'\x00\x01\x02','text/plain')])['attachment_previews'][0]['state'],'unreadable')

    def test_byte_character_file_and_total_limits_are_explicit(self):
        self.assertEqual(self.parse([('a.txt',b'x'*(MAX_BYTES+1),'text/plain')])['attachment_previews'][0]['state'],'too_large')
        msg=self.parse([(f'{i}.txt',b'A'*15000,'text/plain') for i in range(12)])
        self.assertEqual(len(msg['attachment_previews']),10)
        self.assertTrue(msg['attachment_previews_limited'])
        self.assertLessEqual(sum(v['chars'] for v in msg['attachment_previews']),MAX_TOTAL_CHARS)
        self.assertEqual(msg['attachment_previews'][0]['state'],'partial')
        self.assertEqual(msg['attachment_previews'][3]['state'],'limit')

    def test_sensitive_attachment_and_sensitive_mail_never_persist_text(self):
        msg=self.parse([('a.txt',b'Your verification code is 123456.','text/plain')])
        self.assertEqual(msg['attachment_previews'][0]['state'],'protected')
        self.assertNotIn('123456',json.dumps(msg))
        with patch('mailpilot.attachments.preview',side_effect=AssertionError('protected mail must skip attachments')):
            msg=self.parse([('a.txt',b'PRIVATE_ATTACHMENT','text/plain')],body='Your verification code is 123456.')
        self.assertEqual(msg['attachment_previews'],[])

    def test_paths_are_only_names_and_preview_html_is_escaped(self):
        filename='escape-'+uuid.uuid4().hex+'.txt'
        msg=self.parse([('../'+filename,b'<script>x</script>\nHello&nbsp;world','text/plain')])
        self.store.ingest(msg)
        page=detail_page(self.store.detail(1))
        self.assertNotIn('<script>',page);self.assertIn('&lt;script&gt;',page)
        self.assertIn('Hello world',page)
        self.assertIn('../'+filename,page)
        self.assertFalse((Path(self.temp.name).parent/filename).exists())

    def test_summarizer_reply_and_search_agent_never_upload_preview(self):
        msg=self.parse([('a.txt',b'PRIVATE_ATTACHMENT_987','text/plain')])
        seen=[]
        def transport(url,payload,headers):
            seen.append(json.dumps(payload));return model_transport(url,payload,headers)
        Summarizer(dict(LLM,translation_enabled=False),self.store,transport).summarize(msg)
        responses=iter([tool_response(),draft_response()])
        def reply(url,payload,headers):seen.append(json.dumps(payload));return next(responses)
        ReplyAgent(LLM,self.store,'fake',reply).draft(msg,{})
        self.store.ingest(msg)
        cfg=example();cfg['accounts']=[ACCOUNT];cfg['llm']=LLM
        agent=MailAssistant(cfg,self.store,'fake')
        seen.append(json.dumps(agent.search(dict(query='',action_only=False,limit=10))))
        seen.append(json.dumps(agent.read(1)))
        self.assertNotIn('PRIVATE_ATTACHMENT_987',''.join(seen))


class AttachmentConfigurationTests(BaseCase):
    controller=configurations.ConfigurationTests.controller
    fields=configurations.ConfigurationTests.fields

    def test_default_off_gui_opt_in_and_opt_out_preserve_existing_local_preview(self):
        controller=self.controller();controller.save(self.fields(controller))
        cfg=load(controller.path);account=cfg['accounts'][0]
        self.assertFalse(account['attachment_preview'])
        fields=self.fields(controller);fields['new_email']='';fields['attachment_'+account['id']]='on'
        controller.save(fields)
        account=load(controller.path)['accounts'][0];self.assertTrue(account['attachment_preview'])
        msg=parse(raw_with_files([('a.txt',b'LOCAL_PREVIEW','text/plain')]),account,'1',1)
        controller.store.ingest(msg)
        fields=self.fields(controller);fields['new_email']='';controller.save(fields)
        self.assertFalse(load(controller.path)['accounts'][0]['attachment_preview'])
        saved=json.loads(controller.store.detail(1)['payload'])
        self.assertEqual(saved['attachment_previews'][0]['text'],'LOCAL_PREVIEW')

    def test_invalid_flag_rejected_without_config_write(self):
        controller=self.controller();fields=self.fields(controller);fields['new_attachment_preview']='not-bool'
        with self.assertRaises(ValueError):controller.save(fields)
        self.assertFalse(controller.path.exists())
