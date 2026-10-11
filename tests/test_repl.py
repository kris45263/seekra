"""验证 REPL 复用会话、命令处理和失败恢复，不读取真实密钥。"""

import io
import json
from contextlib import redirect_stdout, redirect_stderr
import unittest
from unittest.mock import patch

import httpx
from seekra.cli import main
from test_agent import reply


class ReplTests(unittest.TestCase):
    """通过真实入口和 HTTP 模拟执行连续输入。"""

    def test_cli_has_no_decision_limit_parameter(self):
        with patch('sys.argv', ['seekra', '问题', '--no-session']), \
             patch('seekra.cli.read_config', return_value={'DEEPSEEK_API_KEY': 'fake',
                   'DEEPSEEK_MODEL': 'm', 'TAVILY_API_KEY': 'fake'}), \
             patch('seekra.cli.run_agent', return_value='答案') as run, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(), 0)
            self.assertNotIn('max_iterations', run.call_args.kwargs)

    def test_reuses_history_clears_and_recovers(self):
        """失败和中断后继续；空输入及命令不调用模型。"""
        histories = []
        def handler(request):
            messages = json.loads(request.content)["messages"]
            histories.append(messages)
            if messages[-1]["content"] == "失败":
                return httpx.Response(500)
            if messages[-1]["content"] == "坏响应":
                return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": None}]})
            if messages[-1]["content"] == "中断":
                raise KeyboardInterrupt()
            return reply("答：" + messages[-1]["content"])
        client = httpx.Client(transport=httpx.MockTransport(handler))
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["seekra", "--repl", "--no-session"]), \
             patch("seekra.cli.ReplInput.read", side_effect=["", "第一问", "失败", "坏响应", "中断", "第二问", "/bad", "/new", "新问题", "/exit"]), \
             patch("seekra.cli.read_config", return_value={"DEEPSEEK_API_KEY": "fake", "DEEPSEEK_MODEL": "m", "TAVILY_API_KEY": "fake"}), \
             patch("seekra.cli.httpx.Client", return_value=client), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(main(), 0)
        self.assertEqual(len(histories), 6)
        self.assertEqual([len(m) for m in histories], [2, 4, 4, 4, 4, 2])
        self.assertEqual(histories[4][1]["content"], "第一问")
        self.assertEqual(histories[4][2]["content"], "答：第一问")
        self.assertIn("答：新问题", out.getvalue())
        self.assertTrue(client.is_closed)

    def test_eof_and_input_interrupt_exit_cleanly(self):
        """等待输入时的 EOF 和 Ctrl+C 正常结束循环。"""
        for signal in (EOFError, KeyboardInterrupt):
            with self.subTest(signal=signal), patch("sys.argv", ["seekra", "--repl", "--no-session"]), \
                 patch("seekra.cli.ReplInput.read", side_effect=signal), \
                 patch("seekra.cli.read_config", return_value={}), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(), 0)

    def test_conflicting_or_missing_arguments_fail_before_config(self):
        """非法组合和非法上限在读取配置前拒绝。"""
        for args in ([], ["--repl", "问题"], ["--repl", "--max-iterations", "3"]):
            with self.subTest(args=args), patch("sys.argv", ["seekra", *args]), \
                 patch("seekra.cli.read_config") as config, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main()
                self.assertEqual(raised.exception.code, 2)
                config.assert_not_called()
