"""基础验收脚本按已提交事实生成摘要；模型和展示故障均在本地模拟。"""
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from scripts import run_live_case
from test_agent import call, reply


class LiveCaseTests(unittest.TestCase):
    def test_trace_keeps_committed_tool_result_when_interrupted_before_next_request(self):
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / 'result.json'
                requests = []

                def respond(request):
                    self.assertEqual(request.url.host, 'api.deepseek.com')
                    requests.append(json.loads(request.content))
                    if len(requests) == 1:
                        return reply(calls=[call('list_documents', {}, 'documents')])
                    return reply('完成')

                def event(name, **details):
                    if interrupted and name == 'tool_result':
                        raise KeyboardInterrupt()

                client_factory = httpx.Client
                def client(**kwargs):
                    return client_factory(transport=httpx.MockTransport(respond), **kwargs)

                with patch.object(run_live_case, 'read_config', return_value={
                        'DEEPSEEK_API_KEY':'fake-ds', 'DEEPSEEK_MODEL':'test', 'TAVILY_API_KEY':'fake-tv'}), \
                     patch.object(run_live_case.httpx, 'Client', side_effect=client), \
                     patch.object(run_live_case.AgentDisplay, 'event', side_effect=event), \
                     patch('sys.argv', ['run_live_case.py', '--case', 'simple', '--output', str(output)]), \
                     redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    status = run_live_case.main()
                result = json.loads(output.read_text(encoding='utf-8'))
                self.assertEqual(status, 1 if interrupted else 0)
                self.assertEqual(result['error'], 'KeyboardInterrupt' if interrupted else None)
                self.assertEqual(result['model_calls'], 1 if interrupted else 2)
                self.assertEqual(len(result['trace']), 2)
                self.assertEqual(result['trace'][0]['name'], 'list_documents')
                self.assertEqual(result['trace'][1]['call_id'], 'documents')
                self.assertEqual(result['trace'][1]['operation'], 'list_documents')
                self.assertEqual(result['trace'][1]['status'], 'success')
