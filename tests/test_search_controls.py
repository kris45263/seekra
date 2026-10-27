"""检索控制及普通来源显示的少量边界测试，不访问真实服务。"""
import io
import json
import unittest
from copy import deepcopy
from contextlib import redirect_stderr
from unittest.mock import Mock

import httpx
from seekra.tools import execute_tool, tool_definitions
from seekra.display import AgentDisplay


class SearchControlTests(unittest.TestCase):
    def execute(self, args, handler):
        call = {'id': 'search', 'type': 'function', 'function': {
            'name': 'tavily_search', 'arguments': json.dumps(args)}}
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return json.loads(execute_tool(call, client=client, api_key='fake'))

    def test_parameters_normalize_and_reach_provider_and_result(self):
        seen = []
        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={'results': []})
        result = self.execute({'query':'视觉', 'max_results':6,
                               'chunks_per_source':1,
                               'include_domains':[' API-DOCS.DEEPSEEK.COM ', 'api-docs.deepseek.com']}, handler)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]['max_results'], 6)
        self.assertEqual(seen[0]['chunks_per_source'], 1)
        self.assertEqual(seen[0]['include_domains'], ['api-docs.deepseek.com'])
        self.assertEqual(seen[0]['include_domains_mode'], 'restrict')
        self.assertEqual(result['search_parameters'], {k:v for k,v in seen[0].items() if k != 'query'})
        self.assertEqual(result['status'], 'empty')

    def test_defaults_and_empty_domains_do_not_restrict(self):
        for args in ({'query':'问题'}, {'query':'问题','include_domains':[]}):
            seen=[]
            def handler(request):
                seen.append(json.loads(request.content))
                return httpx.Response(200,json={'results':[]})
            self.execute(args,handler)
            self.assertEqual(seen[0]['max_results'],3)
            self.assertNotIn('include_domains',seen[0])
            self.assertNotIn('chunks_per_source',seen[0])

    def test_invalid_controls_do_not_call_network(self):
        variants = [{'max_results':v} for v in (True,0,11,2.5,'6')]
        variants += [{'chunks_per_source':v} for v in (True,0,4,1.5,'1',None)]
        variants += [{'include_domains':v} for v in ('example.com', ['https://example.com'],
                     ['example.com/a'], ['*.example.com'], ['x@example.com'], ['localhost'],
                     ['127.0.0.1'], ['-bad.example'], ['bad_.example'], [1], ['example.com']*11)]
        for extra in variants:
            with self.subTest(extra=extra):
                handler=Mock()
                result=self.execute({'query':'问题',**extra},handler)
                self.assertEqual(result['error_code'],'invalid_arguments')
                handler.assert_not_called()

    def test_schema_exposes_optional_controls(self):
        schema=tool_definitions()[0]['function']['parameters']
        self.assertEqual(schema['required'],['query'])
        self.assertEqual(schema['properties']['max_results']['maximum'],10)
        self.assertIn('include_domains',schema['properties'])
        self.assertEqual(schema['properties']['chunks_per_source']['maximum'],3)

    def test_display_sources_and_read_ranges_without_mutation(self):
        result={'status':'success','results':[{'title':'标题','url':'https://example.com/a',
                                              'content':'正文不应展开','score':0.9}]}
        saved=deepcopy(result); out=io.StringIO()
        with redirect_stderr(out):
            view=AgentDisplay()
            view.event('tool_call',tool_call={'function':{'name':'tavily_search','arguments':
                       json.dumps({'query':'搜索词','include_domains':['example.com']})}})
            view.event('tool_result',tool_name='tavily_search',result=json.dumps(result))
            view.event('tool_call',tool_call={'function':{'name':'read_page','arguments':
                       json.dumps({'url':'https://example.com/a','start':24000})}})
            view.event('tool_result',tool_name='read_page',result=json.dumps({'status':'partial',
                       'title':'标题','url':'https://example.com/a','next_start':None}))
        self.assertEqual(result,saved)
        for text in ('搜索词','example.com','标题','https://example.com/a','接着读取','这次取得的页面内容已读完'):
            self.assertIn(text,out.getvalue())
        self.assertNotIn('正文不应展开',out.getvalue())

    def test_external_metadata_cannot_emit_terminal_controls_or_secrets(self):
        out=io.StringIO()
        with redirect_stderr(out):
            view=AgentDisplay(secrets=('secret-value',))
            view.event('tool_result',tool_name='tavily_search',result=json.dumps({'status':'success','results':[
                {'title':'\x1b[2Jsecret-value\u202e\n伪状态','url':'https://example.com','content':'原文'}]}))
        self.assertNotIn('\x1b',out.getvalue())
        self.assertNotIn('\u202e',out.getvalue())
        self.assertNotIn('secret-value',out.getvalue())
        self.assertIn('已隐藏密钥',out.getvalue())
