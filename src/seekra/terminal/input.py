"""持续多行输入：编辑器在工具运行期间也接管粘贴，只有明确发送才创建任务。"""
import asyncio
from contextlib import redirect_stdout, redirect_stderr
import os
import sys
from bisect import bisect_right

from prompt_toolkit import Application
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window, Dimension
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output.defaults import create_output
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea
from prompt_toolkit.utils import get_cwidth

from seekra.harness.runtime import CancellationToken

INPUT_HELP = 'Enter 发送 · Shift+Enter 换行 · 滚轮翻看 · /help 帮助'
MODIFIED_ENTER = {'\x1b[27;2;13~', '\x1b[27;5;13~'}


class _ScrollWindow(Window):
    """按显示行滚动，兼容一条逻辑长行折成多屏的输入。"""
    def scroll_lines(self, count):
        info = self.render_info
        if info is None or not info.visible_line_to_row_col:
            return
        buffer = self.content.buffer
        width = max(1, info.window_width)
        starts, column = [0], 0
        for index, char in enumerate(buffer.text):
            if char == '\n':
                starts.append(index + 1)
                column = 0
            else:
                cells = (8 - column % 8) if char == '\t' else get_cwidth(char)
                if column + cells > width:
                    starts.append(index)
                    column = 0
                column += cells
        visible = info.visible_line_to_row_col
        row, col = visible[min(visible) if count < 0 else max(visible)]
        processed = self.content._last_get_processed_line(row)
        anchor = buffer.document.translate_row_col_to_index(row, processed.display_to_source(col))
        target = max(0, min(len(starts)-1, bisect_right(starts, anchor)-1 + count))
        buffer.cursor_position = len(buffer.text) if count > 0 and target == len(starts)-1 else starts[target]

    def _scroll_up(self):
        self.scroll_lines(-3)

    def _scroll_down(self):
        self.scroll_lines(3)


def _text_area(**kwargs):
    area = TextArea(**kwargs)
    window = area.window
    area.window = _ScrollWindow(content=area.control, height=window.height, width=window.width,
                                style=window.style, wrap_lines=window.wrap_lines,
                                left_margins=window.left_margins, right_margins=window.right_margins)
    return area


class _TranscriptOutput:
    """把工作线程的文本送给编辑器，重定向 stdout 时仍保留正文文件输出。"""
    encoding = 'utf-8'

    def __init__(self, emit, forward=None):
        self.emit, self.forward = emit, forward

    def write(self, text):
        if self.forward is not None:
            self.forward.write(text)
        self.emit(text)
        return len(text)

    def flush(self):
        if self.forward is not None:
            self.forward.flush()

    def isatty(self):
        return False


class ReplInput:
    def __init__(self, *, input=None, output=None, stdin=None):
        self._stdin = stdin if stdin is not None else sys.stdin
        self._input = input
        self._output = output
        self._interactive = input is not None or self._stdin.isatty()
        self._read_pipe = False
        self._draft = None
        self.busy = False
        self._cancel = None
        self._worker = None
        self._notice = ''
        self._log = None
        self._pending_log = []
        self._flush_handle = None
        self._app = None
        self._loop = None
        self.session_id = ''
        self._initial_transcript = ''
        self._history_trimmed = False

    def set_conversation(self, session_id, transcript):
        """初次加载或串行命令切换会话；界面修改在事件循环线程中执行。"""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._replace_conversation, session_id, transcript)
        else:
            self.session_id = session_id
            self._history_trimmed = len(transcript) > 200000
            self._initial_transcript = transcript[-200000:]
            if not self._interactive and transcript:
                print(self._initial_transcript, file=sys.stderr)

    def _replace_conversation(self, session_id, transcript):
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        self._pending_log.clear()
        self.session_id = session_id
        self._history_trimmed = len(transcript) > 200000
        text = transcript[-200000:]
        self._log.buffer.set_document(Document(text, len(text)), bypass_readonly=True)
        self._log.window.vertical_scroll = 0
        self._app.invalidate()

    @property
    def draft(self):
        return self._draft.text if self._draft is not None else ''

    def read(self):
        """非交互输入以 EOF 为唯一边界，不逐行调用模型。"""
        if self._read_pipe:
            raise EOFError()
        self._read_pipe = True
        text = self._stdin.read()
        if not text:
            raise EOFError()
        return text

    def run(self, handle):
        """handle(text, cancel) 返回 None 表示继续，整数表示退出码。"""
        if self._interactive:
            return asyncio.run(self.run_async(handle))
        while True:
            try:
                text = self.read()
            except (EOFError, KeyboardInterrupt):
                return 0
            result = handle(text, CancellationToken())
            if result is not None:
                return result

    def _flush_log(self):
        if self._flush_handle is not None:
            self._flush_handle.cancel()
        self._flush_handle = None
        old = self._log.text
        cursor = self._log.buffer.cursor_position
        follow = cursor == len(old)
        text = old + ''.join(self._pending_log)
        self._pending_log.clear()
        # 仅限制屏幕滚动区；模型上下文和持久记录不被修改。
        removed = max(0, len(text)-200000)
        self._history_trimmed = self._history_trimmed or removed > 0
        text = text[removed:]
        position = len(text) if follow else max(0, cursor-removed)
        self._log.window.vertical_scroll = max(0, self._log.window.vertical_scroll-old[:removed].count('\n'))
        self._log.buffer.set_document(Document(text, position), bypass_readonly=True)
        self._app.invalidate()

    def _append_log(self, text):
        self._pending_log.append(text)
        if self._flush_handle is None:
            self._flush_handle = asyncio.get_running_loop().call_later(.03, self._flush_log)

    async def run_async(self, handle):
        loop = asyncio.get_running_loop()
        self._loop = loop
        output = self._output if self._output is not None else create_output(stdout=sys.stderr)
        owned_input = None
        if self._input is None and os.name == 'nt':
            from seekra.terminal.windows_input import WindowsReplInput
            owned_input = WindowsReplInput(self._stdin, output=output)
        self._log = _text_area(text=self._initial_transcript, read_only=True, scrollbar=True,
                             height=Dimension(min=3, preferred=12), focusable=False)
        self._log.buffer.cursor_position = len(self._initial_transcript)
        self._draft = _text_area(multiline=True, wrap_lines=True, scrollbar=True,
                               height=Dimension(min=3, preferred=4, max=8))
        keys = KeyBindings()
        @keys.add('pageup')
        @keys.add('pagedown')
        def page(event):
            window = event.app.layout.current_window
            if isinstance(window, _ScrollWindow) and window.render_info:
                direction = -1 if event.key_sequence[-1].key == 'pageup' else 1
                window.scroll_lines(direction * window.render_info.window_height)

        @keys.add('c-home')
        def beginning(event):
            event.current_buffer.cursor_position = 0

        @keys.add('c-end')
        def end(event):
            event.current_buffer.cursor_position = len(event.current_buffer.text)

        async def work(text):
            try:
                result = await asyncio.to_thread(handle, text, self._cancel)
                if result is not None:
                    self._flush_log()
                    self._app.exit(result=result)
            except Exception as exc:
                self._append_log(f'\n这次操作未能完成（{type(exc).__name__}）。\n')
            finally:
                self.busy = False
                self._notice = ''
                self._app.invalidate()

        @keys.add('enter')
        def submit(event):
            if event.data in MODIFIED_ENTER:
                event.current_buffer.insert_text('\n')
                return
            if self.busy:
                self._notice = '这条消息还没发送。等当前回答结束后，再按 Enter。'
                return
            text = self.draft
            if not text.strip():
                return
            self.busy = True
            self._cancel = CancellationToken()
            self._draft.buffer.reset()
            self._append_log('\n── 你 ──\n' + text + '\n')
            self._worker = asyncio.create_task(work(text))

        @keys.add('c-j')
        @keys.add('escape', '[', '1', '3', ';', '2', 'u')
        @keys.add('escape', '[', '1', '3', ';', '5', 'u')
        def newline(event):
            event.current_buffer.insert_text('\n')

        @keys.add('c-c')
        def cancel_or_exit(event):
            if self.busy:
                self._notice = '正在停止…'
                asyncio.create_task(asyncio.to_thread(self._cancel.cancel))
            elif self.draft:
                self._draft.buffer.reset()
                self._notice = '输入已清空。再按 Ctrl+C 退出。'
            else:
                event.app.exit(result=0)

        @keys.add('c-d')
        def delete_or_exit(event):
            if not self.draft and not self.busy:
                event.app.exit(result=0)
            else:
                event.current_buffer.delete()

        def status():
            state = '正在处理' if self.busy else ('尚未发送' if self.draft else '等待输入')
            trimmed = ' · 更早内容未显示' if self._history_trimmed else ''
            return f'对话 {self.session_id[:8]} · {state}{trimmed}  {self._notice}'

        layout = HSplit([
            Frame(self._log, title=lambda: f'Seekra · 对话 {self.session_id[:8]}'),
            Frame(self._draft, title='你'),
            Window(FormattedTextControl(status), height=1, style='class:status'),
            Window(FormattedTextControl(INPUT_HELP), height=1, style='class:help'),
        ])
        self._app = Application(
            layout=Layout(layout, focused_element=self._draft),
            key_bindings=keys, input=self._input or owned_input, output=output,
            mouse_support=True,
            full_screen=True, erase_when_done=False,
            style=Style.from_dict({'frame.label':'bold ansicyan','status':'reverse','help':'ansibrightblack'}),
        )
        stdout = sys.stdout
        emit = lambda text: loop.call_soon_threadsafe(self._append_log, text)
        try:
            with redirect_stdout(_TranscriptOutput(emit, None if stdout.isatty() else stdout)), \
                 redirect_stderr(_TranscriptOutput(emit)):
                try:
                    return await self._app.run_async()
                except EOFError:
                    return 0
                finally:
                    if self._worker is not None and not self._worker.done():
                        await asyncio.to_thread(self._cancel.cancel)
                        await self._worker
        finally:
            self._loop = None
            if self._flush_handle is not None:
                self._flush_handle.cancel()
                self._flush_handle = None
            if owned_input is not None:
                owned_input.close()
