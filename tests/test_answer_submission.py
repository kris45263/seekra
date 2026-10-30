"""受控终稿引用、提交反馈、持久交付与默认行为边界。"""
from copy import deepcopy
import importlib
import json
from pathlib import Path
import tempfile
import unittest

from seekra.search.agent import execute_run
from seekra.search.history import read_history
from seekra.terminal.display import session_transcript
from seekra.harness.runtime import ModelResponse
from seekra.search.state import SearchSession as Session
from seekra.harness.tools import ToolRegistry
from seekra.search.tools import default_registry
from seekra.search.documents.delivery import remember_delivery
from test_agent import call
from test_documents import add_page, begin
from test_research import deliver


def final_definition():
    return {'type':'function','function':{'name':'finish',
        'parameters':{'type':'object','properties':{},'additionalProperties':False}}}


class AnswerSubmissionTests(unittest.TestCase):
    def register_final(self, registry, handler):
        try:
            registry.register(final_definition(),handler,final_output='answer')
        except TypeError as exc:
            self.fail(f'需要显式注册受控终稿工具：{exc}')

    def test_success_ends_after_persisting_tool_without_an_extra_model_rewrite(self):
        registry=ToolRegistry()
        self.register_final(registry,lambda *args:json.dumps({'status':'success','answer':'程序组织的终稿'}))
        calls=[];shown=[]
        class Model:
            identity='test:final-tool'
            def generate(model,*args,**kwargs):
                calls.append(True)
                if len(calls)>1:raise AssertionError('终稿提交成功不应再让模型重写')
                return ModelResponse({'role':'assistant','tool_calls':[call('finish',{})]},'tool_calls')
        with tempfile.TemporaryDirectory() as directory:
            session=Session.create(Path(directory))
            result=execute_run('研究问题',model=Model(),registry=registry,session=session,on_content=shown.append)
            self.assertEqual((result.status,result.answer),('completed','程序组织的终稿'))
            self.assertEqual(shown,['程序组织的终稿'])
            restored=Session.load(session.path)
            self.assertEqual(restored.records[-1]['answer_record_id'],restored.records[-2]['id'])
            self.assertIn('程序组织的终稿',session_transcript(restored))
            self.assertIn('程序组织的终稿',read_history(restored,restored.records[-1]['id'])['content'])

    def test_failed_submission_returns_feedback_then_accepts_corrected_call(self):
        registry=ToolRegistry();submitted=[]
        def handler(*args):
            submitted.append(True)
            return json.dumps({'status':'failed','error':'引用尚未交付'} if len(submitted)==1
                              else {'status':'success','answer':'修正后的终稿'})
        self.register_final(registry,handler)
        class Model:
            identity='test:final-feedback'
            def generate(model,messages,*args,**kwargs):
                if submitted:self.assertIn('引用尚未交付',messages[-1]['content'])
                return ModelResponse({'role':'assistant','tool_calls':[call('finish',{},f'f{len(submitted)}')]},'tool_calls')
        result=execute_run('问题',model=Model(),registry=registry,session=Session())
        self.assertEqual(result.answer,'修正后的终稿')
        self.assertEqual(len(submitted),2)

    def test_output_failure_resumes_committed_submission_without_model_or_tool_replay(self):
        registry=ToolRegistry();executed=[]
        def handler(*args):
            executed.append(True);return json.dumps({'status':'success','answer':'已提交终稿'})
        self.register_final(registry,handler)
        class Model:
            identity='test:final-output'
            def generate(model,*args,**kwargs):
                if executed:raise AssertionError('恢复不得重跑模型')
                return ModelResponse({'role':'assistant','tool_calls':[call('finish',{})]},'tool_calls')
        with tempfile.TemporaryDirectory() as directory:
            session=Session.create(Path(directory))
            failed=execute_run('问题',model=Model(),registry=registry,session=session,
                               on_content=lambda text:(_ for _ in ()).throw(OSError('输出失败')))
            self.assertEqual(failed.status,'failed')
            self.assertEqual(session.last_run['error_code'],'output_error')
            restored=Session.load(session.path);shown=[]
            result=execute_run(None,model=Model(),registry=registry,session=restored,resume=True,on_content=shown.append)
            self.assertEqual(result.answer,'已提交终稿')
            self.assertEqual(shown,['已提交终稿'])
            self.assertEqual(len(executed),1)

    def test_final_call_must_be_alone_and_ordinary_answer_field_cannot_end_run(self):
        registry=ToolRegistry();finished=[]
        self.register_final(registry,lambda *args:(finished.append(True) or json.dumps({'status':'success','answer':'终稿'})))
        registry.register({'type':'function','function':{'name':'read','parameters':{'type':'object'}}},
                          lambda *args:json.dumps({'status':'success','answer':'这只是普通工具结果'}))
        class Model:
            identity='test:final-batch'
            calls=0
            def generate(model,messages,*args,**kwargs):
                model.calls+=1
                if model.calls==1:return ModelResponse({'role':'assistant','tool_calls':[
                    call('read',{},'read'),call('finish',{},'mixed')]},'tool_calls')
                self.assertFalse(finished)
                self.assertIn('单独',messages[-1]['content'])
                return ModelResponse({'role':'assistant','tool_calls':[call('finish',{},'alone')]},'tool_calls')
        result=execute_run('问题',model=Model(),registry=registry,session=Session())
        self.assertEqual(result.answer,'终稿');self.assertEqual(len(finished),1)

    def answers(self):
        try:return importlib.import_module('seekra.search.answers')
        except ModuleNotFoundError:self.fail('需要受控原文与判断/建议的终稿编译器')

    def draft(self, ref, quote='Pro required.'):
        return {'evidence':[{'reference':ref,'quote':quote}],
                'judgments':[{'text':'需要 Pro','evidence_indices':[1],'conditions':'按本文规则'}],
                'recommendations':[{'text':'先核实账户支持','judgment_indices':[1],'unknown_indices':[1]}],
                'unknowns':[{'question':'账户是否支持','answer_impact':'影响可行性'}]}

    def test_compiler_uses_saved_source_and_rejects_quote_rewrites_and_unread_ranges(self):
        api=self.answers();session=Session();page=add_page(session,'# Rules\n\nPro required.\n\nShared guest excluded.')
        original=deepcopy(session.records)
        with self.assertRaises(ValueError):api.compile_answer(session,self.draft(page['evidence_ref']))
        deliver(session)
        result=api.compile_answer(session,self.draft(page['evidence_ref']))
        self.assertIn('Pro required.',result['answer'])
        self.assertIn('待确认条件：账户是否支持',result['answer'])
        self.assertIn('Pro required.',result['source_quotes'][0]['text'])
        self.assertEqual(result['source_quotes'][0]['anchor']['text'],'Pro required.')
        with self.assertRaises(ValueError):api.compile_answer(session,self.draft(page['evidence_ref'],'Pro is optional.'))
        bad=self.draft(page['evidence_ref']);bad['recommendations'][0]['unknown_indices']=[2]
        with self.assertRaises(ValueError):api.compile_answer(session,bad)
        self.assertEqual(session.records[:len(original)],original)

    def test_quote_anchor_returns_whole_original_paragraph_and_requires_delivery(self):
        api=self.answers();session=Session();body='# Rules\nPro required. Shared guest excluded.\n'
        page=add_page(session,body);deliver(session)
        result=api.compile_answer(session,self.draft(page['evidence_ref']))
        self.assertIn('Shared guest excluded.',result['answer'])
        session.last_run['delivered']={page['document_id']:[(8,21)]}
        with self.assertRaises(ValueError):api.compile_answer(session,self.draft(page['document_id']+'@8-21'))

    def test_registration_is_opt_in_and_final_tool_can_answer_in_progress_stage(self):
        api=self.answers();session=Session();run_id=begin(session)
        registry=default_registry(None,'fake',session)
        self.assertNotIn('submit_answer',{d['function']['name'] for d in registry.definitions})
        api.register_answer_tool(registry,session)
        session.last_run['config']['final_outputs']=registry.final_outputs
        session.last_run['progress_stage']={'base_revision':0}
        from seekra.search.context import prepare_context
        _, tools, _, _ = prepare_context(session.last_run['messages'], registry.definitions, session, 120000)
        self.assertIn('submit_answer', {d['function']['name'] for d in tools})

    def test_submitted_sources_have_local_readback_and_count_only_when_in_actual_input(self):
        api=self.answers();session=Session();page=add_page(session,'# Rules\n\nPro required.');deliver(session)
        compiled=api.compile_answer(session,self.draft(page['evidence_ref']))
        from test_documents import complete_tool
        result=complete_tool(session,'submit_answer',compiled)
        stored=session.records[-1]
        view=read_history(session,stored['id'])
        self.assertIn('Pro required.',view['content'])
        self.assertNotIn('evidence_indices',view['content'])
        self.assertEqual(view['sources'][0]['read_action']['arguments']['view'],'reference')
        delivered={}
        remember_delivery(delivered,[{'role':'tool','content':json.dumps(result,ensure_ascii=False)}])
        self.assertEqual(delivered[page['document_id']],[(compiled['source_quotes'][0]['reference']['start'],
                                                       compiled['source_quotes'][0]['reference']['end'])])
        delivered={};remember_delivery(delivered,[{'role':'assistant','content':json.dumps(result,ensure_ascii=False)}])
        self.assertEqual(delivered,{})

    def test_long_original_paragraph_is_explicit_excerpt_not_false_completeness(self):
        api=self.answers();session=Session()
        page=add_page(session,'# Rules\n\nPro required. '+'More original context. '*800);deliver(session)
        result=api.compile_answer(session,self.draft(page['evidence_ref']))
        self.assertEqual(result['source_quotes'][0]['scope'],'excerpt')
        self.assertIn('局部原文',result['answer'])

    def test_empty_source_unknown_answer_and_invalid_index_types_are_handled(self):
        api=self.answers();session=Session();begin(session)
        draft={'evidence':[],'judgments':[],'recommendations':[],
               'unknowns':[{'question':'原文未取得','answer_impact':'无法核实规则'}]}
        result=api.compile_answer(session,draft)
        self.assertNotIn('原文依据',result['answer'])
        page=add_page(session,'Pro required.');deliver(session)
        for index in [True,0,'1',{}]:
            bad=self.draft(page['evidence_ref']);bad['judgments'][0]['evidence_indices']=[index]
            with self.subTest(index=index),self.assertRaises(ValueError):api.compile_answer(session,bad)

    def test_storage_failure_before_final_tool_commit_cannot_publish_answer(self):
        registry=ToolRegistry()
        self.register_final(registry,lambda *args:json.dumps({'status':'success','answer':'尚未提交的正文'}))
        class Model:
            identity='test:final-storage'
            def generate(model,*args,**kwargs):
                return ModelResponse({'role':'assistant','tool_calls':[call('finish',{})]},'tool_calls')
        session=Session();original=session.record;shown=[]
        def record(kind,**kwargs):
            if kind=='tool_completed':
                session._storage_failed=True
                raise OSError('存储失败')
            return original(kind,**kwargs)
        session.record=record
        with self.assertRaises(OSError):execute_run('问题',model=Model(),registry=registry,session=session,on_content=shown.append)
        self.assertEqual(shown,[])
        self.assertFalse(any(r['kind']=='run_ended' and r['status']=='completed' for r in session.records))

    def test_final_registration_profile_change_rejects_resume(self):
        registry=ToolRegistry();self.register_final(registry,lambda *args:'{}')
        class Model:
            identity='test:final-config'
            def generate(model,*args,**kwargs):raise OSError('暂时离线')
        session=Session()
        result=execute_run('问题',model=Model(),registry=registry,session=session)
        self.assertEqual(result.status,'failed')
        changed=ToolRegistry();changed.register(final_definition(),lambda *args:'{}')
        with self.assertRaisesRegex(ValueError,'配置'):execute_run(None,model=Model(),registry=changed,session=session,resume=True)

    def test_quote_anchor_does_not_drop_conditions_already_in_submitted_reference(self):
        api=self.answers();session=Session()
        body='# Read-only\n\nAvailable since version 3.22.\n\nOne of these conditions must hold:\n\n1. Side files already exist.\n2. Directory is writable.\n'
        page=add_page(session,body);deliver(session)
        draft=self.draft(page['evidence_ref'],'One of these conditions must hold:')
        result=api.compile_answer(session,draft)
        self.assertIn('version 3.22',result['answer'])
        self.assertIn('Side files already exist.',result['answer'])
        self.assertIn('Directory is writable.',result['answer'])

    def test_same_source_context_is_displayed_and_budgeted_once_with_index_mapping(self):
        api=self.answers();session=Session()
        page=add_page(session,'# Rules\n\nPro required. Shared guest excluded.');deliver(session)
        draft=self.draft(page['evidence_ref'])
        draft['evidence'].append({'reference':page['evidence_ref'],'quote':'Shared guest excluded.'})
        draft['judgments'][0]['evidence_indices']=[1,2]
        result=api.compile_answer(session,draft)
        self.assertEqual(len(result['source_quotes']),1)
        self.assertEqual(result['evidence_source_indices'],[1,1])
        self.assertEqual(result['answer'].count('> Pro required. Shared guest excluded.'),1)
        self.assertIn('依据 1',result['answer'])

    def test_ambiguous_literal_anchor_requires_specific_original_range(self):
        api=self.answers();session=Session()
        body='# Rules\n\n## Old\nPro required.\n\n## New\nPro required. Shared guest excluded.'
        page=add_page(session,body);deliver(session)
        with self.assertRaisesRegex(ValueError,'多处'):api.compile_answer(session,self.draft(page['evidence_ref']))
        section=session.documents.read_section(page['outline'][-1]['section_ref'])
        result=api.compile_answer(session,self.draft(section['evidence_ref']))
        self.assertIn('New',result['source_quotes'][0]['path'])

    def test_same_url_new_snapshot_never_replaces_evidence_from_old_snapshot(self):
        api=self.answers();session=Session()
        old=add_page(session,'# Rules\n\nPro required.');deliver(session)
        newer=add_page(session,'# Rules\n\nPro is optional.');deliver(session)
        result=api.compile_answer(session,self.draft(old['evidence_ref']))
        self.assertEqual(result['source_quotes'][0]['document_id'],old['document_id'])
        self.assertEqual(result['source_quotes'][0]['snapshot_id'],old['snapshot_id'])
        self.assertNotEqual(old['document_id'],newer['document_id'])

    def test_overlapping_delivered_contexts_are_unioned_without_losing_source_text(self):
        api=self.answers();session=Session()
        body='# Rules\n\nPro required.\n\nShared guest excluded.\n\nMore conditions.'
        page=add_page(session,body);deliver(session)
        first=session.documents.read_document(page['document_id'],start=0,limit=44)
        second=session.documents.read_document(page['document_id'],start=20,limit=40)
        draft=self.draft(first['evidence_ref'])
        draft['evidence'].append({'reference':second['evidence_ref'],'quote':'Shared guest excluded.'})
        draft['judgments'][0]['evidence_indices']=[1,2]
        result=api.compile_answer(session,draft)
        self.assertEqual(len(result['source_quotes']),1)
        row=result['source_quotes'][0]
        actual=session.documents.read_document(page['document_id'],start=row['reference']['start'],
                                              limit=row['reference']['end']-row['reference']['start'])
        self.assertEqual(row['text'],actual['content'])
        self.assertEqual(result['evidence_source_indices'],[1,1])

    def test_original_markdown_list_item_boundaries_do_not_expand_to_unrelated_items(self):
        api=self.answers();session=Session()
        body='# Parameters\n\n* **mode**: read-only access.\n* **cache**: shared cache.\n* **immutable**: file cannot change. Caution: changes cause errors.\n'
        page=add_page(session,body);deliver(session)
        pos=body.index('* **immutable**')
        ref=f"{page['document_id']}@{pos}-{len(body.rstrip())}"
        result=api.compile_answer(session,self.draft(ref,'file cannot change.'))
        self.assertIn('Caution: changes cause errors.',result['answer'])
        self.assertNotIn('shared cache.',result['answer'])

    def test_budget_feedback_gives_actual_size_and_citation_costs_without_publishing(self):
        api=self.answers();session=Session()
        from test_documents import complete_tool
        evidence=[]
        for n in range(3):
            page=add_page(session,'# Rules\n\nPro required.\n\n'+'Original detail. '*320,url=f'https://example.org/{n}')
            read=session.documents.read_document(page['document_id'])
            complete_tool(session,'read_document',read);deliver(session)
            evidence.append({'reference':read['evidence_ref'],'quote':'Pro required.'})
        registry=default_registry(None,'fake',session);api.register_answer_tool(registry,session)
        draft=self.draft(evidence[0]['reference']);draft['evidence']=evidence
        result=json.loads(registry.execute(call('submit_answer',draft)).content)
        self.assertEqual(result['status'],'failed')
        self.assertIn('display_budget',result,'预算失败应提供实际大小和需减少量')
        self.assertGreater(result['display_budget']['characters'],12000)
        self.assertGreater(result['display_budget']['reduce_by'],0)
        self.assertEqual(len(result['citation_costs']),3)
        self.assertNotIn('answer',result)

    def test_layout_orders_content_and_keeps_advice_dependencies_adjacent(self):
        api=self.answers();session=Session()
        page=add_page(session,'# Rules\n\nPro required. Shared guest excluded.');deliver(session)
        draft=self.draft(page['evidence_ref'])
        draft['layout']=[{'type':'heading','text':'先看可行性','level':2},
                         {'type':'recommendation','index':1},
                         {'type':'heading','text':'判断与出处','level':2},
                         {'type':'judgment','index':1},{'type':'evidence','index':1}]
        try:result=api.compile_answer(session,draft)
        except ValueError as exc:self.fail(f'合法自由排版应能编译：{exc}')
        answer=result['answer']
        self.assertLess(answer.index('先核实账户支持'),answer.index('判断与出处'))
        nearby=answer[answer.index('先核实账户支持'):answer.index('判断与出处')]
        self.assertIn('按本文规则',nearby)
        self.assertIn('账户是否支持',nearby)
        self.assertIn('影响可行性',nearby)
        self.assertNotIn('## 原文依据',answer)
        self.assertEqual(result['draft']['layout'],draft['layout'])
        self.assertEqual(result['draft']['recommendations'][0]['judgment_indices'],[1])

    def test_layout_cannot_omit_judgments_sources_or_unattached_unknowns(self):
        api=self.answers();session=Session()
        page=add_page(session,'# Rules\n\nPro required.');deliver(session)
        draft=self.draft(page['evidence_ref'])
        draft['unknowns'].append({'question':'期限如何计算','answer_impact':'影响离线安排'})
        layout=[{'type':'judgment','index':1},{'type':'evidence','index':1},
                {'type':'recommendation','index':1},{'type':'unknown','index':2}]
        for missing in range(len(layout)):
            with self.subTest(missing=layout[missing]['type']):
                draft['layout']=layout[:missing]+layout[missing+1:]
                with self.assertRaisesRegex(ValueError,'layout.*遗漏'):
                    api.compile_answer(session,draft)

    def test_layout_repeated_merged_source_has_no_orphan_cross_references(self):
        api=self.answers();session=Session()
        page=add_page(session,'# Rules\n\nPro required. Shared guest excluded.');deliver(session)
        draft=self.draft(page['evidence_ref'])
        draft['evidence'].append({'reference':page['evidence_ref'],'quote':'Shared guest excluded.'})
        draft['judgments'][0]['evidence_indices']=[1,2]
        draft['layout']=[{'type':'judgment','index':1},{'type':'evidence','index':2},
                         {'type':'evidence','index':1},{'type':'recommendation','index':1},
                         {'type':'evidence','index':2}]
        try:result=api.compile_answer(session,draft)
        except ValueError as exc:self.fail(f'同一原文可在排版中重复引用：{exc}')
        self.assertEqual(result['answer'].count('> Pro required. Shared guest excluded.'),1)
        self.assertNotIn('参见原文',result['answer'])
        self.assertIn('依据 1',result['answer'])
        self.assertIn('展示范围',result['answer'])
        self.assertIn('不代表整页已核实',result['answer'])
        self.assertEqual(result['evidence_source_indices'],[1,1])

    def test_layout_rejects_invalid_nodes_and_duplicate_model_content(self):
        api=self.answers();session=Session()
        page=add_page(session,'# Rules\n\nPro required.');deliver(session)
        draft=self.draft(page['evidence_ref'])
        valid=[{'type':'judgment','index':1},{'type':'evidence','index':1},
               {'type':'recommendation','index':1}]
        invalid=[{'type':'judgment','index':True},{'type':'unknown','index':2},
                 {'type':'markdown','text':'不受关联约束的结论'},
                 {'type':'heading','text':'标题\n另一段','level':2},
                 {'type':'heading','text':'标题','level':True},
                 {'type':'judgment','index':1}]
        for node in invalid:
            with self.subTest(node=node):
                draft['layout']=valid+[node]
                with self.assertRaisesRegex(ValueError,'layout'):
                    api.compile_answer(session,draft)

    def test_layout_can_answer_with_only_unknowns_and_no_sources(self):
        api=self.answers();session=Session();begin(session)
        draft={'evidence':[],'judgments':[],'recommendations':[],
               'unknowns':[{'question':'未取得相关正文','answer_impact':'不能判断方案可行'}],
               'layout':[{'type':'heading','text':'目前无法判断','level':2},
                         {'type':'unknown','index':1}]}
        try:result=api.compile_answer(session,draft)
        except ValueError as exc:self.fail(f'明确未知的答复无需编造证据：{exc}')
        self.assertIn('目前无法判断',result['answer'])
        self.assertIn('不能判断方案可行',result['answer'])
        self.assertEqual(result['source_quotes'],[])

    def test_layout_feedback_then_output_failure_resumes_saved_answer(self):
        api=self.answers();requests=[]
        good={'evidence':[],'judgments':[],'recommendations':[],
              'unknowns':[{'question':'是否有重置机制','answer_impact':'不能保证周期方案可行'}],
              'layout':[{'type':'heading','text':'仍需确认','level':2},
                        {'type':'unknown','index':1}]}
        bad=deepcopy(good);bad['layout'].pop()
        class Model:
            identity='test:layout-recovery'
            def generate(model,messages,*args,**kwargs):
                requests.append(True)
                if len(requests)>2:raise AssertionError('重载不得再次请求模型')
                if len(requests)==2:
                    feedback=json.loads(messages[-1]['content'])
                    self.assertEqual(feedback['status'],'failed')
                    self.assertIn('unknowns',feedback['error'])
                    self.assertNotIn('answer',feedback)
                return ModelResponse({'role':'assistant','tool_calls':[
                    call('submit_answer',bad if len(requests)==1 else good,f'final-{len(requests)}')]},'tool_calls')
        with tempfile.TemporaryDirectory() as directory:
            session=Session.create(Path(directory));registry=ToolRegistry()
            api.register_answer_tool(registry,session)
            result=execute_run('判断周期方案',model=Model(),registry=registry,session=session,
                               on_content=lambda text:(_ for _ in ()).throw(OSError('输出失败')))
            self.assertEqual(result.status,'failed')
            restored=Session.load(session.path);registry=ToolRegistry()
            api.register_answer_tool(registry,restored)
            shown=[]
            resumed=execute_run(None,model=Model(),registry=registry,session=restored,
                                resume=True,on_content=shown.append)
            self.assertEqual(resumed.status,'completed')
            self.assertEqual(len(requests),2)
            self.assertEqual(shown,[resumed.answer])
            self.assertIn('## 仍需确认',resumed.answer)
            self.assertIn('不能保证周期方案可行',resumed.answer)
            self.assertEqual(len([r for r in restored.records if r['kind']=='tool_completed']),2)

    def test_layout_labels_exact_saved_section_separately_from_paragraph(self):
        api=self.answers();session=Session()
        body='# Rules\n\n## Accounts\n\nPro required.\n\nShared guest excluded.\n\n## Other\n\nOther rules.'
        page=add_page(session,body);deliver(session)
        section=session.documents.read_section(page['outline'][1]['section_ref'])
        draft=self.draft(section['evidence_ref'])
        draft['layout']=[{'type':'judgment','index':1},{'type':'evidence','index':1},
                         {'type':'recommendation','index':1}]
        result=api.compile_answer(session,draft)
        self.assertIn('保存文本中的完整小节',result['answer'])
        self.assertNotIn('> Other rules.',result['answer'])
        start=body.index('Pro required.')
        draft['evidence'][0]['reference']=f"{page['document_id']}@{start}-{start+len('Pro required.')}"
        partial=api.compile_answer(session,draft)
        self.assertIn('展示范围：原段',partial['answer'])
        self.assertNotIn('完整小节',partial['answer'])

    def test_layout_recommendation_needs_a_declared_basis_or_unknown(self):
        api=self.answers();session=Session()
        page=add_page(session,'# Rules\n\nPro required.');deliver(session)
        draft=self.draft(page['evidence_ref'])
        draft['recommendations'][0].update(judgment_indices=[],unknown_indices=[])
        draft['layout']=[{'type':'judgment','index':1},{'type':'evidence','index':1},
                         {'type':'recommendation','index':1},{'type':'unknown','index':1}]
        with self.assertRaisesRegex(ValueError,'recommendations\\[1\\].*关联'):
            api.compile_answer(session,draft)

    def test_budget_preparation_preserves_registered_final_tool_and_research_tools(self):
        api=self.answers();session=Session();begin(session)
        page=add_page(session,'# Rules\n\nPro required.');deliver(session)
        registry=default_registry(None,'fake',session);api.register_answer_tool(registry,session)
        session.last_run['config']['final_outputs']=registry.final_outputs
        session.last_run['progress_stage']={'base_revision':0}
        from seekra.search.context import prepare_context
        messages,tools,info,system=prepare_context(session.last_run['messages'],registry.definitions,session,120000)
        self.assertEqual(tools, registry.definitions)
        self.assertIsNone(info['progress_stage'])
        self.assertEqual(info['request_characters'],len(json.dumps({'messages':messages,'tools':tools},ensure_ascii=False)))

    def test_unknown_prerequisite_marks_candidate_but_not_investigation(self):
        api=self.answers();session=Session()
        page=add_page(session,'# Rules\n\nPro required.');deliver(session)
        draft=self.draft(page['evidence_ref'])
        draft['unknowns']=[{'question':'联网是否重置计时','answer_impact':'决定周期方案能否成立'}]
        draft['recommendations']=[
            {'text':'周期联网方案','judgment_indices':[1],'unknown_indices':[1],'dependency_indices':[1]},
            {'text':'先向维护方核实重置规则','judgment_indices':[],'unknown_indices':[1],'dependency_indices':[]}]
        draft['layout']=[{'type':'judgment','index':1},{'type':'evidence','index':1},
                         {'type':'recommendation','index':1},{'type':'recommendation','index':2}]
        try:result=api.compile_answer(session,draft)
        except ValueError as exc:self.fail(f'需要区分候选方案前提与调查目标：{exc}')
        answer=result['answer'];split=answer.index('先向维护方核实重置规则')
        self.assertIn('候选方案（关键前提尚未确认，不能据此保证可行）',answer[:split])
        self.assertIn('尚未确认的前提：联网是否重置计时',answer[:split])
        self.assertIn('待核实事项：联网是否重置计时',answer[split:])
        self.assertNotIn('候选方案',answer[split:])
        self.assertEqual(result['draft']['recommendations'][0]['dependency_indices'],[1])
        del draft['layout']
        legacy=api.compile_answer(session,draft)
        self.assertIn('候选方案（关键前提尚未确认，不能据此保证可行）',legacy['answer'])

    def test_unknown_dependencies_must_be_valid_related_indices(self):
        api=self.answers();session=Session()
        page=add_page(session,'# Rules\n\nPro required.');deliver(session)
        draft=self.draft(page['evidence_ref'])
        draft['unknowns'].append({'question':'其他未知','answer_impact':'其他作用'})
        for indices in ([2],[True],[1,1],None):
            with self.subTest(indices=indices):
                draft['recommendations'][0]['dependency_indices']=indices
                with self.assertRaisesRegex(ValueError,'dependency_indices'):
                    api.compile_answer(session,draft)

    def test_unclassified_unknown_is_not_automatically_a_blocking_prerequisite(self):
        api=self.answers();session=Session();begin(session)
        page=add_page(session,'# Rules\n\nPro required.');deliver(session)
        draft=self.draft(page['evidence_ref'])
        draft['layout']=[{'type':'judgment','index':1},{'type':'evidence','index':1},
                         {'type':'recommendation','index':1}]
        result=api.compile_answer(session,draft)
        self.assertNotIn('候选方案（',result['answer'])
        self.assertIn('待核实事项：账户是否支持',result['answer'])


if __name__=='__main__':unittest.main()
