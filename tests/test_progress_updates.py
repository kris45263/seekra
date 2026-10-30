"""局部进展更新、导航与正文发现分工，以及持久恢复。"""
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from seekra.search.context import build_context
from seekra.search.state import SearchSession as Session
from seekra.search.tools import default_registry
from test_agent import call
from test_documents import add_page, complete_tool
from test_research import deliver, state


class ProgressUpdateTests(unittest.TestCase):
    def test_reading_state_moves_to_tail_without_rewriting_saved_progress(self):
        session=Session()
        page=add_page(session,'# Guide\n'+'Condition and context.\n'*600)
        deliver(session)
        saved=self.save(session,[
            {'op':'add_finding','summary':'待核实条件','conditions':'','status':'uncertain',
             'references':[page['evidence_ref']]},
            {'op':'add_read_target','target':page['document_id'],'view':'text','purpose':'核对剩余条件'}])
        initial,_=build_context(session.last_run['messages'],[],session)
        progress_index=len(initial)-1
        original=json.dumps(session.records,ensure_ascii=False)
        self.assertEqual(json.loads(initial[-1]['content'])['reading_state']['revision'],1)
        self.assertNotIn('navigation',json.loads(initial[-1]['content']))
        self.assertEqual(original,json.dumps(session.records,ensure_ascii=False))
        complete_tool(session,'read_document',session.documents.read_document(
            f"{page['document_id']}@1800-2400",view='reference'))
        first=deliver(session)
        current,info=build_context(session.last_run['messages'],[],session)
        old=json.loads(current[progress_index]['content'])
        self.assertNotIn('reading_state',old)
        self.assertEqual(old['state'],saved['state'])
        for messages in (first,current):
            payloads=[json.loads(m['content']) for m in messages if m['role']=='tool']
            self.assertEqual(sum('reading_state' in p for p in payloads),1)
        self.assertEqual(first[progress_index],current[progress_index])
        tail=next(json.loads(m['content']) for m in reversed(current) if m['role']=='tool')
        coverage=tail['reading_state']['document_coverage']['documents'][0]
        self.assertEqual(coverage['delivered_characters'],2400)
        self.assertEqual(coverage['current_input'],'part')
        self.assertEqual(tail['reading_state']['navigation'][0]['first_undelivered']['start'],2400)
        self.assertEqual(info['request_characters'],len(json.dumps({'messages':current,'tools':[]},ensure_ascii=False)))
        self.assertEqual(session.last_run['research'],saved)
        # 扩展工具可以返回空对象；动态字段的预算不能多算分隔符而触发多余外置。
        complete_tool(session,'extension_tool',{})
        full,metrics=build_context(session.last_run['messages'],[],session)
        exact,budgeted=build_context(session.last_run['messages'],[],session,metrics['request_characters'])
        self.assertEqual(exact,full)
        self.assertEqual(budgeted['request_characters'],metrics['request_characters'])

    def test_superseded_progress_is_historical_in_selected_input_and_after_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            session=Session.create(Path(directory))
            page=add_page(session,'# Guide\nOnly frozen files are supported.');deliver(session)
            first=self.save(session,[{'op':'add_gap','question':'是否要求文件固定不变'},
                {'op':'add_read_target','target':page['outline'][0]['section_ref'],
                 'view':'section','purpose':'核实固定条件'}],next_step='读取固定条件')
            latest=self.save(session,[{'op':'resolve_gap','id':'gap-1','resolution':'原文明示固定要求',
                'references':[page['evidence_ref']]},{'op':'remove_read_target','id':'target-1'}],1,
                next_step='依据已核实条件回答')
            for active in (session,Session.load(session.path)):
                before=json.dumps(active.records,ensure_ascii=False)
                selected,info=build_context(active.last_run['messages'],[],active)
                states=[json.loads(m['content']) for m in selected if m['role']=='tool'
                        and json.loads(m['content']).get('operation')=='update_research']
                self.assertEqual([p['revision'] for p in states if p['content_kind']=='research_state'],[2])
                self.assertEqual(states[0]['content_kind'],'historical_research_state')
                self.assertIn('本任务',states[0]['notice'])
                self.assertIn('第 2 版',states[0]['notice'])
                self.assertEqual(states[0]['state'],first['state'])
                self.assertNotIn('navigation',states[0])
                self.assertEqual(states[0]['state']['read_targets'],first['state']['read_targets'])
                self.assertEqual(states[1]['state'],latest['state'])
                self.assertEqual(before,json.dumps(active.records,ensure_ascii=False))
                self.assertEqual(info['request_characters'],len(json.dumps({'messages':selected,'tools':[]},ensure_ascii=False)))
                self.assertTrue(info['replacements'])

    def test_superseded_progress_keeps_revision_when_offloaded(self):
        session=Session();add_page(session,'# Guide\nOnly frozen files are supported.');deliver(session)
        self.save(session,[],next_step='待核实的早期计划。'*250)
        self.save(session,[],1,next_step='依据已核实条件回答')
        selected,info=build_context(session.last_run['messages'],[],session)
        old=next(m for m in selected if m['role']=='tool'
                 and json.loads(m['content']).get('revision')==1)
        budget=max(2000,info['request_characters']-len(old['content'])+500)
        compact,metrics=build_context(session.last_run['messages'],[],session,budget)
        progress=[json.loads(m['content']) for m in compact if m['role']=='tool'
                  and json.loads(m['content']).get('operation')=='update_research']
        self.assertTrue(progress[0]['offloaded'])
        self.assertEqual(progress[0]['revision'],1)
        self.assertIn('旧版模型进展',progress[0]['notice'])
        self.assertEqual(progress[1]['revision'],2)
        self.assertEqual(metrics['request_characters'],len(json.dumps({'messages':compact,'tools':[]},ensure_ascii=False)))
        self.assertLessEqual(metrics['request_characters'],budget)

    def test_failed_save_does_not_obsolete_last_successful_progress(self):
        session=Session();add_page(session,'# Guide\nOnly frozen files are supported.')
        self.save(session,[],next_step='核对原文')
        failure=self.update(session,[],99)
        self.assertEqual(failure['status'],'failed')
        complete_tool(session,'update_research',failure)
        selected,_=build_context(session.last_run['messages'],[],session)
        states=[json.loads(m['content']) for m in selected if m['role']=='tool'
                and json.loads(m['content']).get('content_kind')=='research_state']
        self.assertEqual(len(states),1)
        self.assertEqual(states[0]['revision'],1)
        self.assertEqual(states[0]['state']['next_step'],'核对原文')

    def test_gap_answer_impact_survives_partial_updates_compaction_and_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            session=Session.create(Path(directory))
            page=add_page(session,'# Guide\nOnly frozen files are supported.');deliver(session)
            initial=self.save(session,[{'op':'add_gap','question':'冻结文件是否支持实时监控',
                'answer_impact':'属于扩展问题；文件仍会变化时方案已不适用，不影响本题判断'},
                {'op':'add_gap','question':'原场景是否满足冻结前提'}],next_step='核对冻结前提')
            impact=initial['state']['gaps'][0]['answer_impact']
            self.assertNotIn('answer_impact',initial['state']['gaps'][1])
            changed=self.save(session,[{'op':'revise_gap','id':'gap-2',
                'answer_impact':'决定原场景能否采用该方案'},
                {'op':'dismiss_gap','id':'gap-1','resolution':'扩展问题不改变本次适用性结论'}],1,
                next_step='准备回答适用性；实时监控未核实')
            self.assertEqual(changed['state']['gaps'][0]['answer_impact'],impact)
            self.assertEqual(changed['state']['gaps'][1]['question'],'原场景是否满足冻结前提')
            self.assertEqual(changed['state']['gaps'][1]['status'],'open')
            restored=Session.load(session.path)
            selected,info=build_context(restored.last_run['messages'],[],restored,120000,compact=True)
            progress=json.loads(selected[-1]['content'])
            self.assertEqual(progress['state'],changed['state'])
            self.assertEqual(info['request_characters'],len(json.dumps({'messages':selected,'tools':[]},ensure_ascii=False)))
            self.assertEqual(changed['state_budget']['characters'],len(json.dumps(changed['state'],ensure_ascii=False)))

    def test_invalid_answer_impact_rejects_entire_batch_without_status_or_plan_changes(self):
        session=Session();add_page(session,'# Guide\nText.')
        first=self.save(session,[{'op':'add_gap','question':'必要条件未核实',
                                 'answer_impact':'影响原问题的适用性判断'}],next_step='核实必要条件')
        for operations in ([{'op':'add_gap','question':'新问题','answer_impact':False}],
                           [{'op':'revise_gap','id':'gap-1','answer_impact':None}],
                           [{'op':'revise_gap','id':'gap-1'}]):
            with self.subTest(operations=operations):
                failed=self.update(session,operations,1,next_step='准备回答')
                self.assertEqual(failed['status'],'failed')
                self.assertEqual(session.last_run['research'],first)
        cleared=self.save(session,[{'op':'revise_gap','id':'gap-1','answer_impact':''}],1)
        self.assertEqual(cleared['state']['gaps'][0]['status'],'open')
        self.assertEqual(cleared['state']['next_step'],'核实必要条件')

    def test_document_coverage_survives_target_removal_and_uses_actual_selected_input(self):
        session=Session()
        page=add_page(session,'# Guide\nPro required.\n\n'+'Background.\n\n'*500)
        deliver(session)
        first=self.save(session,[{'op':'add_finding','summary':'需要 Pro','conditions':'','status':'supported',
                                  'references':[page['evidence_ref']]},
                                 {'op':'add_read_target','target':page['document_id'],'view':'text','purpose':'核对例外'}])
        final=self.save(session,[{'op':'remove_read_target','id':'target-1'}],1)
        self.assertEqual(final['navigation'],[])
        coverage=final['document_coverage']['documents'][0]
        self.assertEqual(coverage['used_by'],['finding-1'])
        self.assertEqual(coverage['ever_delivered'],'part')
        self.assertEqual(coverage['delivered_characters'],page['end'])
        selected,info=build_context(session.last_run['messages'],[],session,120000,
                                    message_start=len(session.last_run['messages'])-2)
        current=json.loads(selected[-1]['content'])['reading_state']['document_coverage']['documents'][0]
        self.assertEqual(current['ever_delivered'],'part')
        self.assertEqual(current['current_input'],'none')
        self.assertEqual(info['request_characters'],len(json.dumps({'messages':selected,'tools':[]},ensure_ascii=False)))
        self.assertEqual(final['document_coverage']['documents'][0],coverage,'存储的原版本不改写')

    def test_same_source_snapshots_have_separate_coverage(self):
        session=Session()
        first=add_page(session,'# Guide\nOld condition.')
        deliver(session)
        second=add_page(session,'# Guide\nNew condition.')
        result=self.save(session,[{'op':'add_finding','summary':'旧条件','conditions':'旧快照','status':'supported',
                                  'references':[first['evidence_ref']]},
                                 {'op':'add_read_target','target':second['document_id'],'view':'text','purpose':'核对新快照'}])
        old,new=result['document_coverage']['documents']
        self.assertEqual(old['source_id'],new['source_id'])
        self.assertNotEqual(old['document_id'],new['document_id'])
        self.assertEqual(old['ever_delivered'],'full')
        self.assertEqual(new['ever_delivered'],'none')
        self.assertEqual(new['delivered_characters'],0)

    def test_snippet_coverage_is_limited_to_snippet_and_many_documents_are_explicitly_bounded(self):
        session=Session()
        snippet=complete_tool(session,'tavily_search',{'operation':'tavily_search','status':'success',
            'content_kind':'snippet','query':'条件','results':[{'title':'Guide','url':'https://example.org/guide',
                                                            'content':'Pro required.'}]})['results'][0]
        deliver(session)
        result=self.save(session,[{'op':'add_finding','summary':'片段提到 Pro','conditions':'尚未提取页面',
                                  'status':'uncertain','references':[snippet['evidence_ref']]}])
        coverage=result['document_coverage']['documents'][0]
        self.assertEqual(coverage['content_kind'],'search_snippet')
        self.assertEqual(coverage['ever_delivered'],'full')
        self.assertEqual(coverage['total_characters'],len('Pro required.'))
        pages=[add_page(session,f'# Guide {i}\nText.',url=f'https://example.org/{i}') for i in range(8)]
        deliver(session)
        result=self.save(session,[{'op':'add_finding','summary':f'发现 {i}','conditions':'','status':'supported',
                                   'references':[page['evidence_ref']]} for i,page in enumerate(pages)],1)
        self.assertEqual(len(result['document_coverage']['documents']),8)
        self.assertEqual(result['document_coverage']['not_displayed_document_ids'],[pages[-1]['document_id']])

    def test_gap_links_are_explicit_and_resolution_keeps_model_selected_targets(self):
        session=Session();page=add_page(session,'# Guide\nPro required.');deliver(session)
        gap=self.save(session,[{'op':'add_gap','question':'许可证条件是什么'}])['state']['gaps'][0]['id']
        linked=self.save(session,[{'op':'add_finding','summary':'需要 Pro','conditions':'','status':'uncertain',
            'references':[page['evidence_ref']],'gap_ids':[gap]},
            {'op':'add_read_target','target':page['document_id'],'view':'text','purpose':'复核许可证','gap_ids':[gap]}],1)
        self.assertEqual(linked['gap_links'][0],{'gap_id':gap,'status':'open',
                                               'finding_ids':['finding-1'],'target_ids':['target-1']})
        resolved=self.save(session,[{'op':'resolve_gap','id':gap,'resolution':'需要 Pro',
                                    'references':[page['evidence_ref']]}],2)
        self.assertEqual(resolved['gap_links'][0]['status'],'resolved')
        self.assertEqual(len(resolved['state']['read_targets']),1)
        self.assertEqual(resolved['state']['findings'][0]['status'],'uncertain')

    def test_invalid_or_removed_gap_links_fail_whole_batch_until_explicitly_unlinked(self):
        session=Session();page=add_page(session,'# Guide\nPro required.');deliver(session)
        self.save(session,[{'op':'add_gap','question':'许可证条件'}])
        first=self.save(session,[{'op':'add_finding','summary':'需要 Pro','conditions':'','status':'supported',
                                 'references':[page['evidence_ref']],'gap_ids':['gap-1']}],1)
        for operations in ([{'op':'revise_finding','id':'finding-1','gap_ids':['gap-999']}],
                           [{'op':'remove_gap','id':'gap-1'}]):
            with self.subTest(operations=operations):
                failed=self.update(session,operations,2)
                self.assertEqual(failed['status'],'failed')
                self.assertEqual(session.last_run['research'],first)
        removed=self.save(session,[{'op':'remove_gap','id':'gap-1'},
                                   {'op':'revise_finding','id':'finding-1','gap_ids':[]}],2)
        self.assertEqual(removed['gap_links'],[])
        self.assertEqual(removed['state']['findings'][0]['summary'],'需要 Pro')

    def test_exact_evidence_quotes_survive_checkpoint_as_visible_source_excerpts(self):
        with tempfile.TemporaryDirectory() as directory:
            session=Session.create(Path(directory))
            quote='The parameters are recognized as of version 3.15.0.'
            body='# Guide\n\n'+quote+'\n\n'+'Background.\n\n'*600
            page=add_page(session,body);deliver(session)
            result=self.save(session,[{'op':'add_finding','summary':'截至 3.15.0 已识别这些参数',
                'conditions':'原文未给出首次引入时间','status':'uncertain','references':[page['evidence_ref']],
                'evidence_quotes':[{'reference':page['evidence_ref'],'text':quote}]}])
            saved=result['state']['findings'][0]['evidence_quotes'][0]
            self.assertEqual(saved['reference']['start'],body.index(quote))
            self.assertEqual(saved['reference']['end']-saved['reference']['start'],len(quote))
            selected,info=build_context(session.last_run['messages'],[],session,120000,compact=True)
            current=json.loads(selected[-1]['content'])
            self.assertEqual(current['reading_state']['document_coverage']['documents'][0]['current_input'],'part')
            self.assertIn(quote,selected[-1]['content'])
            self.assertEqual(info['request_characters'],len(json.dumps({'messages':selected,'tools':[]},ensure_ascii=False)))
            self.assertEqual(Session.load(session.path).last_run['research'],result)

    def test_quote_paraphrase_cross_snapshot_and_changed_reference_fail_without_overwriting(self):
        session=Session();page=add_page(session,'# Guide\nPro required. Guest excluded.');deliver(session)
        first=self.save(session,[{'op':'add_finding','summary':'需要 Pro','conditions':'访客除外','status':'supported',
            'references':[page['evidence_ref']],
            'evidence_quotes':[{'reference':page['evidence_ref'],'text':'Pro required.'}]}])
        newer=add_page(session,'# Guide\nPro required. Different condition.');deliver(session)
        cases=[{'op':'revise_finding','id':'finding-1','evidence_quotes':[{
                    'reference':page['evidence_ref'],'text':'需要 Pro 许可证。'}]},
               {'op':'revise_finding','id':'finding-1','evidence_quotes':[{
                    'reference':newer['evidence_ref'],'text':'Pro required.'}]},
               {'op':'revise_finding','id':'finding-1','references':[newer['evidence_ref']]}]
        for operation in cases:
            with self.subTest(operation=operation):
                failed=self.update(session,[operation],1)
                self.assertEqual(failed['status'],'failed')
                self.assertEqual(session.last_run['research'],first)

    def test_format_equivalent_selection_saves_exact_raw_source_and_scope_errors_still_fail(self):
        session=Session()
        raw='The options are recognized as of [version 3.15.0](release.html).\nNew options may be added.'
        escaped='The SQLITE\\_OPEN\\_READONLY flag matches sqlite3\\_open().'
        body='# Guide\n\n'+raw+'\n\n'+escaped
        page=add_page(session,body);deliver(session)
        selected=self.save(session,[{'op':'add_finding','summary':'截至该版本已识别','conditions':'不能推出首次引入',
            'status':'uncertain','references':[page['evidence_ref']],
            'evidence_quotes':[{'reference':page['evidence_ref'],
                'text':'The options are recognized as of version 3.15.0. New options may be added.'},
                {'reference':page['evidence_ref'],'text':'The SQLITE_OPEN_READONLY flag matches sqlite3_open().'}]}])
        quotes=selected['state']['findings'][0]['evidence_quotes']
        self.assertEqual([quote['text'] for quote in quotes],[raw,escaped])
        for quote in quotes:
            ref=quote['reference']
            self.assertEqual(body[ref['start']:ref['end']],quote['text'])
        invalid=self.update(session,[{'op':'revise_finding','id':'finding-1','evidence_quotes':[{
            'reference':f"{page['document_id']}@0-8",'text':raw}]}],1)
        self.assertEqual(invalid['status'],'failed')
        self.assertEqual(session.last_run['research'],selected)

    def test_quote_format_selection_does_not_guess_between_multiple_matches(self):
        session=Session()
        page=add_page(session,'# Guide\nThe [condition](a) applies.\n\nThe [condition](b) applies.')
        deliver(session)
        failed=self.update(session,[{'op':'add_finding','summary':'条件适用','conditions':'','status':'uncertain',
            'references':[page['evidence_ref']],
            'evidence_quotes':[{'reference':page['evidence_ref'],'text':'The condition applies.'}]}])
        self.assertEqual(failed['status'],'failed')
        self.assertIn('多处',failed['error'])

    def test_update_without_running_task_has_clear_error(self):
        from seekra.search.research import update_research
        for args in ({'goal':'核对条件','findings':[],'gaps':[],'next_step':''}, {'operations':[]}):
            with self.subTest(args=args), self.assertRaisesRegex(ValueError,'没有正在执行'):
                update_research(Session(),0,**args)

    def update(self, session, operations, base_revision=0, **fields):
        with httpx.Client(transport=httpx.MockTransport(lambda r: self.fail('保存进展不能联网'))) as client:
            registry = default_registry(client, 'fake', session)
            definition = next(d['function'] for d in registry.definitions if d['function']['name']=='update_research')
            self.assertEqual(set(definition['parameters']['required']), {'base_revision','operations'})
            return json.loads(registry.execute(call('update_research', {
                'base_revision': base_revision, 'operations': operations, **fields})).content)

    def save(self, session, operations, base_revision=0, **fields):
        result = self.update(session, operations, base_revision, **fields)
        self.assertEqual(result['status'], 'success', result)
        return complete_tool(session, 'update_research', result)

    def test_navigation_needs_no_borrowed_body_reference(self):
        session = Session()
        page = add_page(session, '# Guide\n\n## Conditions\n' + 'Background. ' * 300 + '\nPro required.')
        section = page['outline'][1]['section_ref']
        result = self.save(session, [
            {'op':'add_gap','question':'许可证条件尚未核实'},
            {'op':'add_read_target','target':section,'view':'section','purpose':'读取许可证条件'}])
        self.assertEqual(result['state']['findings'], [])
        self.assertEqual(result['state']['gaps'][0]['question'], '许可证条件尚未核实')
        nav = result['navigation'][0]
        self.assertEqual(nav['title'], 'Conditions')
        self.assertEqual(nav['read_action'], {'tool':'read_section','arguments':{'section_ref':section}})
        self.assertEqual(nav['ever_delivered'], 'none')
        read = session.documents.read_section(section)
        complete_tool(session, 'read_section', read)
        deliver(session)
        selected, _ = build_context(session.last_run['messages'], [], session)
        latest = next(json.loads(m['content'])['reading_state'] for m in reversed(selected) if m['role']=='tool')
        self.assertEqual(latest['navigation'][0]['ever_delivered'], 'full')
        self.assertEqual(latest['navigation'][0]['current_input'], 'full')
        self.assertEqual(result['navigation'][0]['ever_delivered'], 'none', '原记录不可被实时呈现修改')

    def test_incremental_changes_keep_untouched_conditions_and_ids(self):
        session = Session()
        page = add_page(session, '# Guide\nPro required. Guest accounts excluded.')
        deliver(session)
        first = self.save(session, [{'op':'add_finding','summary':'需要 Pro',
            'conditions':'访客账户除外','status':'supported','references':[page['evidence_ref']]},
            {'op':'add_gap','question':'期限尚未核实'}])
        finding = first['state']['findings'][0]
        second = self.save(session, [{'op':'add_gap','question':'资源包尚未核实'}], 1)
        self.assertEqual(second['state']['findings'][0], finding)
        third = self.save(session, [{'op':'revise_finding','id':finding['id'],'status':'uncertain'}], 2)
        self.assertEqual(third['state']['findings'][0]['conditions'], '访客账户除外')
        self.assertEqual(third['state']['findings'][0]['id'], finding['id'])

    def test_failed_batch_keeps_latest_and_confirmation_is_not_new_content(self):
        session = Session()
        add_page(session, '# Guide\nBody')
        first = self.save(session, [{'op':'add_gap','question':'仍未核实'}])
        failed = self.update(session, [{'op':'add_gap','question':'不能只保存这一项'},
                                      {'op':'revise_finding','id':'missing','status':'supported'}], 1)
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(session.last_run['research'], first)
        confirmed = self.save(session, [], 1)
        self.assertFalse(confirmed['content_changed'])
        self.assertEqual(confirmed['revision'], 2)
        self.assertEqual(confirmed['state'], first['state'])
        self.assertEqual(confirmed['state_budget']['characters'],len(json.dumps(confirmed['state'],ensure_ascii=False)))
        self.assertEqual(confirmed['state_budget']['remaining'],8000-confirmed['state_budget']['characters'])

    def test_resolution_uses_body_and_removal_does_not_reuse_ids(self):
        session = Session()
        page = add_page(session, '# Guide\nPro required.')
        deliver(session)
        first = self.save(session, [{'op':'add_gap','question':'需要什么许可证'}])
        gap_id = first['state']['gaps'][0]['id']
        bad = self.update(session, [{'op':'resolve_gap','id':gap_id,'resolution':'Pro'}], 1)
        self.assertEqual(bad['status'], 'failed')
        resolved = self.save(session, [{'op':'resolve_gap','id':gap_id,'resolution':'需要 Pro',
                                       'references':[page['evidence_ref']]}], 1)
        self.assertEqual(resolved['state']['gaps'][0]['status'], 'resolved')
        self.save(session, [{'op':'remove_gap','id':gap_id}], 2)
        newer = self.save(session, [{'op':'add_gap','question':'另一个问题'}], 3)
        self.assertNotEqual(newer['state']['gaps'][0]['id'], gap_id)

    def test_v1_record_can_be_read_and_then_upgraded_on_patch(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            page = add_page(session, '# Guide\nPro required.')
            deliver(session)
            from seekra.search.research import update_research
            old = update_research(session, **state(page['evidence_ref']))
            complete_tool(session, 'update_research', old)
            restored = Session.load(session.path)
            result = self.save(restored, [{'op':'add_gap','question':'新的缺口'}], 1)
            self.assertEqual(len(result['state']['findings']), 1)
            self.assertIn('id', result['state']['findings'][0])
            self.assertEqual(len(result['state']['gaps']), 2)
            self.assertEqual(Session.load(restored.path).last_run['research'], result)

    def test_current_navigation_follows_actual_cropped_input_and_budget(self):
        session = Session()
        page = add_page(session, '# Guide\n\n## Conditions\n' + 'Known condition.\n\n' * 500)
        section = page['outline'][1]['section_ref']
        self.save(session,[{'op':'add_read_target','target':section,'view':'section','purpose':'核对条件'}])
        read = session.documents.read_section(section)
        complete_tool(session,'read_section',read)
        selected,info=build_context(session.last_run['messages'],[],session,9000)
        self.assertEqual(info['request_characters'],len(json.dumps({'messages':selected,'tools':[]},ensure_ascii=False)))
        self.assertLessEqual(info['request_characters'],9000)
        progress=json.loads(selected[-1]['content'])['reading_state']
        self.assertEqual(progress['navigation'][0]['ever_delivered'],'none')
        self.assertEqual(progress['navigation'][0]['current_input'],'part')

    def test_invalid_target_and_legacy_replacement_cannot_erase_v2_state(self):
        session=Session()
        page=add_page(session,'# Guide\nPro required.')
        deliver(session)
        first=self.save(session,[{'op':'add_gap','question':'未核实项'}])
        bad=self.update(session,[{'op':'add_read_target','target':'S999-P1-H1','view':'section','purpose':'不存在的目标'}],1)
        self.assertEqual(bad['status'],'failed')
        self.assertEqual(session.last_run['research'],first)
        from seekra.search.research import update_research
        with self.assertRaisesRegex(ValueError,'局部更新'):
            update_research(session,**state(page['evidence_ref'],1))
        self.assertEqual(session.last_run['research'],first)

    def test_unread_action_survives_reload_and_stays_in_section(self):
        with tempfile.TemporaryDirectory() as directory:
            session=Session.create(Path(directory))
            page=add_page(session,'# Guide\n\n## Conditions\n'+ 'Background.\n\n'*600 +
                          'Exception at the end.\n\n## Another section\nUnrelated.')
            section=page['outline'][1]['section_ref']
            read=session.documents.read_section(section)
            complete_tool(session,'read_section',read)
            deliver(session)
            result=self.save(session,[{'op':'add_read_target','target':section,'view':'section',
                                      'purpose':'核对末尾例外'}])
            pending=result['navigation'][0]['first_undelivered']
            self.assertEqual(pending['start'],read['end'])
            restored=Session.load(session.path)
            action=pending['read_action']
            tail=restored.documents.read_section(**action['arguments'])
            self.assertEqual(tail['start'],read['end'])
            self.assertIn('Exception at the end.',tail['content'])
            self.assertNotIn('Unrelated.',tail['content'])
            self.assertFalse(tail['complete'], '续读末页不能称为整节一次完整交付')
            complete_tool(restored,'read_section',tail)
            deliver(restored)
            from seekra.search.progress import present_progress
            current=present_progress(restored.last_run['research'],restored)
            self.assertIsNone(current['navigation'][0]['first_undelivered'])

    def test_unread_target_keeps_holes_instead_of_using_last_endpoint(self):
        session=Session()
        page=add_page(session,'# Guide\n'+ 'Text. '*100)
        doc=page['document_id']
        target=session.documents.unread_target(doc,'text',0,400,[(0,100),(200,400)])
        self.assertEqual((target['start'],target['end']),(100,200))
        read=session.documents.read_document(**target['read_action']['arguments'])
        self.assertEqual(read['start'],100)
        reference=f'{doc}@20-300'
        target=session.documents.unread_target(reference,'reference',20,300,[(20,80),(150,300)])
        read=session.documents.read_document(**target['read_action']['arguments'])
        self.assertEqual(read['start'],80)
        self.assertEqual(read['target_end'],300)


if __name__ == '__main__':
    unittest.main()
