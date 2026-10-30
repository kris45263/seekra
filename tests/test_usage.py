"""服务端 usage 可在最终正文块或独立统计块中到达。"""
import json
import unittest
import io
from contextlib import redirect_stdout, redirect_stderr
import httpx
from seekra.providers.deepseek_stream import stream_chat_completion
from seekra.providers.deepseek import DeepSeekModel
from seekra.harness.runtime import CancellationToken, RunCancelled
from test_deepseek_stream import ByteStream, event, DONE


class UsageTests(unittest.TestCase):
    def test_cli_summary_accepts_optional_and_unknown_reasoning_usage(self):
        from seekra.terminal.cli import run_question
        from seekra.search.state import SearchSession as Session
        session = Session()
        for reasoning in (None, None, 6, None):
            with self.subTest(reasoning=reasoning, prior_calls=session.usage_totals()['calls']):
                data = {'choices':[{'index':0, 'delta':{'role':'assistant', 'content':'完整回答'},
                                    'finish_reason':'stop'}],
                        'usage':{'prompt_tokens':50, 'completion_tokens':10,
                                 'completion_tokens_details':{'reasoning_tokens':reasoning}}}
                response = httpx.Response(200, headers={'content-type':'text/event-stream'},
                    content='data: '+json.dumps(data)+'\n\ndata: [DONE]\n\n')
                with httpx.Client(transport=httpx.MockTransport(lambda r:response)) as client, \
                     redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                    status = run_question('问题', client=client, session=session, verbose=False,
                        config={'DEEPSEEK_API_KEY':'fake', 'TAVILY_API_KEY':'fake', 'DEEPSEEK_MODEL':'test'})
                self.assertEqual(status, 0)
                self.assertEqual(session.last_run['status'], 'completed')
                self.assertIn('本次 token：输入 50，输出 10', err.getvalue())
                self.assertEqual(err.getvalue().count('本次 token'), 1)

    def test_partial_usage_keeps_known_input_and_does_not_invent_output(self):
        from seekra.search.state import SearchSession as Session
        from seekra.terminal.display import AgentDisplay
        s=Session();before=s.usage_totals()
        s.record('run_started',run_id='r',question='q',system='s',config={})
        s.record('model_started',run_id='r',round=1,system='s')
        s.record('model_usage',run_id='r',round=1,usage={'input_tokens':100,'output_tokens':None})
        err=io.StringIO()
        with redirect_stderr(err):
            AgentDisplay().usage_summary(before,s.usage_totals())
        self.assertIn('输入 100',err.getvalue())
        self.assertIn('输出 未取得',err.getvalue())
        self.assertNotIn('服务未提供',err.getvalue())

    def test_http_error_keeps_same_reason_in_user_output_and_records(self):
        from seekra.terminal.cli import run_question
        from seekra.search.state import SearchSession as Session
        for code in (401,429,500):
            s=Session();err=io.StringIO()
            with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(code))) as client, \
                 redirect_stdout(io.StringIO()),redirect_stderr(err):
                run_question('问题',client=client,config={'DEEPSEEK_API_KEY':'fake','TAVILY_API_KEY':'fake','DEEPSEEK_MODEL':'test'},
                             session=s,verbose=False)
            self.assertIn(str(code),s.last_run['error'])
            self.assertIn(s.last_run['error'],err.getvalue())
            if code!=401:
                self.assertNotIn('检查 .env',err.getvalue())

    def test_cli_reports_usage_once_after_all_model_rounds(self):
        from seekra.terminal.cli import run_question
        from seekra.search.state import SearchSession as Session
        from test_agent import call
        for fail in (False, True):
            count = 0
            err = io.StringIO()
            def respond(request):
                nonlocal count
                count += 1
                if count == 2:
                    self.assertNotIn('token', err.getvalue())
                    if fail:
                        raise httpx.ConnectError('断网')
                message = ({'role':'assistant','tool_calls':[{'index':0, **call('list_documents', {})}]}
                           if count == 1 else {'role':'assistant','content':'最终回答'})
                data = {'choices':[{'index':0,'delta':message,'finish_reason':'tool_calls' if count==1 else 'stop'}],
                        'usage':{'prompt_tokens':100,'completion_tokens':2}}
                return httpx.Response(200,headers={'content-type':'text/event-stream'},
                                      content='data: '+json.dumps(data)+'\n\ndata: [DONE]\n\n')
            with httpx.Client(transport=httpx.MockTransport(respond)) as client, redirect_stdout(io.StringIO()), redirect_stderr(err):
                status = run_question('问题', client=client, config={'DEEPSEEK_API_KEY':'fake','TAVILY_API_KEY':'fake','DEEPSEEK_MODEL':'test'},
                                      session=Session(),verbose=False)
            self.assertEqual(status, 1 if fail else 0)
            self.assertEqual(err.getvalue().count('本次 token'),1)
            self.assertIn('输入 100，输出 2' if fail else '输入 200，输出 4',err.getvalue())
            if fail:
                self.assertIn('统计不完整',err.getvalue())

    def test_cancel_during_stream_closes_response_and_keeps_partial_out_of_completion(self):
        cancel=CancellationToken()
        shown=[]
        stream=ByteStream([event('部分'),event('不应显示','stop'),DONE])
        with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,
                headers={'content-type':'text/event-stream'},stream=stream))) as client:
            model=DeepSeekModel(client,'test','fake')
            def show(text):
                shown.append(text)
                cancel.cancel()
            with self.assertRaises(RunCancelled):
                model.generate([{'role':'user','content':'问题'}],[],on_content=show,on_usage=lambda u:None,cancel=cancel)
        self.assertEqual(shown,['部分'])
        self.assertTrue(stream.closed)

    def test_missing_and_invalid_usage_are_not_fabricated(self):
        from seekra.providers.usage import normalize_usage
        self.assertEqual(normalize_usage({})['input_tokens'],None)
        self.assertEqual(normalize_usage({'prompt_tokens':True})['input_tokens'],None)
        self.assertEqual(normalize_usage({'prompt_tokens':100,'prompt_cache_hit_tokens':101})['cache_hit_rate'],None)

    def test_final_chunk_usage_is_preserved(self):
        data={'choices':[{'index':0,'delta':{'role':'assistant','content':'你好'},'finish_reason':'stop'}],
              'usage':{'prompt_tokens':100,'completion_tokens':2,'prompt_cache_hit_tokens':80,'total_tokens':102}}
        with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,
                headers={'content-type':'text/event-stream'},content='data: '+json.dumps(data)+'\n\ndata: [DONE]\n\n'))) as client:
            result=stream_chat_completion(client=client,model='test',api_key='fake',messages=[{'role':'user','content':'你好'}])
        self.assertEqual(result.get('usage'),data['usage'])
