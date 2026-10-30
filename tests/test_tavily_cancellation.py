"""使用本机真实 HTTP 连接核对取消，不调用外部服务。"""
import gzip
import json
import threading
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from seekra.harness.runtime import CancellationToken, RunCancelled
from seekra.search.retrieval import TavilyTransport
from seekra.search.agent import run_agent
from seekra.search.state import SearchSession as Session
from test_agent import call, reply


class TavilyCancellationTests(unittest.TestCase):
    def test_cancelled_retrieval_is_saved_and_reload_does_not_repeat_it(self):
        for name, arguments in (('tavily_search', {'query': 'test'}),
                                ('read_page', {'url': 'https://example.org/page'})):
            with self.subTest(tool=name), tempfile.TemporaryDirectory() as directory:
                token = CancellationToken()
                session = Session.create(Path(directory))
                requests = []
                model_requests = []

                def respond(request):
                    if request.url.host == 'api.deepseek.com':
                        messages = json.loads(request.content)['messages']
                        model_requests.append(messages)
                        if len(model_requests) > 1:
                            self.assertIn('cancelled', json.dumps(messages))
                            return reply('恢复完成')
                        return reply(calls=[call(name, arguments)])
                    requests.append(request)
                    self.assertIs(request.extensions.get('seekra_cancel'), token)
                    token.cancel()
                    raise RunCancelled('cancelled request')

                with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                    options = dict(client=client, model='test', deepseek_api_key='fake', tavily_api_key='fake')
                    with self.assertRaises(RunCancelled):
                        run_agent('查询', session=session, cancel=token, **options)
                    self.assertEqual(session.last_run['status'], 'interrupted')
                    self.assertFalse(session.last_run['started_tools'])
                    self.assertFalse(session.last_run['pending'])
                    self.assertEqual(session.last_run['pages'], {})
                    result = json.loads(session.last_run['messages'][-1]['content'])
                    self.assertEqual(result['error_code'], 'cancelled')
                    restored = Session.load(session.path)
                    self.assertEqual(run_agent(None, session=restored, resume=True, **options), '恢复完成')
                    self.assertEqual(len(requests), 1)

    def test_cancel_closes_connection_before_headers_and_during_body(self):
        for phase in ('headers', 'body'):
            with self.subTest(phase=phase):
                entered, disconnected = threading.Event(), threading.Event()
                finished = threading.Event()
                outcome = []

                class Handler(BaseHTTPRequestHandler):
                    def do_POST(self):
                        self.rfile.read(int(self.headers.get('Content-Length', 0)))
                        if self.path == '/ready':
                            body = gzip.compress(b'{"ok":true}')
                            self.send_response(200)
                            self.send_header('Content-Length', str(len(body)))
                            self.send_header('Content-Encoding', 'gzip')
                            self.end_headers()
                            self.wfile.write(body)
                            return
                        if phase == 'body':
                            self.send_response(200)
                            self.send_header('Content-Length', '1000')
                            self.end_headers()
                            self.wfile.write(b'{')
                            self.wfile.flush()
                        entered.set()
                        self.connection.settimeout(5)
                        try:
                            if not self.connection.recv(1):
                                disconnected.set()
                        except (ConnectionError, OSError):
                            pass

                    def log_message(self, *args):
                        pass

                server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                serving = threading.Thread(target=server.serve_forever, daemon=True)
                serving.start()
                token = CancellationToken()
                url = f'http://127.0.0.1:{server.server_port}'
                try:
                    with httpx.Client(transport=TavilyTransport(), trust_env=False) as client:
                        def work():
                            try:
                                client.post(url + '/blocked', json={}, timeout=5,
                                            extensions={'seekra_cancel': token})
                            except BaseException as exc:
                                outcome.append(exc)
                            finally:
                                finished.set()
                        worker = threading.Thread(target=work)
                        worker.start()
                        try:
                            self.assertTrue(entered.wait(3), '请求未进入模拟服务')
                            token.cancel()
                            self.assertTrue(finished.wait(1), '取消后仍在等待网络请求')
                            self.assertTrue(disconnected.wait(1), '本地连接没有关闭')
                            self.assertIsInstance(outcome[0], RunCancelled)
                            self.assertEqual(client.post(url + '/ready', json={}).json(), {'ok': True})
                        finally:
                            worker.join(6)
                finally:
                    server.shutdown()
                    server.server_close()
                    serving.join(2)


if __name__ == '__main__':
    unittest.main()
