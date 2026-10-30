"""持续输入生命周期的真实解析器测试；工作由本地假处理器模拟。"""
import asyncio
import threading
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout

import httpx

from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from seekra.terminal.input import ReplInput


class PasteOutput(DummyOutput):
    def __init__(self):
        self.paste_enabled = False
    def enable_bracketed_paste(self):
        self.paste_enabled = True
    def disable_bracketed_paste(self):
        self.paste_enabled = False


class InputEditorTests(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_keeps_scrolled_transcript_and_wrapped_draft_readable(self):
        with create_pipe_input() as pipe:
            editor = ReplInput(input=pipe, output=DummyOutput())
            editor.set_conversation('test', ''.join(f'输出 {i}\n' for i in range(200)))
            task = asyncio.create_task(editor.run_async(lambda text, cancel: None))
            async def wheel(area, direction):
                position = editor._app.renderer._last_screen.visible_windows_to_write_positions[area.window]
                pipe.send_text(f'\x1b[<{direction};{position.xpos+2};{position.ypos+2}M')
                await asyncio.sleep(.06)
            try:
                await self.wait_until(lambda: editor._app and editor._app.renderer._last_screen)
                await wheel(editor._log, 64)
                top = (editor._log.window.vertical_scroll, editor._log.window.vertical_scroll_2)
                cursor = editor._log.buffer.cursor_position
                editor._append_log('生成中的新内容\n')
                await asyncio.sleep(.08)
                self.assertEqual(editor._log.buffer.cursor_position, cursor)
                self.assertEqual((editor._log.window.vertical_scroll, editor._log.window.vertical_scroll_2), top)
                payload = '中文长行内容' * 500
                pipe.send_text('\x1b[200~'+payload+'\x1b[201~')
                await self.wait_until(lambda: editor.draft == payload)
                await asyncio.sleep(.06)
                bottom = editor._draft.window.vertical_scroll_2
                await wheel(editor._draft, 64)
                self.assertLess(editor._draft.window.vertical_scroll_2, bottom)
                self.assertEqual(editor.draft, payload)
                await wheel(editor._draft, 65)
                self.assertGreater(editor._draft.window.vertical_scroll_2, 0)
                for _ in range(15):
                    if editor._log.buffer.cursor_position == len(editor._log.text):
                        break
                    await wheel(editor._log, 65)
                self.assertEqual(editor._log.buffer.cursor_position, len(editor._log.text))
                editor._append_log('回到底部后继续跟随\n')
                await asyncio.sleep(.08)
                self.assertEqual(editor._log.buffer.cursor_position, len(editor._log.text))
                self.assertEqual(editor.draft, payload)
            finally:
                pipe.send_text('\x03\x03')
                await asyncio.wait_for(task, 3)

    async def test_long_draft_scroll_and_navigation_do_not_submit_or_change_text(self):
        calls = []
        with create_pipe_input() as pipe:
            editor = ReplInput(input=pipe, output=DummyOutput())
            task = asyncio.create_task(editor.run_async(lambda text, cancel: calls.append(text)))
            payload = ''.join(f'{i:04d} 草稿行\n' for i in range(500))
            try:
                pipe.send_text('\x1b[200~'+payload+'\x1b[201~')
                await self.wait_until(lambda: editor.draft == payload)
                await asyncio.sleep(.05)
                self.assertTrue(editor._app.mouse_support())
                bottom = editor._draft.window.vertical_scroll
                screen = editor._app.renderer._last_screen
                position = screen.visible_windows_to_write_positions[editor._draft.window]
                pipe.send_text(f'\x1b[<64;{position.xpos+2};{position.ypos+2}M')
                await self.wait_until(lambda: editor._draft.window.vertical_scroll < bottom)
                pipe.send_text('\x1b[1;5H')
                await self.wait_until(lambda: editor._draft.buffer.cursor_position == 0)
                pipe.send_text('\x1b[6~')
                await self.wait_until(lambda: editor._draft.buffer.cursor_position > 0)
                pipe.send_text('\x1b[1;5F')
                await self.wait_until(lambda: editor._draft.buffer.cursor_position == len(payload))
                pipe.send_text('\x1b[5~')
                await self.wait_until(lambda: editor._draft.buffer.cursor_position < len(payload))
                self.assertEqual(editor.draft, payload)
                self.assertEqual(calls, [])
                pipe.send_text('\x03\x03')
                await asyncio.wait_for(task, 2)
            finally:
                if not task.done():
                    pipe.send_text('\x03\x03')
                    await asyncio.wait_for(task, 2)

    async def test_unsubmitted_busy_draft_never_becomes_a_persistent_turn(self):
        from seekra.terminal.cli import run_question
        from seekra.search.state import SearchSession as Session
        calls = []
        release = threading.Event()
        def response(request):
            calls.append(json.loads(request.content)['messages'][-1]['content'])
            release.wait(3)
            chunk = {'choices':[{'index':0,'delta':{'role':'assistant','content':'答复'},'finish_reason':'stop'}]}
            return httpx.Response(200, headers={'content-type':'text/event-stream'},
                                  content='data: '+json.dumps(chunk)+'\n\ndata: [DONE]\n\n')
        with tempfile.TemporaryDirectory() as directory, create_pipe_input() as pipe, \
             httpx.Client(transport=httpx.MockTransport(response)) as client, redirect_stdout(io.StringIO()):
            session = Session.create(Path(directory))
            config = {'DEEPSEEK_API_KEY':'fake-ds','DEEPSEEK_MODEL':'test','TAVILY_API_KEY':'fake-tv'}
            def handle(text, cancel):
                if text == '/exit':
                    return 0
                run_question(text,client=client,config=config,session=session,
                             verbose=False,cancel=cancel)
            editor = ReplInput(input=pipe,output=DummyOutput())
            task = asyncio.create_task(editor.run_async(handle))
            try:
                pipe.send_text('\x1b[200~第一问\n    多行\n\x1b[201~')
                await self.wait_until(lambda: editor.draft.endswith('多行\n'))
                self.assertEqual(calls, [])
                self.assertEqual(len(session.runs), 0)
                pipe.send_text('\r')
                await self.wait_until(lambda: len(calls) == 1)
                pipe.send_text('\x1b[200~第二问\n还没发送\n\x1b[201~\r')
                await self.wait_until(lambda: editor.draft.endswith('还没发送\n'))
                self.assertEqual(len(session.runs), 1)
                release.set()
                await self.wait_until(lambda: not editor.busy)
                self.assertEqual(len(calls), 1)
                self.assertEqual(len(Session.load(session.path).runs), 1)
                pipe.send_text('\x03')  # 清空未发送草稿。
                await self.wait_until(lambda: not editor.draft)
                pipe.send_text('/exit\r')
                self.assertEqual(await asyncio.wait_for(task, 2), 0)
            finally:
                release.set()
                if not task.done():
                    pipe.send_text('\x03\x03')
                    await asyncio.wait_for(task, 4)

    async def wait_until(self, predicate):
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(.01)
        self.fail('输入状态没有按预期变化')

    async def test_split_paste_and_busy_draft_never_auto_submit(self):
        self.assertTrue(hasattr(ReplInput, 'run_async'), '需要持续接管输入生命周期')
        calls = []
        release = threading.Event()
        def handle(text, cancel):
            if text == '/exit':
                return 0
            calls.append(text)
            release.wait(3)
        with create_pipe_input() as pipe:
            output = PasteOutput()
            editor = ReplInput(input=pipe, output=output)
            task = asyncio.create_task(editor.run_async(handle))
            try:
                await self.wait_until(lambda: output.paste_enabled)
                payload = ('中文\n\n    缩进\n/new\n' * 200) + '末尾\n'
                for part in ['\x1b[200~', payload[:1000], payload[1000:], '\x1b[20', '1~']:
                    pipe.send_text(part)
                    await asyncio.sleep(.03)
                    self.assertEqual(calls, [])
                self.assertEqual(editor.draft, payload)
                pipe.send_text('\r')
                await self.wait_until(lambda: len(calls) == 1)
                self.assertEqual(calls, [payload])
                for part in ['\x1b[200~下一问\n', '/exit\n', '尾部\n\x1b[201~']:
                    pipe.send_text(part)
                    await asyncio.sleep(.03)
                    self.assertTrue(output.paste_enabled)
                    self.assertEqual(len(calls), 1)
                pipe.send_text('补充\n内容\r')
                await asyncio.sleep(.03)
                draft = '下一问\n/exit\n尾部\n补充\n内容'
                self.assertEqual(editor.draft, draft)
                self.assertEqual(len(calls), 1)
                release.set()
                await self.wait_until(lambda: not editor.busy)
                await asyncio.sleep(.03)
                self.assertEqual(len(calls), 1)
                pipe.send_text('\r')
                await self.wait_until(lambda: len(calls) == 2 and not editor.busy)
                self.assertEqual(calls[1], draft)
                pipe.send_text('/exit\r')
                self.assertEqual(await asyncio.wait_for(task, 2), 0)
            finally:
                release.set()
                if not task.done():
                    pipe.send_text('\x03\x03')
                    await asyncio.wait_for(task, 4)

    async def test_newline_key_sequences_do_not_submit(self):
        self.assertTrue(hasattr(ReplInput, 'run_async'))
        calls = []
        def handle(text, cancel):
            calls.append(text)
            return 0
        with create_pipe_input() as pipe:
            editor = ReplInput(input=pipe, output=DummyOutput())
            task = asyncio.create_task(editor.run_async(handle))
            try:
                for sequence in ['a\n', 'b\x1b[13;2u', 'c\x1b[13;5u', 'd\x1b[27;2;13~']:
                    pipe.send_text(sequence)
                    await asyncio.sleep(.03)
                    self.assertEqual(calls, [])
                pipe.send_text('e\r')
                await asyncio.wait_for(task, 2)
                self.assertEqual(calls, ['a\nb\nc\nd\ne'])
            finally:
                if not task.done():
                    pipe.send_text('\x03\x03')
                    await asyncio.wait_for(task, 2)
