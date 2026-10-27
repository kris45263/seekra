"""ChatGPT 订阅通道：协议与鉴权边界，默认不调用真实服务。"""
import importlib
import importlib.util
import json
import io
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from seekra.runtime import CancellationToken
from seekra.deepseek import StreamProtocolError
from test_deepseek_stream import ByteStream


def event(kind, **data):
    return ('data: ' + json.dumps({'type': kind, **data}) + '\n\n').encode()


class ChatGPTTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('seekra.chatgpt'), '需要 ChatGPT 订阅协议适配器')
        self.module = importlib.import_module('seekra.chatgpt')

    def test_stream_and_tools_use_subscription_contract(self):
        captured = []
        output = [{'type':'reasoning','id':'rs_1','summary':[],'encrypted_content':'opaque'},
                  {'type':'function_call','id':'fc_1','call_id':'c1','namespace':'seekra',
                   'name':'tavily_search','arguments':'{"query":"test"}','status':'completed'}]
        stream = ByteStream([event('response.completed', response={'status':'completed','model':'gpt-6.1-sol',
                            'output':output,'usage':{'input_tokens':20,'output_tokens':5}})])
        def respond(request):
            captured.append(json.loads(request.content))
            self.assertEqual(request.headers['authorization'], 'Bearer test-access')
            return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=stream)
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            model=self.module.ChatGPTModel(client,lambda:'test-access')
            result=model.generate([{'role':'system','content':'rules'},{'role':'user','content':'question'}],
                [{'type':'function','function':{'name':'tavily_search','description':'search','parameters':{'type':'object'}}}],
                on_content=lambda t:self.fail('不应展示推理或工具参数'),on_usage=lambda u:None,cancel=CancellationToken())
        payload=captured[0]
        self.assertEqual(payload['model'],'gpt-6.1-sol')
        self.assertEqual(payload['reasoning'],{'effort':'medium'})
        self.assertFalse(payload['store']);self.assertTrue(payload['stream'])
        self.assertEqual(payload['instructions'],'rules')
        self.assertEqual(payload['tools'][0]['type'],'namespace')
        for key in ('max_output_tokens','temperature','previous_response_id'):
            self.assertNotIn(key,payload)
        self.assertEqual(result.finish_reason,'tool_calls')
        self.assertEqual(result.message['tool_calls'][0]['id'],'c1')
        self.assertNotIn('opaque',json.dumps(result.message))
        self.assertEqual(result.continuation['items'][0]['encrypted_content'],'opaque')
        self.assertTrue(stream.closed)

    def test_partial_stream_is_never_a_completed_answer(self):
        for terminal in (b'', event('response.failed',response={'status':'failed','error':{
                'code':'subscription_sharing_usage_limit_exceeded','message':'secret-detail'}}),
                event('response.incomplete',response={'status':'incomplete'})):
            with self.subTest(terminal=bool(terminal)):
                stream=ByteStream([event('response.output_text.delta',delta='partial'),terminal])
                seen=[]
                with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,
                     headers={'content-type':'text/event-stream'},stream=stream))) as client:
                    with self.assertRaises(RuntimeError) as caught:
                        self.module.ChatGPTModel(client,lambda:'test-access').generate(
                            [{'role':'user','content':'q'}],[],on_content=seen.append,on_usage=lambda u:None,
                            cancel=CancellationToken())
                self.assertEqual(seen,['partial'])
                self.assertNotIn('secret-detail',str(caught.exception))
                self.assertTrue(stream.closed)

    def test_missing_content_type_accepts_only_complete_responses_events(self):
        valid=event('response.completed',response={'status':'completed','output':[{
            'type':'message','role':'assistant','content':[{'type':'output_text','text':'SEEKRA_OK'}]}]})
        for body, accepted in ((valid,True),(b'<html>Gateway page</html>',False),
                               (event('response.output_text.delta',delta='partial'),False)):
            with self.subTest(accepted=accepted):
                with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,content=body))) as client:
                    model=self.module.ChatGPTModel(client,lambda:'token')
                    run=lambda:model.generate([{'role':'user','content':'test'}],[],on_content=lambda t:None,
                                              on_usage=lambda u:None,cancel=CancellationToken())
                    if accepted:
                        self.assertEqual(run().message['content'],'SEEKRA_OK')
                    else:
                        with self.assertRaises(StreamProtocolError):run()

    def test_compact_completed_event_uses_finished_output_items(self):
        item={'type':'message','role':'assistant','status':'completed',
              'content':[{'type':'output_text','text':'SEEKRA_OK'}]}
        for index,accepted in ((0,True),(1,False)):
            with self.subTest(index=index):
                stream=ByteStream([event('response.output_text.delta',delta='SEEKRA_OK'),
                    event('response.output_item.done',output_index=index,item=item),
                    event('response.completed',response={'status':'completed','output':[]})])
                with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,stream=stream))) as client:
                    run=lambda:self.module.ChatGPTModel(client,lambda:'token').generate(
                        [{'role':'user','content':'test'}],[],on_content=lambda t:None,on_usage=lambda u:None,
                        cancel=CancellationToken())
                    if accepted:self.assertEqual(run().message['content'],'SEEKRA_OK')
                    else:
                        with self.assertRaises(StreamProtocolError):run()

    def test_completed_text_and_tool_history_are_translated_without_duplicate_output(self):
        messages=[{'role':'system','content':'rules'},{'role':'user','content':'q'},
            {'role':'assistant','content':None,'tool_calls':[{'id':'c1','type':'function',
             'function':{'name':'read_document','arguments':'{}'}}]},
            {'role':'tool','tool_call_id':'c1','content':'source'}]
        seen=[];usage=[]
        stream=ByteStream([event('response.output_text.delta',delta='answer'),
            event('response.completed',response={'status':'completed','output':[{'type':'message','role':'assistant',
                   'content':[{'type':'output_text','text':'answer'}]}],
                   'usage':{'input_tokens':9,'output_tokens':4,'input_tokens_details':{'cached_tokens':3}}})])
        def respond(request):
            data=json.loads(request.content)
            self.assertEqual(data['input'][-1],{'type':'function_call_output','call_id':'c1','output':'source'})
            self.assertEqual(data['input'][-2]['namespace'],'seekra')
            return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=stream)
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            result=self.module.ChatGPTModel(client,lambda:'token').generate(messages,[],on_content=seen.append,
                     on_usage=usage.append,cancel=CancellationToken())
        self.assertEqual(seen,['answer'])
        self.assertEqual(result.message['content'],'answer')
        self.assertEqual(usage[0]['cache_read_tokens'],3)

    def test_cli_uses_own_search_loop_without_deepseek_key(self):
        from seekra.cli import main
        requests=[]
        payloads=[]
        def respond(request):
            requests.append(request.url.host)
            if request.url.host=='api.tavily.com':
                return httpx.Response(200,json={'results':[{'url':'https://example.org/source','title':'Source','content':'Supported fact.'}]})
            data=json.loads(request.content)
            payloads.append(data)
            if len(requests)==1:
                output=[{'type':'function_call','call_id':'call_1','namespace':'seekra','name':'tavily_search',
                         'arguments':'{"query":"test"}','status':'completed'}]
            else:
                self.assertEqual(data['input'][-1]['type'],'function_call_output')
                self.assertIn('Supported fact.',data['input'][-1]['output'])
                output=[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'Final answer'}]}]
            return httpx.Response(200,headers={'content-type':'text/event-stream'},
                content=event('response.completed',response={'status':'completed','output':output,
                    'usage':{'input_tokens':20,'output_tokens':5,'output_tokens_details':{'reasoning_tokens':3}}}))
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'.env';config.write_text('TAVILY_API_KEY=fake-tavily',encoding='utf-8')
            client=httpx.Client(transport=httpx.MockTransport(respond))
            with patch('seekra.cli.httpx.Client',return_value=client), \
                 patch('seekra.chatgpt_auth.ChatGPTAuth.read',return_value={'access_token':'fake-access'}), \
                 patch('seekra.chatgpt_auth.ChatGPTAuth.access_token',return_value='fake-access'), \
                 patch('sys.argv',['seekra','--provider','chatgpt','--env-file',str(config),'--no-session','question']), \
                 redirect_stdout(io.StringIO()) as out,redirect_stderr(io.StringIO()) as err:
                self.assertEqual(main(),0)
            self.assertEqual(out.getvalue(),'Final answer\n')
            self.assertIn('本次 token：输入 40，输出 10',err.getvalue())
        self.assertEqual(requests,['api.openai.com','api.tavily.com','api.openai.com'])
        self.assertEqual(payloads[0]['instructions'],payloads[1]['instructions'])
        self.assertEqual(payloads[0]['tools'],payloads[1]['tools'])
        self.assertEqual(payloads[0]['input'],payloads[1]['input'][:len(payloads[0]['input'])])

    def test_reload_rebuilds_without_persisting_encrypted_reasoning_or_repeating_tool(self):
        from seekra.session import Session
        from seekra.chatgpt import run_chatgpt
        from seekra.chatgpt_auth import ChatGPTAuth
        calls=[]; token=CancellationToken()
        def respond(request):
            calls.append(request.url.host)
            if request.url.host=='api.tavily.com':
                return httpx.Response(200,json={'results':[]})
            payload=json.loads(request.content)
            if len(calls)==1:
                output=[{'type':'reasoning','id':'rs','encrypted_content':'PRIVATE_OPAQUE','summary':[]},
                        {'type':'function_call','call_id':'c1','namespace':'seekra','name':'tavily_search','arguments':'{"query":"test"}'}]
            else:
                self.assertIn('恢复资料',json.dumps(payload,ensure_ascii=False))
                self.assertNotIn('PRIVATE_OPAQUE',json.dumps(payload))
                output=[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'Done'}]}]
            return httpx.Response(200,headers={'content-type':'text/event-stream'},content=event('response.completed',
                        response={'status':'completed','output':output}))
        def stop(name,**details):
            if name=='tool_result':token.cancel()
        with tempfile.TemporaryDirectory() as directory:
            session=Session.create(Path(directory))
            with httpx.Client(transport=httpx.MockTransport(respond)) as client, \
                 patch.object(ChatGPTAuth,'access_token',return_value='AUTH_TOKEN'):
                auth=ChatGPTAuth(Path(directory)/'auth')
                with self.assertRaises(KeyboardInterrupt):
                    run_chatgpt('question',client=client,tavily_api_key='TAVILY_SECRET',auth=auth,
                                session=session,cancel=token,on_event=stop)
                saved=session.path.read_text(encoding='utf-8')
                for private in ('AUTH_TOKEN','PRIVATE_OPAQUE','TAVILY_SECRET'):
                    self.assertNotIn(private,saved)
                restored=Session.load(session.path)
                answer=run_chatgpt(None,client=client,tavily_api_key='TAVILY_SECRET',auth=auth,session=restored,resume=True)
                self.assertEqual(answer,'Done')
                self.assertEqual(calls.count('api.tavily.com'),1)
