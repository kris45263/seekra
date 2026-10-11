"""覆盖获取结果边界与参数模式隔离，不调用真实服务。"""

import io
import json
from contextlib import redirect_stdout, redirect_stderr
import unittest
from unittest.mock import patch

import httpx

from seekra.retrieval import PAGE_CHARS, read_page, tavily_search
from seekra.tools import TOOLS, tool_definitions, execute_tool
from test_agent import call, reply


class RetrievalEdgeTests(unittest.TestCase):
    """检验边界行为，不以返回字典存在代替正确性断言。"""

    def test_exact_page_boundary_and_invalid_range(self):
        """恰好一页不标 partial，越界续读不重新联网。"""
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"results": [{"url": "https://example.org", "raw_content": "甲" * PAGE_CHARS}], "failed_results": []})
        pages = {}
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            first = read_page("https://example.org", client=client, api_key="fake", pages=pages)
            self.assertEqual(first["status"], "success")
            self.assertIsNone(first["next_start"])
            end = read_page("https://example.org", start=PAGE_CHARS, client=client, api_key="fake", pages=pages)
        self.assertEqual(end["error_code"], "invalid_range")
        self.assertEqual(len(requests), 1)

    def test_refresh_changes_snapshot_and_continuation_uses_new_text(self):
        """start=0 确实重新获取，同 URL 的内容变化不会复用旧尾部。"""
        replies = iter(["甲" * PAGE_CHARS + "旧尾", "乙" * PAGE_CHARS + "新尾"])
        def handler(request):
            return httpx.Response(200, json={"results": [{"url": "https://example.org", "raw_content": next(replies)}], "failed_results": []})
        pages = {}
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            old = read_page("https://example.org", client=client, api_key="fake", pages=pages)
            new = read_page("https://example.org", client=client, api_key="fake", pages=pages)
            tail = read_page("https://example.org", start=PAGE_CHARS, client=client, api_key="fake", pages=pages)
        self.assertNotEqual(old["snapshot_id"], new["snapshot_id"])
        self.assertEqual(tail["content"], "新尾")
        self.assertEqual(tail["snapshot_id"], new["snapshot_id"])

    def test_provider_reported_url_is_not_claimed_as_final_url(self):
        """服务报告不同地址时如实区分，不臆测重定向链。"""
        response = {"results": [{"url": "https://example.org/current", "raw_content": "文字"}], "failed_results": []}
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response))) as client:
            result = read_page("https://example.org/old", client=client, api_key="fake", pages={})
        self.assertEqual(result["requested_url"], "https://example.org/old")
        self.assertEqual(result["url"], "https://example.org/current")
        self.assertIsNone(result["final_url"])

    def test_malformed_extract_responses_do_not_create_snapshots(self):
        """畸形条目、错误类型和重复条目不会成为有效页面。"""
        for raw in [[], {}, {"results": [None], "failed_results": []},
                    {"results": [{"url": "https://example.org", "raw_content": 5}], "failed_results": []},
                    {"results": [{"url": "file:///x", "raw_content": "文字"}], "failed_results": []},
                    {"results": [{}, {}], "failed_results": []},
                    {"results": [], "failed_results": [None]}]:
            with self.subTest(raw=raw):
                pages = {}
                with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=raw))) as client:
                    result = read_page("https://example.org", client=client, api_key="fake", pages=pages)
                self.assertEqual(result["error_code"], "invalid_response")
                self.assertEqual(pages, {})

    def test_failure_reason_mapping_does_not_leak_provider_body(self):
        """明确原因被保留，未知错误及潜在敏感正文不直接回传。"""
        for reason, expected in [("403 denied", "forbidden"), ("request timeout", "timeout"),
                                 ("request timed out", "timeout"), ("secret-provider-detail", "unspecified")]:
            with self.subTest(reason=reason):
                raw = {"results": [], "failed_results": [{"url": "https://example.org", "error": reason}]}
                with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=raw))) as client:
                    result = read_page("https://example.org", client=client, api_key="fake", pages={})
                self.assertEqual(result["reason_code"], expected)
                self.assertNotIn("secret-provider-detail", json.dumps(result))

    def test_search_status_and_error_categories(self):
        """正常空结果、全被过滤、网络失败、超时和非法 JSON 保持可区分。"""
        for mode, expected in [("empty", "empty"), ("filtered", "all_filtered"),
                               ("network", "network_error"), ("timeout", "timeout"), ("json", "invalid_response")]:
            with self.subTest(mode=mode):
                def handler(request):
                    if mode == "network": raise httpx.ConnectError("不可回传的内部消息")
                    if mode == "timeout": raise httpx.ReadTimeout("内部消息")
                    if mode == "json": return httpx.Response(200, text="not-json")
                    return httpx.Response(200, json={"results": [] if mode == "empty" else [{}]})
                with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                    result = tavily_search("问题", client=client, api_key="fake")
                self.assertEqual(result.get("error_code", result["status"]), expected)
                self.assertNotIn("内部消息", json.dumps(result, ensure_ascii=False))

    def test_note_modes_do_not_mutate_global_schema_and_reject_bad_inputs(self):
        """实验模式不会污染后续默认会话，笔记字段和引用接受严格类型检查。"""
        original = json.dumps(TOOLS, sort_keys=True)
        structured = tool_definitions(True)
        structured[0]["function"]["name"] = "changed"
        self.assertEqual(json.dumps(TOOLS, sort_keys=True), original)
        self.assertNotIn('think_tool', {d['function']['name'] for d in tool_definitions()})
        args = {"goal": "目标", "observations": "观察", "assessment": "判断", "next_step": "下一步", "references": []}
        bad = [{**args, "goal": ""}, {**args, "observations": 2}, {**args, "references": "x"},
               {**args, "references": [1]}, {**args, "references": ["note1"]}, {**args, "extra": True}]
        with httpx.Client(transport=httpx.MockTransport(lambda r: self.fail("笔记不得联网"))) as client:
            for item in bad:
                with self.subTest(item=item):
                    result = json.loads(execute_tool(call("think_tool", item), client=client, api_key="fake",
                                                    structured_think=True, known_operations={"note1": "think_tool"}))
                    self.assertEqual(result["error_code"], "invalid_arguments")

    def test_cli_uses_progress_without_legacy_think_flag(self):
        """默认集合统一到进展；旧 CLI 开关不能静默改变新协议。"""
        from seekra.cli import main
        for enabled in (False, True):
            def handler(request):
                body = json.loads(request.content)
                self.assertNotIn('think_tool',{t['function']['name'] for t in body['tools']})
                fields = next(t['function']['parameters']['properties'] for t in body['tools'] if t['function']['name']=='update_research')
                self.assertIn('operations',fields)
                return reply("答案")
            client = httpx.Client(transport=httpx.MockTransport(handler))
            with patch("sys.argv", ["seekra", "问题", "--no-session"] + (["--structured-think"] if enabled else [])), \
                 patch("seekra.cli.read_config", return_value={"DEEPSEEK_API_KEY": "d", "DEEPSEEK_MODEL": "m", "TAVILY_API_KEY": "t"}), \
                 patch("seekra.cli.httpx.Client", return_value=client), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                if enabled:
                    with self.assertRaises(SystemExit) as stopped: main()
                    self.assertEqual(stopped.exception.code,2)
                else:
                    self.assertEqual(main(),0)
