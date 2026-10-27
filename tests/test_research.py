"""研究进展的引用、提交和实际上下文保留，不调用真实服务。"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from seekra.agent import run_agent, system_prompt
from seekra.context import build_context
from seekra.session import Session
from seekra.tools import default_registry
from test_agent import call, reply
from test_documents import add_page, begin, complete_tool, URL


def deliver(session, max_chars=120000):
    run = session.last_run
    selected, info = build_context(run['messages'], [], session, max_chars)
    session.record('context_selected', run_id=run['id'], **info)
    session.record('model_started', run_id=run['id'], round=run['calls']+1,
                   system=run['messages'][0]['content'])
    session.record('model_completed', run_id=run['id'],
                   message={'role': 'assistant', 'content': '已收到用于本次测试的输入'}, finish_reason='stop')
    return selected


def state(reference, revision=0):
    return {'base_revision': revision, 'goal': '核实离线使用条件', 'findings': [
        {'summary': '需要 Pro 许可证', 'conditions': '适用于当前文档版本',
         'status': 'supported', 'references': [reference]}],
        'gaps': ['尚未核实离线期限'], 'next_step': '读取期限所在小节'}


def pressure_budget(client, session, question):
    """实际初始协议之上留正文和旧轮次空间，再由交互触发80%阈值。"""
    messages=[{'role':'system','content':system_prompt()}, {'role':'user','content':question}]
    size=len(json.dumps({'messages':messages,'tools':default_registry(client,'fake',session).definitions},
                        ensure_ascii=False))
    return (size+6000)*5//4


class ResearchTests(unittest.TestCase):
    def execute(self, session, args):
        with httpx.Client(transport=httpx.MockTransport(lambda r: self.fail('进展工具不应联网'))) as client:
            registry = default_registry(client, 'fake', session)
            self.assertIn('update_research', {d['function']['name'] for d in registry.definitions},
                          '模型需要明确的进展更新工具')
            return json.loads(registry.execute(call('update_research', args), {}, {}))

    def save(self, session, args):
        result = self.execute(session, args)
        self.assertEqual(result['status'], 'success', result)
        return complete_tool(session, 'update_research', result)

    def test_update_is_published_only_after_commit_and_survives_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            page = add_page(session, '# Guide\nPro license required.\n')
            deliver(session)
            result = self.execute(session, state(page['reference']))
            self.assertIsNone(session.last_run.get('research'))
            complete_tool(session, 'update_research', result)
            saved = session.last_run['research']
            self.assertEqual(saved['revision'], 1)
            self.assertEqual(saved['state']['gaps'], ['尚未核实离线期限'])
            restored = Session.load(session.path)
            self.assertEqual(restored.last_run['research'], saved)
            next_state = state(page['reference'], revision=1)
            next_state['findings'][0]['status'] = 'uncertain'
            self.save(restored, next_state)
            self.assertEqual(restored.last_run['research']['revision'], 2)
            self.assertEqual(len([r for r in restored.records if r.get('research_version') == 1]), 2)

    def test_unseen_stored_text_and_only_selected_but_unsent_text_cannot_be_evidence(self):
        session = Session()
        page = add_page(session, '# Guide\n' + '正文。' * 3000)
        args = state(page['reference'])
        self.assertEqual(self.execute(session, args)['status'], 'failed')
        _, info = build_context(session.last_run['messages'], [], session)
        session.record('context_selected', run_id=session.last_run['id'], **info)
        self.assertEqual(self.execute(session, args)['status'], 'failed')
        session.record('model_started', run_id=session.last_run['id'], round=1, system='test')
        self.assertEqual(self.execute(session, args)['status'], 'failed',
                         '仅发起请求可能在传输前失败，需要已完成模型响应确认交付')
        deliver(session)
        self.assertEqual(self.execute(session, args)['status'], 'success')
        args['findings'][0]['references'][0] = {**page['reference'], 'end': page['total_characters']}
        self.assertEqual(self.execute(session, args)['status'], 'failed')

    def test_invalid_or_stale_update_keeps_latest_progress(self):
        session = Session()
        page = add_page(session, '# Guide\nPro license required.\n')
        deliver(session)
        self.save(session, state(page['reference']))
        saved = deepcopy(session.last_run['research'])
        variants = [state(page['reference']), state({**page['reference'], 'source_id': 'S999'}, 1),
                    state({**page['reference'], 'end': page['total_characters']+1}, 1)]
        oversized = state(page['reference'], 1)
        oversized['goal'] = 'x' * 10000
        variants.append(oversized)
        no_evidence = state(page['reference'], 1)
        no_evidence['findings'][0]['references'] = []
        variants.append(no_evidence)
        for args in variants:
            with self.subTest(args=str(args)[:100]):
                result = self.execute(session, args)
                self.assertEqual(result['status'], 'failed')
                complete_tool(session, 'update_research', result)
                self.assertEqual(session.last_run['research'], saved)

    def test_reference_error_locates_finding_gap_and_unchanged_revision(self):
        session = Session()
        page = add_page(session, 'x' * 4000)
        tail = session.documents.read_document(page['document_id'], start=2000, limit=100)
        complete_tool(session, 'read_document', tail)
        deliver(session)
        self.save(session, state(page['reference']))
        saved = deepcopy(session.last_run['research'])
        args = state(page['reference'], revision=1)
        args['findings'].append(deepcopy(args['findings'][0]))
        args['findings'][1]['references'] = [{**page['reference'], 'start': 1790, 'end': 2100}]
        result = self.execute(session, args)
        self.assertEqual(result['status'], 'failed')
        for text in ('findings[1]', 'references[0]', page['document_id'], '[1800,2000)', 'base_revision', '1'):
            self.assertIn(text, result['error'])
        complete_tool(session, 'update_research', result)
        self.assertEqual(session.last_run['research'], saved)
        args['findings'][1]['references'] = [
            {**page['reference'], 'start': 1790, 'end': 1800}, tail['reference']]
        self.assertEqual(self.execute(session, args)['status'], 'success')
        args['findings'][1]['references'][0]['source_id'] = 'S999'
        result = self.execute(session, args)
        self.assertIn('findings[1]', result['error'])
        self.assertIn('references[0]', result['error'])

    def test_evidence_ref_is_resolved_and_saved_without_manual_coordinates(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            page = add_page(session, '# Guide\nA Pro license is required.\n' + 'x' * 4000)
            hit = session.documents.find_in_documents('license', page['document_id'])['matches'][0]
            complete_tool(session, 'find_in_documents', {'operation': 'find_in_documents',
                          'status': 'success', 'matches': [hit]})
            deliver(session)
            self.save(session, state(hit['evidence_ref']))
            restored = Session.load(session.path)
            stored = restored.last_run['research']['state']['findings'][0]['references'][0]
            self.assertEqual(stored, hit['reference'])
            self.assertIn('Pro license is required', restored.documents.read_document(hit['evidence_ref'])['content'])
            args = state(f"{page['document_id']}@2000-2100", revision=1)
            self.assertEqual(self.execute(restored, args)['status'], 'failed')
            restored.clear()
            begin(restored)
            self.assertEqual(self.execute(restored, state(hit['evidence_ref']))['status'], 'failed')

    def test_latest_progress_remains_when_earlier_body_is_offloaded(self):
        session = Session()
        page = add_page(session, '# Guide\n\n## Terms\n' + 'Pro license required. ' * 650)
        section = session.documents.read_section(page['outline'][1]['section_ref'])
        complete_tool(session, 'read_section', section)
        deliver(session)
        args = state(section['reference'])
        args['findings'][0]['summary'] += '保留条件。' * 250
        saved = self.save(session, args)
        complete_tool(session, 'read_section', section)
        messages = session.last_run['messages']
        selected, info = build_context(messages, [], session, 12000)
        results = [json.loads(m['content']) for m in selected if m['role'] == 'tool']
        self.assertIn(saved, results, '最新进展不能被当作普通大工具结果外置')
        self.assertTrue(any(r.get('offloaded') and r.get('document_id') for r in results))
        self.assertEqual(info['research_revision'], 1)
        self.assertEqual(selected, [info['replacements'].get(str(i), messages[i]) for i in info['message_indices']])

    def test_new_task_and_reset_do_not_inherit_current_progress(self):
        session = Session()
        page = add_page(session, '# Guide\nPro license required.\n')
        deliver(session)
        self.save(session, state(page['reference']))
        previous = session.last_run['id']
        session.record('run_ended', run_id=previous, status='completed')
        begin(session)
        self.assertIsNone(session.last_run.get('research'))
        self.assertEqual(session.runs[previous]['research']['revision'], 1)
        selected, info = build_context(session.last_run['messages'], [], session)
        historical = [json.loads(m['content']) for m in selected if m['role'] == 'tool'
                      and json.loads(m['content']).get('operation') == 'update_research']
        self.assertEqual(historical[0]['content_kind'], 'historical_research_state')
        self.assertIn('当前任务', historical[0]['notice'])
        self.assertEqual(info['research_revision'], 0)
        self.assertTrue(info['replacements'], '历史标识必须记录到实际请求投影')
        self.assertEqual(self.execute(session, state(page['reference']))['status'], 'failed')
        session.clear()
        begin(session)
        self.assertIsNone(session.last_run.get('research'))
        self.assertEqual(self.execute(session, state(page['reference']))['status'], 'failed')

    def test_storage_failure_keeps_last_committed_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            page = add_page(session, '# Guide\nPro license required.\n')
            deliver(session)
            self.save(session, state(page['reference']))
            saved = deepcopy(session.last_run['research'])
            candidate = self.execute(session, state(page['reference'], 1))
            run_id = session.last_run['id']
            session.record('model_completed', run_id=run_id, message={
                'role': 'assistant', 'content': None, 'tool_calls': [call('update_research', {}, 'bad-write')]},
                finish_reason='tool_calls')
            session.record('tool_started', run_id=run_id, call_id='bad-write', tool_name='update_research')
            with patch('seekra.session.os.fsync', side_effect=OSError('disk failed')), self.assertRaises(OSError):
                session.record('tool_completed', run_id=run_id, call_id='bad-write', tool_name='update_research',
                               result=json.dumps(candidate), page_updates={})
            self.assertEqual(session.last_run['research'], saved)

    def test_redacted_progress_is_identical_after_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            secret = 'private-"-key'
            session.secrets = (secret,)
            page = add_page(session, '# Guide\nPro license required.\n')
            deliver(session)
            args = state(page['reference'])
            args['goal'] += secret
            result = self.save(session, args)
            self.assertNotIn(secret, result['state']['goal'])
            self.assertEqual(Session.load(session.path).last_run['research'], result)
            self.assertNotIn(json.dumps(secret)[1:-1], session.path.read_text(encoding='utf-8'))

    def test_offloaded_windows_do_not_count_as_delivered_evidence(self):
        session = Session()
        page = add_page(session, '# Guide\n' + '正文。' * 7000)
        middle = session.documents.read_document(page['document_id'], start=7000)
        complete_tool(session, 'read_document', middle)
        tail = session.documents.read_document(page['document_id'], start=13000)
        complete_tool(session, 'read_document', tail)
        selected = deliver(session, max_chars=10000)
        outputs = [json.loads(m['content']) for m in selected if m['role']=='tool']
        self.assertTrue(outputs[0].get('offloaded'))
        self.assertTrue(outputs[1].get('offloaded'))
        self.assertEqual(self.execute(session, state(page['reference']))['status'], 'failed')
        self.assertEqual(self.execute(session, state(middle['reference']))['status'], 'failed')
        self.assertEqual(self.execute(session, state(tail['reference']))['status'], 'success')
        # 即使两端已读，中间缺口也不能被一个连续引用掩盖。
        short = session.documents.read_document(page['document_id'], start=0, limit=100)
        complete_tool(session, 'read_document', short)
        deliver(session, max_chars=10000)
        across_gap = {**tail['reference'], 'start': 0}
        self.assertEqual(self.execute(session, state(across_gap))['status'], 'failed')

    def test_offloaded_navigation_keeps_only_actually_delivered_reference_ranges(self):
        session = Session()
        page = add_page(session, 'x' * 9000)
        session.last_run['delivered'] = {page['document_id']: [(0, 200), (500, 800)]}
        latest = session.documents.read_document(page['document_id'], start=2000)
        complete_tool(session, 'read_document', latest)
        selected, _ = build_context(session.last_run['messages'], [], session, 9500)
        offloaded = next(json.loads(m['content']) for m in selected if m['role'] == 'tool'
                         and json.loads(m['content']).get('offloaded'))
        self.assertEqual([row['evidence_ref'] for row in offloaded['delivered_reads']],
                         [f"{page['document_id']}@0-200", f"{page['document_id']}@500-800"])
        for row in offloaded['delivered_reads']:
            self.assertEqual(row['read_action']['arguments'], {'document_id': row['evidence_ref'], 'view': 'reference'})

    def test_unrecognized_tool_payload_does_not_break_delivery_replay(self):
        for rows in (None, 'not a list', [None, 'not a row']):
            with self.subTest(rows=rows):
                session = Session()
                begin(session)
                # 扩展工具的未知内容仍是执行记录，不能让进展索引使记录无法重放。
                complete_tool(session, 'custom', {'operation': 'find_in_documents', 'status': 'success',
                                                  'matches': rows})
                deliver(session)
                self.assertEqual(session.last_run.get('delivered'), {})

    def test_real_loop_saves_progress_then_resumes_and_reads_same_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            requests = []
            def handler(request):
                if request.url.path == '/extract':
                    return httpx.Response(200, json={'results': [{'url': URL,
                        'raw_content': '# Guide\n\n## Terms\nPro license required.\n'}], 'failed_results': []})
                body = json.loads(request.content)
                requests.append(body)
                if len(requests) == 1:
                    return reply(calls=[call('read_page', {'url': URL}, 'page')])
                if len(requests) == 2:
                    page = json.loads(body['messages'][-1]['content'])
                    return reply(calls=[call('update_research', state(page['reference']), 'progress')])
                raise httpx.ConnectError('中断')
            with httpx.Client(transport=httpx.MockTransport(handler)) as client, self.assertRaises(httpx.ConnectError):
                run_agent('核实使用条件', client=client, model='test', deepseek_api_key='ds-key',
                          tavily_api_key='tv-key', session=session)
            restored = Session.load(session.path)
            saved = restored.last_run['research']
            self.assertEqual(saved['revision'], 1)
            turns = 0
            def resume(request):
                nonlocal turns
                self.assertNotEqual(request.url.host, 'api.tavily.com')
                body = json.loads(request.content)
                self.assertEqual(body['thinking'], {'type':'enabled'})
                self.assertIn('research_state', str(body['messages']), '原生重载可在恢复资料中保留进展')
                turns += 1
                if turns == 1:
                    ref = saved['state']['findings'][0]['references'][0]
                    return reply(calls=[call('read_document', {'document_id': ref['document_id']}, 'back')])
                self.assertIn('Pro license required.', json.loads(body['messages'][-1]['content'])['content'])
                return reply('有原文支持；期限仍待核实。')
            with httpx.Client(transport=httpx.MockTransport(resume)) as client:
                answer = run_agent(None, client=client, model='test', deepseek_api_key='ds-key',
                                   tavily_api_key='tv-key', session=restored, resume=True)
            self.assertIn('期限仍待核实', answer)

    def test_pressure_compacts_without_progress_and_resumes_reading_original(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            requests, summaries = [], []
            def handler(request):
                if request.url.path == '/extract':
                    return httpx.Response(200, json={'results': [{'url': URL,
                        'raw_content': '# Guide\nPro license required.\n'}], 'failed_results': []})
                body = json.loads(request.content)
                if not body.get('tools'):
                    summaries.append(body)
                    return reply('已读 S1-P1，指南要求 Pro 许可证；原问题仍需完成。')
                requests.append(body)
                if len(requests) == 1:
                    return reply(calls=[call('read_page', {'url': URL}, 'page')])
                if '[harness 提供的恢复资料]' in str(body['messages']):
                    raise httpx.ConnectError('压缩提交后断网')
                return reply('旧的调查步骤。' * 180,
                             calls=[call('list_documents', {}, f'index-{len(requests)}')])
            with httpx.Client(transport=httpx.MockTransport(handler)) as client, self.assertRaises(httpx.ConnectError):
                budget=pressure_budget(client,session,'核实许可证条件')
                run_agent('核实许可证条件', client=client, model='test', deepseek_api_key='ds',
                          tavily_api_key='tv', session=session, max_context_chars=budget)
            self.assertEqual(len(summaries), 1)
            restored = Session.load(session.path)
            self.assertIsNone(restored.last_run['research'])
            self.assertEqual(restored.last_run['provider_window']['reason'], 'compaction')
            self.assertIn('旧的调查步骤', str(restored.records))
            calls = 0
            def resume(request):
                nonlocal calls
                self.assertNotEqual(request.url.host, 'api.tavily.com')
                body = json.loads(request.content)
                messages = body['messages']
                self.assertIn('tavily_search', {d['function']['name'] for d in body['tools']})
                self.assertIn('指南要求 Pro', str(messages))
                calls += 1
                if calls == 1:
                    return reply(calls=[call('read_document', {'document_id': 'S1-P1'}, 'back')])
                self.assertIn('Pro license required', messages[-1]['content'])
                return reply('有原文支持：需要 Pro 许可证。')
            with httpx.Client(transport=httpx.MockTransport(resume)) as client:
                answer = run_agent(None, client=client, model='test', deepseek_api_key='ds',
                                   tavily_api_key='tv', session=restored, resume=True, max_context_chars=budget)
            self.assertIn('Pro', answer)
            for request in requests:
                known = set()
                for message in request['messages']:
                    known.update(c['id'] for c in message.get('tool_calls', []))
                    if message['role'] == 'tool':
                        self.assertIn(message['tool_call_id'], known)


if __name__ == '__main__':
    unittest.main()
