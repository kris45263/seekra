"""通过真实循环和磁盘重载验证恢复，不调用真实服务。"""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest

import httpx

from seekra.search.agent import run_agent
from seekra.search.state import SearchSession as Session
from test_agent import call, reply


class PersistentRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(Session, 'create'), '需要持久会话入口')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = Session.create(Path(self.tmp.name))

    def ask(self, handler, question='问题', session=None, **kwargs):
        kwargs.setdefault('thinking',False)  # 本组检验公开业务消息恢复；原生续接在 test_reasoning 中验证。
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return run_agent(question, client=client, model='test', deepseek_api_key='fake-ds',
                             tavily_api_key='fake-tv', session=session or self.session, **kwargs)

    def test_completed_history_and_usage_survive_reload(self):
        self.ask(lambda r: reply('答案'))
        restored = Session.load(self.session.path)
        self.assertEqual(restored.messages, self.session.messages)
        self.assertEqual(restored.last_run['status'], 'completed')
        self.assertEqual(restored.usage_totals()['missing_calls'], 1)

    def test_resume_after_network_error_does_not_repeat_search(self):
        count = 0
        def handler(request):
            nonlocal count
            count += 1
            if count == 1:
                return reply(calls=[call('tavily_search', {'query':'查询'})])
            if request.url.host == 'api.tavily.com':
                return httpx.Response(200,json={'results':[{'title':'来源','url':'https://example.org','content':'独特证据'}]})
            raise httpx.ConnectError('断网',request=request)
        with self.assertRaises(httpx.ConnectError):
            self.ask(handler)
        restored = Session.load(self.session.path)
        self.assertEqual(restored.messages, [])
        self.assertEqual(restored.last_run['status'], 'failed')
        def resumed(request):
            self.assertNotEqual(request.url.host,'api.tavily.com')
            self.assertIn('独特证据', json.loads(request.content)['messages'][-1]['content'])
            return reply('核实后的答案')
        self.assertEqual(self.ask(resumed, question=None, session=restored, resume=True), '核实后的答案')
        self.assertEqual(Session.load(restored.path).last_run['status'], 'completed')

    def test_page_snapshot_survives_failed_run_and_resumes_without_extract(self):
        count = 0
        def handler(request):
            nonlocal count
            if request.url.host=='api.tavily.com':
                return httpx.Response(200,json={'results':[{'url':'https://example.org','raw_content':'x'*24000+'尾部'}],'failed_results':[]})
            count += 1
            if count==1:return reply(calls=[call('read_page',{'url':'https://example.org'})])
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):self.ask(handler)
        restored=Session.load(self.session.path)
        self.assertEqual(restored.pages,{})
        def resumed(request):
            self.assertNotEqual(request.url.host,'api.tavily.com')
            messages=json.loads(request.content)['messages']
            if '尾部' not in messages[-1]['content']:
                page = json.loads(messages[-1]['content'])
                return reply(calls=[call('read_document',{'document_id':page['document_id'],'start':24000},'tail')])
            return reply('尾部已读取')
        self.assertEqual(self.ask(resumed,None,restored,resume=True),'尾部已读取')
        self.assertIn('https://example.org',Session.load(restored.path).pages)

    def test_torn_tail_is_recovered_but_corrupt_committed_line_is_rejected(self):
        self.ask(lambda r:reply('完成'))
        with self.session.path.open('ab') as f:f.write(b'{"id":')
        restored=Session.load(self.session.path)
        self.assertTrue(restored.recovered_tail)
        self.ask(lambda r:reply('继续'),session=restored)
        self.assertEqual(Session.load(restored.path).messages[-1]['content'],'继续')
        with self.session.path.open('ab') as f:f.write(b'bad-record\n')
        with self.assertRaises(ValueError):Session.load(self.session.path)

    def test_unknown_tool_outcome_blocks_resume(self):
        def handler(request):
            if request.url.host=='api.tavily.com':raise KeyboardInterrupt()
            return reply(calls=[call('tavily_search',{'query':'查询'})])
        with self.assertRaises(KeyboardInterrupt):self.ask(handler)
        restored=Session.load(self.session.path)
        with self.assertRaisesRegex(RuntimeError,'结果未知'):
            self.ask(lambda r:self.fail('不能自动重复未知操作'),None,restored,resume=True)
        self.assertEqual(restored.last_run['status'],'needs_attention')

    def test_model_change_on_resume_requires_new_question(self):
        with self.assertRaises(httpx.ConnectError):
            self.ask(lambda r:(_ for _ in ()).throw(httpx.ConnectError('离线')))
        restored=Session.load(self.session.path)
        with httpx.Client() as client, self.assertRaisesRegex(ValueError,'配置'):
            run_agent(None,client=client,model='other',deepseek_api_key='d',tavily_api_key='t',session=restored,resume=True)

    def test_process_crash_after_final_response_does_not_request_model_again(self):
        from seekra.search.agent import execute_run
        from seekra.harness.tools import ToolRegistry
        directory = Path(self.tmp.name) / 'crash'
        script = '''
import os, sys
from pathlib import Path
from seekra.search.agent import execute_run
from seekra.harness.runtime import ModelResponse
from seekra.search.state import SearchSession as Session
from seekra.harness.tools import ToolRegistry
class Model:
    identity = 'test:crash'
    def generate(self, *args, **kwargs):
        return ModelResponse({'role':'assistant','content':'已落盘的最终答案'}, 'stop')
def event(name, **details):
    if name == 'model_response':
        os._exit(17)
execute_run('问题', model=Model(), registry=ToolRegistry(),
            session=Session.create(Path(sys.argv[1])), on_event=event)
'''
        process = subprocess.run([sys.executable, '-B', '-X', 'utf8', '-c', script, str(directory)],
                                 capture_output=True, timeout=15)
        self.assertEqual(process.returncode, 17, process.stderr.decode('utf-8'))
        restored = Session.load(next(directory.glob('*.jsonl')))
        self.assertEqual(restored.messages, [])
        class Model:
            identity = 'test:crash'
            def generate(self, *args, **kwargs):
                raise AssertionError('恢复已完成响应不能重新请求模型')
        result = execute_run(None, model=Model(), registry=ToolRegistry(), session=restored, resume=True)
        self.assertEqual(result.answer, '已落盘的最终答案')
        self.assertEqual(Session.load(restored.path).last_run['status'], 'completed')


if __name__=='__main__':unittest.main()
