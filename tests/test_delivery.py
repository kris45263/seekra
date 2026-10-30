"""多结果展示预算保护实际正文范围、独立游标和稳定快照，不访问网络。"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from seekra.search.documents.delivery import fit_latest_batch, latest_batch_indices
from seekra.search.context import build_context, ContextLimit
from seekra.search.documents.delivery import remember_delivery
from seekra.search.state import SearchSession as Session
from test_documents import add_page


def batch(session, tasks):
    calls = [{'id': f'batch-{len(session.records)}-{i}', 'type': 'function', 'function': {
        'name': name, 'arguments': json.dumps(arguments)}}
        for i, (name, arguments, _) in enumerate(tasks)]
    run_id = session.last_run['id']
    session.record('model_completed', run_id=run_id, finish_reason='tool_calls',
                   message={'role': 'assistant', 'content': None, 'tool_calls': calls})
    for call, (name, _, payload) in zip(calls, tasks):
        session.record('tool_started', run_id=run_id, call_id=call['id'], tool_name=name)
        session.record('tool_completed', run_id=run_id, call_id=call['id'], tool_name=name,
                       result=json.dumps(payload, ensure_ascii=False), page_updates={})
    return session.last_run['messages']


def project(messages, documents, budget):
    replacements, info = fit_latest_batch(messages, documents, budget)
    selected = [replacements.get(i, message) for i, message in enumerate(messages)]
    return selected, info


class DeliveryTests(unittest.TestCase):
    def assert_reference(self, documents, row):
        self.assertEqual(documents.validate_reference(row['evidence_ref']), row['reference'])
        doc = documents._document(row['document_id'])
        self.assertEqual(row['content'], doc.content[row['start']:row['end']])
        self.assertEqual(row['end']-row['start'], len(row['content']))

    def test_two_sections_share_budget_and_resume_after_reload_without_changing_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            body = '# Guide\n\n## First\n' + 'first paragraph.\n\n'*600
            body += '## Second\n' + 'second paragraph.\n\n'*600 + '## Outside\nnot part of either section'
            page = add_page(session, body)
            refs = [section['section_ref'] for section in page['outline'][1:3]]
            tasks = [('read_section', {'section_ref': ref}, session.documents.read_section(ref)) for ref in refs]
            messages = batch(session, tasks)
            original, records, pages = deepcopy(messages), deepcopy(session.records), deepcopy(session.pages)
            selected, info = project(messages, session.documents, 6000)
            self.assertEqual(info['status'], 'reflowed')
            self.assertLessEqual(info['characters_after'], 6000)
            self.assertEqual(messages, original)
            self.assertEqual(session.records, records)
            self.assertEqual(session.pages, pages)
            self.assertEqual(selected[-3], original[-3], '调用消息原样保留')
            ledger = {}
            remember_delivery(ledger, selected[-2:])
            restored = Session.load(session.path)
            for result, (_, _, before) in zip(selected[-2:], tasks):
                row = json.loads(result['content'])
                self.assertGreater(len(row['content']), 100, '每个结果应获得有用的正文线索')
                self.assertLess(row['end'], before['end'])
                self.assert_reference(restored.documents, row)
                self.assertIn((row['start'], row['end']), ledger[row['document_id']])
                self.assertFalse(any(left <= before['end']-1 < right for left, right in ledger[row['document_id']]))
                self.assertEqual(row['remaining_characters'], row['target_end']-row['end'])
                cursor = restored.documents._decode(row['next_cursor'], 'section')
                self.assertEqual(cursor['start'], row['end'])
                next_page = restored.documents.read_section(**row['continuation']['arguments'])
                self.assertEqual(next_page['start'], row['end'])
                self.assertEqual(next_page['target_end'], row['target_end'])
                self.assertNotIn('not part of either', next_page['content'])

    def test_reference_mode_is_not_widened_or_given_a_document_cursor(self):
        session = Session()
        text = 'BEFORE\n\n' + 'Quoted material.\n\n'*700 + 'AFTER'
        page = add_page(session, text)
        left, right = text.index('Quoted'), text.index('AFTER')
        reference = f"{page['document_id']}@{left}-{right}"
        arguments = {'document_id': reference, 'view': 'reference'}
        messages = batch(session, [('read_document', arguments, session.documents.read_document(**arguments))])
        selected, info = project(messages, session.documents, 3200)
        self.assertEqual(info['status'], 'reflowed')
        row = json.loads(selected[-1]['content'])
        self.assert_reference(session.documents, row)
        self.assertEqual((row['target_start'], row['target_end']), (left, right))
        self.assertEqual(row['continuation']['arguments']['view'], 'reference')
        with self.assertRaises(ValueError):
            session.documents.read_document(page['document_id'], cursor=row['next_cursor'])
        follow = session.documents.read_document(**row['continuation']['arguments'])
        self.assertEqual(follow['start'], row['end'])
        self.assertNotIn('AFTER', follow['content'])

    def test_omitted_find_and_list_rows_have_frozen_continuation(self):
        session = Session()
        for index in range(12):
            add_page(session, f'# Guide {index}\n\n' + 'context '*30 + 'license ' + 'condition '*30,
                     url=f'https://example.org/{index}')
        found = session.documents.find_in_documents('license')
        index = session.documents.list_documents()
        messages = batch(session, [('find_in_documents', {'query': 'license'}, found),
                                   ('list_documents', {}, index)])
        selected, info = project(messages, session.documents, 5000)
        self.assertEqual(info['status'], 'reflowed')
        self.assertGreater(info['not_displayed_rows'], 0)
        add_page(session, '# New\nlicense introduced after the frozen request', url='https://example.org/new')
        late_id = next(reversed(session.documents._docs))
        for message in selected[-2:]:
            payload = json.loads(message['content'])
            key = 'matches' if payload['operation'] == 'find_in_documents' else 'documents'
            for row in payload[key]:
                if row.get('content'):
                    self.assert_reference(session.documents, row)
                    self.assertIn('license', row['content'])
            if isinstance(payload.get('not_displayed'), dict):
                self.assertGreater(payload['not_displayed']['rows'], 0)
                action = payload['continuation']
                following = getattr(session.documents, action['tool'])(**action['arguments'])
                self.assertNotIn(late_id, [row['document_id'] for row in following[key]])
                original_rows = found['matches'] if key == 'matches' else index['documents']
                expected = original_rows[len(payload[key])]
                self.assertEqual(following[key][0]['document_id'], expected['document_id'])

    def test_page_body_and_outline_continue_independently_when_navigation_does_not_fit(self):
        session = Session()
        body = '# Guide\n\n' + ''.join(f'## Part {i}\n' + 'details. '*40 + '\n\n' for i in range(35))
        page = add_page(session, body)
        # 保留 read_page 的真实保存视图，不执行第二次页面获取。
        messages = session.last_run['messages']
        selected, info = project(messages, session.documents, 2800)
        self.assertEqual(info['status'], 'reflowed')
        result = json.loads(selected[-1]['content'])
        self.assertGreater(result['outline_not_displayed'], 0)
        outline_action = result['outline_continuation']
        outline = session.documents.read_document(**outline_action['arguments'])
        self.assertEqual(outline['outline'][0]['section_ref'], page['outline'][len(result['outline'])]['section_ref'])
        body_action = result['continuation']
        follow = session.documents.read_document(**body_action['arguments'])
        self.assertEqual(follow['start'], result['end'])
        with self.assertRaises(ValueError):
            session.documents.read_document(result['document_id'], cursor=outline_action['arguments']['cursor'])
        if result['content']:
            self.assert_reference(session.documents, result)

    def test_search_omission_preserves_local_reread_and_real_content(self):
        session = Session()
        add_page(session, '# Initial\n')
        payload = {'operation': 'tavily_search', 'status': 'success', 'query': 'conditions',
                   'results': [{'url': f'https://example.org/search/{i}', 'title': 'long title '*30,
                                'content': 'search condition '*200} for i in range(5)]}
        messages = batch(session, [('tavily_search', {'query': 'conditions'}, payload)])
        selected, info = project(messages, session.documents, 2300)
        self.assertEqual(info['status'], 'reflowed')
        result = json.loads(selected[-1]['content'])
        self.assertGreater(result['not_displayed']['rows'], 0)
        action = result['not_displayed']['read_actions'][0]
        restored = session.documents.read_document(**action['arguments'])
        self.assertIn('search condition', restored['content'])
        for row in result['results']:
            if row.get('content'):
                self.assert_reference(session.documents, row)

    def test_navigation_floor_and_incomplete_groups_do_not_publish_partial_changes(self):
        session = Session()
        page = add_page(session, '# Guide\n' + 'body '*2000)
        research = {'operation': 'update_research', 'status': 'success', 'content_kind': 'research_state',
                    'revision': 1, 'state': {'goal': 'retain this', 'findings': [], 'gaps': []}}
        messages = batch(session, [('read_document', {'document_id': page['document_id']},
                                   session.documents.read_document(page['document_id'])),
                                  ('custom_state', {}, research)])
        replacements, info = fit_latest_batch(messages, session.documents, 50)
        self.assertEqual(info['status'], 'cannot_fit')
        self.assertEqual(replacements, {})
        self.assertGreater(info['minimum_characters'], 50)
        selected, info = project(messages, session.documents, 3000)
        self.assertEqual(selected[-1], messages[-1], '最新进展不能按正文压缩')
        self.assertEqual(latest_batch_indices(messages[:-1]), [])
        replacements, _ = fit_latest_batch(messages[:-1], session.documents, 50)
        self.assertEqual(replacements, {})

    def test_actual_context_projection_and_completed_request_record_only_budgeted_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            body = '# Guide\n\n## First\n' + 'First fact.\n\n'*1000
            body += '## Second\n' + 'Second fact.\n\n'*1000 + '## Outside\nOutside boundary'
            page = add_page(session, body)
            sections = page['outline'][1:3]
            tasks = [('read_section', {'section_ref': row['section_ref']},
                      session.documents.read_section(row['section_ref'])) for row in sections]
            messages = batch(session, tasks)
            originals, pages, snapshots = deepcopy(messages), deepcopy(session.pages), {
                doc.id: doc.content for doc in session.documents._docs.values()}
            definitions = [{'type': 'function', 'function': {'name': 'read_section',
                            'parameters': {'type': 'object'}}}]
            selected, info = build_context(messages, definitions, session, max_chars=8000)
            self.assertEqual(info['batch_delivery']['status'], 'reflowed')
            self.assertEqual(info['request_characters'],
                             len(json.dumps({'messages': selected, 'tools': definitions}, ensure_ascii=False)))
            self.assertLessEqual(info['request_characters'], 8000)
            self.assertEqual(messages, originals)
            self.assertEqual(session.pages, pages)
            self.assertEqual(snapshots, {doc.id: doc.content for doc in session.documents._docs.values()})
            self.assertEqual([m['content'] for m in selected if m['role'] == 'user'],
                             [m['content'] for m in messages if m['role'] == 'user'])
            self.assertEqual([selected[i]['tool_call_id'] for i in latest_batch_indices(selected)],
                             [messages[i]['tool_call_id'] for i in latest_batch_indices(messages)])
            run_id = session.last_run['id']
            session.record('context_selected', run_id=run_id, **info)
            self.assertEqual(session.last_run['delivered'], {}, '尚未发起或完成请求不算交付')
            session.record('model_started', run_id=run_id, round=1, system=messages[0]['content'])
            self.assertEqual(session.last_run['_request_messages'], selected,
                             '实际请求必须由投影索引和替换值逐字重建')
            self.assertEqual(session.last_run['delivered'], {})
            session.record('model_completed', run_id=run_id, finish_reason='stop',
                           message={'role': 'assistant', 'content': 'Received these windows'})
            restored = Session.load(session.path)
            self.assertEqual(restored.last_run['delivered'], session.last_run['delivered'])
            for message, (_, _, original) in zip(selected[-2:], tasks):
                row = json.loads(message['content'])
                self.assert_reference(restored.documents, row)
                spans = restored.last_run['delivered'][row['document_id']]
                self.assertTrue(any(left <= row['start'] < row['end'] <= right for left, right in spans))
                self.assertLess(row['end'], original['end'])
                self.assertFalse(any(left <= original['end']-1 < right for left, right in spans),
                                 '保存的完整结果不能误算为已经发给模型')
                continuation = restored.documents.read_section(**row['continuation']['arguments'])
                self.assertEqual(continuation['start'], row['end'])
                self.assertEqual(continuation['content'], body[continuation['start']:continuation['end']])
                self.assertNotIn('Outside boundary', continuation['content'])

    def test_context_reports_unfit_non_document_result_without_discarding_user(self):
        session = Session()
        page = add_page(session, '# Guide\n' + 'readable '*300)
        state = {'operation': 'update_research', 'status': 'success',
                 'content_kind': 'research_state', 'revision': 1,
                 'state': {'goal': 'preserve latest progress', 'findings': [],
                           'gaps': ['pending '*900], 'next_step': 'continue'}}
        messages = batch(session, [('read_document', {'document_id': page['document_id']},
                                   session.documents.read_document(page['document_id'])),
                                  ('update_research', {}, state)])
        original = deepcopy(messages)
        with self.assertRaises(ContextLimit):
            build_context(messages, [], session, max_chars=3000)
        self.assertEqual(messages, original)
        self.assertIn('preserve latest progress', session.last_run['research']['state']['goal'])


if __name__ == '__main__':
    unittest.main()
