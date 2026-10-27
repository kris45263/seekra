"""原生 reasoning 的续接、预算与业务恢复；不访问真实服务。"""
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from seekra.agent import run_agent
from seekra.runtime import CancellationToken
from seekra.session import Session
from test_agent import call
from test_deepseek_stream import ByteStream, event, DONE


SECRET = 'private-provider-reasoning'


def native(text='', calls=None, reasoning=SECRET):
    message = {'role': 'assistant', 'content': text, 'reasoning_content': reasoning}
    if calls:
        message['tool_calls'] = [dict(c, index=i) for i, c in enumerate(calls)]
    chunk = {'model': 'deepseek-flash', 'system_fingerprint': 'fp-test',
             'choices': [{'index': 0, 'delta': message, 'finish_reason': 'tool_calls' if calls else 'stop'}],
             'usage': {'prompt_tokens': 50, 'completion_tokens': 10,
                       'completion_tokens_details': {'reasoning_tokens': 6}}}
    return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                          content='data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n')


class ReasoningTests(unittest.TestCase):
    def test_recovery_latest_batch_is_scoped_to_its_model_response(self):
        for recovery in ('budget', 'reload'):
            with self.subTest(recovery=recovery), tempfile.TemporaryDirectory() as directory:
                session = Session.create(Path(directory))
                cancel = CancellationToken()
                requests, searches = [], []
                def transport(request):
                    body = json.loads(request.content)
                    if request.url.host == 'api.tavily.com':
                        searches.append(body['query'])
                        return httpx.Response(200, json={'results':[{
                            'url':'https://example.org/'+body['query'], 'title':body['query'],
                            'content':'Evidence for '+body['query']}]})
                    requests.append(body)
                    number = len(requests)
                    if number <= 3:
                        return native(calls=[call('tavily_search', {'query':f'query-{number}'}, 'reused')],
                            reasoning=SECRET*10000 if recovery == 'budget' and number == 3 else SECRET)
                    return native('已完成。')
                def interrupt(name, **details):
                    if recovery == 'reload' and name == 'tool_result' and len(searches) == 3:
                        cancel.cancel()
                if recovery == 'reload':
                    with self.assertRaises(KeyboardInterrupt):
                        self.ask(session, transport, cancel=cancel, on_event=interrupt)
                    session = Session.load(session.path)
                    self.ask(session, transport, question=None, resume=True)
                else:
                    self.ask(session, transport)
                self.assertEqual(searches, ['query-1', 'query-2', 'query-3'])
                self.assertEqual(len(requests), 4)
                user = next(m for m in requests[-1]['messages'] if m['role'] == 'user')
                snapshot = json.loads(user['content'].split('[harness 提供的恢复资料]\n')[1])
                latest = snapshot['latest_results']
                self.assertEqual(len(latest), 1)
                self.assertEqual(json.loads(latest[0]['arguments']), {'query':'query-3'})
                self.assertEqual(latest[0]['result']['query'], 'query-3')
                record = session.record_entry(latest[0]['record_id'])
                self.assertEqual(json.loads(record['result']), latest[0]['result'])
                self.assertEqual(len(snapshot['recent_results']), 3)
                self.assertEqual(session.last_run['status'], 'completed')

    def test_recovery_directory_keeps_other_original_sections_as_navigation_only(self):
        from seekra.provider_context import _recovery_snapshot
        from seekra.checkpoint import present_actions
        from seekra.delivery import remember_delivery
        from test_documents import add_page
        session=Session();page=add_page(session,'# Guide\n\n## Requirements\nPro required.\n\n## Limitations\nGuest excluded.')
        snapshot=_recovery_snapshot(session)
        row=snapshot['documents']['documents'][0]
        self.assertEqual(row['section_count'],3)
        self.assertEqual([s['title'] for s in row['outline_preview']],['Guide','Requirements','Limitations'])
        section=row['outline_preview'][-1]
        action=section['read_action']
        self.assertIn('Guest excluded.',session.documents.read_section(**action['arguments'])['content'])
        marked=present_actions(snapshot,{'base_revision':0})
        self.assertNotIn('availability',marked['documents']['documents'][0]['outline_action'])
        self.assertNotIn('availability',marked['documents']['documents'][0]['outline_preview'][-1]['read_action'])
        delivered={};remember_delivery(delivered,[{'role':'tool','content':json.dumps(snapshot['documents'])}])
        self.assertEqual(delivered,{},'目录和标题不作为正文交付')

    def test_recovery_omits_old_excerpts_before_cutting_saved_progress(self):
        from copy import deepcopy
        from seekra.models import DeepSeekModel
        from seekra.provider_context import _recovery_snapshot, prepare_model_context
        from seekra.research import update_research
        from test_documents import add_page, complete_tool
        from test_research import deliver
        session=Session();page=add_page(session,'# Guide\nPro required.\n\n'+'Background. '*1600);deliver(session)
        for start in (2000,8000):
            complete_tool(session,'read_document',session.documents.read_document(page['document_id'],start=start))
            deliver(session)
        saved=complete_tool(session,'update_research',update_research(session,0,operations=[
            {'op':'add_finding','summary':'需要 Pro','conditions':'条件不能遗漏','status':'supported',
             'references':[page['evidence_ref']],
             'evidence_quotes':[{'reference':page['evidence_ref'],'text':'Pro required.'}]}]))
        snapshot=_recovery_snapshot(session);original=deepcopy(snapshot)
        session.last_run['provider_window']={'version':1,'start':len(session.last_run['messages']),
                                            'reason':'missing_continuation','snapshot':snapshot}
        _,_,full,_=prepare_model_context(session.last_run['messages'],[],session,120000,
                                         DeepSeekModel(None,'test','fake'))
        budget=full['request_characters']-2000
        messages,tools,info,_=prepare_model_context(session.last_run['messages'],[],session,budget,
                                                   DeepSeekModel(None,'test','fake'))
        user=next(m for m in messages if m['role']=='user')
        recovered=json.loads(user['content'].split('[harness 提供的恢复资料]\n')[1])
        self.assertEqual(recovered['sources'],[])
        self.assertEqual(len(recovered['not_displayed_sources']),len(snapshot['sources']))
        self.assertEqual(recovered['research']['state'],saved['state'])
        self.assertIn('Pro required.',user['content'])
        self.assertEqual(snapshot,original)
        self.assertEqual(info['request_characters'],len(json.dumps({'messages':messages,'tools':tools},ensure_ascii=False)))
        self.assertLessEqual(info['request_characters'],budget)

    def test_failed_progress_save_remains_visible_after_business_rebuild(self):
        from seekra.models import DeepSeekModel
        from seekra.provider_context import _recovery_snapshot, prepare_model_context
        from test_documents import add_page, complete_tool
        session=Session();add_page(session,'# Guide\nText.')
        error={'operation':'update_research','status':'failed','error_code':'invalid_arguments',
               'error':'引用未交付，版本仍为0。'}
        complete_tool(session,'update_research',error)
        snapshot=_recovery_snapshot(session)
        session.last_run['provider_window']={'version':1,'start':len(session.last_run['messages']),
                                            'reason':'missing_continuation','snapshot':snapshot}
        messages,_,_,_=prepare_model_context(session.last_run['messages'],[],session,120000,
                                             DeepSeekModel(None,'test','fake'))
        self.assertIn('引用未交付，版本仍为0。',next(m['content'] for m in messages if m['role']=='user'))
        self.assertIsNone(session.last_run.get('research'))

    def test_rebuild_preserves_latest_tool_body_tail_in_actual_request(self):
        from seekra.models import DeepSeekModel
        from seekra.provider_context import _recovery_snapshot, prepare_model_context
        from seekra.delivery import remember_delivery
        from test_documents import add_page, complete_tool
        from test_research import deliver
        session=Session()
        body='# Guide\n\n## Conditions\n'+'Background. '*400+'\nException: guest accounts are excluded.'
        page=add_page(session,body);deliver(session)
        fresh=session.documents.read_section(page['outline'][1]['section_ref'])
        complete_tool(session,'read_section',fresh)
        snapshot=_recovery_snapshot(session)
        session.last_run['provider_window']={'version':1,'start':len(session.last_run['messages']),
                                           'reason':'provider_context_budget','snapshot':snapshot}
        messages,tools,info,_=prepare_model_context(session.last_run['messages'],[],session,120000,
                                                   DeepSeekModel(None,'test','fake'))
        user=next(m for m in messages if m['role']=='user')
        self.assertIn('Exception: guest accounts are excluded.',user['content'])
        delivery={};remember_delivery(delivery,[{'role':'tool','content':json.dumps(s,ensure_ascii=False)}
                                              for s in info['recovery_sources']])
        self.assertTrue(any(left<=body.index('Exception:')<right for left,right in delivery[page['document_id']]))

    def test_recovery_latest_batch_reflows_with_true_ranges_and_continuation(self):
        from copy import deepcopy
        from seekra.models import DeepSeekModel
        from seekra.provider_context import _recovery_snapshot, prepare_model_context
        from test_documents import add_page, complete_tool
        session=Session();body='# Guide\n\n## Conditions\n'+'Background. '*490
        page=add_page(session,body)
        fresh=session.documents.read_section(page['outline'][1]['section_ref'])
        complete_tool(session,'read_section',fresh)
        snapshot=_recovery_snapshot(session);original=deepcopy(snapshot)
        session.last_run['provider_window']={'version':1,'start':len(session.last_run['messages']),
                                            'reason':'missing_continuation','snapshot':snapshot}
        _,_,full,_=prepare_model_context(session.last_run['messages'],[],session,120000,
                                         DeepSeekModel(None,'test','fake'))
        budget=full['request_characters']-3000
        messages,tools,info,_=prepare_model_context(session.last_run['messages'],[],session,budget,
                                                   DeepSeekModel(None,'test','fake'))
        user=next(m for m in messages if m['role']=='user')
        recovered=json.loads(user['content'].split('[harness 提供的恢复资料]\n')[1])
        result=recovered['latest_results'][0]['result']
        self.assertEqual(result['content'],body[result['start']:result['end']])
        self.assertLess(result['end'],fresh['end'])
        self.assertFalse(result['complete'])
        action=result['continuation']
        remaining=session.documents.read_section(**action['arguments'])
        self.assertEqual(remaining['start'],result['end'])
        self.assertEqual(snapshot,original)
        self.assertEqual(info['request_characters'],len(json.dumps({'messages':messages,'tools':tools},ensure_ascii=False)))
        self.assertLessEqual(info['request_characters'],budget)

    def test_default_run_agent_enables_reasoning_and_replays_tool_continuation(self):
        session, requests = Session(), []
        def transport(request):
            body = json.loads(request.content)
            requests.append(body)
            self.assertEqual(body['thinking'], {'type':'enabled'})
            self.assertEqual(body['reasoning_effort'], 'high')
            if len(requests)==1:
                return native(calls=[call('list_documents', {}, 'documents')])
            self.assertEqual(next(m for m in body['messages'] if m.get('tool_calls'))['reasoning_content'],SECRET)
            return native('尚无可核实正文。')
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            run_agent('核实条件', client=client, model='deepseek-flash', deepseek_api_key='fake-ds',
                      tavily_api_key='fake-tv', session=session)
        self.assertEqual(len(requests),2)
        self.assertTrue(any(entry.value is not None for entry in session.model_continuations(session.messages)))

    def ask(self, session, transport, question='核实条件', **kwargs):
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            return run_agent(question, client=client, model='deepseek-flash', deepseek_api_key='fake-ds',
                             tavily_api_key='fake-tv', session=session, thinking=True, **kwargs)

    def test_complete_reasoning_is_replayed_and_not_published(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            requests, events, shown = [], [], []
            def transport(request):
                body = json.loads(request.content)
                requests.append(body)
                self.assertEqual(body['model'], 'deepseek-flash')
                self.assertEqual(body['thinking'], {'type': 'enabled'})
                self.assertEqual(body['reasoning_effort'], 'high')
                self.assertNotIn('tool_choice', body)
                if len(requests) == 1:
                    return native(calls=[call('list_documents', {}, 'index')])
                previous = next(m for m in body['messages'] if m.get('tool_calls'))
                self.assertEqual(previous['reasoning_content'], SECRET)
                self.assertEqual(previous['content'], '')
                public = [{k: v for k, v in m.items() if k != 'reasoning_content'} for m in body['messages']]
                self.assertEqual(session.last_run['_request_messages'], public)
                return native('条件尚未核实。')
            answer = self.ask(session, transport, on_content=shown.append,
                              on_event=lambda name, **details: events.append({'event': name, **details}))
            self.assertEqual(answer, '条件尚未核实。')
            self.assertNotIn(SECRET, ''.join(shown))
            self.assertNotIn(SECRET, json.dumps(events, ensure_ascii=False))
            self.assertNotIn(SECRET, session.path.read_text(encoding='utf-8'))
            selected = [r for r in session.records if r['kind'] == 'context_selected'][-1]
            actual = {'messages': requests[-1]['messages'], 'tools': requests[-1]['tools']}
            self.assertEqual(selected['request_characters'], len(json.dumps(actual, ensure_ascii=False)))
            self.assertEqual(selected['reasoning_characters'], len(SECRET))
            self.assertEqual(session.usage_totals()['reasoning_tokens'], 12)
            report = [r for r in session.records if r['kind'] == 'model_completed'][-1]['provider']
            self.assertEqual(report['response_model'], 'deepseek-flash')
            self.assertEqual(report['system_fingerprint'], 'fp-test')

    def test_pending_call_survives_reload_then_business_context_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            cancel = CancellationToken()
            def stop_after_response(name, **details):
                if name == 'model_response':
                    cancel.cancel()
            with self.assertRaises(KeyboardInterrupt):
                self.ask(session, lambda request: native(calls=[call('read_page',
                    {'url': 'https://example.org/conditions'}, 'page')]),
                    cancel=cancel, on_event=stop_after_response)
            restored = Session.load(session.path)
            requests = []
            def transport(request):
                if request.url.host == 'api.tavily.com':
                    return httpx.Response(200, json={'results': [{'url': 'https://example.org/conditions',
                                             'raw_content': '# Conditions\nPro license required.'}], 'failed_results': []})
                body = json.loads(request.content)
                requests.append(body)
                self.assertIn('Pro license required.', str(body['messages']))
                self.assertNotIn(SECRET, str(body))
                self.assertFalse(any(m.get('tool_calls') for m in body['messages']))
                return native('需要 Pro 许可证。')
            answer = self.ask(restored, transport, question=None, resume=True)
            self.assertIn('Pro', answer)
            self.assertEqual(len(requests), 1)
            selection = [r for r in restored.records if r['kind'] == 'context_selected'][-1]
            self.assertEqual(selection['provider_window']['reason'], 'missing_continuation')
            self.assertTrue(restored.last_run['delivered']['S1-P1'])
            self.assertEqual(Session.load(restored.path).last_run['messages'], restored.last_run['messages'])
            self.assertNotIn(SECRET, restored.path.read_text(encoding='utf-8'))

    def test_reasoning_that_cannot_fit_triggers_explicit_context_rebuild(self):
        session = Session()
        calls = 0
        def transport(request):
            nonlocal calls
            calls += 1
            body = json.loads(request.content)
            if calls == 1:
                return native(calls=[call('update_research', {'base_revision':0,'operations':[
                    {'op':'add_gap','question':'未核实事项保持为缺口',
                     'answer_impact':'只影响扩展说明；原问题可带明确限制回答'}]}, 'progress')],
                              reasoning=SECRET * 4000)
            self.assertNotIn(SECRET, str(body))
            self.assertLessEqual(len(json.dumps({'messages': body['messages'], 'tools': body['tools']}, ensure_ascii=False)), 25000)
            self.assertIn('只影响扩展说明；原问题可带明确限制回答',str(body['messages']))
            return native('信息不足，尚未核实。')
        self.assertIn('信息不足', self.ask(session, transport, max_context_chars=25000))
        self.assertEqual(session.last_run['provider_window']['reason'], 'provider_context_budget')
        self.assertIn('未核实事项保持为缺口', str(session.last_run['provider_window']))

    def test_cancel_during_reasoning_keeps_partial_data_out_of_records_and_cache(self):
        session, cancel = Session(), CancellationToken()
        stream = ByteStream([event(reasoning_content=SECRET), cancel.cancel, event('不应发布', 'stop'), DONE])
        def transport(request):
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)
        with self.assertRaises(KeyboardInterrupt):
            self.ask(session, transport, cancel=cancel)
        self.assertTrue(stream.closed)
        self.assertEqual(session.last_run['status'], 'interrupted')
        self.assertFalse(any(entry.value is not None for entry in session.model_continuations(session.last_run['messages'])))
        self.assertNotIn(SECRET, str(session.records))
        self.assertFalse(any(r['kind'] == 'model_completed' for r in session.records))

    def test_mode_profile_cannot_change_during_resume_and_reset_clears_cache(self):
        session, cancel = Session(), CancellationToken()
        def event(name, **details):
            if name == 'tool_result': cancel.cancel()
        with self.assertRaises(KeyboardInterrupt):
            self.ask(session, lambda request: native(calls=[call('list_documents', {}, 'index')]),
                     cancel=cancel, on_event=event)
        previous_messages = list(session.last_run['messages'])
        self.assertTrue(any(entry.value is not None for entry in session.model_continuations(previous_messages)))
        with self.assertRaisesRegex(ValueError, '恢复配置'):
            self.ask(session, lambda request: self.fail('不能发出不同配置的恢复请求'),
                     question=None, resume=True, reasoning_effort='low')
        session.clear()
        self.assertTrue(all(not entry.required and entry.value is None
                            for entry in session.model_continuations(previous_messages)))

    def test_recovery_accepts_plain_text_from_custom_tools(self):
        from seekra.provider_context import _recovery_snapshot
        from test_documents import begin
        session = Session()
        run_id = begin(session)
        session.record('model_completed', run_id=run_id, message={'role': 'assistant', 'content': None,
                       'tool_calls': [call('custom', {}, 'custom')]}, finish_reason='tool_calls')
        session.record('tool_started', run_id=run_id, call_id='custom', tool_name='custom')
        session.record('tool_completed', run_id=run_id, call_id='custom', tool_name='custom',
                       result='plain custom result', page_updates={})
        snapshot = _recovery_snapshot(session)
        self.assertEqual(snapshot['recent_results'][0]['preview'], 'plain custom result')

    def test_new_progress_removes_stale_recovery_state_from_actual_input(self):
        from seekra.models import DeepSeekModel
        from seekra.provider_context import _recovery_snapshot, prepare_model_context
        from seekra.research import update_research
        from test_documents import begin, complete_tool
        session = Session()
        begin(session)
        complete_tool(session, 'update_research', update_research(session, 0, operations=[
            {'op':'add_gap','question':'OLD_RECOVERY_GAP'}]))
        window = {'version':1, 'start':len(session.last_run['messages']),
                  'reason':'missing_continuation', 'snapshot':_recovery_snapshot(session)}
        session.last_run['provider_window'] = window
        complete_tool(session, 'update_research', update_research(session, 1, operations=[
            {'op':'remove_gap','id':'gap-1'}, {'op':'add_gap','question':'NEW_CURRENT_GAP'}]))
        messages, _, info, _ = prepare_model_context(session.last_run['messages'], [], session, 120000,
                                                     DeepSeekModel(None, 'test', 'fake', thinking=True))
        self.assertNotIn('OLD_RECOVERY_GAP', json.dumps(messages))
        self.assertIn('NEW_CURRENT_GAP', json.dumps(messages))
        self.assertIn('OLD_RECOVERY_GAP', json.dumps(window['snapshot']), '恢复记录本身保持不变')
        self.assertEqual(info['recovery_sources'], [])

    def test_recovery_navigation_uses_actual_input_and_keeps_snapshot_immutable(self):
        from copy import deepcopy
        from seekra.models import DeepSeekModel
        from seekra.provider_context import _recovery_snapshot, prepare_model_context
        from seekra.research import update_research
        from test_documents import add_page, complete_tool
        from test_research import deliver
        session=Session()
        page=add_page(session,'# Guide\n\n## Conditions\n' + 'Long text.\n\n'*500)
        deliver(session)
        complete_tool(session,'update_research',update_research(session,0,operations=[
            {'op':'add_read_target','target':page['document_id'],'view':'text','purpose':'核对未读条件'}]))
        snapshot=_recovery_snapshot(session)
        snapshot['sources']=[]
        # 保存时曾有正文；恢复请求只保留导航，不交付这段正文。
        snapshot['research']['navigation'][0]['current_input']='full'
        original=deepcopy(snapshot)
        session.last_run['provider_window']={'version':1,'start':len(session.last_run['messages']),
                                              'reason':'missing_continuation','snapshot':snapshot}
        messages,tools,info,_=prepare_model_context(session.last_run['messages'],[],session,120000,
                                                   DeepSeekModel(None,'test','fake'))
        user=next(m for m in messages if m['role']=='user')
        recovered=json.loads(user['content'].split('[harness 提供的恢复资料]\n')[1])
        nav=recovered['research']['navigation'][0]
        self.assertEqual(nav['current_input'],'none')
        self.assertEqual(nav['ever_delivered'],'part')
        self.assertEqual(nav['first_undelivered']['start'],page['end'])
        self.assertEqual(snapshot,original)
        self.assertEqual(info['request_characters'],len(json.dumps({'messages':messages,'tools':tools},ensure_ascii=False)))

        # 恢复后已有新的工具结果时，动态状态移到末端；后续交付不改写早期恢复资料。
        complete_tool(session,'read_document',session.documents.read_document(
            f"{page['document_id']}@{page['end']}-2400",view='reference'))
        first,_,first_info,_=prepare_model_context(session.last_run['messages'],[],session,120000,
                                                   DeepSeekModel(None,'test','fake'))
        first_user=next(m for m in first if m['role']=='user')
        recovered=json.loads(first_user['content'].split('[harness 提供的恢复资料]\n')[1])
        self.assertNotIn('navigation',recovered['research'])
        session.record('context_selected',run_id=session.last_run['id'],**first_info)
        session.record('model_started',run_id=session.last_run['id'],round=session.last_run['calls']+1,
                       system=session.last_run['messages'][0]['content'])
        complete_tool(session,'read_document',session.documents.read_document(
            f"{page['document_id']}@2400-3000",view='reference'))
        second,_,second_info,_=prepare_model_context(session.last_run['messages'],[],session,120000,
                                                     DeepSeekModel(None,'test','fake'))
        self.assertEqual(next(m for m in second if m['role']=='user'),first_user)
        reading=json.loads(second[-1]['content'])['reading_state']
        self.assertEqual(reading['navigation'][0]['first_undelivered']['start'],2400)
        self.assertEqual(reading['document_coverage']['documents'][0]['current_input'],'part')
        self.assertEqual(snapshot,original)
        self.assertEqual(second_info['request_characters'],len(json.dumps({'messages':second,'tools':[]},ensure_ascii=False)))

    def test_recovery_actions_remain_available_within_budget(self):
        from copy import deepcopy
        from seekra.models import DeepSeekModel
        from seekra.provider_context import _recovery_snapshot, prepare_model_context
        from seekra.research import update_research
        from seekra.tools import default_registry
        from test_documents import add_page, complete_tool
        from test_research import deliver
        session=Session()
        page=add_page(session,'# Guide\n\n## Conditions\n'+'Long text.\n\n'*500)
        deliver(session)
        complete_tool(session,'update_research',update_research(session,0,operations=[
            {'op':'add_read_target','target':page['document_id'],'view':'text','purpose':'核对条件'}]))
        snapshot=_recovery_snapshot(session); original=deepcopy(snapshot)
        session.last_run['progress_stage']={'base_revision':1}
        session.last_run['provider_window']={'version':1,'start':len(session.last_run['messages']),
                                              'reason':'missing_continuation','snapshot':snapshot}
        with httpx.Client() as client:
            definitions=default_registry(client,'fake',session).definitions
        messages,tools,info,_=prepare_model_context(session.last_run['messages'],definitions,session,42000,
                                                   DeepSeekModel(None,'test','fake'))
        user=next(m for m in messages if m['role']=='user')
        recovered=json.loads(user['content'].split('[harness 提供的恢复资料]\n')[1])
        self.assertNotIn('availability',recovered['research']['navigation'][0]['read_action'])
        local=recovered['sources'][0]
        self.assertEqual(local['continuation'],None)
        self.assertEqual(snapshot,original)
        self.assertEqual(info['request_characters'],len(json.dumps({'messages':messages,'tools':tools},ensure_ascii=False)))
        self.assertLessEqual(info['request_characters'],42000)

    def test_recovery_quote_only_input_is_counted_as_source_excerpt_not_full_page(self):
        from copy import deepcopy
        from seekra.models import DeepSeekModel
        from seekra.provider_context import _recovery_snapshot, prepare_model_context
        from seekra.research import update_research
        from seekra.delivery import remember_delivery
        from test_documents import add_page, complete_tool
        from test_research import deliver
        session=Session()
        page=add_page(session,'# Guide\nPro required.\n\n'+'Background.\n\n'*600)
        deliver(session)
        complete_tool(session,'update_research',update_research(session,0,operations=[
            {'op':'add_finding','summary':'需要 Pro','conditions':'','status':'supported',
             'references':[page['evidence_ref']],
             'evidence_quotes':[{'reference':page['evidence_ref'],'text':'Pro required.'}]}]))
        snapshot=_recovery_snapshot(session);snapshot['sources']=[];original=deepcopy(snapshot)
        session.last_run['provider_window']={'version':1,'start':len(session.last_run['messages']),
                                              'reason':'missing_continuation','snapshot':snapshot}
        messages,tools,info,_=prepare_model_context(session.last_run['messages'],[],session,42000,
                                                   DeepSeekModel(None,'test','fake'))
        user=next(m for m in messages if m['role']=='user')
        recovered=json.loads(user['content'].split('[harness 提供的恢复资料]\n')[1])
        self.assertEqual(recovered['research']['document_coverage']['documents'][0]['current_input'],'part')
        delivered={}
        remember_delivery(delivered,[{'role':'tool','content':json.dumps(s,ensure_ascii=False)}
                                     for s in info['recovery_sources']])
        ref=recovered['research']['state']['findings'][0]['evidence_quotes'][0]['reference']
        self.assertEqual(delivered[page['document_id']],[(ref['start'],ref['end'])])
        self.assertEqual(snapshot,original)
        self.assertEqual(info['request_characters'],len(json.dumps({'messages':messages,'tools':tools},ensure_ascii=False)))


if __name__ == '__main__':
    unittest.main()
