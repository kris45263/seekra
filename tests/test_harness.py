"""验证页面获取、统一结果、预算和显式研究笔记的协作。"""

from copy import deepcopy
import importlib.util
import json
import io
from contextlib import redirect_stderr
import unittest

import httpx

from seekra.display import AgentDisplay

from seekra.agent import run_agent
from seekra.session import Session
from test_agent import call, reply


class HarnessTests(unittest.TestCase):
    """只替换网络传输，观察真实消息与工具结果。"""

    def test_read_page_and_budget_are_visible(self):
        """正文、请求地址和预算进入模型上下文；完整答案才提交会话。"""
        history = []
        def handler(request):
            body = json.loads(request.content)
            if request.url.path == "/extract":
                self.assertEqual(body["urls"], ["https://example.org/v1"])
                return httpx.Response(200, json={"results": [{"url": "https://example.org/v1",
                    "title": "文档", "raw_content": "仅在条件 A 下成立"}], "failed_results": []})
            history.append(body["messages"])
            self.assertIn("read_page", [t["function"]["name"] for t in body["tools"]])
            if len(history) == 1:
                self.assertIn("本次之后最多还能调用 1 次", body["messages"][0]["content"])
                return reply(calls=[call("read_page", {"url": "https://example.org/v1"})])
            result = json.loads(body["messages"][-1]["content"])
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["content_kind"], "extracted_text")
            self.assertEqual(result["content"], "仅在条件 A 下成立")
            self.assertIsNone(result["final_url"])
            self.assertIn("本次之后最多还能调用 0 次", body["messages"][0]["content"])
            return reply("有条件成立")
        session = Session()
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            self.assertEqual(run_agent("问题", client=client, model="m", deepseek_api_key="d",
                             tavily_api_key="t", session=session, max_iterations=2), "有条件成立")

    def test_page_ranges_reuse_snapshot(self):
        """长文本可继续读同一快照，不静默丢掉尾部或重复联网。"""
        self.assertIsNotNone(importlib.util.find_spec("seekra.retrieval"))
        from seekra.retrieval import read_page, PAGE_CHARS
        pages = {}
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"results": [{"url": "https://example.org",
                "raw_content": "甲" * PAGE_CHARS + "尾部证据"}], "failed_results": []})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            first = read_page("https://example.org", client=client, api_key="t", pages=pages)
            last = read_page("https://example.org", start=first["next_start"], client=client, api_key="t", pages=pages)
            self.assertEqual(first["status"], "partial")
            self.assertEqual(last["content"], "尾部证据")
            self.assertEqual(last["status"], "partial")
            self.assertIsNone(last["next_start"])
            self.assertEqual(first["snapshot_id"], last["snapshot_id"])
            self.assertEqual(len(requests), 1)

    def test_failed_reads_have_specific_status(self):
        """区分请求超时、提取失败、空内容和结构异常。"""
        self.assertIsNotNone(importlib.util.find_spec("seekra.retrieval"))
        from seekra.retrieval import read_page
        for mode, code in (("timeout", "timeout"), ("failed", "extraction_failed"),
                           ("empty", "empty_content"), ("malformed", "invalid_response")):
            with self.subTest(mode=mode):
                def handler(request):
                    if mode == "timeout":
                        raise httpx.ReadTimeout("模拟超时")
                    data = {"results": [], "failed_results": [{"url": "https://example.org", "error": "404"}]}
                    if mode == "empty":
                        data = {"results": [{"url": "https://example.org", "raw_content": " "}], "failed_results": []}
                    if mode == "malformed":
                        data = {"results": None}
                    return httpx.Response(200, json=data)
                with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                    result = read_page("https://example.org", client=client, api_key="t", pages={})
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error_code"], code)
                if mode == "failed":
                    self.assertEqual(result["reason_code"], "not_found")
                    self.assertIn("404", result["error"])

    def test_pages_rollback_with_messages(self):
        """读取快照也随当前问题回退，避免状态部分保存。"""
        session = Session()
        before = deepcopy(session)
        def handler(request):
            if request.url.path == "/extract":
                return httpx.Response(200, json={"results": [{"url": "https://example.org", "raw_content": "资料"}], "failed_results": []})
            return reply(calls=[call("read_page", {"url": "https://example.org"})])
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(RuntimeError):
                run_agent("问题", client=client, model="m", deepseek_api_key="d", tavily_api_key="t", session=session, max_iterations=1)
        self.assertEqual(session.messages, before.messages)
        self.assertEqual(session.pages, before.pages)
        self.assertTrue(session.records)  # 未完成输入的执行事实保留，不进入完成历史。
        self.assertEqual(session.pages, {})

    def test_structured_notes_are_optional_and_references_checked(self):
        """结构化模式检查操作引用，笔记不冒充外部证据。"""
        from seekra.tools import execute_tool
        args = {"goal": "目标", "observations": "已观察", "assessment": "仍不确定", "next_step": "继续查找", "references": ["missing"]}
        with httpx.Client(transport=httpx.MockTransport(lambda r: self.fail("笔记不得联网"))) as client:
            result = json.loads(execute_tool(call("think_tool", args), client=client, api_key="t", structured_think=True, known_operations={}))
            self.assertEqual(result["error_code"], "invalid_arguments")
            args["references"] = ["search1"]
            result = json.loads(execute_tool(call("think_tool", args), client=client, api_key="t", structured_think=True,
                                            known_operations={"search1": "tavily_search"}))
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["content_kind"], "model_note")
            self.assertEqual(result["note"], args)

    def test_read_display_does_not_change_requests(self):
        """读取模式的显示仍与模型输入分离，正文只在该次结果中展示。"""
        runs = []
        for verbose in (False, True):
            requests = []
            def handler(request):
                body = json.loads(request.content)
                requests.append(body)
                if request.url.path == "/extract":
                    return httpx.Response(200, json={"results": [{"url": "https://example.org",
                        "title": "标题", "raw_content": "独特正文"}], "failed_results": []})
                if len(body["messages"]) == 2:
                    return reply(calls=[call("read_page", {"url": "https://example.org"})])
                return reply("最终正文")
            err = io.StringIO()
            with httpx.Client(transport=httpx.MockTransport(handler)) as client, redirect_stderr(err):
                run_agent("问题", client=client, model="m", deepseek_api_key="fake-ds",
                          tavily_api_key="fake-tv", on_event=AgentDisplay(verbose).event)
            self.assertEqual(err.getvalue().count("独特正文"), int(verbose))
            self.assertNotIn("最终正文", err.getvalue())
            runs.append(requests)
        self.assertEqual(runs[0], runs[1])

    def test_invalid_page_requests_do_not_connect(self):
        """参数错误、内网字面地址和无快照续读不产生网络操作。"""
        from seekra.retrieval import read_page
        with httpx.Client(transport=httpx.MockTransport(lambda r: self.fail("不应联网"))) as client:
            for url, start in [("file:///tmp/a", 0), ("http://127.0.0.1", 0),
                               ("http://localhost", 0), ("https://user:pw@example.org", 0),
                               ("https://example.org", True), ("https://example.org", -1)]:
                with self.subTest(url=url, start=start):
                    self.assertEqual(read_page(url, start=start, client=client, api_key="t", pages={})["error_code"], "invalid_arguments")
            self.assertEqual(read_page("https://example.org", start=12, client=client, api_key="t", pages={})["error_code"], "missing_snapshot")

    def test_failed_refresh_removes_stale_page(self):
        """新读取失败时不能继续把旧快照当新响应；Ctrl+C 仍然传播。"""
        from seekra.retrieval import read_page
        pages = {"https://example.org": {"content": "旧内容"}}
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403))) as client:
            result = read_page("https://example.org", client=client, api_key="t", pages=pages)
        self.assertEqual(result["error_code"], "http_error")
        self.assertEqual(result["http_status"], 403)
        self.assertEqual(pages, {})
        def interrupt(request):
            raise KeyboardInterrupt()
        with httpx.Client(transport=httpx.MockTransport(interrupt)) as client:
            with self.assertRaises(KeyboardInterrupt):
                read_page("https://example.org", client=client, api_key="t", pages={})

    def test_structured_note_loop_and_session_clear(self):
        """实验模式可在一次研究中引用真实操作，并随 Session 清空。"""
        requests = []
        def handler(request):
            body = json.loads(request.content)
            if request.url.path == "/extract":
                return httpx.Response(200, json={"results": [{"url": "https://example.org", "raw_content": "正文"}], "failed_results": []})
            requests.append(body)
            fields = next(t for t in body["tools"] if t["function"]["name"] == "think_tool")["function"]["parameters"]["properties"]
            self.assertIn("assessment", fields)
            if len(requests) == 1:
                return reply(calls=[call("read_page", {"url": "https://example.org"}, "read1")])
            if len(requests) == 2:
                return reply(calls=[call("think_tool", {"goal": "目标", "observations": "正文已取得",
                    "assessment": "足以回答", "next_step": "回答", "references": ["read1"]})])
            self.assertEqual(json.loads(body["messages"][-1]["content"])["content_kind"], "model_note")
            return reply("答案")
        session = Session()
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            run_agent("问题", client=client, model="m", deepseek_api_key="d", tavily_api_key="t", session=session, structured_think=True)
        self.assertTrue(session.pages)
        session.clear()
        self.assertEqual(session.pages, {})
        self.assertEqual(session.messages, [])
