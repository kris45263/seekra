"""预算准备的正文外置与最新结果保护；不再依赖研究进展阶段。"""
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from seekra.search.agent import run_agent
from seekra.search.state import SearchSession as Session
from test_agent import call, reply
from test_research import state, deliver, pressure_budget
from test_documents import add_page


URL = 'https://example.org/conditions'
PAGE = '# Guide\n\n## Conditions\nPro license required.\n'
REFERENCE = f'S1-P1@0-{len(PAGE.strip())}'


class CheckpointTests(unittest.TestCase):
    def test_soft_budget_offloads_old_sources_before_restricting_tools_and_survives_reload(self):
        from copy import deepcopy
        from seekra.search.context import prepare_context
        from seekra.search.context import build_context
        from seekra.search.tools import default_registry
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            for i in range(9):
                add_page(session, '# Guide\n\n' + f'condition {i}. ' * 600,
                         url=f'https://example.org/{i}')
            deliver(session)
            latest = add_page(session, '# Latest\n\n' + 'latest condition. ' * 600,
                              url='https://example.org/latest')
            definitions = default_registry(None, 'fake', session).definitions
            messages = session.last_run['messages']
            _, full = build_context(messages, definitions, session, 200000)
            budget = full['request_characters'] * 10 // 9
            records, original = deepcopy(session.records), deepcopy(messages)
            selected, tools, info, _ = prepare_context(messages, definitions, session, budget)
            self.assertIsNone(info['progress_stage'], '仍可外置旧正文时，不应先关闭普通工具')
            self.assertEqual(tools, definitions)
            self.assertLess(info['request_characters'], info['checkpoint_threshold'])
            self.assertEqual(info['request_characters'], len(json.dumps({'messages': selected, 'tools': tools}, ensure_ascii=False)))
            self.assertEqual(json.loads(selected[-1]['content']), latest, '最新结果不为软阈值裁短')
            offloaded = [json.loads(m['content']) for m in selected if m['role'] == 'tool'
                         and json.loads(m['content']).get('offloaded')]
            self.assertTrue(offloaded)
            reference = offloaded[0]['delivered_reads'][0]['evidence_ref']
            source = session.documents.read_document(reference, view='reference')
            self.assertIn('condition 0', source['content'])
            self.assertEqual(messages, original)
            self.assertEqual(session.records, records)
            restored = Session.load(session.path)
            again, _, restored_info, _ = prepare_context(restored.last_run['messages'], definitions, restored, budget)
            self.assertEqual(again, selected)
            self.assertEqual(restored_info, info)

    def test_soft_budget_does_not_hide_tool_failure_details(self):
        from seekra.search.context import prepare_context
        from seekra.search.context import build_context
        from seekra.search.tools import default_registry
        from test_documents import complete_tool
        session = Session()
        failure = {'operation': 'read_page', 'status': 'failed', 'error_code': 'timeout',
                   'error': 'failure detail. ' * 150 + 'FAILURE_TAIL'}
        complete_tool(session, 'read_page', failure)
        for i in range(9):
            add_page(session, '# Guide\n\n' + 'body. ' * 1000, url=f'https://example.org/{i}')
        definitions = default_registry(None, 'fake', session).definitions
        _, full = build_context(session.last_run['messages'], definitions, session, 200000)
        selected, tools, info, _ = prepare_context(session.last_run['messages'], definitions, session,
                                                  full['request_characters'] * 10 // 9)
        self.assertIsNone(info['progress_stage'])
        self.assertEqual(json.loads(selected[3]['content']), failure)
        self.assertIn('FAILURE_TAIL', selected[3]['content'])

    def test_soft_target_keeps_latest_body_even_when_it_cannot_reach_target(self):
        from seekra.search.context import prepare_context
        from seekra.search.context import build_context
        from seekra.search.tools import default_registry
        session = Session()
        latest = add_page(session, '# Guide\n\n' + 'body. ' * 1000)
        definitions = default_registry(None, 'fake', session).definitions
        _, full = build_context(session.last_run['messages'], definitions, session, 200000)
        selected, tools, info, _ = prepare_context(session.last_run['messages'], definitions, session,
                                                  full['request_characters'] * 10 // 9)
        self.assertIsNone(info['progress_stage'], '预算不足也不通过保存进展解锁工具')
        body = json.loads(selected[-1]['content'])
        for field in ('content', 'start', 'end', 'evidence_ref'):
            self.assertEqual(body[field], latest[field])
        self.assertNotIn('batch_delivery', info)

    def test_soft_target_without_offloadable_content_does_not_expand_or_fail_request(self):
        from seekra.search.context import build_context
        messages=[{'role':'system','content':'x'*2100},{'role':'user','content':'question'}]
        size=len(json.dumps({'messages':messages,'tools':[]},ensure_ascii=False))
        selected,info=build_context(messages,[],Session(),size+10,offload_target=size*4//5)
        self.assertEqual(selected,messages)
        self.assertFalse(info['offloaded'])
        self.assertEqual(info['request_characters'],size)

    def test_old_stage_does_not_restrict_tools_or_navigation(self):
        from seekra.search.context import prepare_context, present_actions
        from seekra.search.tools import default_registry
        session = Session()
        add_page(session, PAGE)
        session.last_run['progress_stage'] = {'base_revision': 0}
        definitions = default_registry(None, 'fake', session).definitions
        _, tools, info, _ = prepare_context(session.last_run['messages'], definitions, session, 120000)
        self.assertEqual(tools, definitions)
        self.assertIsNone(info['progress_stage'])
        action = {'tool': 'read_document', 'arguments': {'document_id': 'S1-P1'}, 'availability': 'after_progress'}
        self.assertNotIn('availability', present_actions(action))
        self.assertIn('availability', action, '旧执行记录不改写')


if __name__ == '__main__':
    unittest.main()
