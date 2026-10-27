"""正文验收的请求捕获在本地处理研究和独立摘要，不访问真实服务。"""
import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from scripts import check_documents
from seekra.session import Session


class CheckDocumentsTests(unittest.TestCase):
    def selected_request(self):
        session = Session()
        session.record('run_started', run_id='r', question='问题', system='research', config={})
        messages = session.last_run['messages']
        encoded = json.dumps({'messages': messages, 'tools': []}, ensure_ascii=False)
        session.record('context_selected', run_id='r', message_indices=[0, 1],
                       request_sha256=hashlib.sha256(encoded.encode()).hexdigest())
        session.record('model_started', run_id='r', round=1, system='research')
        request = httpx.Request('POST', 'https://api.deepseek.com/chat/completions',
                                json={'model': 'test', 'messages': messages})
        return session, request

    def test_model_without_tools_is_still_checked_as_research(self):
        session, request = self.selected_request()
        row = check_documents.capture_deepseek_request(request, session)
        self.assertEqual(row['purpose'], 'model')
        self.assertEqual(row['payload']['messages'], session.last_run['_request_messages'])

    def test_compaction_after_research_does_not_reuse_previous_selection(self):
        session, request = self.selected_request()
        check_documents.capture_deepseek_request(request, session)
        session.record('model_completed', run_id='r', message={'role': 'assistant', 'content': 'history'})
        self.assertNotIn('_request_messages', session.last_run)
        session.record('compaction_started', run_id='r', boundary=2, input_characters=10, model='test')
        session.record('observation', run_id='r', content='unrelated event')
        request = httpx.Request('POST', 'https://api.deepseek.com/chat/completions', json={
            'model': 'test', 'tools': [], 'messages': [{'role': 'user', 'content': 'summarize history'}]})
        row = check_documents.capture_deepseek_request(request, session)
        self.assertEqual(row['purpose'], 'compaction')
        self.assertEqual(row['payload']['messages'][0]['content'], 'summarize history')
        self.assertEqual(row['request_sha256'], hashlib.sha256(request.content).hexdigest())

    def test_model_still_rejects_hash_or_public_projection_mismatch(self):
        for mismatch in ('hash', 'public_projection'):
            with self.subTest(mismatch=mismatch):
                session, request = self.selected_request()
                payload = json.loads(request.content)
                payload['messages'][1]['content'] = 'unselected question'
                if mismatch == 'public_projection':
                    # 使哈希匹配更改后的请求，单独确认公开消息断言仍然生效。
                    encoded = json.dumps({'messages': payload['messages'], 'tools': []}, ensure_ascii=False)
                    selection = next(r for r in session.records if r['kind'] == 'context_selected')
                    selection['request_sha256'] = hashlib.sha256(encoded.encode()).hexdigest()
                changed = httpx.Request('POST', str(request.url), json=payload)
                with self.assertRaises(AssertionError):
                    check_documents.capture_deepseek_request(changed, session)

    def test_interceptor_captures_compaction_before_research_without_selection(self):
        sent = []

        def respond(request):
            sent.append(request)
            return httpx.Response(200, json={'ok': True})

        client_factory = httpx.Client

        def client(**kwargs):
            kwargs.setdefault('transport', httpx.MockTransport(respond))
            return client_factory(**kwargs)

        def run(question, **kwargs):
            session = kwargs['session']
            session.record('run_started', run_id='r', question=question, system='research', config={})
            session.record('compaction_started', run_id='r', boundary=2, input_characters=10, model='test')
            kwargs['client'].post('https://api.deepseek.com/chat/completions', json={
                'model': 'test', 'messages': [
                    {'role': 'system', 'content': 'summarize'},
                    {'role': 'user', 'content': 'history', 'reasoning_content': 'private summary'}]})
            messages = session.last_run['messages']
            tools = [{'type': 'function', 'function': {'name': 'test'}}]
            encoded = json.dumps({'messages': messages, 'tools': tools}, ensure_ascii=False)
            session.record('context_selected', run_id='r', message_indices=[0, 1],
                           request_sha256=hashlib.sha256(encoded.encode()).hexdigest())
            session.record('model_started', run_id='r', round=1, system='research')
            kwargs['client'].post('https://api.deepseek.com/chat/completions', json={
                'model': 'test', 'messages': messages, 'tools': tools})
            session.record('run_ended', run_id='r', status='completed')
            return '完成'

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'result.json'
            with patch.object(check_documents, 'read_config', return_value={
                    'DEEPSEEK_API_KEY': 'fake-ds', 'DEEPSEEK_MODEL': 'test', 'TAVILY_API_KEY': 'fake-tv'}), \
                 patch.object(check_documents, 'run_agent', side_effect=run), \
                 patch.object(check_documents.httpx, 'Client', side_effect=client), \
                 redirect_stdout(io.StringIO()):
                status = check_documents.run_case('long-section', output)
            text = output.read_text(encoding='utf-8')
            result = json.loads(text)
        self.assertEqual(status, 0, result['error'])
        self.assertEqual(result['answer'], '完成')
        self.assertEqual([row['purpose'] for row in result['requests']], ['compaction', 'model'])
        self.assertEqual(len(sent), 2)
        for row, request in zip(result['requests'], sent):
            self.assertEqual(row['request_sha256'], hashlib.sha256(request.content).hexdigest())
            self.assertEqual(row['request_characters'], len(request.content.decode()))
        summary = result['requests'][0]
        self.assertEqual(summary['payload']['messages'][1]['content'], 'history')
        self.assertEqual(summary['message_count'], 2)
        self.assertEqual(summary['reasoning_characters_sent'], len('private summary'))
        self.assertNotIn('private summary', text)
        self.assertNotIn('reasoning_content', text)
