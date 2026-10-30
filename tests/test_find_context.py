"""查找交付保留原文结构、条件、准确引用和有界回读，不使用模型服务。"""
import unittest

from seekra.search.state import SearchSession as Session
from seekra.search.documents.delivery import remember_delivery
from test_documents import add_page


# SQLite uri.html；来自 cache-reading-state-20261021 的原始 tool_completed 18，非模型答案。
SQLITE_IMMUTABLE = (
    '**immutable=1**\n:   The immutable query parameter is a boolean that signals to SQLite that '
    'the underlying database file is held on read-only media and cannot be modified, even by '
    'another process with elevated privileges. SQLite always opens immutable database files '
    'read-only and it skips all file locking and change detection on immutable database files. '
    'If this query parameter (or the [SQLITE\\_IOCAP\\_IMMUTABLE](c3ref/c_iocap_atomic.html) bit in '
    'xDeviceCharacteristics) asserts that a database file is immutable and that file changes '
    'anyhow, then SQLite might return incorrect query results and/or '
    '[SQLITE\\_CORRUPT](rescode.html#corrupt) errors.')


class FindContextTests(unittest.TestCase):
    def find(self, body, query):
        session = Session()
        page = add_page(session, body)
        return session, session.documents.find_in_documents(query, page['document_id'])

    def assert_source(self, session, row, body):
        self.assertEqual(row['content'], body[row['start']:row['end']])
        self.assertEqual(row['evidence_ref'], f"{row['document_id']}@{row['start']}-{row['end']}")
        back = session.documents.read_document(**row['read_context_action']['arguments'])
        self.assertEqual(back['content'], body[row['target_start']:min(row['target_end'], row['target_start']+6000)])

    def test_condition_after_long_sentence_and_adjacent_exception_are_delivered(self):
        body = '# Rule\n\nThe immutable setting skips change detection. ' + 'Supporting detail. ' * 20
        body += 'If the file changes, incorrect results can occur.\n\nException: do not apply to a live database.\n\n# Other\nUnrelated.'
        session, result = self.find(body, 'change detection')
        row = result['matches'][0]
        self.assertIn('incorrect results', row['content'])
        self.assertIn('Exception: do not apply', row['content'])
        self.assertNotIn('# Other', row['content'])
        self.assertTrue(row['complete'])
        self.assert_source(session, row, body)

    def test_fence_with_blank_lines_and_table_headers_stay_in_original_slice(self):
        for body, query, expected in [
            ('# Example\n\n```python\nfirst = 1\n\nneedle = 2\n```\n\nDo not run in production.', 'needle', '```python'),
            ('# Limits\n\n| Mode | Limit |\n| --- | --- |\n| A | 30 days |\n| B | 14 days |', '30 days', '| Mode | Limit |'),
            ('# Rules\n\nAny one of the following:\n\n1. needle is enabled.\n\n2. Permission is granted.\n\nThese do not imply all modes are safe.', 'needle', 'Any one of the following:')]:
            with self.subTest(query=query):
                session, result = self.find(body, query)
                row = result['matches'][0]
                self.assertIn(expected, row['content'])
                self.assertTrue(row['complete'])
                self.assert_source(session, row, body)
                if query == 'needle' and '```' in body:
                    self.assertEqual(row['content'].count('```'), 2)

    def test_oversize_block_is_bounded_and_full_context_is_recoverable(self):
        body = '# Long\n\n' + 'prefix ' * 200 + 'needle ' + 'suffix ' * 300
        session, result = self.find(body, 'needle')
        row = result['matches'][0]
        self.assertLessEqual(len(row['content']), 2000)
        self.assertIn('needle', row['content'])
        self.assertFalse(row['complete'])
        self.assertTrue(row['truncated'])
        self.assertGreater(row['target_end']-row['target_start'], len(row['content']))
        self.assert_source(session, row, body)

    def test_second_budget_crop_preserves_full_target_and_never_overclaims_delivery(self):
        body = '# Table\n\n| Name | Rule |\n| --- | --- |\n' + ''.join(f'| item-{i} | detail |\n' for i in range(30))
        session, result = self.find(body, 'item-20')
        before = result['matches'][0]
        view = session.documents.delivery_view(result, {'query': 'item-20'})
        full = view.render(10000)['matches'][0]
        self.assertEqual(full['content'], before['content'], '二次分配有足够空间时不得改变窗口')
        cropped = view.render(60)
        row = cropped['matches'][0]
        self.assertIn('item-20', row['content'])
        self.assertFalse(row['complete'])
        self.assertEqual(row['read_context_action'], before['read_context_action'])
        self.assert_source(session, row, body)
        delivered = {}
        import json
        remember_delivery(delivered, [{'role': 'tool', 'content': json.dumps(cropped)}])
        self.assertEqual(delivered[row['document_id']], [(row['start'], row['end'])])
        hidden = view.render(0)['matches'][0]
        self.assertNotIn('evidence_ref', hidden)
        self.assertEqual(hidden['content'], '')

    def test_repeated_hits_in_one_window_do_not_repeat_the_same_content(self):
        body = '# One\n\nneedle first, needle second.\n\nSame paragraph rule.\n\n# Two\n\nneedle third.'
        session, result = self.find(body, 'needle')
        self.assertEqual(len(result['matches']), 2)
        self.assertIn('needle second', result['matches'][0]['content'])
        self.assertIn('needle third', result['matches'][1]['content'])
        self.assertTrue(result['scan_complete'])

    def test_context_does_not_expand_into_child_heading_or_first_section(self):
        for body in ('# Parent\n\nneedle parent.\n\n## Child\n\nChild unrelated.',
                     'needle preamble.\n\n# New\nNew unrelated.'):
            with self.subTest(body=body):
                _, result = self.find(body, 'needle')
                self.assertNotIn('unrelated', result['matches'][0]['content'])
        body = '# Parent\n\nBefore.\n\n## Child\n\nAfter.'
        query = 'Before.\n\n## Child'
        _, result = self.find(body, query)
        self.assertIn(query, result['matches'][0]['content'])

    def test_sqlite_original_paragraph_keeps_condition_and_error_consequence(self):
        session, result = self.find(SQLITE_IMMUTABLE, 'change detection')
        row = result['matches'][0]
        self.assertEqual(row['content'], SQLITE_IMMUTABLE)
        self.assertIn('cannot be modified', row['content'])
        self.assertIn('incorrect query results', row['content'])
        self.assertIn('[SQLITE\\_CORRUPT](rescode.html#corrupt)', row['content'])
        self.assert_source(session, row, SQLITE_IMMUTABLE)


if __name__ == '__main__':
    unittest.main()
