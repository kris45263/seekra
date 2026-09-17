"""上下文、取消、用量和会话锁的实际接口测试。"""
from copy import deepcopy
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from seekra.context import build_context
from seekra.runtime import CancellationToken, ModelResponse
from seekra.agent import execute_run
from seekra.session import Session
from seekra.tools import ToolRegistry
from seekra.display import AgentDisplay


class RuntimeEdgeTests(unittest.TestCase):
    def test_unsupported_session_version_is_not_reported_as_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'abcd1234.jsonl'
            path.write_text(json.dumps({'id':'1','kind':'session_created','session_id':'abcd1234','version':2})+'\n',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'版本') as raised:
                Session.load(path)
            self.assertNotIn('损坏',str(raised.exception))
            self.assertEqual(Session.list_saved(Path(directory))[0]['status'],'unsupported')

    def test_recovery_hint_uses_saved_answer_and_actual_limit_reason(self):
        from seekra.cli import continuation_hint
        class Model:
            identity='test:hint'
            def generate(self,*args,**kwargs):
                return ModelResponse({'role':'assistant','content':'完整答案'},'stop')
        def stop(name,**kwargs):
            if name=='model_response':
                raise RuntimeError('完成标记前中断')
        session=Session()
        execute_run('问题',model=Model(),registry=ToolRegistry(),session=session,max_iterations=1,on_event=stop)
        self.assertNotIn('上限',continuation_hint(session,1))
        self.assertIn('/continue',continuation_hint(session,1))
        session=Session()
        session.record('run_started',run_id='r',question='问题',system='规则',config={})
        session.record('model_started',run_id='r',round=1,system='规则')
        session.record('run_ended',run_id='r',status='paused',error_code='iteration_limit',error='达到次数上限')
        self.assertNotIn('上下文',continuation_hint(session,2))
        self.assertIn('/continue',continuation_hint(session,2))

    def test_committed_model_text_is_not_saved_again_as_a_partial_answer(self):
        from seekra.display import session_transcript
        class Model:
            identity='test:partial'
            def generate(self,messages,tools,**callbacks):
                callbacks['on_content']('准备查找')
                return ModelResponse({'role':'assistant','content':'准备查找','tool_calls':[
                    {'id':'c','type':'function','function':{'name':'probe','arguments':'{}'}}]},'tool_calls')
        registry=ToolRegistry()
        def broken(*args):
            raise RuntimeError('工具中断')
        registry.register({'type':'function','function':{'name':'probe','parameters':{'type':'object'}}},broken)
        session=Session()
        execute_run('问题',model=Model(),registry=registry,session=session)
        self.assertEqual(session.records[-1].get('partial_content'), '')
        self.assertEqual(session_transcript(session).count('准备查找'),1)

    def test_completed_runs_do_not_retain_copies_of_all_prior_messages(self):
        class Model:
            identity = 'test:memory-growth'
            def generate(self, *args, **kwargs):
                return ModelResponse({'role':'assistant','content':'答案'}, 'stop')
        session = Session()
        for _ in range(50):
            execute_run('问题', model=Model(), registry=ToolRegistry(), session=session)
        retained = sum(len(run.get('messages', [])) for run in session.runs.values())
        self.assertLessEqual(retained, len(session.messages))
        self.assertEqual(len(session.runs), 50)

    def test_redaction_does_not_corrupt_numeric_fields_or_entry_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            session.secrets = ('1',)
            session.record('observation', content='secret 1', count=1)
            restored = Session.load(session.path)
            self.assertEqual(restored.records[-1]['count'], 1)
            self.assertEqual(restored.records[-1]['id'], '2')
            self.assertIn('已隐藏密钥', restored.records[-1]['content'])

    def test_unknown_record_type_is_rejected_on_load(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            with session.path.open('a', encoding='utf-8') as file:
                file.write('{"id":"2","kind":"unknown_kind"}\n')
            with self.assertRaises(ValueError):
                Session.load(session.path)

    def test_offloaded_tool_body_can_be_read_without_mutating_source(self):
        from seekra.tools import default_registry
        import httpx
        s=Session()
        original=json.dumps({'operation':'read_page','status':'partial','content':'z'*8000},ensure_ascii=False)
        s.records.append({'id':'1','kind':'tool_completed','call_id':'page','result':original})
        messages=[{'role':'system','content':'规则'},{'role':'user','content':'旧问题'},
                  {'role':'assistant','content':'旧回答'},{'role':'user','content':'当前问题'},
                  {'role':'assistant','content':None,'tool_calls':[
                      {'id':'page','type':'function','function':{'name':'read_page','arguments':'{}'}}]},
                  {'role':'tool','tool_call_id':'page','content':original}]
        selected,info=build_context(messages,[],s,max_chars=3000)
        self.assertTrue(info['offloaded'])
        self.assertEqual([m['content'] for m in selected if m['role']=='user'],['旧问题','当前问题'])
        self.assertEqual(json.loads(selected[-1]['content'])['record_id'],'1')
        with httpx.Client() as client:
            registry=default_registry(client,'unused',s)
            result=json.loads(registry.execute({'id':'h','function':{'name':'read_history',
                'arguments':'{"record_id":"1","limit":24000}'}},{},{}))
        self.assertEqual(json.loads(result['content'])['result'],original)
        self.assertEqual(messages[-1]['content'],original)

    def test_latest_history_read_reaches_next_model_context(self):
        session = Session()
        source = json.dumps({'operation':'read_page','status':'success','content':'x'*8000+'TAIL-EVIDENCE'})
        session.records.append({'id':'1','kind':'tool_completed','call_id':'page','result':source})
        retrieved = json.dumps(session.read_record('1'), ensure_ascii=False)
        session.records.append({'id':'2','kind':'tool_completed','call_id':'history','result':retrieved})
        def exchange(identifier, name, content):
            return [{'role':'assistant','content':None,'tool_calls':[
                {'id':identifier,'type':'function','function':{'name':name,'arguments':'{}'}}]},
                {'role':'tool','tool_call_id':identifier,'content':content}]
        messages = [{'role':'system','content':'规则'}, {'role':'user','content':'读取尾部证据'}]
        messages += exchange('page','read_page',source) + exchange('history','read_history',retrieved)
        selected, info = build_context(messages, [], session, max_chars=12000)
        self.assertLessEqual(info['request_characters'], 12000)
        self.assertEqual(selected[-1]['content'], retrieved)
        self.assertIn('TAIL-EVIDENCE', selected[-1]['content'])

    def test_storage_failure_does_not_append_a_second_record(self):
        with tempfile.TemporaryDirectory() as d:
            s=Session.create(Path(d))
            with patch('seekra.session.os.fsync',side_effect=OSError('磁盘故障')):
                with self.assertRaises(OSError):s.clear()
            before=s.path.read_bytes()
            with self.assertRaises(RuntimeError):s.clear()
            self.assertEqual(s.path.read_bytes(),before)

    def test_context_never_moves_old_messages_after_current_question(self):
        messages=[{'role':'system','content':'规则'}, {'role':'user','content':'旧问题'},
                  {'role':'assistant','content':'旧回答'*500}, {'role':'user','content':'当前问题'},
                  {'role':'assistant','content':'近期说明'}]
        original=deepcopy(messages)
        selected,info=build_context(messages,[],Session(),max_chars=2000)
        current=next(i for i,m in enumerate(selected) if m.get('content')=='当前问题')
        self.assertFalse(any(m.get('content')=='旧问题' for m in selected[current+1:]))
        self.assertEqual(messages,original)
        self.assertLessEqual(info['request_characters'],2000)

    def test_cancellation_after_tool_return_preserves_known_result(self):
        cancel=CancellationToken()
        registry=ToolRegistry()
        def tool(call,pages,known):
            cancel.cancel()
            return '{"status":"success","value":42}'
        registry.register({'type':'function','function':{'name':'calculate','parameters':{'type':'object'}}},tool)
        class Model:
            identity='test:model'
            def generate(self,*args,**kwargs):
                return ModelResponse({'role':'assistant','content':None,'tool_calls':[
                    {'id':'calc','type':'function','function':{'name':'calculate','arguments':'{}'}}]},'tool_calls')
        session=Session()
        result=execute_run('计算',model=Model(),registry=registry,session=session,cancel=cancel)
        self.assertEqual(result.status,'interrupted')
        self.assertFalse(session.last_run['started_tools'])
        self.assertEqual(session.last_run['messages'][-1]['content'],'{"status":"success","value":42}')

    def test_usage_display_reports_missing_not_zero_and_exact_last_input(self):
        out,err=io.StringIO(),io.StringIO()
        with redirect_stdout(out),redirect_stderr(err):
            view=AgentDisplay(verbose=True)
            view.event('usage',usage={'input_tokens':100,'output_tokens':2,'cache_hit_rate':0.8},
                       totals={'input_tokens':100,'output_tokens':2,'missing_calls':0,'calls':1})
            view.event('usage',usage=None,totals={'input_tokens':100,'output_tokens':2,'missing_calls':1,'calls':2})
        self.assertIn('100',err.getvalue())
        self.assertIn('80.0%',err.getvalue())
        self.assertIn('未取得',err.getvalue())
        self.assertIn('不完整',err.getvalue())
        self.assertEqual(out.getvalue(),'')

    def test_stale_writer_and_concurrent_writer_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            a=Session.create(Path(d));b=Session.load(a.path)
            with a.exclusive():
                with self.assertRaises(RuntimeError):
                    with b.exclusive():pass
            a.clear()
            with self.assertRaises(RuntimeError):b.clear()

    def test_disk_redaction_keeps_live_content_but_never_credentials_or_reasoning(self):
        with tempfile.TemporaryDirectory() as d:
            s=Session.create(Path(d));s.secrets=('secret-test-value',)
            s.record('observation',content='secret-test-value',reasoning_content='private-thought')
            text=s.path.read_text(encoding='utf-8')
            self.assertNotIn('secret-test-value',text)
            self.assertNotIn('private-thought',text)
            self.assertEqual(s.records[-1]['content'],'secret-test-value')
