"""预算压力下的进展保存阶段，验证真实循环、工具执行和重载边界。"""
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from seekra.agent import run_agent
from seekra.session import Session
from seekra.checkpoint import blocked_call
from test_agent import call, reply
from test_research import state, deliver, pressure_budget
from test_documents import add_page


URL = 'https://example.org/conditions'
PAGE = '# Guide\n\n## Conditions\nPro license required.\n'
REFERENCE = f'S1-P1@0-{len(PAGE.strip())}'


class CheckpointTests(unittest.TestCase):
    def test_soft_budget_offloads_old_sources_before_restricting_tools_and_survives_reload(self):
        from copy import deepcopy
        from seekra.checkpoint import prepare_context
        from seekra.context import build_context
        from seekra.tools import default_registry
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
        from seekra.checkpoint import prepare_context
        from seekra.context import build_context
        from seekra.tools import default_registry
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
        from seekra.checkpoint import prepare_context
        from seekra.context import build_context
        from seekra.tools import default_registry
        session = Session()
        latest = add_page(session, '# Guide\n\n' + 'body. ' * 1000)
        definitions = default_registry(None, 'fake', session).definitions
        _, full = build_context(session.last_run['messages'], definitions, session, 200000)
        selected, tools, info, _ = prepare_context(session.last_run['messages'], definitions, session,
                                                  full['request_characters'] * 10 // 9)
        self.assertIsNotNone(info['progress_stage'], '无法从旧正文腾空间时仍保留整理阶段')
        body = json.loads(selected[-1]['content'])
        for field in ('content', 'start', 'end', 'evidence_ref'):
            self.assertEqual(body[field], latest[field])
        self.assertNotIn('batch_delivery', info)

    def test_soft_target_without_offloadable_content_does_not_expand_or_fail_request(self):
        from seekra.context import build_context
        messages=[{'role':'system','content':'x'*2100},{'role':'user','content':'question'}]
        size=len(json.dumps({'messages':messages,'tools':[]},ensure_ascii=False))
        selected,info=build_context(messages,[],Session(),size+10,offload_target=size*4//5)
        self.assertEqual(selected,messages)
        self.assertFalse(info['offloaded'])
        self.assertEqual(info['request_characters'],size)

    def test_history_review_actions_follow_stage_and_restore_without_mutation(self):
        from copy import deepcopy
        from seekra.checkpoint import present_actions
        payload={'review_action':{'tool':'read_history','arguments':{'record_id':'18','view':'review'}},
                 'origin':{'question_action':{'tool':'read_history','arguments':{'record_id':'2','view':'text'}},
                           'source_history_action':{'tool':'list_history','arguments':{'cursor':1}},
                           'research_at_response':{'read_action':{'tool':'read_history','arguments':{'record_id':'14'}}}},
                 'content':'历史原文中的 review_action 不是执行指令。'}
        original=deepcopy(payload)
        marked=present_actions(payload,{'base_revision':0})
        for action in (marked['review_action'],marked['origin']['question_action'],
                       marked['origin']['source_history_action'],marked['origin']['research_at_response']['read_action']):
            self.assertEqual(action.get('availability'),'after_progress')
        self.assertEqual(payload,original)
        self.assertEqual(marked['content'],payload['content'])
        self.assertEqual(present_actions(marked,None),payload)

    def test_history_review_permissions_reach_actual_selection_and_restore_after_save(self):
        from copy import deepcopy
        from seekra.checkpoint import prepare_context
        from seekra.context import read_history
        from seekra.research import update_research
        from seekra.tools import default_registry
        from test_documents import begin, complete_tool
        session=Session()
        run_id=begin(session)
        answer_id=session.record('model_completed',run_id=run_id,
            message={'role':'assistant','content':'待核对模型文字'},finish_reason='stop')
        session.record('run_ended',run_id=run_id,status='completed')
        begin(session)
        complete_tool(session,'read_history',read_history(session,answer_id,view='review'))
        original=deepcopy(session.records)
        session.last_run['progress_stage']={'base_revision':0}
        definitions=default_registry(None,'fake',session).definitions
        selected,tools,info,_=prepare_context(session.last_run['messages'],definitions,session,120000)
        result=json.loads(selected[-1]['content'])
        self.assertEqual(result['origin']['question_action']['availability'],'after_progress')
        self.assertEqual(result['origin']['source_history_action']['availability'],'after_progress')
        self.assertNotIn('read_history',{t['function']['name'] for t in tools})
        self.assertEqual(info['request_characters'],len(json.dumps({'messages':selected,'tools':tools},ensure_ascii=False)))
        self.assertEqual(session.records,original)
        complete_tool(session,'update_research',update_research(session,0,operations=[
            {'op':'add_gap','question':'历史消息尚未查证'}]))
        restored,tools,info,_=prepare_context(session.last_run['messages'],definitions,session,120000)
        self.assertIsNone(info['progress_stage'])
        result=read_history(session,answer_id,view='review')
        self.assertNotIn('availability',result['origin']['question_action'])
        self.assertIn('read_history',{t['function']['name'] for t in tools})

    def test_system_compaction_guidance_respects_restricted_tools(self):
        from seekra.checkpoint import prepare_context
        from seekra.research import update_research
        from seekra.tools import default_registry
        from test_documents import complete_tool
        session=Session();page=add_page(session,'# Guide\n'+'Body. '*1800);deliver(session)
        complete_tool(session,'update_research',update_research(session,0,operations=[
            {'op':'add_gap','question':'未读条件'}]))
        complete_tool(session,'read_document',session.documents.read_document(page['document_id']))
        session.last_run['progress_stage']={'base_revision':1}
        with httpx.Client() as client:definitions=default_registry(client,'fake',session).definitions
        selected,_,info,_=prepare_context(session.last_run['messages'],definitions,session,18000)
        self.assertGreater(info['compacted_messages'],0)
        system=selected[0]['content']
        self.assertNotIn('用 list_documents、read_section',system)
        self.assertNotIn('可调用 list_history',system)
        self.assertIn('普通资料和历史回查在保存进展后恢复',system)

    def test_offloaded_guidance_matches_stage_and_restores_without_changing_records(self):
        from seekra.context import _reference
        from seekra.checkpoint import present_actions
        cases=[{'operation':'read_section','content_kind':'page_text','document_id':'S1-P1',
                'section_ref':'S1-P1-H2','start':0,'end':30,'content':'Original source text.'},
               {'operation':'tavily_search','results':[{'document_id':'S1-R1','url':URL,
                                                      'content':'Search snippet.'}]},
               {'operation':'update_research','content_kind':'research_state','revision':1},
               {'operation':'custom','status':'success'}]
        for payload in cases:
            with self.subTest(operation=payload['operation']):
                reference=_reference(payload,'record-1',json.dumps(payload),{'S1-P1':[(0,30)]})
                before=json.dumps(reference,ensure_ascii=False)
                marked=present_actions(reference,{'base_revision':1})
                self.assertIn('整理阶段',marked['notice'])
                self.assertIn('保存',marked['notice'])
                self.assertNotIn('用 read_section(section_ref)',marked['notice'])
                if marked.get('document_id'):
                    self.assertEqual(marked['delivered_reads'][0]['read_action']['availability'],'local_reference')
                    self.assertEqual(marked['preview'],payload['content'])
                self.assertEqual(json.dumps(reference,ensure_ascii=False),before)
                self.assertEqual(present_actions(marked,None),reference)

    def test_actual_stage_request_offloads_body_with_consistent_guidance_and_budget(self):
        from copy import deepcopy
        from seekra.checkpoint import prepare_context
        from seekra.tools import default_registry
        from test_documents import complete_tool
        session=Session()
        page=add_page(session,'# Guide\n\n## Conditions\n'+'Known condition.\n\n'*1300);deliver(session)
        complete_tool(session,'read_document',session.documents.read_document(page['document_id'],start=4000))
        deliver(session)
        complete_tool(session,'read_document',session.documents.read_document(page['document_id'],start=11000))
        session.last_run['progress_stage']={'base_revision':0}
        original=deepcopy(session.records)
        with httpx.Client() as client:
            definitions=default_registry(client,'fake',session).definitions
        selected,tools,info,_=prepare_context(session.last_run['messages'],definitions,session,18000)
        references=[json.loads(m['content']) for m in selected if m['role']=='tool'
                    and json.loads(m['content']).get('offloaded')]
        self.assertTrue(references,'测试必须实际外置早期正文')
        self.assertTrue(all('整理阶段' in row['notice'] for row in references))
        self.assertEqual({t['function']['name'] for t in tools},{'update_research','read_document'})
        self.assertEqual(info['request_characters'],len(json.dumps({'messages':selected,'tools':tools},ensure_ascii=False)))
        self.assertLessEqual(info['request_characters'],18000)
        self.assertEqual(session.records,original)

    def test_valid_ordinary_read_is_deferred_without_delivery_or_argument_loss(self):
        from copy import deepcopy
        from seekra.checkpoint import prepare_context
        from seekra.research import update_research
        from seekra.provider_context import _recovery_snapshot
        from seekra.tools import default_registry
        from test_documents import complete_tool
        session=Session()
        page=add_page(session,'# Guide\n\n## Conditions\n'+'Known text.\n\n'*700)
        deliver(session)
        read=session.documents.read_document(page['document_id'])
        session.last_run['progress_stage']={'base_revision':0}
        before=deepcopy(session.last_run['delivered'])
        for args in ({'document_id':page['document_id'],'view':'text','cursor':read['next_cursor']},
                     {'document_id':page['document_id'],'start':3000,'limit':1200}):
            with self.subTest(args=args):
                blocked=json.loads(blocked_call(call('read_document',args),session))
                self.assertEqual(blocked['error_code'],'progress_required')
                self.assertEqual(blocked['deferred_action']['arguments'],args)
                self.assertEqual(session.last_run['delivered'],before)
        complete_tool(session,'read_document',blocked)
        self.assertEqual(_recovery_snapshot(session)['recent_results'][0]['deferred_action'],blocked['deferred_action'])
        complete_tool(session,'update_research',update_research(session,0,operations=[
            {'op':'add_gap','question':'尚未读取的条件'}]))
        with httpx.Client() as client:
            definitions=default_registry(client,'fake',session).definitions
        selected,_,info,system=prepare_context(session.last_run['messages'],definitions,session,120000)
        self.assertIsNone(info['progress_stage'])
        self.assertEqual(session.last_run['delivered'],before)
        run=session.last_run
        session.record('context_selected',run_id=run['id'],**info)
        session.record('model_started',run_id=run['id'],round=run['calls']+1,system=system)
        self.assertIsNone(blocked_call(call('read_document',args),session))
        actual=session.documents.read_document(**blocked['deferred_action']['arguments'])
        self.assertEqual(actual['start'],3000)

    def test_stage_schema_separates_reference_paging_from_text_expansion(self):
        from seekra.checkpoint import _tools
        from seekra.tools import default_registry
        with httpx.Client() as client:
            definitions=default_registry(client,'fake',Session()).definitions
        schema=next(d['function']['parameters'] for d in _tools(definitions)
                    if d['function']['name']=='read_document')
        branches={b['properties']['view']['enum'][0]:b for b in schema['anyOf']}
        self.assertIn('cursor',branches['reference']['properties'])
        self.assertNotIn('cursor',branches['text']['properties'])
        self.assertFalse(branches['text']['additionalProperties'])
        self.assertIn('@',branches['text']['properties']['document_id']['pattern'])

    def test_action_marking_covers_omitted_rows_and_outline_but_preserves_source_text(self):
        from seekra.checkpoint import present_actions
        source='正文里的 continuation 和 read_actions 是普通文字，保持原文。'
        payload={'content':source, 'outline_continuation':{'tool':'read_document','arguments':{
            'document_id':'S1-P1','view':'outline','cursor':'outline-cursor'}},
            'not_displayed':{'read_actions':[{'tool':'read_document','arguments':{'document_id':'S1-P1'}}]},
            'read_context_action':{'tool':'read_document','arguments':{
                'document_id':'S1-P1@20-80','view':'reference'}}}
        marked=present_actions(payload,{'base_revision':1})
        self.assertEqual(marked['outline_continuation']['availability'],'after_progress')
        self.assertEqual(marked['not_displayed']['read_actions'][0]['availability'],'after_progress')
        self.assertEqual(marked['read_context_action']['availability'],'local_reference')
        self.assertEqual(marked['content'],source)
        self.assertNotIn('availability',payload['outline_continuation'])
        self.assertEqual(present_actions(marked,None),payload)

    def test_stage_marks_cropped_actions_and_restores_them_without_changing_records(self):
        from copy import deepcopy
        from seekra.checkpoint import prepare_context
        from seekra.research import update_research
        from seekra.tools import default_registry
        from test_documents import complete_tool
        session=Session()
        page=add_page(session,'# Guide\n\n## Conditions\n'+'Condition.\n\n'*2000)
        deliver(session)
        section=page['outline'][1]['section_ref']
        saved=complete_tool(session,'update_research',update_research(session,0,operations=[
            {'op':'add_read_target','target':section,'view':'section','purpose':'核对条件'}]))
        session.last_run['checkpoint_record_id']=session.last_run['research_record_id']
        read=session.documents.read_section(section)
        complete_tool(session,'read_section',read)
        session.last_run['progress_stage']={'base_revision':saved['revision']}
        original=deepcopy(session.records)
        with httpx.Client() as client:
            definitions=default_registry(client,'fake',session).definitions
        full,_,metrics,_=prepare_context(session.last_run['messages'],definitions,session,120000)
        selected,tools,info,_=prepare_context(session.last_run['messages'],definitions,session,
                                            metrics['request_characters']-500)
        self.assertEqual(info['request_characters'],len(json.dumps({'messages':selected,'tools':tools},ensure_ascii=False)))
        self.assertEqual(info['batch_delivery']['status'],'reflowed')
        body=json.loads(selected[-1]['content'])
        self.assertEqual(body['continuation']['availability'],'after_progress')
        progress=next(json.loads(m['content']) for m in selected if m['role']=='tool' and
                      json.loads(m['content']).get('content_kind')=='research_state')
        self.assertEqual(progress['navigation'][0]['read_action']['availability'],'after_progress')
        self.assertEqual(session.records,original)
        complete_tool(session,'update_research',update_research(session,1,operations=[]))
        normal,_,info,_=prepare_context(session.last_run['messages'],definitions,session,120000)
        self.assertIsNone(info['progress_stage'])
        restored=json.loads(normal[-1]['content'])
        self.assertNotIn('availability',restored['navigation'][0]['read_action'])

    def test_local_correction_rejects_sections_unread_ranges_and_wrong_cursor(self):
        session = Session()
        page = add_page(session, '# Guide\n\n## Conditions\n' + 'condition text. ' * 400)
        deliver(session)
        session.last_run['progress_stage'] = {'base_revision': 0}
        section_ref = page['outline'][1]['section_ref']
        cases = [({'document_id': section_ref, 'view': 'reference'}, 'read_section'),
                 ({'document_id': f"{page['document_id']}@2000-2100", 'view': 'reference'}, 'gaps'),
                 ({'document_id': page['evidence_ref'], 'view': 'text', 'cursor': 'wrong'}, 'cursor')]
        for args, hint in cases:
            with self.subTest(args=args):
                error = json.loads(blocked_call(call('read_document', args), session))
                self.assertEqual(error['error_code'], 'invalid_arguments')
                self.assertIn(hint, error['error'])
        self.assertIsNone(blocked_call(call('read_document', {'document_id': page['evidence_ref'],
                                                             'view': 'reference'}), session))

    def start_pressure(self, handler, session):
        """用多轮工具交互积累上下文，不依赖测试修改阈值。"""
        count = 0
        extractions = 0
        def transport(request):
            nonlocal count, extractions
            if request.url.host == 'api.tavily.com':
                extractions += 1
                self.assertEqual(extractions, 1, '整理阶段及快照回读不能重新联网')
                self.assertEqual(request.url.path, '/extract')
                return httpx.Response(200, json={'results': [{'url': URL, 'raw_content': PAGE}],
                                               'failed_results': []})
            body = json.loads(request.content)
            selection = next(r for r in reversed(session.records) if r['kind'] == 'context_selected')
            self.assertEqual(selection['request_tools'], body['tools'])
            self.assertEqual(selection['request_characters'],
                             len(json.dumps({'messages': body['messages'], 'tools': body['tools']}, ensure_ascii=False)))
            self.assertLessEqual(selection['request_characters'], budget)
            count += 1
            if count == 1:
                return reply(calls=[call('read_page', {'url': URL}, 'page')])
            stage = session.last_run.get('progress_stage')
            if not stage and not session.last_run.get('research'):
                self.assertLess(count, 12, '必须进入整理阶段，不能一直累积旧轮次')
                return reply('较早的研究步骤。' * 200, calls=[call('list_documents', {}, f'index-{count}')])
            return handler(body, count)
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            budget=pressure_budget(client,session,'核实许可证及尚未找到的期限条件')
            return run_agent('核实许可证及尚未找到的期限条件', client=client, model='test',
                             deepseek_api_key='fake-ds', tavily_api_key='fake-tv',
                             session=session, max_context_chars=budget, thinking=False)

    def test_stage_restricts_tools_then_saves_and_restores_reading(self):
        session = Session()
        phase = 0
        def handler(body, count):
            nonlocal phase
            phase += 1
            names = {d['function']['name'] for d in body['tools']}
            if phase == 1:
                self.assertEqual(names, {'update_research', 'read_document'})
                self.assertIn('临时整理阶段', body['messages'][0]['content'])
                return reply(calls=[call('tavily_search', {'query': '不应执行的搜索'}, 'blocked')])
            if phase == 2:
                failure = json.loads(body['messages'][-1]['content'])
                self.assertEqual(failure['error_code'], 'progress_required')
                self.assertIsNone(session.last_run.get('checkpoint_record_id'))
                return reply(calls=[call('update_research', state(REFERENCE), 'save')])
            if phase == 3:
                self.assertIn('tavily_search', names)
                self.assertIn('read_section', names)
                self.assertIsNone(session.last_run.get('progress_stage'))
                self.assertIn('普通搜索与阅读已恢复', body['messages'][0]['content'])
                self.assertIn('不代表研究已完成', body['messages'][0]['content'])
                self.assertNotIn('较早的研究步骤', str(body['messages']))
                self.assertIn('尚未核实离线期限', str(body['messages']))
                self.assertEqual(body['messages'][1]['content'], '核实许可证及尚未找到的期限条件')
                return reply(calls=[call('read_document', {'document_id': 'S1-P1'}, 'back')])
            self.assertIn('Pro license required', body['messages'][-1]['content'])
            return reply('许可证已核实；期限仍有缺口。')
        self.assertIn('期限仍有缺口', self.start_pressure(handler, session))
        self.assertIn('较早的研究步骤', str(session.records))
        self.assertIsNotNone(session.last_run['checkpoint_record_id'])

    def test_failed_save_and_reference_correction_remain_in_stage(self):
        session = Session()
        phase = 0
        def handler(body, count):
            nonlocal phase
            phase += 1
            if phase <= 3:
                self.assertIsNotNone(session.last_run.get('progress_stage'))
                self.assertNotIn('read_page', {d['function']['name'] for d in body['tools']})
                self.assertIn('较早的研究步骤', str(session.last_run['messages']))
            if phase == 1:
                args = state('S1-P1@0-9999')
                return reply(calls=[call('update_research', args, 'invalid')])
            if phase == 2:
                self.assertEqual(json.loads(body['messages'][-1]['content'])['status'], 'failed')
                self.assertIsNone(session.last_run.get('checkpoint_record_id'))
                return reply(calls=[call('read_document', {'document_id': REFERENCE,
                                                         'view': 'text'}, 'correct')])
            if phase == 3:
                result = json.loads(body['messages'][-1]['content'])
                self.assertIn('Pro license required', result['content'])
                return reply(calls=[call('update_research', state(result['evidence_ref']), 'valid')])
            self.assertIsNone(session.last_run.get('progress_stage'))
            return reply('已有依据；未核实期限。')
        self.assertIn('已有依据', self.start_pressure(handler, session))
        self.assertEqual(session.last_run['research']['revision'], 1)

    def test_stage_can_answer_without_saving(self):
        session = Session()
        def handler(body, count):
            self.assertIsNotNone(session.last_run.get('progress_stage'))
            return reply('需要 Pro 许可证；尚未核实期限。')
        self.assertIn('需要 Pro', self.start_pressure(handler, session))
        self.assertIsNone(session.last_run.get('research'))

    def test_success_does_not_enable_another_call_in_the_same_batch(self):
        session = Session()
        phase = 0
        def handler(body, count):
            nonlocal phase
            phase += 1
            if phase == 1:
                return reply(calls=[call('update_research', state(REFERENCE), 'save'),
                                    call('read_page', {'url': URL}, 'same-batch')])
            self.assertEqual(json.loads(body['messages'][-1]['content'])['error_code'], 'progress_required')
            self.assertEqual(session.last_run['research']['revision'], 1)
            self.assertIsNone(session.last_run.get('progress_stage'))
            return reply('保存已完成，期限仍需核实。')
        self.assertIn('保存已完成', self.start_pressure(handler, session))

    def test_repeated_pressure_saves_two_checkpoints(self):
        session = Session()
        stages = []
        def handler(body, count):
            self.assertLess(count, 30, '连续整理必须释放旧轮次占用')
            revision = (session.last_run.get('research') or {}).get('revision', 0)
            if revision == 2:
                self.assertIsNone(session.last_run.get('progress_stage'))
                self.assertIn('尚未核实离线期限', str(body['messages']))
                return reply('两次进展已保留，期限尚未核实。')
            if session.last_run.get('progress_stage') is not None:
                stages.append(session.last_run['progress_stage']['base_revision'])
                return reply(calls=[call('update_research', state(REFERENCE, revision), f'save-{revision}')])
            return reply('后续研究步骤。' * 200, calls=[call('list_documents', {}, f'next-{count}')])
        self.assertIn('两次进展', self.start_pressure(handler, session))
        self.assertEqual(stages, [0, 1])
        self.assertEqual(session.last_run['research']['revision'], 2)

    def test_unsent_stage_does_not_publish_or_change_last_sent_system(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            original = session.record
            def record(kind, **data):
                result = original(kind, **data)
                if kind == 'context_selected' and data.get('progress_stage') is not None:
                    raise KeyboardInterrupt()
                return result
            session.record = record
            with self.assertRaises(KeyboardInterrupt):
                self.start_pressure(lambda body, count: self.fail('尚未发出整理请求'), session)
            self.assertIsNone(session.last_run.get('progress_stage'))
            self.assertNotIn('临时整理阶段', session.last_run['messages'][0]['content'])
            restored = Session.load(session.path)
            self.assertEqual(restored.last_run['messages'], session.last_run['messages'])
            self.assertIsNone(restored.last_run.get('progress_stage'))

    def test_stage_survives_reload_and_blocks_pending_network_call(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            def handler(body, count):
                return reply(calls=[call('read_page', {'url': URL}, 'pending-network')])
            # 和 start_pressure 相同的资料与预算；只在完整响应后中断。
            original = session.record
            def record(kind, **data):
                result = original(kind, **data)
                if kind == 'model_completed' and any(c['id'] == 'pending-network'
                                                      for c in data['message'].get('tool_calls') or []):
                    raise KeyboardInterrupt()
                return result
            session.record = record
            with self.assertRaises(KeyboardInterrupt):
                self.start_pressure(handler, session)
            restored = Session.load(session.path)
            self.assertIsNotNone(restored.last_run.get('progress_stage'))
            self.assertIn('pending-network', restored.last_run['pending'])
            def resume(request):
                self.assertNotEqual(request.url.host, 'api.tavily.com')
                body = json.loads(request.content)
                self.assertEqual(json.loads(body['messages'][-1]['content'])['error_code'], 'progress_required')
                self.assertEqual({d['function']['name'] for d in body['tools']},
                                 {'update_research', 'read_document'})
                return reply('已有资料支持许可证条件，期限尚未确定。')
            with httpx.Client(transport=httpx.MockTransport(resume)) as client:
                budget=pressure_budget(client,restored,'核实许可证及尚未找到的期限条件')
                self.assertIn('期限尚未确定', run_agent(None, client=client, model='test',
                    deepseek_api_key='fake-ds', tavily_api_key='fake-tv', session=restored,
                    resume=True, max_context_chars=budget, thinking=False))
            self.assertEqual(Session.load(restored.path).last_run['messages'], restored.last_run['messages'])


if __name__ == '__main__':
    unittest.main()
