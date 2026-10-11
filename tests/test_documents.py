"""正文工具的来源、范围和恢复契约，不访问网络。"""
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
from unittest.mock import patch

import httpx

from seekra.agent import run_agent
from seekra.context import build_context, read_history
from seekra.session import Session
from seekra.tools import default_registry
from test_agent import call, reply


URL = 'https://example.org/guide'


def begin(session):
    run_id = 'run-' + str(len(session.records))
    session.record('run_started', run_id=run_id, question='核实使用条件',
                   system='test', config={})
    return run_id


def complete_tool(session, name, payload, pages=None):
    run_id = session.last_run['id'] if session.last_run else begin(session)
    call_id = 'call-' + str(len(session.records))
    call = {'id': call_id, 'type': 'function', 'function': {
        'name': name, 'arguments': '{}'}}
    session.record('model_completed', run_id=run_id,
                   message={'role': 'assistant', 'content': None, 'tool_calls': [call]},
                   finish_reason='tool_calls')
    session.record('tool_started', run_id=run_id, call_id=call_id, tool_name=name)
    session.record('tool_completed', run_id=run_id, call_id=call_id, tool_name=name,
                   result=json.dumps(payload, ensure_ascii=False), page_updates=pages or {})
    return json.loads(session.last_run['messages'][-1]['content'])


def add_page(session, body, url=URL):
    import hashlib
    snapshot = hashlib.sha256(body.encode()).hexdigest()[:16]
    return complete_tool(session, 'read_page', {
        'operation': 'read_page', 'status': 'success', 'content_kind': 'extracted_text',
        'title': '指南', 'requested_url': url, 'url': url,
        'content': body[:24000], 'snapshot_id': snapshot, 'start': 0,
        'end': min(24000, len(body)), 'total_characters': len(body),
        'next_start': 24000 if len(body) > 24000 else None,
        'coverage': '提取文本',
    }, {url: {'title': '指南', 'url': url, 'content': body, 'snapshot_id': snapshot}})


class DocumentTests(unittest.TestCase):
    def store(self, session):
        self.assertTrue(hasattr(session, 'documents'), '需要正文索引与读取入口')
        return session.documents

    def test_navigation_exposes_exact_preview_time_and_remaining_text(self):
        session = Session()
        body = '# Guide\n\n## Terms\n旧版条件。\n\n## Terms\n新版条件。\n' + '后续。' * 2500
        page = add_page(session, body)
        self.assertIn('retrieved_at', page, '同一天多次获取也应能区分取得时间')
        self.assertEqual(page['retrieved_at'], session.records[-1]['time'])
        self.assertEqual(page['snapshot_id'], session.documents.read_document(page['document_id'])['snapshot_id'])
        for section in page['outline']:
            self.assertEqual(section['preview'], body[section['preview_start']:section['preview_end']])
            self.assertLessEqual(section['preview_end'], section['end'])
        self.assertIn('旧版', page['outline'][1]['preview'])
        self.assertIn('新版', page['outline'][2]['preview'])
        first = session.documents.read_section(page['outline'][2]['section_ref'])
        self.assertEqual(first['remaining_characters'], first['target_end'] - first['end'])
        self.assertFalse(first['reached_end'])
        last = session.documents.read_section(**first['continuation']['arguments'])
        self.assertTrue(last['reached_end'])
        self.assertEqual(last['remaining_characters'], 0)
        self.assertFalse(last['complete'])

    def test_snippet_and_find_return_references_to_delivered_text(self):
        session = Session()
        snippet = complete_tool(session, 'tavily_search', {
            'operation': 'tavily_search', 'status': 'success', 'query': 'license',
            'results': [{'url': URL, 'title': 'Terms', 'content': 'license ' * 300}]})['results'][0]
        self.assertIn('reference', snippet, '应直接提供实际返回范围的引用')
        self.assertEqual(snippet['reference']['end'], len(snippet['content']))
        self.assertEqual(snippet['query'], 'license')
        page = add_page(session, '# Guide\n\n## Terms\nA Pro license is required.\n')
        hit = session.documents.find_in_documents('license', page['document_id'])['matches'][0]
        ref = hit['reference']
        self.assertEqual((ref['start'], ref['end']), (hit['context_start'], hit['context_end']))
        self.assertEqual((hit['start'], hit['end']), (ref['start'], ref['end']))
        self.assertEqual('license', page['content'][
            hit['match_range']['start']:hit['match_range']['end']])
        self.assertEqual(session.documents.validate_reference(hit['evidence_ref']), ref)
        self.assertEqual(session.documents.validate_reference(snippet['evidence_ref']), snippet['reference'])
        self.assertEqual(session.documents.read_document(ref['document_id'], start=ref['start'],
                         limit=ref['end']-ref['start'])['content'], hit['content'])

    def test_search_and_page_share_source_but_keep_distinct_documents(self):
        session = Session()
        result = complete_tool(session, 'tavily_search', {
            'operation': 'tavily_search', 'status': 'success', 'query': '启用条件',
            'content_kind': 'snippet', 'processing': {'kept': 1},
            'results': [{'url': URL, 'title': '指南', 'content': '见 Requirements'}]})
        self.assertIn('document_id', result['results'][0], '搜索结果需提供回查引用')
        page = add_page(session, '# Guide\n\n## Requirements\n专业版。\n')
        snippet = result['results'][0]
        self.assertEqual(snippet['source_id'], page['source_id'])
        self.assertNotEqual(snippet['document_id'], page['document_id'])
        self.assertEqual(self.store(session).read_document(snippet['document_id'])['content'],
                         '见 Requirements')
        self.assertEqual(page['content_kind'], 'page_text')
        self.assertEqual(page['outline'][1]['title'], 'Requirements')
        self.assertNotIn('processing', result)

    def test_exact_repeated_snippet_is_compact_but_new_conditions_are_preserved(self):
        session = Session()
        text = 'License conditions. ' * 100 + 'Final exception: shared guest accounts are excluded.'
        def search(query, content):
            return complete_tool(session, 'tavily_search', {'operation': 'tavily_search', 'status': 'success',
                   'query': query, 'results': [{'url': URL, 'title': 'Terms', 'content': content}]})['results'][0]
        first = search('license', text)
        repeated = search('guest accounts', text)
        self.assertEqual(repeated['duplicate_of'], first['document_id'])
        self.assertLess(len(repeated['content']), len(first['content']))
        self.assertEqual(repeated['query'], 'guest accounts')
        self.assertTrue(repeated['truncated'])
        self.assertEqual(session.documents.read_document(repeated['document_id'])['content'], text)
        self.assertEqual(repeated['reference']['end'], len(repeated['content']))
        changed = search('new exception', text + '\nNew condition: trial licenses are excluded.')
        self.assertNotIn('duplicate_of', changed)
        self.assertEqual(len(changed['content']), len(first['content']))
        self.assertIn('trial licenses', session.documents.read_document(changed['document_id'])['content'])

    def test_reference_read_is_exact_and_context_expansion_issues_new_reference(self):
        session = Session()
        text = 'Background.\n\n' * 80 + 'Crawlers may cache the fetched file.\n\n' + 'Cached copies should not be used for more than 24 hours.\n'
        page = add_page(session, text)
        left = text.index('Crawlers may')
        right = text.index('Cached copies')
        reference = f"{page['document_id']}@{left}-{right}"
        exact = session.documents.read_document(reference, view='reference')
        self.assertEqual(exact['content'], text[left:right])
        self.assertEqual((exact['target_start'], exact['target_end']), (left, right))
        self.assertTrue(exact['complete'])
        self.assertNotIn('24 hours', exact['content'])
        expanded = session.documents.read_document(reference)
        self.assertIn('24 hours', expanded['content'])
        self.assertNotEqual(exact['evidence_ref'], expanded['evidence_ref'])
        self.assertEqual(expanded['anchor_ref'], reference)
        with self.assertRaises(ValueError):
            session.documents.read_document(reference, view='reference', start=left)

    def test_reference_pagination_never_crosses_quoted_range(self):
        session = Session()
        text = 'OUTSIDE BEFORE\n\n' + 'Quoted paragraph.\n\n' * 800 + 'OUTSIDE AFTER'
        page = add_page(session, text)
        left = text.index('Quoted paragraph')
        right = text.index('OUTSIDE AFTER')
        reference = f"{page['document_id']}@{left}-{right}"
        result = session.documents.read_document(reference, view='reference')
        parts = []
        while True:
            parts.append(result['content'])
            self.assertGreaterEqual(result['start'], left)
            self.assertLessEqual(result['end'], right)
            continuation = result['continuation']
            if continuation is None:
                break
            self.assertEqual(continuation['arguments']['view'], 'reference')
            previous_end = result['end']
            result = session.documents.read_document(**continuation['arguments'])
            self.assertEqual(result['start'], previous_end)
        self.assertEqual(''.join(parts), text[left:right])
        self.assertTrue(result['reached_end'])
        self.assertFalse(result['complete'])

    def test_history_text_routes_saved_sources_without_duplicate_event_bodies(self):
        session = Session()
        page = add_page(session, '# Guide\n\n' + 'Recorded body.\n\n' * 900 + 'TAIL-SOURCE')
        view = read_history(session, page['record_id'])
        self.assertEqual(view['view'], 'text')
        self.assertLess(view['total_characters'], 1200)
        self.assertNotIn('page_updates', view['content'])
        self.assertNotIn('retrieval_result', view['content'])
        self.assertEqual(view['sources'][0]['document_id'], page['document_id'])
        document = session.documents.read_document(**view['sources'][0]['read_action']['arguments'])
        while document['continuation']:
            document = session.documents.read_document(**document['continuation']['arguments'])
        self.assertIn('TAIL-SOURCE', document['content'])
        raw = read_history(session, page['record_id'], view='raw', limit=24000)
        self.assertEqual(raw['content'], json.dumps(session.record_entry(page['record_id']), ensure_ascii=False)[:24000])
        self.assertEqual(raw['offset_basis'], 'event_json')
        session.clear()
        with self.assertRaises(ValueError):
            read_history(session, page['record_id'])

    def test_history_text_paging_is_stable_when_run_status_changes(self):
        session = Session()
        run_id = begin(session)
        record_id = session.record('model_completed', run_id=run_id, message={
            'role': 'assistant', 'content': '模型消息原文。' * 1000}, finish_reason='stop')
        first = read_history(session, record_id, limit=1000)
        session.record('run_ended', run_id=run_id, status='completed')
        parts = [first['content']]
        result = first
        while result['continuation']:
            result = read_history(session, **{k:v for k,v in result['continuation']['arguments'].items()})
            parts.append(result['content'])
        whole = read_history(session, record_id, limit=24000)
        self.assertEqual(''.join(parts), whole['content'])
        self.assertIn('历史模型消息原文', whole['content'])
        self.assertEqual(whole['run_status'], 'completed')

    def test_history_source_navigation_keeps_original_section_and_body_position(self):
        session = Session()
        body = '# Guide\n\n## Intro\n' + 'intro text. ' * 600 + '\n\n## Restrictions\nTAIL-RESTRICTION\n'
        page = add_page(session, body)
        section = session.documents.read_section(page['outline'][2]['section_ref'])
        complete_tool(session, 'read_section', section)
        record = session.records[-1]
        original = json.dumps(record, ensure_ascii=False)
        source = session.documents.record_sources(record)[0]
        self.assertEqual(source['section_ref'], section['section_ref'])
        self.assertEqual((source['start'], source['end']), (section['start'], section['end']))
        self.assertIn('TAIL-RESTRICTION', source['preview'])
        self.assertEqual(source['read_action']['tool'], 'read_section')
        reread = session.documents.read_section(**source['read_action']['arguments'])
        self.assertEqual(reread['content'], section['content'])
        self.assertEqual(json.dumps(record, ensure_ascii=False), original, '导航不能修改原始记录')

    def test_history_source_navigation_rereads_requested_reference_and_text_window(self):
        session = Session()
        body = 'OUTSIDE BEFORE\n\n' + 'Quoted paragraph.\n\n' * 800 + 'OUTSIDE AFTER'
        page = add_page(session, body)
        left, right = body.index('Quoted paragraph'), body.index('OUTSIDE AFTER')
        requested = f"{page['document_id']}@{left}-{right}"
        first = session.documents.read_document(requested, view='reference')
        second = session.documents.read_document(**first['continuation']['arguments'])
        complete_tool(session, 'read_document', second)
        source = session.documents.record_sources(session.records[-1])[0]
        self.assertEqual(source['requested_reference'], requested)
        self.assertEqual(source['evidence_ref'], second['evidence_ref'])
        self.assertEqual(source['start'], second['start'])
        self.assertEqual(source['read_action']['arguments'], {'document_id': requested, 'view': 'reference'})
        window = session.documents.read_document(page['document_id'], start=left+100, limit=100)
        complete_tool(session, 'read_document', window)
        source = session.documents.record_sources(session.records[-1])[0]
        reread = session.documents.read_document(**source['read_action']['arguments'])
        self.assertEqual(reread['content'], window['content'])
        self.assertEqual(source['evidence_ref'], window['evidence_ref'])

    def test_history_source_navigation_keeps_distinct_find_hits_without_marking_delivery(self):
        from seekra.research import remember_delivery
        session = Session()
        body = 'Intro. ' * 1000 + '\nFIRST needle condition.\n' + 'gap ' * 300 + '\nSECOND needle exception.\n'
        page = add_page(session, body)
        found = session.documents.find_in_documents('needle', page['document_id'])
        complete_tool(session, 'find_in_documents', found)
        sources = session.documents.record_sources(session.records[-1])
        self.assertEqual(len(sources), 2, '同一资料中的不同命中需要分别保留入口')
        self.assertEqual([source['match_ref'] for source in sources],
                         [match['match_ref'] for match in found['matches']])
        for source, match in zip(sources, found['matches']):
            self.assertEqual(source['lookup_query'], 'needle')
            self.assertEqual(source['preview'], body[match['start']:min(match['start']+200, match['end'])])
            expanded = session.documents.read_document(**source['read_action']['arguments'])
            self.assertIn('needle', expanded['content'])
        delivery = {}
        remember_delivery(delivery, [{'role': 'tool', 'content': json.dumps(
            read_history(session, session.records[-1]['id']), ensure_ascii=False)}])
        self.assertEqual(delivery, {}, '历史导航和预览不能认证正文已交付')

    def test_sections_follow_markdown_structure_not_fences_or_duplicate_names(self):
        session = Session()
        body = ('Preamble\n\n# Guide\n\n## Requirements\nPro license.\n\n'
                '```md\n## Fake heading\n```\n\n### Windows\nWindows 10.\n\n'
                '## Limits\n30 days.\n\n## Requirements\nOther conditions.\n\n'
                'Appendix\n--------\nDetails.\n')
        add_page(session, body)
        docs = self.store(session)
        document_id = docs.list_documents()['documents'][0]['document_id']
        outline = docs.read_document(document_id, view='outline')['outline']
        self.assertEqual([s['title'] for s in outline],
                         ['Guide', 'Requirements', 'Windows', 'Limits', 'Requirements', 'Appendix'])
        first, second = outline[1], outline[4]
        self.assertNotEqual(first['section_ref'], second['section_ref'])
        section = docs.read_section(first['section_ref'])
        self.assertTrue(section['complete'])
        self.assertIn('Windows 10.', section['content'])
        self.assertNotIn('30 days.', section['content'])
        self.assertEqual(section['content'], body[first['start']:first['end']])
        self.assertEqual(outline[2]['path'], ['Guide', 'Requirements', 'Windows'])

    def test_long_section_pages_without_gaps_or_crossing_after_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            body = '# Guide\n\n## Requirements\n' + ('条件正文。' * 230 + '\n\n') * 12
            body += '最后一个限制。\n\n## Next\n不得跨入这里。\n'
            page = add_page(session, body)
            self.assertIn('outline', page, '首读需要提供真实小节目录')
            target = next(s for s in page['outline'] if s['title'] == 'Requirements')
            first = self.store(session).read_section(target['section_ref'])
            self.assertFalse(first['complete'])
            self.assertIsNotNone(first['next_cursor'])
            restored = Session.load(session.path)
            docs = self.store(restored)
            chunks = [first['content']]
            cursor, previous_end = first['next_cursor'], first['end']
            for _ in range(100):
                if cursor is None:
                    break
                result = docs.read_section(target['section_ref'], cursor=cursor)
                self.assertEqual(result['start'], previous_end)
                self.assertGreater(result['end'], previous_end)
                self.assertFalse(result['complete'], '末页不等于本次包含整节')
                chunks.append(result['content'])
                cursor, previous_end = result['next_cursor'], result['end']
            self.assertIsNone(cursor)
            self.assertEqual(''.join(chunks), body[target['start']:target['end']])
            self.assertEqual(previous_end, target['end'])

    def test_refresh_keeps_old_reference_and_reset_rejects_it(self):
        session = Session()
        first = add_page(session, '# Old\n旧条件。\n')
        self.assertIn('document_id', first, '页面结果需提供稳定引用')
        second = add_page(session, '# New\n新条件。\n')
        self.assertEqual(first['source_id'], second['source_id'])
        self.assertNotEqual(first['document_id'], second['document_id'])
        self.assertIn('旧条件', self.store(session).read_document(first['document_id'])['content'])
        session.clear()
        add_page(session, '# After reset\n另一任务。\n')
        with self.assertRaises(ValueError):
            self.store(session).read_document(first['document_id'])

    def test_literal_find_returns_readable_match_and_section(self):
        session = Session()
        body = '# Guide\n\n## Conditions\nRequires Pro.\n\n## Limits\nLicense expires in 30 days.\n'
        add_page(session, body)
        docs = self.store(session)
        document_id = docs.list_documents()['documents'][0]['document_id']
        found = docs.find_in_documents('30 days', document_id=document_id)
        match = found['matches'][0]
        self.assertEqual(body[match['match_range']['start']:match['match_range']['end']], '30 days')
        self.assertIn('License', docs.read_document(match['match_ref'])['content'])
        self.assertIn('30 days', docs.read_section(match['section_ref'])['content'])
        missing = docs.find_in_documents('不存在的词', document_id=document_id)
        self.assertEqual(missing['matches'], [])
        self.assertTrue(missing['scan_complete'])

    def test_saved_and_live_document_ranges_use_same_redacted_text(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            secret = 'secret-with-"-quote'
            session.secrets = (secret,)
            page = add_page(session, '# Guide\r\n\r\n## Terms\r\n' + secret + '\r\nEnd.\r\n')
            self.assertIn('document_id', page, '需要规范正文的稳定引用')
            live = self.store(session).read_document(page['document_id'])
            saved = self.store(Session.load(session.path)).read_document(page['document_id'])
            self.assertEqual(live, saved)
            self.assertNotIn(secret, live['content'])
            self.assertNotIn('\r', live['content'])
            raw = session.path.read_text(encoding='utf-8')
            self.assertNotIn(json.dumps(secret)[1:-1], raw)

    def test_model_can_follow_search_outline_section_and_local_find(self):
        session = Session()
        requests, seen, events = [], [], []
        body = '# Guide\n\n## Requirements\n需要专业版。\n\n## Limits\n每 30 天联网。\n'
        def handler(request):
            requests.append(request.url.path)
            if request.url.path == '/search':
                return httpx.Response(200, json={'results': [
                    {'title': '官方指南', 'url': URL, 'content': '条件见 Requirements。'}]})
            if request.url.path == '/extract':
                return httpx.Response(200, json={'results': [{'url': URL, 'raw_content': body}], 'failed_results': []})
            payload = json.loads(request.content)
            names = {t['function']['name'] for t in payload['tools']}
            self.assertTrue({'read_section', 'read_document', 'find_in_documents', 'list_documents'} <= names,
                            '正文能力必须接入模型实际可用的工具集合')
            seen.append(payload['messages'])
            turn = len(seen)
            if turn == 1:
                return reply(calls=[call('tavily_search', {'query': '软件启用条件'}, 's')])
            result = json.loads(payload['messages'][-1]['content'])
            if turn == 2:
                return reply(calls=[call('read_page', {'url': result['results'][0]['url']}, 'p')])
            if turn == 3:
                section = next(s for s in result['outline'] if s['title'] == 'Requirements')
                return reply(calls=[call('read_section', {'section_ref': section['section_ref']}, 'section')])
            if turn == 4:
                self.assertEqual(result['content'], '## Requirements\n需要专业版。\n\n')
                return reply(calls=[call('find_in_documents', {'query': '30 天', 'document_id': result['document_id']}, 'find')])
            self.assertIn('每 30 天联网', result['matches'][0]['content'])
            return reply('需要专业版，并每 30 天联网。')
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            answer = run_agent('核实条件', client=client, model='test', deepseek_api_key='fake-ds',
                               tavily_api_key='fake-tv', session=session,
                               on_event=lambda event, **kw: events.append((event, kw)))
        self.assertIn('30 天', answer)
        self.assertEqual(requests.count('/search'), 1)
        self.assertEqual(requests.count('/extract'), 1)
        delivered = [kw['result'] for event, kw in events if event == 'tool_result']
        self.assertEqual(delivered, [m[-1]['content'] for m in seen[1:]])

    def test_offloaded_views_keep_direct_references_and_actual_selection(self):
        session = Session()
        page = add_page(session, '# Guide\n\n## Conditions\n' + '证据' * 5000)
        doc = self.store(session)
        section_ref = page['outline'][1]['section_ref']
        for _ in range(3):
            complete_tool(session, 'read_section', doc.read_section(section_ref))
        messages = session.last_run['messages']
        selected, info = build_context(messages, [], session, max_chars=11500)
        old_views = [json.loads(m['content']) for m in selected if m['role'] == 'tool']
        compact = [v for v in old_views if v.get('offloaded')]
        self.assertTrue(compact)
        self.assertIn('document_id', compact[0], '外置入口必须直接指向正文')
        self.assertIn('证据', json.loads(selected[-1]['content'])['content'])
        self.assertIn('message_indices', info, '需要复核最终请求的实际选择')
        rebuilt = [info['replacements'].get(str(i), messages[i]) for i in info['message_indices']]
        self.assertEqual(rebuilt, selected)

    def test_invalid_document_tool_arguments_do_not_access_network(self):
        session = Session()
        add_page(session, '# Guide\n\n## Conditions\n条件。\n')
        with httpx.Client(transport=httpx.MockTransport(lambda r: self.fail('本地回查不得联网'))) as client:
            registry = default_registry(client, 'fake', session)
            for name, args in [('read_section', {'section_ref': 'missing'}),
                               ('read_document', {'document_id': '../private'}),
                               ('find_in_documents', {'query': ''}),
                               ('list_documents', {'limit': True})]:
                with self.subTest(name=name):
                    result = json.loads(registry.execute(call(name, args), {}, {}))
                    self.assertEqual(result['status'], 'failed')
                    self.assertEqual(result['error_code'], 'invalid_arguments')

    def test_match_read_honors_explicit_window_limit(self):
        session = Session()
        add_page(session, 'prefix ' * 1000 + '\n许可证条件在这里。\n' + 'suffix ' * 1000)
        docs = self.store(session)
        match = docs.find_in_documents('许可证')['matches'][0]
        result = docs.read_document(match['match_ref'], limit=100)
        self.assertLessEqual(len(result['content']), 100)
        self.assertIn('许可证', result['content'])

    def test_listing_and_find_cursors_keep_original_collection(self):
        session = Session()
        for i in range(12):
            add_page(session, f'无标题资料 {i}：needle。\n', url=f'{URL}/{i}')
        docs = self.store(session)
        listing = docs.list_documents(limit=5)
        findings = docs.find_in_documents('needle')
        self.assertIsNotNone(findings['next_cursor'])
        add_page(session, '新加入的 needle。\n', url=URL + '/new')
        docs = self.store(session)
        remaining = docs.list_documents(cursor=listing['next_cursor'], limit=20)
        found_tail = docs.find_in_documents('needle', cursor=findings['next_cursor'])
        self.assertEqual(len(listing['documents']) + len(remaining['documents']), 12)
        self.assertEqual(len(found_tail['matches']), 2)
        self.assertTrue(found_tail['scan_complete'])
        self.assertFalse(any('/new' in row['url'] for row in remaining['documents']))

    def test_outline_pagination_and_wrong_target_cursor(self):
        session = Session()
        page = add_page(session, '# Guide\n\n' + ''.join(
            f'## Heading {i}\n' + ('x' * 6500 if i == 0 else 'value') + '\n\n' for i in range(24)))
        docs = self.store(session)
        self.assertIsNotNone(page['outline_cursor'])
        rest = docs.read_document(page['document_id'], view='outline', cursor=page['outline_cursor'])
        self.assertEqual(len(page['outline']) + len(rest['outline']), 25)
        self.assertIsNone(rest['next_cursor'])
        first = docs.read_section(page['outline'][1]['section_ref'])
        self.assertIsNotNone(first['next_cursor'])
        with self.assertRaises(ValueError):
            docs.read_section(page['outline'][2]['section_ref'], cursor=first['next_cursor'])

    def test_failed_storage_does_not_publish_uncommitted_page(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            begin(session)
            # 在 tool_completed 落盘时失败，验证获取结果未变成可读资料。
            original_record = session.record
            def record(kind, **data):
                if kind == 'tool_completed':
                    with patch('seekra.session.os.fsync', side_effect=OSError('disk failed')):
                        return original_record(kind, **data)
                return original_record(kind, **data)
            with patch.object(session, 'record', side_effect=record), self.assertRaises(OSError):
                add_page(session, '# Not committed\n应不可读。\n')
            self.assertEqual(self.store(session).list_documents()['documents'], [])

    def test_continuation_identifies_its_tool_and_complete_arguments(self):
        session = Session()
        page = add_page(session, '# Guide\n\n## Terms\n' + 'long paragraph ' * 900)
        self.assertIn('continuation', page, '游标需附带所属工具和可直接复制的参数')
        self.assertEqual(page['continuation']['tool'], 'read_document')
        docs = self.store(session)
        next_page = docs.read_document(**page['continuation']['arguments'])
        self.assertEqual(next_page['start'], page['end'])
        section = docs.read_section(page['outline'][1]['section_ref'])
        action = section['continuation']
        self.assertEqual(action['tool'], 'read_section')
        self.assertEqual(action['arguments']['section_ref'], section['section_ref'])
        next_section = docs.read_section(**action['arguments'])
        self.assertEqual(next_section['start'], section['end'])

    def test_new_process_can_continue_the_same_saved_section(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            page = add_page(session, '# Guide\n\n## Conditions\n' + '正文和条件。' * 1800)
            section = session.documents.read_section(page['outline'][1]['section_ref'])
            script = ('import json,sys; from pathlib import Path; from seekra.session import Session; '
                      's=Session.load(Path(sys.argv[1])); '
                      'print(json.dumps(s.documents.read_section(sys.argv[2],cursor=sys.argv[3]),ensure_ascii=False))')
            process = subprocess.run([sys.executable, '-X', 'utf8', '-c', script,
                                      str(session.path), section['section_ref'], section['next_cursor']],
                                     capture_output=True, text=True, encoding='utf-8', timeout=15)
            self.assertEqual(process.returncode, 0, process.stderr)
            returned = json.loads(process.stdout)
            self.assertEqual(returned, session.documents.read_section(
                section['section_ref'], cursor=section['next_cursor']))

    def test_source_context_does_not_reintroduce_secret_from_user_question(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            secret = 'secret-mentioned-in-question'
            session.secrets = (secret,)
            session.record('run_started', run_id='r', question='核实资料 ' + secret, system='test', config={})
            page = add_page(session, '# Guide\n正常正文。\n')
            self.assertNotIn(secret, page['question'])
            self.assertNotIn(secret, session.path.read_text(encoding='utf-8'))
            self.assertEqual(session.documents.read_document(page['document_id']),
                             Session.load(session.path).documents.read_document(page['document_id']))


if __name__ == '__main__':
    unittest.main()
