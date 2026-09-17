"""从独立工作目录验证安装后的 CLI、UTF-8 和配置定位，不调用真实服务。"""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class InstalledCliTests(unittest.TestCase):
    """使用当前虚拟环境中的真实命令入口，不依赖仓库顶层模块。"""

    def invoke(self, directory, *arguments, text=""):
        """刻意关闭 Python UTF-8 模式，验证 CLI 自己处理中文输入输出。"""
        executable = Path(sys.executable).with_name("seekra.exe" if os.name == "nt" else "seekra")
        self.assertTrue(executable.is_file(), "安装项目后应生成 seekra 命令")
        env = {**os.environ, "PYTHONUTF8": "0", "PYTHONIOENCODING": "ascii",
               "DEEPSEEK_API_KEY": "environment-fake", "DEEPSEEK_MODEL": "environment-model",
               "TAVILY_API_KEY": "environment-fake"}
        return subprocess.run(
            [str(executable), *arguments], cwd=directory, env=env,
            input=text.encode("utf-8"), capture_output=True, timeout=15,
        )

    def write_config(self, directory):
        """仅写假配置；交互只执行本地命令，不发送问题。"""
        path = Path(directory) / ".env"
        path.write_text("DEEPSEEK_API_KEY=fake\nDEEPSEEK_MODEL=test-model\nTAVILY_API_KEY=fake\n", encoding="utf-8")
        return path

    def test_help_and_version_need_no_configuration(self):
        """安装后的帮助和版本在空目录中也能运行。"""
        with tempfile.TemporaryDirectory() as directory:
            for flag in ("--help", "--version"):
                with self.subTest(flag=flag):
                    result = self.invoke(directory, flag)
                    self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
                    self.assertIn("seekra", result.stdout.decode("utf-8").lower())

    def test_repl_help_prompt_and_clear_work_without_a_model_request(self):
        """中文欢迎信息、本地帮助、清空和退出均走真实入口。"""
        with tempfile.TemporaryDirectory() as directory:
            self.write_config(directory)
            results = [self.invoke(directory, "--repl", text=command+'\n')
                       for command in ('/help', '/new', '/未知命令', '/exit')]
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
            self.assertEqual(result.stdout, b"")
        shown = ''.join(result.stderr.decode('utf-8') for result in results)
        self.assertIn("Seekra", shown)
        self.assertIn("Enter 发送", shown)
        self.assertIn("/help", shown)
        self.assertIn("不认识这个命令", shown)
        self.assertIn("新对话准备好了", shown)
        self.assertNotIn("你：", shown)

    def test_explicit_config_works_outside_project(self):
        """配置可以显式指向独立目录中的文件。"""
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as config_dir:
            path = self.write_config(config_dir)
            result = self.invoke(directory, "--env-file", str(path), "--repl", text="/exit\n")
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))

    def test_missing_local_config_does_not_use_checkout_or_environment(self):
        """空目录不会误用安装源仓库的配置或环境变量。"""
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke(directory, "--repl", text="/exit\n")
        self.assertEqual(result.returncode, 1)
        shown = result.stderr.decode("utf-8")
        self.assertIn("--env-file", shown)
        self.assertIn(".env", shown)
        self.assertNotIn("Traceback", shown)

    def test_module_entry_works_outside_checkout(self):
        """标准 python -m 入口同样来自安装后的包。"""
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-m", "seekra", "--version"],
                                    cwd=directory, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        self.assertIn(b"seekra", result.stdout.lower())
