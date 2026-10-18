"""统一展示入口的行为约定；不调用真实服务。"""

import io
import json
import unittest
from contextlib import redirect_stdout, redirect_stderr

from seekra.display import AgentDisplay, session_transcript
from seekra.agent import run_agent
from seekra.session import Session
import httpx

from test_agent import reply, call


class DisplayEventTests(unittest.TestCase):
    def test_outline_result_is_displayed_as_navigation_not_body(self):
        from test_documents import add_page
        for count in (2, 25):
            with self.subTest(sections=count):
                session = Session()
                page = add_page(session, '\n\n'.join(f'## Section {i}\nText.' for i in range(count)))
                result = session.documents.read_document(page['document_id'], view='outline')
                err = io.StringIO()
                with redirect_stderr(err):
                    AgentDisplay().event('tool_result', tool_name='read_document', result=json.dumps(result))
                self.assertIn('目录', err.getvalue())
                self.assertNotIn('正文', err.getvalue())
                self.assertIn('可继续翻页' if count > 20 else '已列完', err.getvalue())
                err = io.StringIO()
                with redirect_stderr(err):
                    AgentDisplay().event('tool_call', tool_call=call('read_document', {
                        'document_id': page['document_id'], 'view': 'outline'}))
                self.assertIn('目录', err.getvalue())
                self.assertNotIn('正文', err.getvalue())

    def test_unlimited_call_display_does_not_print_a_remaining_count(self):
        err = io.StringIO()
        with redirect_stderr(err):
            AgentDisplay(verbose=True).event('model_call', round=13, message_count=28)
        self.assertIn('第 13 轮', err.getvalue())
        self.assertNotIn('None', err.getvalue())
        self.assertNotIn('剩余', err.getvalue())

    def test_tool_failure_shows_reason_without_promising_the_next_action(self):
        err=io.StringIO()
        with redirect_stderr(err):
            AgentDisplay().event('tool_result',tool_name='read_page',result=json.dumps({
                'status':'failed','error':'Tavily 报告目标页面返回 404。'}))
        self.assertIn('页面读取未完成',err.getvalue())
        self.assertIn('404',err.getvalue())
        self.assertNotIn('继续根据',err.getvalue())

    def test_old_duplicate_partial_record_does_not_repeat_completed_message(self):
        session=Session(records=[
            {'kind':'model_completed','run_id':'r','message':{'role':'assistant','content':'先查找','tool_calls':[{}]}},
            {'kind':'run_ended','run_id':'r','status':'needs_attention','partial_content':'先查找','error':'结果未知'}])
        self.assertEqual(session_transcript(session).count('先查找'),1)
        session.records.insert(1,{'kind':'model_started','run_id':'r'})
        self.assertEqual(session_transcript(session).count('先查找'),2)

    def test_restored_transcript_distinguishes_partial_answers_without_exposing_internal_messages(self):
        session = Session()
        session.record('run_started', run_id='r', question='我的问题', system='内部系统说明', config={})
        session.record('model_completed', run_id='r', finish_reason='tool_calls', message={
            'role':'assistant', 'content':'先搜索', 'tool_calls':[call('tavily_search', {'query':'查询'})]})
        session.record('run_ended', run_id='r', status='interrupted', error='取消', partial_content='未完成 secret')
        session.record('run_resumed', run_id='r')
        session.record('model_completed', run_id='r', finish_reason='stop', message={'role':'assistant','content':'完整答案'})
        session.record('run_ended', run_id='r', status='completed')
        before = json.dumps(session.records, ensure_ascii=False)
        text = session_transcript(session, ('secret',))
        self.assertIn('Seekra · 回答未完成', text)
        self.assertIn('已停止', text)
        self.assertIn('继续回答', text)
        self.assertEqual(text.count('完整答案'), 1)
        self.assertNotIn('内部系统说明', text)
        self.assertNotIn('tool_calls', text)
        self.assertNotIn('secret', text)
        self.assertEqual(json.dumps(session.records, ensure_ascii=False), before)

    def test_display_alone_renders_verbose_lifecycle_without_reprinting_content(self):
        """完整诊断由展示对象提供，不依赖 Agent 再建立隐藏输出器。"""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            view = AgentDisplay(verbose=True)
            view.event('start', question='问题')
            view.event('model_call', round=1, message_count=2)
            view.content('唯一正文')
            view.event('model_response', content='唯一正文', tool_calls=None)
            view.event('final_answer', round=1)
        self.assertEqual(out.getvalue(), '唯一正文\n')
        self.assertNotIn('唯一正文', err.getvalue())
        self.assertIn('上下文 2 条消息', err.getvalue())
        self.assertIn('运行结束：1 次模型调用', err.getvalue())

    def test_collector_receives_events_without_terminal_output(self):
        """采集实验事件不要求创建终端视图，返回答案仍可独立消费。"""
        events = []
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), httpx.Client(
                transport=httpx.MockTransport(lambda request: reply('答案'))) as client:
            answer = run_agent('问题', client=client, model='test',
                               deepseek_api_key='fake-ds', tavily_api_key='fake-tv',
                               on_event=lambda event, **details: events.append((event, details)))
        self.assertEqual(answer, '答案')
        self.assertEqual((out.getvalue(), err.getvalue()), ('', ''))
        self.assertEqual([event for event, _ in events],
                         ['start', 'context', 'model_call', 'model_response', 'usage', 'final_answer'])

    def test_broken_diagnostics_preserve_requests_answer_and_session(self):
        """诊断写入或刷新故障都不影响真实循环、正文及成功提交。"""
        class BrokenWrite(io.StringIO):
            def write(self, text):
                raise OSError('诊断输出不可写')

        class BrokenFlush(io.StringIO):
            def flush(self):
                raise OSError('诊断输出不可刷新')

        runs = []
        for error_stream in (io.StringIO(), BrokenWrite(), BrokenFlush()):
            requests, out, session = [], io.StringIO(), Session()

            def handler(request):
                body = json.loads(request.content)
                requests.append(body)
                if len(requests) == 1:
                    return reply('先整理', [call('list_documents', {})])
                self.assertEqual(json.loads(body['messages'][-1]['content'])['status'], 'success')
                return reply('最终答案')

            with redirect_stdout(out), redirect_stderr(error_stream), httpx.Client(
                    transport=httpx.MockTransport(handler)) as client:
                view = AgentDisplay(verbose=True)
                answer = run_agent('问题', client=client, model='test',
                                   deepseek_api_key='fake-ds', tavily_api_key='fake-tv',
                                   session=session, on_event=view.event, on_content=view.content)
            runs.append((requests, answer, session.messages, session.pages, out.getvalue()))
        self.assertEqual(runs[0], runs[1])
        self.assertEqual(runs[0], runs[2])
        self.assertEqual(runs[0][-1], '先整理\n最终答案\n')

    def test_model_line_finishes_before_tool_status(self):
        """两条标准流共享终端时，工具状态必须出现在完整正文行之后。"""
        terminal = io.StringIO()
        with redirect_stdout(terminal), redirect_stderr(terminal):
            view = AgentDisplay()
            view.content('先查资料')
            view.event('model_response', content='先查资料')
            view.event('tool_call', tool_call=call('tavily_search', {'query': '关键词'}))
        self.assertTrue(terminal.getvalue().startswith('先查资料\n正在搜索'))


if __name__ == '__main__':
    unittest.main()
