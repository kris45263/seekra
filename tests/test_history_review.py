"""历史模型消息的核对入口按当时执行事实绑定，不依赖当前进展。"""
import json
from pathlib import Path
import tempfile
import unittest

from seekra.context import history_index, read_history
from seekra.agent import execute_run
from seekra.research import update_research
from seekra.delivery import remember_delivery
from seekra.runtime import ModelResponse
from seekra.session import Session
from seekra.tools import default_registry
from test_documents import add_page, begin, complete_tool
from test_research import deliver


class HistoryReviewTests(unittest.TestCase):
    def review(self, session, answer_id, **kwargs):
        try:
            return read_history(session, answer_id, view='review', **kwargs)
        except ValueError as exc:
            self.fail(f'应提供历史消息的核对视图：{exc}')

    def old_answer(self, session):
        run_id = begin(session)
        question_id = session.records[-1]['id']
        page = add_page(session, '# Rules\nBefore activation obtain owner approval.')
        progress = update_research(session, base_revision=0, operations=[
            {'op':'add_gap', 'question':'事后补救流程未知'}])
        complete_tool(session, 'update_research', progress)
        progress_id = session.records[-1]['id']
        answer_id = session.record('model_completed', run_id=run_id,
            message={'role':'assistant', 'content':'已保存补救未知项。'}, finish_reason='stop')
        session.record('run_ended', run_id=run_id, status='completed')
        return question_id, page, progress_id, answer_id

    def test_review_binds_original_run_question_and_progress_after_new_task(self):
        session = Session()
        question_id, page, progress_id, answer_id = self.old_answer(session)
        old_run = session.records[int(answer_id)-1]['run_id']
        begin(session)
        complete_tool(session, 'update_research', update_research(
            session, base_revision=0, operations=[]))
        result = self.review(session, answer_id)
        origin = result['origin']
        self.assertEqual(origin['run_id'], old_run)
        self.assertEqual(origin['response_record_id'], answer_id)
        self.assertEqual(origin['question_action']['arguments']['record_id'], question_id)
        self.assertEqual(origin['research_at_response']['record_id'], progress_id)
        self.assertEqual(origin['research_at_response']['revision'], 1)
        prior = read_history(session, **origin['research_at_response']['read_action']['arguments'])
        self.assertIn('事后补救流程未知', prior['content'])
        self.assertEqual(result['content'], '已保存补救未知项。')

    def test_later_same_url_snapshot_and_progress_do_not_rewrite_origin(self):
        session = Session()
        _, old_page, old_progress, answer_id = self.old_answer(session)
        before = self.review(session, answer_id)['origin']
        begin(session)
        new_page = add_page(session, '# Rules\nChanged policy.')
        complete_tool(session, 'update_research', update_research(
            session, base_revision=0, operations=[{'op':'add_gap','question':'新任务缺口'}]))
        after = self.review(session, answer_id)['origin']
        self.assertEqual(before, after)
        self.assertEqual(after['source_snapshots'][0]['document_id'], old_page['document_id'])
        self.assertEqual(after['source_snapshots'][0]['snapshot_id'], old_page['snapshot_id'])
        self.assertNotEqual(old_page['document_id'], new_page['document_id'])
        self.assertEqual(after['research_at_response']['record_id'], old_progress)

    def test_failed_update_and_later_same_run_state_do_not_replace_prior_commit(self):
        session = Session()
        run_id = begin(session)
        progress = update_research(session, base_revision=0, operations=[
            {'op':'add_gap','question':'旧未确认事项'}])
        complete_tool(session, 'update_research', progress)
        progress_id = session.records[-1]['id']
        complete_tool(session, 'update_research', {'operation':'update_research','status':'failed',
                                                   'error_code':'invalid_arguments'})
        answer_id = session.record('model_completed', run_id=run_id,
            message={'role':'assistant','content':'旧稿'}, finish_reason='stop')
        complete_tool(session, 'update_research', update_research(
            session, base_revision=1, operations=[{'op':'remove_gap','id':'gap-1'}]))
        self.assertEqual(self.review(session, answer_id)['origin']
                         ['research_at_response']['record_id'], progress_id)

    def test_absence_is_explicit_and_index_offers_review_action(self):
        session = Session()
        run_id = begin(session)
        answer_id = session.record('model_completed', run_id=run_id,
            message={'role':'assistant','content':'直接回答'}, finish_reason='stop')
        result = self.review(session, answer_id)
        self.assertIsNone(result['origin']['research_at_response'])
        self.assertEqual(result['origin']['source_snapshots'], [])
        row = next(r for r in history_index(session)['records'] if r['record_id'] == answer_id)
        self.assertEqual(row['review_action']['arguments'], {'record_id':answer_id, 'view':'review'})

    def test_review_paging_reload_and_reset_keep_record_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            _, _, _, answer_id = self.old_answer(session)
            before = self.review(session, answer_id, limit=4)
            restored = Session.load(session.path)
            self.assertEqual(self.review(restored, answer_id, limit=4), before)
            parts, result = [before['content']], before
            while result['continuation']:
                result = read_history(restored, **result['continuation']['arguments'])
                self.assertEqual(result['origin'], before['origin'])
                parts.append(result['content'])
            self.assertEqual(''.join(parts), '已保存补救未知项。')
            self.assertEqual(before['offset_basis'], 'model_content_v1')
            restored.clear()
            with self.assertRaises(ValueError):
                read_history(restored, answer_id, view='review')

    def test_review_rejects_non_model_records_and_invalid_ranges(self):
        session = Session()
        _, page, _, answer_id = self.old_answer(session)
        with self.assertRaises(ValueError):
            read_history(session, page['record_id'], view='review')
        for kwargs in ({'start':True}, {'limit':0}, {'start':999}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                read_history(session, answer_id, view='review', **kwargs)

    def test_inherited_snapshot_cited_by_then_progress_keeps_acquisition_identity(self):
        session = Session()
        _, page, _, _ = self.old_answer(session)
        run_id = begin(session)
        deliver(session)
        result = update_research(session, base_revision=0, operations=[{
            'op':'add_finding','summary':'启用前需要批准','conditions':'', 'status':'supported',
            'references':[page['evidence_ref']]}])
        self.assertEqual(result['status'], 'success')
        complete_tool(session, 'update_research', result)
        answer_id = session.record('model_completed', run_id=run_id,
            message={'role':'assistant','content':'继承快照中的条件。'}, finish_reason='stop')
        sources = self.review(session, answer_id)['origin']['source_snapshots']
        self.assertTrue(sources, '当时进展引用继承快照时，应给出该快照的原获取身份')
        source = sources[0]
        self.assertEqual(source['document_id'], page['document_id'])
        self.assertEqual(source['record_id'], page['record_id'])

    def test_many_sources_are_bounded_with_record_range_and_history_entry(self):
        session = Session()
        run_id = begin(session)
        for n in range(10):
            add_page(session, f'# Rules\nVersion {n}.', url=f'https://example.org/{n}')
        answer_id = session.record('model_completed', run_id=run_id,
            message={'role':'assistant','content':'比较结果。'}, finish_reason='stop')
        origin = self.review(session, answer_id)['origin']
        self.assertEqual(len(origin['source_snapshots']), 8)
        self.assertEqual(origin['omitted_source_count'], 2)
        self.assertEqual(origin['source_record_range']['through'], answer_id)
        self.assertEqual(origin['source_history_action']['tool'], 'list_history')

    def test_review_tool_reaches_next_actual_request_without_certifying_navigation(self):
        session = Session()
        _, _, progress_id, answer_id = self.old_answer(session)
        seen = []

        class Model:
            identity = 'test:review-navigation'
            def generate(model, messages, tools, **kwargs):
                if not seen:
                    seen.append(True)
                    definition = next(t['function'] for t in tools if t['function']['name']=='read_history')
                    self.assertIn('review', definition['parameters']['properties']['view']['enum'])
                    return ModelResponse({'role':'assistant','tool_calls':[{
                        'id':'review-old','type':'function','function':{'name':'read_history',
                        'arguments':json.dumps({'record_id':answer_id,'view':'review'})}}]}, 'tool_calls')
                result = json.loads(messages[-1]['content'])
                self.assertEqual(result['origin']['research_at_response']['record_id'], progress_id)
                spans = {}
                remember_delivery(spans, [messages[-1]])
                self.assertEqual(spans, {}, '历史模型文字和快照导航不计为原文交付')
                return ModelResponse({'role':'assistant','content':'将核对当时进展。'}, 'stop')

        result = execute_run('核对旧回答', model=Model(),
                             registry=default_registry(None,'fake',session), session=session)
        self.assertEqual(result.status, 'completed')


if __name__ == '__main__':
    unittest.main()
