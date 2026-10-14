"""主循环流式工具边界：完整结束后执行，断流不提交。"""
import json
import unittest
from copy import deepcopy
from unittest.mock import patch
import httpx
from seekra.agent import run_agent
from seekra.session import Session
from test_deepseek_stream import ByteStream, event, DONE


class ToolStreamTests(unittest.TestCase):
    def test_interleaved_calls_wait_for_done_and_preserve_history(self):
        seen, shown = [], []
        def tc(index, **function):
            return {"index": index, "function": function}
        def handler(request):
            body = json.loads(request.content)
            self.assertTrue(body['stream'])
            if len(body['messages']) == 2:
                def checkpoint():
                    self.assertEqual(seen, [])
                    self.assertEqual(shown, ['先整理。'])
                return httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=ByteStream([
                    event('先整理。'),
                    event(tool_calls=[dict(tc(0, name='tavily_search', arguments='{"query":"'), id='a', type='function'),
                                      dict(tc(1, name='tavily_search', arguments='{"query":"'), id='b', type='function')]),
                    event(tool_calls=[tc(1, arguments='乙"}'), tc(0, arguments='甲"}')]),
                    event(finish='tool_calls'), checkpoint, DONE]))
            self.assertEqual(seen, ['a', 'b'])
            self.assertEqual([m['role'] for m in body['messages']], ['system','user','assistant','tool','tool'])
            self.assertEqual(body['messages'][2]['tool_calls'][0]['function']['arguments'], '{"query":"甲"}')
            return httpx.Response(200, headers={'content-type':'text/event-stream'}, content=event('回答', 'stop')+DONE)
        def execute(call, **kwargs):
            seen.append(call['id'])
            return '{}'
        with httpx.Client(transport=httpx.MockTransport(handler)) as client, patch('seekra.tools.execute_tool', side_effect=execute):
            answer = run_agent('问题', client=client, model='test', deepseek_api_key='x', tavily_api_key='y', on_content=shown.append)
        self.assertEqual(answer, '回答')
        self.assertEqual(shown, ['先整理。','回答'])

    def test_missing_done_never_executes_or_commits(self):
        session = Session(messages=[{'role':'system','content':'old'}, {'role':'user','content':'旧问'}, {'role':'assistant','content':'旧答'}])
        before = deepcopy(session)
        stream = ByteStream([event(tool_calls=[{'index':0,'id':'a','type':'function','function':{'name':'tavily_search','arguments':'{"query":"查询"}'}}]), event(finish='tool_calls')])
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=stream))) as client, patch('seekra.tools.execute_tool') as execute:
            with self.assertRaises(RuntimeError):
                run_agent('新问', client=client, model='test', deepseek_api_key='x', tavily_api_key='y', session=session)
            execute.assert_not_called()
        self.assertEqual(session.messages, before.messages)
        self.assertEqual(session.pages, before.pages)
        self.assertTrue(stream.closed)
