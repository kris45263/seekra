"""通过 CLI 创建、列出和重新打开会话；用假模型避免外部请求。"""
from contextlib import redirect_stdout, redirect_stderr
import asyncio
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from seekra.terminal.cli import main
from seekra.terminal.input import ReplInput
from seekra.search.state import SearchSession as Session
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from test_agent import reply


class CliSessionTests(unittest.TestCase):
    def test_final_notification_and_content_errors_have_distinct_cli_outcomes(self):
        from seekra.terminal.cli import run_question
        from seekra.terminal.display import AgentDisplay
        from seekra.harness.runtime import ContentOutputError
        config = {'DEEPSEEK_API_KEY':'fake', 'TAVILY_API_KEY':'fake', 'DEEPSEEK_MODEL':'test'}
        original_event = AgentDisplay.event
        for phase in ('final_answer', 'content'):
            for error, code, state in ((ContentOutputError('输出失败'), 74, 'failed'),
                                       (KeyboardInterrupt(), 130, 'interrupted')):
                with self.subTest(phase=phase, error=type(error).__name__):
                    session, out, err = Session(), io.StringIO(), io.StringIO()
                    def event(view, name, **details):
                        if name == 'final_answer':
                            raise error
                        return original_event(view, name, **details)
                    target = 'event' if phase == 'final_answer' else 'content'
                    replacement = event if phase == 'final_answer' else lambda *args: (_ for _ in ()).throw(error)
                    with httpx.Client(transport=httpx.MockTransport(lambda r:reply('完整答案'))) as client, \
                         patch.object(AgentDisplay, target, replacement), \
                         redirect_stdout(out), redirect_stderr(err):
                        actual = run_question('问题', client=client, config=config, session=session, verbose=False)
                    if phase == 'final_answer':
                        self.assertEqual(actual, 0)
                        self.assertEqual(session.last_run['status'], 'completed')
                        self.assertEqual(out.getvalue(), '完整答案\n')
                        self.assertNotIn('已停止', err.getvalue())
                        self.assertNotIn('/continue', err.getvalue())
                    else:
                        self.assertEqual(actual, code)
                        self.assertEqual(session.last_run['status'], state)
                        self.assertEqual(session.messages, [])

    def test_resume_without_saved_conversations_is_an_empty_state(self):
        for args,commands in ((['--resume'],['/exit']),(['--repl'],['/resume','/exit'])):
            with tempfile.TemporaryDirectory() as directory:
                err=io.StringIO()
                with patch('sys.argv',['seekra','--session-dir',directory,*args]), \
                     patch('seekra.terminal.cli.read_config',return_value={}), \
                     patch('seekra.terminal.cli.ReplInput.read',side_effect=commands), \
                     patch('httpx.Client.send',side_effect=AssertionError('空态不能联网')), \
                     redirect_stdout(io.StringIO()),redirect_stderr(err):
                    self.assertEqual(main(),0)
                self.assertIn('还没有可以接着聊的对话',err.getvalue())
                self.assertNotIn('失败',err.getvalue())

    def test_storage_failure_does_not_claim_records_are_saved_or_offer_continue(self):
        from seekra.terminal.cli import run_question
        session=Session();record=session.record
        def fail(kind,**data):
            if kind=='model_completed':
                session._storage_failed=True
                raise OSError('模拟写入失败')
            return record(kind,**data)
        err=io.StringIO()
        with httpx.Client(transport=httpx.MockTransport(lambda r:reply('答案'))) as client, \
             patch.object(session,'record',side_effect=fail),redirect_stdout(io.StringIO()),redirect_stderr(err):
            status=run_question('问题',client=client,config={'DEEPSEEK_API_KEY':'fake','TAVILY_API_KEY':'fake','DEEPSEEK_MODEL':'test'},
                                session=session,verbose=False)
        self.assertEqual(status,74)
        self.assertNotIn('执行记录保留',err.getvalue())
        self.assertNotIn('/continue',err.getvalue())

    def test_config_rejection_does_not_recommend_the_same_continue_command(self):
        config={'DEEPSEEK_API_KEY':'fake','TAVILY_API_KEY':'fake','DEEPSEEK_MODEL':'original'}
        def fail(request):
            config['DEEPSEEK_MODEL']='changed'
            return httpx.Response(500)
        err=io.StringIO()
        with patch('sys.argv',['seekra','--repl','--no-session']), \
             patch('seekra.terminal.cli.read_config',return_value=config), \
             patch('seekra.terminal.cli.ReplInput.read',side_effect=['问题','/continue','/exit']), \
             patch('seekra.terminal.cli.httpx.Client',return_value=httpx.Client(transport=httpx.MockTransport(fail))), \
             redirect_stdout(io.StringIO()),redirect_stderr(err):
            self.assertEqual(main(),0)
        self.assertIn('恢复配置与原任务不同',err.getvalue())
        self.assertNotIn('输入 /continue',err.getvalue().split('恢复配置与原任务不同')[-1])

    def test_repl_switch_restores_visible_history_and_followup_context(self):
        with tempfile.TemporaryDirectory() as directory, create_pipe_input() as pipe:
            session = Session.create(Path(directory))
            session.record('run_started', run_id='old', question='记住树叶', system='规则', config={})
            session.record('model_completed', run_id='old', message={'role':'assistant','content':'已记住树叶'}, finish_reason='stop')
            session.record('run_ended', run_id='old', status='completed')
            editor = ReplInput(input=pipe, output=DummyOutput())
            requests = []
            def respond(request):
                requests.append(json.loads(request.content)['messages'])
                return reply('暗号是树叶')
            async def drive(handle):
                task = asyncio.create_task(editor.run_async(handle))
                async def wait_for(predicate):
                    for _ in range(100):
                        if predicate():
                            return
                        await asyncio.sleep(.01)
                    self.fail('会话界面未更新')
                async def submit(text):
                    pipe.send_text(text+'\r')
                    await asyncio.sleep(.08)
                    await wait_for(lambda: not editor.busy)
                    await asyncio.sleep(.05)
                def visible_text():
                    screen = editor._app.renderer._last_screen
                    return '\n'.join(''.join(cell.char for _,cell in sorted(row.items()))
                                     for _,row in sorted(screen.data_buffer.items()))
                try:
                    await wait_for(lambda: editor._app and editor._app.renderer._last_screen)
                    self.assertTrue(editor._app.full_screen)
                    self.assertIn('已记住树叶', editor._log.text)
                    self.assertIn(session.id[:8], visible_text())
                    await submit('/new')
                    self.assertNotIn('树叶', editor._log.text)
                    self.assertIn(editor.session_id[:8], visible_text())
                    await submit('新的问题')
                    self.assertNotIn('树叶', str(requests[-1]))
                    await submit('/open')
                    self.assertIn('再输入 /open', editor._log.text)
                    await submit('/sessions')
                    self.assertIn('/open 序号', editor._log.text)
                    await submit('/open 2')
                    self.assertIn('记住树叶', editor._log.text)
                    self.assertIn('已记住树叶', editor._log.text)
                    self.assertNotIn('新的问题', editor._log.text)
                    self.assertEqual(editor.session_id, session.id)
                    self.assertIn(session.id[:8], visible_text())
                    await submit('/session')
                    self.assertIn('不认识这个命令',editor._log.text)
                    self.assertNotIn('/session 已取消',editor._log.text)
                    await submit('/open 00000000')
                    self.assertIn('已记住树叶', editor._log.text)
                    self.assertEqual(editor.session_id, session.id)
                    await submit('暗号呢')
                    self.assertIn('已记住树叶', str(requests[-1]))
                    self.assertNotIn('新的问题', str(requests[-1]))
                    self.assertIn('暗号是树叶', editor._log.text)
                    await submit('/new')
                    await submit('/resume')
                    self.assertEqual(editor.session_id, session.id)
                    self.assertIn(session.id[:8], visible_text())
                finally:
                    pipe.send_text('\x03\x03')
                    await asyncio.wait_for(task, 3)
                return 0
            config={'DEEPSEEK_API_KEY':'fake-ds','DEEPSEEK_MODEL':'test','TAVILY_API_KEY':'fake-tv'}
            with patch('sys.argv',['seekra','--session-dir',directory,'--session',session.id,'--repl']), \
                 patch('seekra.terminal.cli.read_config',return_value=config), \
                 patch('seekra.terminal.cli.ReplInput',return_value=editor), \
                 patch.object(editor,'run',side_effect=lambda handle: asyncio.run(drive(handle))), \
                 patch('seekra.terminal.cli.httpx.Client',return_value=httpx.Client(transport=httpx.MockTransport(respond))), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(),0)

    def test_empty_resume_is_command_feedback_without_a_failed_answer(self):
        err = io.StringIO()
        with patch('sys.argv',['seekra','--repl','--no-session']), \
             patch('seekra.terminal.cli.read_config',return_value={'DEEPSEEK_API_KEY':'fake','TAVILY_API_KEY':'fake','DEEPSEEK_MODEL':'test'}), \
             patch('seekra.terminal.cli.ReplInput.read',side_effect=['/continue','/exit']), \
             patch('httpx.Client.send',side_effect=AssertionError('命令不能联网')), \
             redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(main(),0)
        self.assertNotIn('本次回答未完成',err.getvalue())
        self.assertNotIn('── Seekra ──',err.getvalue())
        self.assertIn('没有需要继续的回答',err.getvalue())

    def test_resume_opens_latest_nonempty_conversation_without_running_model(self):
        with tempfile.TemporaryDirectory() as directory:
            for index in range(3):
                session = Session.create(Path(directory))
                if index < 2:
                    session.record('run_started',run_id='r',question=f'旧问题{index}',system='规则',config={})
                    session.record('model_completed',run_id='r',message={'role':'assistant','content':f'旧回答{index}'},finish_reason='stop')
                    session.record('run_ended',run_id='r',status='completed')
                os.utime(session.path,(1000+index,1000+index))
            for args, commands in ((['--repl'],['/resume','/exit']), (['--resume'],['/exit'])):
                out, err = io.StringIO(), io.StringIO()
                with patch('sys.argv',['seekra','--session-dir',directory,*args]), \
                     patch('seekra.terminal.cli.read_config',return_value={}), \
                     patch('seekra.terminal.cli.ReplInput.read',side_effect=commands), \
                     patch('httpx.Client.send',side_effect=AssertionError('恢复对话不能调用模型')), \
                     redirect_stdout(out),redirect_stderr(err):
                    self.assertEqual(main(),0)
                self.assertIn('旧回答1',err.getvalue())
                self.assertNotIn('旧回答0',err.getvalue())
                self.assertNotIn('没有可继续',err.getvalue())

    def test_saved_cli_session_can_be_selected_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            config={'DEEPSEEK_API_KEY':'fake-ds','DEEPSEEK_MODEL':'test','TAVILY_API_KEY':'fake-tv'}
            def invoke(args,handler):
                out,err=io.StringIO(),io.StringIO()
                with patch('sys.argv',['seekra','--session-dir',directory,*args]),patch('seekra.terminal.cli.read_config',return_value=config),\
                     patch('seekra.terminal.cli.httpx.Client',return_value=httpx.Client(transport=httpx.MockTransport(handler))),\
                     redirect_stdout(out),redirect_stderr(err):
                    self.assertEqual(main(),0,err.getvalue())
                return out.getvalue(),err.getvalue()
            invoke(['记住树叶'],lambda r:reply('已记住'))
            files=list(root.glob('*.jsonl'))
            self.assertEqual(len(files),1)
            def second(request):
                messages=json.loads(request.content)['messages']
                self.assertIn('记住树叶',str(messages))
                return reply('树叶')
            out,err=invoke(['--session',files[0].stem,'暗号呢'],second)
            self.assertEqual(out,'树叶\n')
            out,err=invoke(['--sessions'],lambda r:self.fail('列表不能联网'))
            self.assertIn(files[0].stem,err)

    def test_no_session_flag_does_not_write_records(self):
        with tempfile.TemporaryDirectory() as directory, patch('sys.argv',['seekra','--session-dir',directory,'--no-session','--repl']),\
             patch('seekra.terminal.cli.read_config',return_value={}),patch('seekra.terminal.cli.ReplInput.read',side_effect=['/exit']),\
             redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
            self.assertEqual(main(),0)
            self.assertEqual(list(Path(directory).iterdir()),[])
