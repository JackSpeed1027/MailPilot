"""Navigation continuity and recoverable configuration errors."""
import json
from html.parser import HTMLParser
from urllib.parse import urlparse, parse_qs
from mailpilot.config import example
from mailpilot.credentials import Credentials
from mailpilot.web import dashboard, detail_page
from mailpilot.ui import configuration_page
from test_mailpilot import BaseCase, message, RESULT


class Links(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.hrefs = []
        self.feed(markup)
    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.hrefs.append(dict(attrs).get('href', ''))


class ExperienceTests(BaseCase):
    def test_filtered_list_reading_and_return_preserve_context(self):
        self.store.ingest(message())
        self.store.complete(1, RESULT, 'offline', 'offline', False)
        filters = dict(q='Meeting', account='qq', view='action', page='1')
        markup = dashboard(self.store, filters=filters)
        href = next(h for h in Links(markup).hrefs if h.startswith('/messages/1?'))
        params = {k: v[0] for k, v in parse_qs(urlparse(href).query).items()}
        self.assertEqual(params, filters)
        reading = detail_page(self.store.detail(1), filters=params)
        returns = [h for h in Links(reading).hrefs if h.startswith('/?') and 'q=' in h]
        self.assertEqual(parse_qs(urlparse(returns[0]).query), {k:[v] for k,v in filters.items()})
        unsafe = detail_page(self.store.detail(1), filters={'return': 'https://evil.example', 'q': '"><script>x</script>'})
        self.assertNotIn('https://evil.example', unsafe)
        self.assertNotIn('<script>', unsafe)

    def test_failed_settings_preserve_choices_without_echoing_secrets(self):
        cfg = example()
        cfg['accounts'] = []
        class Controller:
            status = '离线测试'
            def read(self):return cfg, 'revision'
        controller = Controller()
        controller.store = self.store
        controller.credentials = Credentials(self.temp.name, None)
        fields = dict(new_label='临时备注 <script>', new_email='test@example.com', new_provider='gmail',
                      model='changed-model', model_key='never-echo-this-secret', new_secret='never-echo-mail-secret',
                      reply_signature='未保存的署名', reply_tone='friendly', llm_enabled='on')
        page = configuration_page(controller, 'csrf', '未保存：检查间隔', fields)
        self.assertIn('value="临时备注 &lt;script&gt;"', page)
        self.assertIn('value="changed-model"', page)
        self.assertIn('value="未保存的署名"', page)
        self.assertIn('value="gmail" selected', page)
        self.assertIn('value="friendly" selected', page)
        self.assertNotIn('never-echo', page)
        self.assertNotIn('<script>', page)
        self.assertIn('<details open ', page)
