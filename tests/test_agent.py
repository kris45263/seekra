"""使用模拟 HTTP 响应验证真实闭环、消息协议和配置读取，不访问外部服务。"""

import importlib.util
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from seekra.display import AgentDisplay


def reply(content=None, calls=None, finish=None):
    """构造 DeepSeek Chat Completions 的关键响应字段。"""
    delta = {"role": "assistant", "content": content}
    if calls:
        delta["tool_calls"] = [{"index": n, **c} for n, c in enumerate(calls)]
    delta["reasoning_content"] = "隐藏推理不可展示"
    data = {"choices": [{"index": 0, "delta": delta,
                        "finish_reason": finish or ("tool_calls" if calls else "stop")}]}
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content="data: " + json.dumps(data, ensure_ascii=False) + "\n\ndata: [DONE]\n\n")



def call(name, arguments, identifier="call_1"):
    """构造带关联编号的函数调用。"""
    return {"id": identifier, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


class AgentTests(unittest.TestCase):
    """仅替换 HTTP 传输层，保留循环和工具的实际实现。"""

    def setUp(self):
        """明确报告尚未实现的入口，避免导入错误掩盖测试意图。"""
        self.assertIsNotNone(importlib.util.find_spec("seekra.agent"), "尚未实现 agent.py")
        from seekra.agent import run_agent
        self.run_agent = run_agent

    def run_with(self, handler):
        """用内存传输执行一次研究；密钥全部为测试占位值。"""
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return self.run_agent("调查项目最新版本", client=client,
                                  model="test-deepseek", deepseek_api_key="fake-ds",
                                  tavily_api_key="fake-tv")

    def test_search_progress_then_search_depends_on_previous_result(self):
        """两种不同搜索结果必须引出不同的后续查询，且所有历史逐轮保留。"""
        for discovered in ("青松", "白桦"):
            with self.subTest(discovered=discovered):
                histories, queries = [], []

                def handler(request):
                    body = json.loads(request.content)
                    if request.url.host == "api.tavily.com":
                        self.assertEqual(request.headers["authorization"], "Bearer fake-tv")
                        queries.append(body["query"])
                        return httpx.Response(200, json={"results": [{
                            "title": "发布说明", "url": "https://example.org/release",
                            "content": discovered if len(queries) == 1 else "版本 2 已发布"}]})
                    self.assertEqual(str(request.url), "https://api.deepseek.com/chat/completions")
                    self.assertEqual(request.headers["authorization"], "Bearer fake-ds")
                    self.assertEqual(body["model"], "test-deepseek")
                    self.assertEqual(body["thinking"], {"type": "enabled"})
                    self.assertNotIn('tool_choice', body)
                    self.assertEqual({t["function"]["name"] for t in body["tools"]},
                                     {"tavily_search", "read_page", "list_history", "read_history",
                                      "list_documents", "find_in_documents", "read_document", "read_section", "update_research"})
                    messages = body["messages"]
                    if histories:
                        previous=[dict(m) for m in histories[-1]]
                        if previous[-1]['role']=='tool':
                            payload=json.loads(previous[-1]['content'])
                            if 'reading_state' in payload:
                                payload.pop('reading_state')
                                previous[-1]['content']=json.dumps(payload,ensure_ascii=False)
                                self.assertIn('reading_state',json.loads(messages[-1]['content']))
                        self.assertEqual(messages[1:len(previous)],previous[1:])
                    histories.append(messages)
                    turn = len(histories)
                    if turn == 1:
                        self.assertEqual([m["role"] for m in messages], ["system", "user"])
                        return reply(calls=[call("tavily_search", {"query": "项目发布"})])
                    self.assertEqual(messages[-1]["role"], "tool")
                    self.assertEqual(messages[-1]["tool_call_id"], messages[-2]["tool_calls"][0]["id"])
                    if turn == 2:
                        found = json.loads(messages[-1]["content"])["results"][0]["content"]
                        return reply(calls=[call("update_research", {'base_revision':0,'operations':[
                            {'op':'add_gap','question':f'缺少 {found} 版本详情'}]}, "progress")])
                    if turn == 3:
                        # 后续搜索词从收到的工具结果提取，不由被测循环预先安排。
                        found = json.loads(messages[-1]["content"])["state"]["gaps"][0]['question'].split("缺少 ")[1].split(" 版本")[0]
                        return reply(calls=[call("tavily_search", {"query": f"{found} 版本详情"}, "search2")])
                    self.assertIn("版本 2 已发布", messages[-1]["content"])
                    return reply("版本 2 已发布。[发布说明](https://example.org/release)")

                answer = self.run_with(handler)
                self.assertIn("版本 2 已发布", answer)
                self.assertEqual(queries, ["项目发布", f"{discovered} 版本详情"])
                self.assertEqual(len(histories), 4)

    def test_direct_answer_stops_immediately(self):
        """无工具调用的回答立即结束。"""
        seen = []
        def handler(request):
            seen.append(request)
            return reply("直接答案")
        self.assertEqual(self.run_with(handler), "直接答案")
        self.assertEqual(len(seen), 1)

    def test_multiple_calls_have_matching_results(self):
        """同一条 assistant 消息中的全部工具调用均被响应。"""
        seen = []
        def handler(request):
            messages = json.loads(request.content)["messages"]
            seen.append(messages)
            if len(seen) == 1:
                return reply("继续整理", [call("list_documents", {}, "a"), call("list_documents", {}, "b")])
            self.assertEqual([m["tool_call_id"] for m in messages[-2:]], ["a", "b"])
            self.assertEqual(messages[-3]["content"], "继续整理")
            return reply("完成")
        self.assertEqual(self.run_with(handler), "完成")

    def test_external_cancel_is_not_a_final_answer(self):
        """调用者取消后保留未完成状态，不产生最终答案。"""
        from seekra.runtime import CancellationToken
        cancel = CancellationToken()
        seen = []
        def handler(request):
            seen.append(request)
            return reply(calls=[call("list_documents", {})])
        def event(name, **details):
            if name == 'tool_result' and len(seen) == 2:
                cancel.cancel()
        with httpx.Client(transport=httpx.MockTransport(handler)) as client, self.assertRaises(KeyboardInterrupt):
            self.run_agent('调查', client=client, model='test', deepseek_api_key='fake-ds',
                           tavily_api_key='fake-tv', cancel=cancel, on_event=event)
        self.assertEqual(len(seen), 2)

    def test_invalid_arguments_and_unknown_tools_are_returned_to_model(self):
        """错误工具请求可由模型在下一轮修正，且不执行任意工具。"""
        for tool in [call("unknown", {}), call("tavily_search", {"query": 3}),
                     call("tavily_search", {"query": ""}),
                     call("tavily_search", {"query": "查询", "extra": 1}),
                     {"id": "bad", "type": "function", "function": {
                         "name": "tavily_search", "arguments": "{"}}]:
            with self.subTest(tool=tool):
                seen = []
                def handler(request):
                    messages = json.loads(request.content)["messages"]
                    seen.append(messages)
                    if len(seen) == 1:
                        return reply(calls=[tool])
                    result = json.loads(messages[-1]["content"])
                    self.assertEqual(result["error_code"], "invalid_arguments")
                    self.assertTrue(result["error"])
                    if tool["function"]["name"] == "unknown":
                        self.assertIn("未注册工具", result["error"])
                    else:
                        self.assertNotIn("未注册工具", result["error"])
                    return reply("已停止")
                self.assertEqual(self.run_with(handler), "已停止")

    def test_search_http_failure_is_visible_without_response_body(self):
        """搜索失败作为工具错误回传，不将远端错误正文带入模型。"""
        turns = []
        def handler(request):
            if request.url.host == "api.tavily.com":
                return httpx.Response(401, text="sensitive-server-body")
            messages = json.loads(request.content)["messages"]
            turns.append(messages)
            if len(turns) == 1:
                return reply(calls=[call("tavily_search", {"query": "版本"})])
            self.assertIn("401", messages[-1]["content"])
            self.assertNotIn("sensitive-server-body", messages[-1]["content"])
            return reply("搜索失败，无法核实。")
        self.assertIn("无法核实", self.run_with(handler))

    def test_truncated_or_empty_output_is_not_success(self):
        """截断和空输出应明确失败，避免产生伪完成状态。"""
        for response in [reply("半句话", finish="length"), reply(None)]:
            with self.subTest(response=response):
                with self.assertRaises(RuntimeError):
                    self.run_with(lambda request: response)


class ConfigTests(unittest.TestCase):
    """验证配置严格来自指定 .env 文件。"""

    def test_env_file_only_and_no_variable_interpolation(self):
        """进程环境中已有的密钥不能替代空 .env，也不能通过插值导入。"""
        self.assertIsNotNone(importlib.util.find_spec("seekra.cli"), "尚未实现 seekra.cli")
        from seekra.cli import read_config
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "process-secret"}):
                path.write_text("DEEPSEEK_API_KEY=\nDEEPSEEK_MODEL=m\nTAVILY_API_KEY=t\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "DEEPSEEK_API_KEY"):
                    read_config(path)
                path.write_text("DEEPSEEK_API_KEY=file-key\nDEEPSEEK_MODEL=m\nTAVILY_API_KEY=t\n", encoding="utf-8")
                self.assertEqual(read_config(path)["DEEPSEEK_API_KEY"], "file-key")
                path.write_text("DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY}\nDEEPSEEK_MODEL=m\nTAVILY_API_KEY=t\n", encoding="utf-8")
                with self.assertRaises(ValueError):
                    read_config(path)


class VerboseTests(unittest.TestCase):
    """验证开关仅改变终端进度，不改变请求、历史或最终答案。"""

    def test_long_search_results_keep_full_text_behind_bounded_views(self):
        """片段预览有界，原始片段和各次获取仍完整保存。"""
        from seekra.agent import run_agent
        from seekra.session import Session
        session = Session()
        snippet = "第一行\n" + "完整片段" * 500 + "\n最后一行"
        def handler(request):
            if request.url.host == "api.tavily.com":
                return httpx.Response(200, json={"results": [{
                    "title": "重复来源", "url": "https://example.org/repeated", "content": snippet}]})
            messages = json.loads(request.content)["messages"]
            if len(messages) < 6:
                return reply(calls=[call("tavily_search", {"query": "再次搜索"})])
            return reply("答案正文")
        err = io.StringIO()
        with httpx.Client(transport=httpx.MockTransport(handler)) as client, redirect_stderr(err):
            run_agent("问题", client=client, model="m", deepseek_api_key="fake-ds",
                      tavily_api_key="fake-tv", session=session,
                      on_event=AgentDisplay(True, secrets=('fake-ds', 'fake-tv')).event)
        records = [r for r in session.records if r['kind'] == 'tool_completed']
        self.assertEqual([r['retrieval_result']['results'][0]['content'] for r in records], [snippet, snippet])
        for record in records:
            row = json.loads(record['result'])['results'][0]
            self.assertTrue(row['truncated'])
            self.assertEqual(session.documents.read_document(row['document_id'])['content'], snippet)
        self.assertEqual(err.getvalue().count('片段仅展示部分'), 2)
        self.assertEqual(err.getvalue().count("https://example.org/repeated"), 2)
        self.assertNotIn("答案正文", err.getvalue())

    def test_failures_and_empty_search_remain_observable(self):
        """空结果、搜索错误和取消都有真实事件，错误结束不伪装成答案。"""
        from seekra.agent import run_agent
        for mode in ("empty", "search_error", "cancel", "model_error", "truncated"):
            with self.subTest(mode=mode):
                err = io.StringIO()
                def handler(request):
                    if request.url.host == "api.tavily.com":
                        return (httpx.Response(401, text="不要显示的远端正文") if mode == "search_error"
                                else httpx.Response(200, json={"results": []}))
                    if mode == "model_error":
                        return httpx.Response(401, text="不要显示的远端正文")
                    if mode == "truncated":
                        return reply("截断的显式内容", finish="length")
                    if mode == 'cancel' and len(json.loads(request.content)['messages']) > 2:
                        raise KeyboardInterrupt()
                    if len(json.loads(request.content)["messages"]) == 2:
                        return reply(calls=[call("tavily_search", {"query": "查询"})])
                    return reply("信息不足")
                with httpx.Client(transport=httpx.MockTransport(handler)) as client, redirect_stderr(err):
                    if mode in {"cancel", "model_error", "truncated"}:
                        with self.assertRaises((RuntimeError, httpx.HTTPStatusError, KeyboardInterrupt)):
                            run_agent("问题", client=client, model="m", deepseek_api_key="fake-ds",
                                      tavily_api_key="fake-tv", on_event=AgentDisplay(True).event)
                    else:
                        run_agent("问题", client=client, model="m", deepseek_api_key="fake-ds",
                                  tavily_api_key="fake-tv", on_event=AgentDisplay(True, secrets=('fake-ds', 'fake-tv')).event)
                if mode in {"cancel", "model_error", "truncated"}:
                    self.assertIn("异常结束", err.getvalue())
                    self.assertNotIn("模型决定：直接回答", err.getvalue())
                else:
                    self.assertIn("HTTP 401" if mode == "search_error" else "返回 0 条结果", err.getvalue())
                self.assertNotIn("不要显示的远端正文", err.getvalue())

    def test_redaction_only_changes_display(self):
        """正文意外包含密钥时仅遮蔽终端副本，不修改真实答案。"""
        from seekra.agent import run_agent
        err = io.StringIO()
        with httpx.Client(transport=httpx.MockTransport(lambda request: reply("回显 fake-ds"))) as client:
            with redirect_stderr(err):
                answer = run_agent("问题 fake-tv", client=client, model="m", deepseek_api_key="fake-ds",
                                   tavily_api_key="fake-tv", on_event=AgentDisplay(True, secrets=('fake-ds', 'fake-tv')).event)
        self.assertEqual(answer, "回显 fake-ds")
        self.assertNotIn("fake-ds", err.getvalue())
        self.assertNotIn("fake-tv", err.getvalue())
        self.assertNotIn("问题 fake-tv", err.getvalue())

    @patch('seekra.session.datetime')
    @patch('seekra.agent.uuid4')
    def test_cli_verbose_keeps_requests_identical_and_separates_output(self, run_uuid, clock):
        """使用真实命令行入口及循环，仅模拟配置和 HTTP 传输。"""
        run_uuid.return_value.hex = 'fixed-verbose-run'
        clock.now.return_value.isoformat.return_value = '2026-09-30T00:00:00+00:00'
        from seekra.cli import main
        runs = []
        for verbose in (False, True):
            requests = []
            out, err = io.StringIO(), io.StringIO()

            def handler(request):
                body = json.loads(request.content)
                requests.append((str(request.url), body))
                if request.url.host == "api.tavily.com":
                    if verbose:
                        self.assertIn("工具执行：开始", err.getvalue())
                    return httpx.Response(200, json={"results": [{
                        "title": "官方", "url": "https://example.org", "content": "资料"},
                        {"title": "重复", "url": "https://example.org", "content": "资料"},
                        {"url": "https://example.org/empty", "content": " "}]})
                messages = body["messages"]
                if len(messages) == 2:
                    response = reply("我先查官方资料。", calls=[call("tavily_search", {"query": "Python 官方改进"})])
                    return response
                if len(messages) == 4:
                    data = json.loads(messages[-1]["content"])
                    self.assertNotIn('processing', data)  # 诊断计数保留在日志，不进入模型阅读视图。
                    self.assertIn('document_id', data['results'][0])
                    self.assertEqual(len(data["results"]), 1)
                    return reply(calls=[call('update_research',{'base_revision':0,'operations':[
                        {'op':'add_gap','question':'内部笔记正文'}]})])
                return reply("这是测试用的答案正文。")

            client = httpx.Client(transport=httpx.MockTransport(handler))
            with patch("sys.argv", ["seekra", "问题", "--no-session"] + (["--verbose"] if verbose else [])), \
                 patch("seekra.cli.read_config", return_value={"DEEPSEEK_API_KEY": "fake-ds",
                       "DEEPSEEK_MODEL": "test-model", "TAVILY_API_KEY": "fake-tv"}), \
                 patch("seekra.cli.httpx.Client", return_value=client), \
                 redirect_stdout(out), redirect_stderr(err):
                status = main()
                self.assertEqual(status, 0, err.getvalue())
            self.assertEqual(out.getvalue(), "我先查官方资料。\n这是测试用的答案正文。\n")
            if verbose:
                for expected in ("Python 官方改进", "https://example.org", "资料",
                                 "内部笔记正文"):
                    self.assertIn(expected, err.getvalue())
                for hidden in ("fake-ds", "fake-tv", "隐藏推理不可展示", "reasoning_content",
                               "这是测试用的答案正文。", "你是搜索研究助手", "additionalProperties"):
                    self.assertNotIn(hidden, err.getvalue())
                for count in (2, 4, 6):
                    self.assertIn(f"上下文 {count} 条消息", err.getvalue())
                self.assertNotIn("已写回上下文", err.getvalue())
                self.assertIn("收到 3 条，保留 1 条", err.getvalue())
                self.assertIn("URL 与正文完全重复 1 条", err.getvalue())
                self.assertIn("空正文 1 条", err.getvalue())
                self.assertIn("运行结束：3 次模型调用", err.getvalue())
                for unique in ("Python 官方改进", "https://example.org", "内部笔记正文"):
                    self.assertEqual(err.getvalue().count(unique), 1)
            else:
                self.assertIn("正在思考", err.getvalue())
                self.assertNotIn("上下文", err.getvalue())
            runs.append(requests)
        self.assertEqual(runs[0], runs[1])


if __name__ == "__main__":
    unittest.main()
