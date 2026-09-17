"""多行输入必须以显式提交结束，不能按粘贴中的换行调用模型。"""
import io
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout, redirect_stderr
import httpx
from seekra.cli import main
from test_agent import reply


class PasteRegressionTests(unittest.TestCase):
    def test_piped_multiline_is_one_user_turn(self):
        text = '请分析这段内容：\n\n- 第一项\n- 第二项\n    保留缩进\n'
        questions = []
        def handler(request):
            import json
            questions.append(json.loads(request.content)['messages'][-1]['content'])
            return reply('已分析')
        with patch('sys.argv',['seekra','--repl','--no-session']), patch('sys.stdin',io.StringIO(text)),\
             patch('seekra.cli.read_config',return_value={'DEEPSEEK_API_KEY':'fake-ds','DEEPSEEK_MODEL':'m','TAVILY_API_KEY':'fake-tv'}),\
             patch('seekra.cli.httpx.Client',return_value=httpx.Client(transport=httpx.MockTransport(handler))),\
             redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
            self.assertEqual(main(),0)
        self.assertEqual(questions,[text])
