"""Windows 原生键盘协议：保留修饰键，同时保留 bracketed paste。

协议：microsoft/terminal doc/specs/#4999 - Improved keyboard handling in Conpty.md
"""
from contextlib import contextmanager
import re
import sys

from prompt_toolkit.input.win32 import (
    Win32Input, ConsoleInputReader, Vt100ConsoleInputReader, KEY_EVENT_RECORD,
)
from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.key_binding import KeyPress
from prompt_toolkit.keys import Keys
from prompt_toolkit.output.defaults import create_output


class ReplConsoleReader(ConsoleInputReader):
    def _event_to_key_presses(self, event):
        if event.KeyDown and event.VirtualKeyCode == 13 and event.ControlKeyState & 0x1c:
            return [KeyPress(Keys.ControlJ, '\n')]
        return super()._event_to_key_presses(event)


class Win32KeyParser:
    """解码 CSI Vk;Sc;Uc;Kd;Cs;Rc_，再由 VT 解析器处理文字及粘贴边界。"""
    _event = re.compile(r'^\x1b\[([\d;]*)_')
    _prefix = re.compile(r'^\x1b\[[\d;]*$')

    def __init__(self, callback):
        self._vt = Vt100Parser(callback)
        self._mapper = object.__new__(ReplConsoleReader)
        self._pending = ''
        self._surrogate = ''

    def _text(self, text):
        for char in text:
            if self._surrogate:
                if '\udc00' <= char <= '\udfff':
                    char = chr(0x10000 + ((ord(self._surrogate)-0xd800) << 10) + ord(char)-0xdc00)
                else:
                    self._vt.feed('\ufffd')
                self._surrogate = ''
            if '\ud800' <= char <= '\udbff':
                self._surrogate = char
            else:
                self._vt.feed(char)

    def feed(self, text):
        self._pending += text
        while self._pending:
            match = self._event.match(self._pending)
            if match:
                fields = match[1].split(';')
                values = [int(value) if value else (1 if index == 5 else 0)
                          for index, value in enumerate(fields)]
                values += [0] * max(0, 5-len(values))
                if len(values) == 5:
                    values.append(1)
                if len(values) == 6 and values[2] <= 65535 and values[3] == 1:
                    vk, scan, char, down, modifiers, repeats = values
                    event = KEY_EVENT_RECORD()
                    event.KeyDown, event.VirtualKeyCode, event.VirtualScanCode = down, vk, scan
                    event.uChar.UnicodeChar, event.ControlKeyState = chr(char), modifiers
                    for _ in range(repeats):
                        for key in self._mapper._event_to_key_presses(event):
                            self._text(self._mapper._insert_key_data(key).data)
                self._pending = self._pending[match.end():]
            elif (self._pending in ('\x1b', '\x1b[') or self._prefix.fullmatch(self._pending)) and len(self._pending) < 100:
                break
            else:
                self._text(self._pending[0])
                self._pending = self._pending[1:]

    def flush(self):
        if self._pending:
            self._text(self._pending)
            self._pending = ''
        self._vt.flush()


class ReplVtReader(Vt100ConsoleInputReader):
    def __init__(self):
        super().__init__()
        self._vt100_parser = Win32KeyParser(lambda key: self._buffer.append(key))


class WindowsReplInput(Win32Input):
    def __init__(self, stdin=None, output=None):
        super().__init__(stdin)
        self._output = output if output is not None else create_output(stdout=sys.__stderr__)
        replacement = ReplVtReader() if self._use_virtual_terminal_input else ReplConsoleReader(recognize_paste=False)
        self.console_input_reader.close()
        self.console_input_reader = replacement

    @contextmanager
    def raw_mode(self):
        with super().raw_mode():
            if self._use_virtual_terminal_input:
                self._output.write_raw('\x1b[?9001h')
                self._output.flush()
            try:
                yield
            finally:
                if self._use_virtual_terminal_input:
                    self._output.write_raw('\x1b[?9001l')
                    self._output.flush()
