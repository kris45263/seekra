"""上下文、取消、用量和会话锁的实际接口测试。"""
from copy import deepcopy
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from seekra.context import build_context, ContextLimit, history_index
from seekra.runtime import CancellationToken, ModelResponse
from seekra.agent import execute_run
from seekra.session import Session
from seekra.tools import ToolRegistry
from seekra.display import AgentDisplay


class RuntimeEdgeTests(unittest.TestCase):
    def test_context_pause_does_not_publish_unsent_system_prompt(self):
        class Model:
            identity = 'test:context-pause'
            def generate(self, messages, tools, **kwargs):
                return ModelResponse({'role': 'assistant', 'tool_calls': [{
                    'id': 'large', 'type': 'function', 'function': {'name': 'probe',
                    'arguments': json.dumps({'payload': 'x' * 3000})}}]}, 'tool_calls')
        registry = ToolRegistry()
        registry.register({'type': 'function', 'function': {'name': 'probe',
                          'parameters': {'type': 'object'}}}, lambda *args: '{"status":"success"}')
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            result = execute_run('问题', model=Model(), registry=registry, session=session,
                                 max_context_chars=5000)
            self.assertEqual(result.status, 'paused')
            restored = Session.load(session.path)
            self.assertEqual(restored.last_run['messages'], session.last_run['messages'])
            self.assertEqual(restored.last_run['calls'], 1)

    def test_run_passes_twelve_calls_and_keeps_count_after_cancel_and_resume(self):
        class Model:
            identity = 'test:optional-limit'
            def generate(self, messages, tools, **kwargs):
                reads = sum(m['role'] == 'tool' for m in messages)
                if reads < 13:
                    return ModelResponse({'role': 'assistant', 'tool_calls': [{
                        'id': f'probe-{reads}', 'type': 'function',
                        'function': {'name': 'probe', 'arguments': '{}'}}]}, 'tool_calls')
                return ModelResponse({'role': 'assistant', 'content': '已完成'}, 'stop')
        registry = ToolRegistry()
        registry.register({'type': 'function', 'function': {'name': 'probe',
                          'parameters': {'type': 'object'}}}, lambda *args: '{"value":1}')
        events = []
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            cancel = CancellationToken()
            def stop_after_two_tools(name, **data):
                if name == 'tool_result' and session.last_run['calls'] == 2:
                    cancel.cancel()
            result = execute_run('问题', model=Model(), registry=registry, session=session,
                                 cancel=cancel, on_event=stop_after_two_tools)
            self.assertEqual(result.status, 'interrupted')
            self.assertEqual(session.last_run['calls'], 2)
            restored = Session.load(session.path)
            result = execute_run(None, model=Model(), registry=registry, session=restored,
                                 resume=True, on_event=lambda name, **data: events.append((name, data)))
            self.assertEqual((result.status, restored.last_run['calls']), ('completed', 14))
            prompts = [r['system'] for r in restored.records if r['kind'] == 'model_started'][2:]
            self.assertTrue(all('最多有' not in p and '最多还能' not in p for p in prompts))
            self.assertTrue(all('remaining' not in data for name, data in events if name == 'model_call'))
        fresh = Session()
        self.assertEqual(execute_run('问题', model=Model(), registry=registry, session=fresh).status, 'completed')
        self.assertEqual(fresh.last_run['calls'], 14)

    def test_final_notification_failure_preserves_committed_result_after_reload(self):
        from seekra.runtime import ContentOutputError
        from seekra.cli import continuation_hint
        class Model:
            identity = 'test:final-notification'
            def generate(self, *args, **kwargs):
                kwargs['on_content']('完整答案')
                return ModelResponse({'role':'assistant','content':'完整答案'}, 'stop')
        for error in (ContentOutputError('最终显示失败'), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as directory:
                session = Session.create(Path(directory))
                shown = []
                def event(name, **details):
                    if name == 'final_answer':
                        self.assertEqual(session.last_run['status'], 'completed')
                        raise error
                try:
                    result = execute_run('问题', model=Model(), registry=ToolRegistry(), session=session,
                                         on_content=shown.append, on_event=event)
                except (ContentOutputError, KeyboardInterrupt):
                    self.fail('完成通知异常泄漏到调用者')
                self.assertEqual((result.status, result.answer, result.cause), ('completed', '完整答案', None))
                self.assertEqual(shown, ['完整答案'])
                restored = Session.load(session.path)
                self.assertEqual(restored.last_run['status'], 'completed')
                self.assertEqual(restored.messages[-1]['content'], '完整答案')
                self.assertEqual([r['status'] for r in restored.records if r['kind']=='run_ended'], ['completed'])
                self.assertEqual(restored.records[-1]['kind'], 'observation')
                self.assertEqual(restored.records[-1]['event'], 'final_answer')
                self.assertEqual(restored.records[-1]['exception_type'], type(error).__name__)
                self.assertEqual(continuation_hint(restored), '')
                with patch.object(Model, 'generate', side_effect=AssertionError('不能重复请求')):
                    with self.assertRaisesRegex(ValueError, '没有可继续'):
                        execute_run(None, model=Model(), registry=ToolRegistry(), session=restored, resume=True)

    def test_reset_blocks_history_reads_without_deleting_records_after_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            session.record('run_started', run_id='old', question='旧问题', system='规则', config={})
            entry = session.record('observation', content='旧数据')
            session.record('model_completed', run_id='old', message={'role':'assistant', 'tool_calls':[
                {'id':'page','type':'function','function':{'name':'read_page','arguments':'{}'}}]})
            session.record('tool_completed', run_id='old', call_id='page', result='旧正文',
                           page_updates={'https://example.org':{'content':'旧缓存'}})
            session.record('model_completed', run_id='old', message={'role':'assistant','content':'旧答案'})
            session.record('run_ended', run_id='old', status='completed')
            self.assertTrue(session.pages)
            session.clear()
            self.assertEqual(session.messages, [])
            self.assertIsNone(session.last_run)
            current = session.record('run_started', run_id='new', question='新问题', system='规则', config={})
            for view in (session, Session.load(session.path)):
                with self.subTest(persistent=view is not session):
                    self.assertEqual(view.records[int(entry)-1]['content'], '旧数据')
                    self.assertEqual(view.pages, {})
                    self.assertNotIn('old', view.runs)
                    with self.assertRaisesRegex(ValueError, '重置'):
                        view.read_record(entry)
                    rows = history_index(view)['records']
                    self.assertEqual([r['record_id'] for r in rows], [current])
                    self.assertEqual(json.loads(view.read_record(current)['content'])['question'], '新问题')

    def test_failed_records_are_readable_but_labeled_and_not_inherited(self):
        for status in ('failed', 'interrupted'):
            with self.subTest(status=status):
                session = Session()
                session.record('run_started', run_id='old', question='未完成问题', system='规则', config={})
                entry = session.record('model_completed', run_id='old', message={
                    'role':'assistant','content':'未提交回答'})
                session.record('run_ended', run_id='old', status=status)
                session.record('run_started', run_id='new', question='新问题', system='规则', config={})
                self.assertNotIn('未完成问题', str(session.last_run['messages']))
                result = session.read_record(entry)
                self.assertEqual(result['run_status'], status)
                self.assertEqual(result['question'], '未完成问题')
                row = next(r for r in history_index(session)['records'] if r['record_id']==entry)
                self.assertEqual(row['run_status'], status)

    def test_context_removes_whole_old_tasks_including_all_tool_rounds(self):
        messages = [{'role':'system','content':'规则'},
                    {'role':'user','content':'故意给错答案，不可信。' * 200},
                    {'role':'assistant','content':'巴黎是德国首都。'},
                    {'role':'assistant','tool_calls':[{'id':'a'}]},
                    {'role':'tool','tool_call_id':'a','content':'旧结果'},
                    {'role':'assistant','tool_calls':[{'id':'b'}]},
                    {'role':'tool','tool_call_id':'b','content':'另一结果'},
                    {'role':'assistant','content':'旧结论'},
                    {'role':'user','content':'刚才说得对吗？'}]
        selected, info = build_context(messages, [], Session(), 2000)
        self.assertEqual(selected[1:], messages[-1:])
        self.assertEqual(info['request_characters'], len(json.dumps(
            {'messages':selected, 'tools':[]}, ensure_ascii=False)))

    def test_current_task_cannot_lose_intermediate_decisions_to_fit(self):
        messages = [{'role':'system','content':'规则'}, {'role':'user','content':'当前问题'},
                    {'role':'assistant','content':'必须保留的前提' * 400},
                    {'role':'assistant','tool_calls':[{'id':'a'}]},
                    {'role':'tool','tool_call_id':'a','content':'最新反馈'}]
        with self.assertRaises(ContextLimit):
            build_context(messages, [], Session(), 2000)

    def test_status_callback_failure_is_recorded_without_stopping_execution(self):
        class Model:
            identity = 'test:events'
            def generate(self, *args, **kwargs):
                return ModelResponse({'role':'assistant','content':'答案'}, 'stop')
        for event in ('start', 'context', 'model_call', 'model_response', 'usage', 'final_answer'):
            with self.subTest(event=event), tempfile.TemporaryDirectory() as directory:
                session = Session.create(Path(directory))
                def broken(name, **details):
                    if name == event:
                        raise ValueError('展示格式错误')
                result = execute_run('问题', model=Model(), registry=ToolRegistry(),
                                     session=session, on_event=broken)
                self.assertEqual(result.status, 'completed')
                restored = Session.load(session.path)
                self.assertEqual(restored.last_run['status'], 'completed')
                observations = [r for r in restored.records if r['kind']=='observation']
                self.assertEqual(observations[0]['event'], event)
                self.assertEqual(observations[0]['error_code'], 'display_error')

    def test_start_callback_interrupt_ends_run(self):
        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt()
        class Model:
            identity = 'test:start-cancel'
            generate = interrupt
        session = Session()
        try:
            result = execute_run('问题', model=Model(), registry=ToolRegistry(),
                                 session=session, on_event=interrupt)
        except KeyboardInterrupt:
            self.fail('start 中断没有进入任务结束处理')
        self.assertEqual(result.status, 'interrupted')
        self.assertEqual(session.last_run['status'], 'interrupted')

    def test_content_failure_has_separate_reason_and_preserves_partial_record(self):
        class Model:
            identity = 'test:output'
            def generate(self, *args, **kwargs):
                kwargs['on_content']('残稿')
                raise AssertionError('输出失败后不能继续')
        def broken(text):
            raise ValueError('输出转换失败')
        session = Session()
        result = execute_run('问题', model=Model(), registry=ToolRegistry(), session=session,
                             on_content=broken)
        self.assertEqual(result.status, 'failed')
        self.assertEqual(session.last_run['error_code'], 'output_error')
        self.assertEqual(session.records[-1]['partial_content'], '残稿')
        self.assertEqual(session.messages, [])

    def test_output_finish_failure_resumes_saved_answer_without_model_replay(self):
        class Model:
            identity = 'test:finish'
            def generate(self, *args, **kwargs):
                kwargs['on_content']('完整答案')
                return ModelResponse({'role':'assistant','content':'完整答案'}, 'stop')
        class BrokenNewline(io.StringIO):
            def write(self, text):
                if text == '\n':
                    raise OSError('输出换行失败')
                return super().write(text)
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            with redirect_stdout(BrokenNewline()), redirect_stderr(io.StringIO()):
                view = AgentDisplay()
                result = execute_run('问题', model=Model(), registry=ToolRegistry(), session=session,
                                     on_content=view.content, on_event=view.event)
            self.assertEqual(result.status, 'failed')
            self.assertEqual(session.last_run['error_code'], 'output_error')
            restored = Session.load(session.path)
            with patch.object(Model, 'generate', side_effect=AssertionError('不能重放')):
                result = execute_run(None, model=Model(), registry=ToolRegistry(), session=restored, resume=True)
            self.assertEqual(result.answer, '完整答案')

    def test_end_display_failure_does_not_replace_model_failure(self):
        error = ValueError('模型数据错误')
        class Model:
            identity = 'test:model-error'
            def generate(self, *args, **kwargs):
                raise error
        def event(name, **details):
            if name == 'end':
                raise RuntimeError('显示错误')
        session = Session()
        result = execute_run('问题', model=Model(), registry=ToolRegistry(), session=session, on_event=event)
        self.assertIs(result.cause, error)
        self.assertEqual(session.last_run['error'], '模型数据错误')

    def test_output_failure_in_error_display_does_not_mask_execution_error(self):
        from seekra.runtime import ContentOutputError
        error = ValueError('模型数据错误')
        class Model:
            identity = 'test:cleanup'
            def generate(self, *args, **kwargs):
                raise error
        def event(name, **details):
            if name == 'end':
                raise ContentOutputError('错误说明的正文输出失败')
        session = Session()
        result = execute_run('问题', model=Model(), registry=ToolRegistry(), session=session, on_event=event)
        self.assertIs(result.cause, error)
        self.assertEqual(session.last_run['error'], '模型数据错误')

    def test_execute_storage_failure_preserves_original_error_and_stops(self):
        error = OSError('磁盘故障')
        class Model:
            identity = 'test:storage'
            def generate(self, *args, **kwargs):
                with patch('seekra.session.os.fsync', side_effect=error):
                    kwargs['on_usage']({'input_tokens':3})
                raise AssertionError('存储失败后不能继续')
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            with self.assertRaises(OSError) as raised:
                execute_run('问题', model=Model(), registry=ToolRegistry(), session=session)
            self.assertIs(raised.exception, error)
            self.assertTrue(session._storage_failed)
            self.assertFalse(any(r['kind']=='run_ended' for r in session.records))

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
        session=Session()
        session.record('run_started',run_id='r',question='问题',system='规则',config={})
        session.record('model_started',run_id='r',round=1,system='规则')
        session.record('model_completed',run_id='r',message={'role':'assistant','content':'完整答案'})
        self.assertNotIn('上限',continuation_hint(session))
        self.assertIn('/continue',continuation_hint(session))
        session=Session()
        session.record('run_started',run_id='r',question='问题',system='规则',config={})
        session.record('model_started',run_id='r',round=1,system='规则')
        session.record('run_ended',run_id='r',status='paused',error_code='iteration_limit',error='达到次数上限')
        self.assertNotIn('上下文',continuation_hint(session))
        self.assertIn('配置一致',continuation_hint(session))
        self.assertNotIn('输入 /continue 继续',continuation_hint(session))

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
        self.assertEqual(selected[0], messages[0], '仅外置正文且保留所有任务时，不改写 system')
        self.assertEqual(json.loads(selected[-1]['content'])['record_id'],'1')
        with httpx.Client() as client:
            registry=default_registry(client,'unused',s)
            result=json.loads(registry.execute({'id':'h','function':{'name':'read_history',
                'arguments':'{"record_id":"1","limit":24000}'}},{},{}))
        self.assertIn(original, result['content'])
        self.assertEqual(messages[-1]['content'],original)

    def test_dropped_old_task_keeps_notice_and_exact_budget(self):
        messages = [{'role':'system','content':'规则'}, {'role':'user','content':'旧问题'},
                    {'role':'assistant','content':'旧回答。'*1500},
                    {'role':'user','content':'当前问题'}]
        original = deepcopy(messages)
        selected, info = build_context(messages, [], Session(), max_chars=2000)
        self.assertEqual(info['message_indices'], [0, 3])
        self.assertEqual(selected[-1], messages[-1])
        self.assertIn('较旧任务', selected[0]['content'])
        self.assertIn('read_history', selected[0]['content'])
        self.assertEqual(info['request_characters'], len(json.dumps(
            {'messages':selected, 'tools':[]}, ensure_ascii=False)))
        self.assertLessEqual(info['request_characters'], 2000)
        self.assertEqual(messages, original)

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
