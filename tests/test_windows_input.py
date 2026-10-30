"""Windows 原生按键记录的转换；不以 VT 字符串冒充物理组合键。"""
import sys
import unittest


@unittest.skipUnless(sys.platform == 'win32', 'Windows console input')
class WindowsInputTests(unittest.TestCase):
    def test_encoded_events_across_chunks_keep_paste_unicode_and_navigation(self):
        from seekra.terminal.windows_input import Win32KeyParser
        from prompt_toolkit.keys import Keys
        keys = []
        parser = Win32KeyParser(keys.append)
        wire = ('\x1b[13;28;13;1;16;1_'  # Shift+Enter
                '\x1b[13;28;13;0;16;1_'  # release: ignored
                '\x1b[36;71;0;1;8;1_'   # Ctrl+Home
                '\x1b[0;0;55357;1;0;1_\x1b[0;0;56832;1;0;1_')
        text = '\x1b[200~中文\n    /exit\n\x1b[201~'
        wire += ''.join(f'\x1b[0;0;{ord(char)};1;0;1_' for char in text)
        wire += '\x1b[13;28;13;1;0;1_'
        for char in wire:
            parser.feed(char)
        self.assertEqual([key.key for key in keys], [Keys.ControlJ, Keys.ControlHome, '😀', Keys.BracketedPaste, Keys.ControlM])
        self.assertEqual(keys[-2].data, '中文\n    /exit\n')

    def test_modified_enter_survives_actual_console_read_and_mode_is_restored(self):
        import ctypes
        from ctypes.wintypes import DWORD, HANDLE
        from prompt_toolkit.input.win32 import INPUT_RECORD
        from prompt_toolkit.keys import Keys
        from seekra.terminal.windows_input import WindowsReplInput
        handle = HANDLE(ctypes.windll.kernel32.GetStdHandle(-10))
        original = DWORD()
        if not ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(original)):
            self.skipTest('需要独立 Windows 控制台；使用 PTY 运行本测试')
        reader = WindowsReplInput()
        try:
            with reader.raw_mode():
                for modifiers, expected in ((16, Keys.ControlJ), (8, Keys.ControlJ), (0, Keys.ControlM)):
                    records = (INPUT_RECORD * 1)()
                    records[0].EventType = 1
                    event = records[0].Event.KeyEvent
                    event.KeyDown = True
                    event.RepeatCount = 1
                    event.VirtualKeyCode = 13
                    event.VirtualScanCode = 28
                    event.uChar.UnicodeChar = '\r'
                    event.ControlKeyState = modifiers
                    written = DWORD()
                    self.assertTrue(ctypes.windll.kernel32.WriteConsoleInputW(reader.handle, records, 1, ctypes.byref(written)))
                    self.assertEqual([key.key for key in reader.read_keys()], [expected])
                # 分批粘贴走同一控制台读取链，换行和命令文本都保持为一次粘贴。
                for text in ('\x1b[200~中文\n    缩进\n', '/exit\n\x1b[201~'):
                    records = (INPUT_RECORD * len(text))()
                    for index, char in enumerate(text):
                        records[index].EventType = 1
                        event = records[index].Event.KeyEvent
                        event.KeyDown = True
                        event.RepeatCount = 1
                        event.uChar.UnicodeChar = char
                    written = DWORD()
                    self.assertTrue(ctypes.windll.kernel32.WriteConsoleInputW(reader.handle, records, len(text), ctypes.byref(written)))
                    keys = reader.read_keys()
                    if text.startswith('\x1b[200~'):
                        self.assertEqual(keys, [])
                    else:
                        self.assertEqual([k.key for k in keys], [Keys.BracketedPaste])
                        self.assertEqual(keys[0].data, '中文\n    缩进\n/exit\n')
            restored = DWORD()
            self.assertTrue(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(restored)))
            self.assertEqual(restored.value, original.value)
        finally:
            reader.close()

    def test_modified_return_preserves_newline(self):
        from prompt_toolkit.input.win32 import KEY_EVENT_RECORD
        from prompt_toolkit.keys import Keys
        from seekra.terminal.windows_input import ReplConsoleReader
        for modifier in (0x10, 0x08, 0x04):
            event = KEY_EVENT_RECORD()
            event.KeyDown = True
            event.VirtualKeyCode = 13
            event.ControlKeyState = modifier
            event.uChar.UnicodeChar = '\r'
            native = object.__new__(ReplConsoleReader)
            self.assertEqual(native._event_to_key_presses(event)[0].key, Keys.ControlJ)
            # 未修饰 Enter 仍为发送键；粘贴记录不冒充组合键。
            event.ControlKeyState = 0
            self.assertEqual(native._event_to_key_presses(event)[0].key, Keys.ControlM)
            event.VirtualKeyCode = 0
            event.ControlKeyState = modifier
            self.assertEqual(native._event_to_key_presses(event)[0].key, Keys.ControlM)
