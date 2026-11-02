"""CLI and evaluator contracts that survive the runtime replacement."""
import asyncio
from contextlib import redirect_stdout,redirect_stderr
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from langchain_core.messages import AIMessage,HumanMessage,ToolMessage
from seekra.terminal.cli import main
from seekra.terminal.display import AgentDisplay,display_text
from seekra.sdk.events import Event
from test_sdk_integration import ScriptModel,app_at,call

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from run_live_case import evaluate, cases, main as evaluation_main


class CLITests(unittest.TestCase):
    def test_help_and_version_without_credentials(self):
        for flag in ('--help','--version'):
            with redirect_stdout(io.StringIO()),self.assertRaises(SystemExit) as exit:
                main([flag])
            self.assertEqual(exit.exception.code,0)

    def test_piped_multiline_is_one_user_turn_not_commands(self):
        with tempfile.TemporaryDirectory() as root:
            model=ScriptModel(replies=[AIMessage(content='ok')],profile={'max_input_tokens':64000})
            app=app_at(root,model)
            text='first\n/new\n/exit\n    indentation\n'
            with patch('seekra.terminal.cli.load_settings',return_value=app.settings),\
                 patch('seekra.terminal.cli.load_secrets',return_value=app.secrets),\
                 patch('seekra.terminal.cli.Seekra',return_value=app),\
                 patch('sys.stdin',io.StringIO(text)),redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
                self.assertEqual(main(['--repl']),0)
            item=app.sessions.list()[0]
            history=asyncio.run(app.history(item))
            self.assertEqual([m.content for m in history if isinstance(m,HumanMessage)],[text])
            self.assertEqual(item['title'],text[:80])

    def test_only_final_event_writes_answer_channel(self):
        output,progress=io.StringIO(),io.StringIO()
        with redirect_stdout(output),redirect_stderr(progress):
            view=AgentDisplay(secrets=('secret-key',))
            view.event(Event('text',{'text':'draft secret-key\x1b[2J'}))
            view.event(Event('text',{'text':'child'},('subagent',)))
            view.event(Event('model_end',{'id':'m','usage':None}))
            view.event(Event('final',{'answer':'final'}))
            view.finish()
        self.assertEqual(output.getvalue(),'final\n')
        self.assertNotIn('secret-key',progress.getvalue())
        self.assertNotIn('\x1b',progress.getvalue())
        self.assertNotIn('child',progress.getvalue())
        self.assertIn('未知',progress.getvalue())

    def test_product_has_no_old_runtime_imports(self):
        root=Path(__file__).resolve().parents[1]/'src/seekra'
        self.assertFalse((root/'harness').exists())
        self.assertFalse((root/'providers').exists())
        for path in root.rglob('*.py'):
            text=path.read_text(encoding='utf-8-sig')
            for legacy in ('seekra.harness','seekra.providers','SearchSession','max_context_chars'):
                self.assertNotIn(legacy,text,str(path))


class EvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_failure_reaches_model_without_network_or_grading_leak(self):
        case = next(item for item in cases() if item['id'] == 'retrieval-failure')
        with tempfile.TemporaryDirectory() as root:
            model = ScriptModel(replies=[call('fetch_page', {'url': case['url']}),
                                         AIMessage(content='无法核实')],
                                profile={'max_input_tokens': 64000})
            app = app_at(root, model)
            with patch('run_live_case.Seekra', return_value=app), \
                 patch('seekra.search.retrieval.Retrieval.fetch', side_effect=AssertionError('network')):
                result = await evaluate(case, app.settings, app.secrets)
            self.assertIsNone(result['error'])
            self.assertEqual(result['sources'], [])
            feedback = next(message for message in model.seen[-1] if isinstance(message, ToolMessage))
            self.assertEqual(json.loads(feedback.content), case['retrieval_error'])
            self.assertEqual([m.content for m in model.seen[0] if isinstance(m, HumanMessage)],
                             [case['question']])
            self.assertNotIn(case['expected'][0], str(model.seen))
            self.assertEqual(result['case_definition'], case)
            case['expected'].append('later change')
            self.assertNotIn('later change', result['case_definition']['expected'])

    async def test_fixed_document_report_keeps_exact_supplied_content_identity(self):
        import hashlib
        case = next(item for item in cases() if item['id'] == 'offline-requirements')
        content = (Path(__file__).resolve().parents[1] / 'evals' / case['fixture']).read_text(encoding='utf-8')
        with tempfile.TemporaryDirectory() as root:
            model = ScriptModel(replies=[call('fetch_page', {'url': case['url']}), AIMessage(content='done')],
                                profile={'max_input_tokens': 64000})
            app = app_at(root, model)
            with patch('run_live_case.Seekra', return_value=app):
                result = await evaluate(case, app.settings, app.secrets)
            self.assertIsNone(result['error'])
            self.assertEqual(result['sources'][0]['content'], content)
            self.assertEqual(result['fixture_sha256'], hashlib.sha256(content.encode('utf-8')).hexdigest())

    def test_list_and_invalid_options_do_not_load_credentials(self):
        with patch('run_live_case.load_secrets', side_effect=AssertionError('credentials')):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(evaluation_main(['--list']), 0)
            for args in ([], ['--case', 'retrieval-failure', '--compaction-messages', '5']):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    evaluation_main(args)
                self.assertEqual(error.exception.code, 2)

    async def test_call_limit_does_not_discard_final_response_at_limit(self):
        with tempfile.TemporaryDirectory() as root:
            model=ScriptModel(replies=[AIMessage(content='done')],profile={'max_input_tokens':64000})
            app=app_at(root,model)
            with patch('run_live_case.Seekra',return_value=app):
                result=await evaluate({'id':'test','mode':'live','question':'hi'},app.settings,app.secrets,max_model_calls=1)
            self.assertIsNone(result['error'])
            self.assertEqual(result['result']['answer'],'done')

    async def test_call_limit_stops_before_next_model_request(self):
        with tempfile.TemporaryDirectory() as root:
            model=ScriptModel(replies=[call('fetch_page',{'url':'https://example.com'}),AIMessage(content='not reached')],
                              profile={'max_input_tokens':64000})
            app=app_at(root,model)
            with patch('run_live_case.Seekra',return_value=app):
                result=await evaluate({'id':'test','mode':'live','question':'hi'},app.settings,app.secrets,max_model_calls=1)
            self.assertIn('eval_model_call_limit',result['error'])
            self.assertEqual(len(model.seen),1)
