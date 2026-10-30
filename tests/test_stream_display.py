"""验证正文增量输出与换行，不把 stdout 故障当成可忽略的调试错误。"""

import io
import unittest
from contextlib import redirect_stdout

from seekra.terminal import display


class Output(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flushes = 0

    def flush(self):
        self.flushes += 1


class StreamDisplayTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(display, "TextStreamWriter"), "需要实现正文 writer")

    def test_immediate_flush_and_idempotent_finish(self):
        out = Output()
        with redirect_stdout(out):
            writer = display.TextStreamWriter()
            writer.write("你好")
            self.assertEqual(out.getvalue(), "你好")
            self.assertEqual(out.flushes, 1)
            writer.write("\n  世界")
            self.assertEqual(out.flushes, 2)
            writer.finish()
            writer.finish()
        self.assertEqual(out.getvalue(), "你好\n  世界\n")

    def test_empty_or_already_newline_has_no_extra_output(self):
        for content in ("", "正文\n"):
            out = Output()
            with redirect_stdout(out):
                writer = display.TextStreamWriter()
                writer.write(content)
                writer.finish()
                writer.finish()
            self.assertEqual(out.getvalue(), content)

    def test_write_flush_and_finish_errors_propagate(self):
        class Broken(Output):
            def write(self, text):
                raise BrokenPipeError()
        class BrokenFlush(Output):
            def flush(self):
                raise OSError()
        for out in (Broken(), BrokenFlush()):
            with redirect_stdout(out), self.assertRaises(OSError):
                display.TextStreamWriter().write("正文")
        out = Broken()
        with redirect_stdout(Output()):
            writer = display.TextStreamWriter()
            writer.write("正文")
        # writer 绑定构造时的输出流，下面通过已绑定对象制造结束时故障。
        class FinishBroken(Output):
            def write(self, text):
                if text == "\n":
                    raise OSError()
                return super().write(text)
        with redirect_stdout(FinishBroken()):
            writer = display.TextStreamWriter()
            writer.write("正文")
            with self.assertRaises(OSError):
                writer.finish()
