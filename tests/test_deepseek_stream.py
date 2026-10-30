"""验证无工具 SSE 的真实增量、协议边界及连接释放，不访问网络。"""

import importlib.util
import json
import unittest

import httpx


def event(content=None, finish=None, **delta):
    """生成一个完整 SSE 事件。"""
    if content is not None:
        delta["content"] = content
    return ("data: " + json.dumps({"choices": [{"index": 0, "delta": delta,
            "finish_reason": finish}]}, ensure_ascii=False) + "\n\n").encode()


DONE = b"data: [DONE]\n\n"


class ByteStream(httpx.SyncByteStream):
    """可在实际消费位置插入断言/异常，并记录关闭。"""

    def __init__(self, items):
        self.items = items
        self.closed = False

    def __iter__(self):
        for item in self.items:
            if callable(item):
                item()
            elif isinstance(item, BaseException):
                raise item
            else:
                yield item

    def close(self):
        self.closed = True


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("seekra.providers.deepseek_stream"), "需要实现 deepseek.py")
        from seekra.providers.deepseek_stream import stream_chat_text
        self.receive = stream_chat_text

    def ask(self, items, callback=None, status=200, media="text/event-stream", messages=None):
        stream = ByteStream(items)
        self.stream = stream
        requests = []
        def handler(request):
            requests.append(request)
            body = json.loads(request.content)
            self.assertTrue(body["stream"])
            self.assertNotIn("tools", body)
            self.assertNotIn("tool_choice", body)
            self.assertEqual(body["thinking"], {"type": "disabled"})
            self.assertEqual(body["model"], "test-model")
            return httpx.Response(status, headers={"content-type": media}, stream=stream)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            try:
                return self.receive(client=client, model="test-model", api_key="fake",
                                    messages=messages if messages is not None else [{"role": "user", "content": "问题"}],
                                    on_content=callback)
            finally:
                self.assertLessEqual(len(requests), 1, "不能自动重试")

    def test_callback_before_next_fragment_and_no_message_mutation(self):
        shown = []
        messages = [{"role": "user", "content": "问题"}]
        def checkpoint():
            self.assertEqual(shown, ["你好"])
        result = self.ask([event("你好"), checkpoint, event("\n  世界 "),
                           event(finish="stop"), DONE], shown.append, messages=messages)
        self.assertEqual(result, "你好\n  世界 ")
        self.assertEqual("".join(shown), result)
        self.assertEqual(messages, [{"role": "user", "content": "问题"}])
        self.assertTrue(self.stream.closed)

    def test_utf8_byte_splits_crlf_multiline_data_and_metadata(self):
        multiline = 'data: {"choices":\r\ndata: [{"index":0,"delta":{"content":"中文"},"finish_reason":null}]}\r\n\r\n'
        raw = b": heartbeat\r\n\r\n" + event(role="assistant") + multiline.encode() + event("", reasoning_content="不显示")
        raw += event("！", finish="stop")
        raw += b'data: {"choices":[],"usage":{"completion_tokens":2}}\n\n' + DONE
        shown = []
        self.assertEqual(self.ask([bytes([b]) for b in raw], shown.append), "中文！")
        self.assertEqual(shown, ["中文", "！"])
        self.assertTrue(self.stream.closed)

    def test_incomplete_or_invalid_streams_fail_closed(self):
        cases = {
            "missing_done": [event("半句", "stop")],
            "missing_finish": [event("半句"), DONE],
            "unfinished_event": [event("正文", "stop"), b"data: [DONE]"],
            "empty": [event(finish="stop"), DONE],
            "whitespace": [event(" \n", "stop"), DONE],
            "length": [event("半句", "length"), DONE],
            "tool_end": [event("半句", "tool_calls"), DONE],
            "filter": [event(finish="content_filter"), DONE],
            "resource": [event(finish="insufficient_system_resource"), DONE],
            "after_finish": [event("一", "stop"), event("二"), DONE],
            "duplicate_finish": [event("一", "stop"), event(finish="stop"), DONE],
            "json": [b"data: SECRET-not-json\n\n"],
            "error": [b'data: {"error":{"message":"SECRET"}}\n\n'],
            "array": [b"data: []\n\n"],
            "empty_choices": [b'data: {"choices":[]}\n\n'],
            "wrong_content": [event(["SECRET"])],
            "wrong_role": [event("正文", role="tool")],
            "tool": [event(tool_calls=[{"index": 0, "function": {"arguments": "SECRET"}}])],
            "legacy_tool": [event(function_call={"name": "SECRET"})],
            "choice": [b'data: {"choices":[{"index":1,"delta":{},"finish_reason":null}]}\n\n'],
            "bool_index": [b'data: {"choices":[{"index":false,"delta":{},"finish_reason":null}]}\n\n'],
            "reason_only": [event(finish="stop", reasoning_content="SECRET"), DONE],
        }
        for name, chunks in cases.items():
            with self.subTest(name=name), self.assertRaises(RuntimeError) as caught:
                self.ask(chunks)
            self.assertNotIn("SECRET", str(caught.exception))
            self.assertTrue(self.stream.closed)

    def test_http_and_media_errors_close_without_body_disclosure(self):
        for status, media, exception in [(401, "application/json", httpx.HTTPStatusError),
                                         (200, "application/json", RuntimeError)]:
            with self.subTest(status=status), self.assertRaises(exception):
                self.ask([b"SECRET"], status=status, media=media)
            self.assertTrue(self.stream.closed)

    def test_read_interrupt_and_callback_errors_close(self):
        for exception in [httpx.ReadError("test"), httpx.ReadTimeout("test"), KeyboardInterrupt()]:
            with self.subTest(exception=type(exception)), self.assertRaises(type(exception)):
                self.ask([event("已收到"), exception])
            self.assertTrue(self.stream.closed)
        def broken(text):
            raise BrokenPipeError("test")
        with self.assertRaises(BrokenPipeError):
            self.ask([event("正文"), event(finish="stop"), DONE], broken)
        self.assertTrue(self.stream.closed)

    def test_rejects_tool_history_before_network(self):
        for messages in [[{"role": "tool", "content": "结果"}],
                         [{"role": "assistant", "tool_calls": [{"id": "x"}]}]]:
            with self.assertRaises(ValueError):
                self.ask([], messages=messages)

    def test_no_callback_and_charset(self):
        self.assertEqual(self.ask([event("正文", "stop"), DONE],
                                 media="text/event-stream; charset=utf-8"), "正文")


if __name__ == "__main__":
    unittest.main()
