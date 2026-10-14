"""使用模拟 HTTP 验证跨输入历史、成功保存、失败回退和会话隔离。"""

from copy import deepcopy
from datetime import date
import importlib.util
import json
import unittest
from unittest.mock import patch

import httpx

from seekra.agent import run_agent
from test_agent import call, reply


class SessionTests(unittest.TestCase):
    """会话只保存已完成输入的完整消息历史。"""

    def setUp(self):
        """为每项测试建立独立会话。"""
        self.assertIsNotNone(importlib.util.find_spec("seekra.session"), "尚未实现 session.py")
        from seekra.session import Session
        self.session = Session()

    def ask(self, question, handler, session=None):
        """运行真实闭环，仅替换 HTTP 请求传输。"""
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return run_agent(question, client=client, model="test", deepseek_api_key="fake-ds",
                             tavily_api_key="fake-tv",
                             session=self.session if session is None else session, thinking=False)

    def test_followup_keeps_tools_and_answer_without_duplicates(self):
        """第二次输入收到完整旧历史，并更新唯一系统提示词。"""
        def first(request):
            if request.url.host == "api.tavily.com":
                return httpx.Response(200, json={"results": [{"title": "来源",
                    "url": "https://example.org", "content": "功能 A 的证据"}]})
            if len(json.loads(request.content)["messages"]) == 2:
                return reply(calls=[call("tavily_search", {"query": "功能 A"})])
            return reply("第一项是功能 A")
        self.ask("有哪些功能？", first)
        saved = deepcopy(self.session.messages)
        self.assertEqual(len(saved), 5)

        def second(request):
            messages = json.loads(request.content)["messages"]
            self.assertEqual(messages[1:-1], saved[1:])
            self.assertEqual(messages[-1]["content"], "第一项呢？")
            self.assertEqual(sum(m["role"] == "system" for m in messages), 1)
            self.assertIn("2030-01-02", messages[0]["content"])
            self.assertIn("1 次", messages[0]["content"])
            self.assertEqual(self.session.messages, saved)
            return reply("功能 A 的详情")
        with patch("seekra.agent.date") as today:
            today.today.return_value = date(2030, 1, 2)
            self.assertEqual(self.ask("第一项呢？", second), "功能 A 的详情")
        self.assertEqual(len(self.session.messages), 7)
        self.assertEqual(self.session.messages[-1]["content"], "功能 A 的详情")

    def test_failed_or_interrupted_turn_leaves_previous_history_unchanged(self):
        """工具后失败、截断、空答案和中断均不保存临时消息。"""
        self.ask("已完成问题", lambda request: reply("已完成答案"))
        saved = deepcopy(self.session.messages)
        for mode in ("http", "interrupt", "truncated", "empty"):
            with self.subTest(mode=mode):
                calls = []
                def handler(request):
                    calls.append(request)
                    if len(calls) == 1:
                        return reply(calls=[call("list_documents", {})])
                    if mode == "http":
                        return httpx.Response(500)
                    if mode == "interrupt":
                        raise KeyboardInterrupt()
                    return reply("半句话", finish="length") if mode == "truncated" else reply(None)
                with self.assertRaises((httpx.HTTPStatusError, RuntimeError, KeyboardInterrupt)):
                    self.ask("失败问题", handler)
                self.assertEqual(self.session.messages, saved)
        self.ask("继续提问", lambda request: reply("恢复成功"))
        self.assertEqual(len(self.session.messages), 5)

    def test_clear_and_new_session_are_isolated(self):
        """清空后的会话和新建会话均不继承旧内容。"""
        from seekra.session import Session
        self.ask("旧问题", lambda request: reply("旧答案"))
        fresh = Session()
        self.assertEqual(fresh.messages, [])
        self.session.clear()
        self.assertEqual(self.session.messages, [])
        def handler(request):
            self.assertEqual(len(json.loads(request.content)["messages"]), 2)
            return reply("新答案")
        self.ask("新问题", handler)
        self.assertEqual(fresh.messages, [])
        self.ask("独立问题", handler, session=fresh)
        self.assertEqual(self.session.messages[1]["content"], "新问题")

    def test_tool_error_is_saved_when_model_finishes_normally(self):
        """工具错误可作为正常研究历史保存，不等同于整个输入失败。"""
        def handler(request):
            messages = json.loads(request.content)["messages"]
            if len(messages) == 2:
                return reply(calls=[call("unknown", {})])
            return reply("无法获取资料")
        self.ask("问题", handler)
        self.assertIn("error", json.loads(self.session.messages[-2]["content"]))
        self.assertEqual(self.session.messages[-1]["content"], "无法获取资料")
