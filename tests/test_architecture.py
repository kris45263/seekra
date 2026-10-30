"""验证新职责边界及跨边界事务，不用目录数量代替行为检查。"""
import ast
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx

from seekra.harness.context import AgentDefinition
from seekra.harness.loop import execute_run
from seekra.harness.runtime import ModelResponse
from seekra.harness.session import Session
from seekra.harness.tools import ToolRegistry, ToolResult
from seekra.search.state import SearchSession
from seekra.search.tools import default_registry


def call(name='calculate'):
    return {'id': 'c1', 'type': 'function',
            'function': {'name': name, 'arguments': '{}'}}


def registry_for(handler):
    registry = ToolRegistry()
    registry.register({'type': 'function', 'function': {
        'name': 'calculate', 'parameters': {'type': 'object'}}}, handler)
    return registry


class ArchitectureTests(unittest.TestCase):
    def test_dependency_direction_in_production_imports(self):
        root = Path(__file__).resolve().parents[1] / 'src/seekra'
        for package, forbidden in [('harness', {'search', 'providers', 'terminal'}),
                                    ('providers', {'search', 'terminal'})]:
            for path in (root / package).rglob('*.py'):
                module = 'seekra.' + '.'.join(path.relative_to(root).with_suffix('').parts)
                parent = module if path.name == '__init__.py' else module.rpartition('.')[0]
                for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                    names = []
                    if isinstance(node, ast.Import):
                        names = [a.name for a in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        names = [importlib.util.resolve_name('.' * node.level + (node.module or ''), parent)
                                 if node.level else node.module or '']
                    for name in names:
                        with self.subTest(path=path, dependency=name):
                            self.assertFalse(any(name == 'seekra.' + item or name.startswith('seekra.' + item + '.')
                                                 for item in forbidden))
                            if package == 'providers' and path.stem in {'deepseek', 'deepseek_stream', 'chatgpt', 'chatgpt_auth'}:
                                others = {'deepseek', 'deepseek_stream'} if path.stem.startswith('chatgpt') else {'chatgpt', 'chatgpt_auth'}
                                self.assertNotIn(name, {'seekra.providers.' + item for item in others})

    def test_generic_loop_runs_without_importing_search_or_terminal(self):
        script = '''
import sys
from seekra.harness.context import AgentDefinition
from seekra.harness.loop import execute_run
from seekra.harness.runtime import ModelResponse
from seekra.harness.session import Session
from seekra.harness.tools import ToolRegistry
class Model:
    identity = 'plain'
    def generate(self, messages, tools, **callbacks):
        assert messages[0]['content'] == 'Reply directly.'
        return ModelResponse({'role':'assistant','content':'done'}, 'stop')
result = execute_run('hello', model=Model(), registry=ToolRegistry(), session=Session(),
                     agent=AgentDefinition.from_prompt('Reply directly.'))
assert result.status == 'completed', result
assert not any(name.startswith(('seekra.search', 'seekra.terminal', 'seekra.providers')) for name in sys.modules)
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_generic_tool_result_and_resume_do_not_need_page_state(self):
        executed = []
        registry = registry_for(lambda request: (executed.append(request['id']) or ToolResult('42', {'measurement': 42})))
        agent = AgentDefinition.from_prompt('Use calculate.')
        class Model:
            identity = 'plain-tool'
            fail = True
            def generate(model, messages, tools, **callbacks):
                if len(messages) == 2:
                    return ModelResponse({'role': 'assistant', 'tool_calls': [call()]}, 'tool_calls')
                if model.fail:
                    raise RuntimeError('temporary failure')
                self.assertEqual(messages[-1]['content'], '42')
                return ModelResponse({'role': 'assistant', 'content': '42'}, 'stop')
        model = Model()
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            result = execute_run('calculate', model=model, registry=registry, session=session, agent=agent)
            self.assertEqual(result.status, 'failed')
            self.assertNotIn('pages', session.last_run)
            self.assertNotIn('research', session.last_run)
            saved = next(r for r in session.records if r['kind'] == 'tool_completed')
            self.assertEqual(saved['measurement'], 42)
            restored = Session.load(session.path)
            model.fail = False
            result = execute_run(None, model=model, registry=registry, session=restored, agent=agent, resume=True)
            self.assertEqual((result.status, result.answer), ('completed', '42'))
            self.assertEqual(executed, ['c1'])

    def test_basic_context_counts_provider_wire_content(self):
        class Model:
            identity = 'large-wire'
            def request_content(self, messages, tools):
                return {'instructions': 'x' * 3000, 'messages': messages}
            def generate(self, *args, **kwargs):
                raise AssertionError('must not send oversized content')
        result = execute_run('hello', model=Model(), registry=ToolRegistry(), session=Session(),
                             agent=AgentDefinition.from_prompt('short'), max_context_chars=2000)
        self.assertEqual(result.status, 'paused')

    def test_missing_continuation_does_not_trigger_implicit_search_recovery(self):
        class Model:
            identity = 'needs-continuation'
            def project_messages(self, messages, states):
                return messages, {1}
            def generate(self, *args, **kwargs):
                raise AssertionError('must not send')
        session = Session()
        result = execute_run('hello', model=Model(), registry=ToolRegistry(), session=session,
                             agent=AgentDefinition.from_prompt('plain'))
        self.assertEqual(result.status, 'paused')
        self.assertEqual(session.last_run['error_code'], 'missing_continuation')

    def test_tool_metadata_cannot_replace_execution_identity(self):
        for key in ('run_id', 'call_id', 'kind', 'result', 'final_output_field'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                ToolResult('42', {key: 'forged'})

    def test_search_page_is_staged_until_successful_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            session = SearchSession.create(Path(directory))
            session.record('run_started', run_id='run', question='read', system='search', config={})
            request = call('read_page')
            request['function']['arguments'] = json.dumps({'url': 'https://example.org/rules'})
            session.record('model_completed', run_id='run', message={'role': 'assistant', 'tool_calls': [request]}, finish_reason='tool_calls')
            session.record('tool_started', run_id='run', call_id='c1', tool_name='read_page')
            with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
                'results': [{'url': 'https://example.org/rules', 'raw_content': '# Rule\nOnly with A.'}],
                'failed_results': []}))) as client:
                output = default_registry(client, 'fake', session).execute(request)
            self.assertTrue(output.record_fields['page_updates'])
            self.assertEqual(session.last_run['pages'], {})
            self.assertEqual(session.documents.list_documents()['documents'], [])
            before = len(session.records)
            with patch('seekra.harness.session.os.fsync', side_effect=OSError('disk failed')):
                with self.assertRaises(OSError):
                    session.record('tool_completed', run_id='run', call_id='c1', tool_name='read_page',
                                   result=output.content, **output.record_fields)
            self.assertEqual(len(session.records), before)
            self.assertEqual(session.last_run['pages'], {})
            self.assertEqual(session.documents.list_documents()['documents'], [])
            # fsync 失败时磁盘是否已有完整行不确定；不宣称外部效果或磁盘写入已撤销。

    def test_invalid_search_projection_is_rejected_before_append(self):
        with tempfile.TemporaryDirectory() as directory:
            session = SearchSession.create(Path(directory))
            session.record('run_started', run_id='run', question='q', system='s', config={})
            before = session.path.read_bytes()
            with self.assertRaises(ValueError):
                session.record('tool_completed', run_id='run', call_id='c', tool_name='update_research',
                               result=json.dumps({'schema_version': 900, 'status': 'success'}))
            self.assertEqual(session.path.read_bytes(), before)
            self.assertIsNone(session.last_run['research'])

    def test_search_identity_does_not_depend_on_module_path_or_date(self):
        import hashlib
        from seekra.search.agent import SEARCH_AGENT
        from seekra.search.prompt import SYSTEM_PROMPT
        self.assertEqual(SEARCH_AGENT.prompt_identity, hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest())
        with patch('seekra.search.prompt.date') as clock:
            clock.today.return_value.isoformat.return_value = '2040-01-01'
            self.assertIn('2040-01-01', SEARCH_AGENT.system_prompt())

    def test_public_search_imports_retain_their_meaning(self):
        from seekra.agent import run_agent
        from seekra.search.agent import run_agent as implementation
        from seekra.session import Session as LegacySession
        self.assertIs(run_agent, implementation)
        self.assertIs(LegacySession, SearchSession)
