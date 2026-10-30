"""独立上下文压缩的提交、费用、失败及重载；不比较模型答题质量。"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from seekra.harness.runtime import CancellationToken, ModelResponse
from seekra.search.state import SearchSession as Session
from test_documents import complete_tool


class SummaryModel:
    identity = 'summary-test'

    def __init__(self, text='此前核实旧版本条件，仍需查证新版本。', fail=False):
        self.text, self.fail, self.requests = text, fail, []

    def generate(self, messages, tools, *, on_content, on_usage, cancel):
        self.requests.append(deepcopy((messages, tools)))
        on_usage({'input_tokens': 100, 'output_tokens': 20, 'cache_read_tokens': 0})
        if self.fail:
            raise RuntimeError('summary unavailable')
        on_content(self.text)
        return ModelResponse({'role': 'assistant', 'content': self.text}, 'stop')


def seed(session, count=12):
    for i in range(count):
        complete_tool(session, 'inspect', {'operation': 'inspect', 'status': 'success',
                                         'content': f'条件 {i}: ' + 'bounded observation ' * 28})


class CompactionTests(unittest.TestCase):
    def prepare(self, session, model, budget=9000):
        from seekra.search.compaction import prepare_model_turn
        return prepare_model_turn(session.last_run['messages'], [], session, budget, model,
                                  cancel=CancellationToken())

    def test_pressure_without_progress_compacts_and_preserves_latest_interaction(self):
        from seekra.search.context import prepare_context
        session = Session()
        seed(session)
        model = SummaryModel()
        # 容量管理不再创建研究进展阶段或收缩工具。
        definitions = [{'type': 'function', 'function': {'name': 'inspect', 'parameters': {}}}]
        _, tools, info, _ = prepare_context(session.last_run['messages'], definitions, session, 18000)
        self.assertEqual(tools, definitions)
        self.assertFalse(info.get('checkpoint_requested'))
        result = self.prepare(session, model)
        self.assertEqual(len(model.requests), 1)
        self.assertIn(model.text, json.dumps(result[0], ensure_ascii=False))
        self.assertEqual(result[0][-1], session.last_run['messages'][-1])
        self.assertIsNone(session.last_run['research'])
        self.assertLess(result[2]['request_characters'], 9000)
        self.assertEqual(len([r for r in session.records if r['kind'] == 'context_compacted']), 1)
        self.assertEqual(session.usage_totals()['input_tokens'], 100)
        self.assertEqual(session.usage_totals()['calls'], 1)
        self.assertEqual(model.requests[0][1], [], '摘要请求没有工具权限')

    def test_compaction_reload_and_provider_rebuild_preserve_summary_without_redelivery(self):
        from seekra.search.recovery import _recovery_snapshot, prepare_model_context
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(Path(directory))
            seed(session)
            self.prepare(session, SummaryModel('线索 S1-P1@0-50 必须回查，不能作为正文。'))
            restored = Session.load(session.path)
            before = deepcopy(restored.last_run['delivered'])
            snapshot = _recovery_snapshot(restored)
            self.assertIn('S1-P1@0-50', snapshot['compaction']['summary'])
            selected = prepare_model_context(restored.last_run['messages'], [], restored, 9000, SummaryModel())
            self.assertIn('S1-P1@0-50', json.dumps(selected[0], ensure_ascii=False))
            self.assertEqual(restored.last_run['delivered'], before)

    def test_summary_error_or_empty_response_keeps_old_window_and_records_usage(self):
        for model in (SummaryModel(fail=True), SummaryModel('')):
            with self.subTest(fail=model.fail):
                session = Session()
                seed(session)
                messages = deepcopy(session.last_run['messages'])
                self.prepare(session, model, budget=12000)
                self.assertIsNone(session.last_run.get('provider_window'))
                self.assertEqual(session.last_run['messages'], messages)
                self.assertFalse(any(r['kind'] == 'context_compacted' for r in session.records))
                self.assertEqual(session.usage_totals()['input_tokens'], 100)
                # 同一待压缩历史不会无变化地反复请求。
                self.prepare(session, model, budget=12000)
                self.assertEqual(len(model.requests), 1)

    def test_storage_failure_does_not_publish_compacted_window(self):
        session = Session()
        seed(session)
        record = session.record
        def fail(kind, **data):
            if kind == 'context_compacted':
                raise OSError('disk full')
            return record(kind, **data)
        with patch.object(session, 'record', side_effect=fail):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.prepare(session, SummaryModel())
        self.assertIsNone(session.last_run.get('provider_window'))

    def test_summary_does_not_enter_task_messages_or_change_research(self):
        session = Session()
        seed(session)
        original = deepcopy(session.last_run['messages'])
        self.prepare(session, SummaryModel())
        self.assertEqual(session.last_run['messages'], original)
        self.assertEqual(session.last_run['calls'], 0)
        self.assertFalse(session.last_run['pending'])

    def test_cancellation_and_oversized_summary_do_not_replace_old_context(self):
        from seekra.search.compaction import prepare_model_turn
        from seekra.harness.runtime import RunCancelled
        session = Session()
        seed(session)
        cancel = CancellationToken()
        class CancelModel(SummaryModel):
            def generate(self, *args, **kwargs):
                cancel.cancel()
                return super().generate(*args, **kwargs)
        with self.assertRaises(RunCancelled):
            prepare_model_turn(session.last_run['messages'], [], session, 9000, CancelModel(), cancel=cancel)
        self.assertIsNone(session.last_run.get('provider_window'))
        self.assertEqual(session.usage_totals()['calls'], 1)
        other = Session()
        seed(other)
        self.prepare(other, SummaryModel('过长摘要' * 5000), budget=12000)
        self.assertIsNone(other.last_run.get('provider_window'))

    def test_failed_summary_over_hard_limit_pauses_without_discarding_history(self):
        from seekra.search.context import ContextLimit
        session = Session()
        seed(session)
        before = deepcopy(session.last_run['messages'])
        with self.assertRaisesRegex(ContextLimit, '旧记录保留'):
            self.prepare(session, SummaryModel(fail=True))
        self.assertEqual(session.last_run['messages'], before)
        self.assertIsNone(session.last_run.get('provider_window'))

    def test_cancel_during_final_projection_keeps_previous_window(self):
        from seekra.search.compaction import prepare_model_turn
        from seekra.harness.runtime import RunCancelled
        session = Session()
        seed(session)
        cancel = CancellationToken()
        class CancelProjection(SummaryModel):
            def request_content(self, rows, tools):
                if self.text in json.dumps(rows, ensure_ascii=False):
                    cancel.cancel()
                return {'messages': rows, 'tools': tools}
        with self.assertRaises(RunCancelled):
            prepare_model_turn(session.last_run['messages'], [], session, 9000, CancelProjection(), cancel=cancel)
        self.assertIsNone(session.last_run.get('provider_window'))
        self.assertFalse(any(r['kind'] == 'context_compacted' for r in session.records))

    def test_repeated_compaction_keeps_previous_summary_and_newer_conditions(self):
        session = Session()
        seed(session)
        self.prepare(session, SummaryModel('第一阶段：旧版需要 30 天复验。'))
        first_start = session.last_run['provider_window']['start']
        seed(session)
        second = SummaryModel('第二阶段：旧版 30 天，新版 14 天；版本不能混用。')
        self.prepare(session, second)
        self.assertEqual(len(second.requests), 1)
        self.assertIn('旧版需要 30 天', str(second.requests[0]))
        self.assertGreater(session.last_run['provider_window']['start'], first_start)
        self.assertEqual(session.usage_totals()['calls'], 2)

    def test_snapshot_removed_from_actual_input_is_not_resurrected_for_summary(self):
        from seekra.search.research import update_research
        session = Session()
        seed(session, count=2)
        session.last_run['provider_window'] = {'version': 1, 'start': 2, 'reason': 'missing_continuation',
            'snapshot': {'sources': [], 'latest_results': [], 'research': None,
                         'recent_results': [{'preview': 'REMOVED_RECOVERY_EXCERPT'}]}}
        complete_tool(session, 'update_research', update_research(session, 0, operations=[
            {'op': 'add_gap', 'question': '未核实新版本'}]))
        seed(session)
        model = SummaryModel()
        self.prepare(session, model, budget=14000)
        self.assertEqual(len(model.requests), 1)
        self.assertNotIn('REMOVED_RECOVERY_EXCERPT', str(model.requests))

    def test_summary_receives_previously_delivered_body_before_new_offload(self):
        from test_documents import add_page
        from test_research import deliver
        session = Session()
        add_page(session, '# Conditions\n\n' + 'background ' * 80 + 'ONLY_WHEN_FILE_NEVER_CHANGES')
        seed(session, count=18)
        deliver(session, max_chars=200000)
        complete_tool(session, 'inspect', {'operation': 'inspect', 'content': '最新结果仍需保留'})
        model = SummaryModel()
        self.prepare(session, model, budget=18000)
        self.assertEqual(len(model.requests), 1)
        self.assertIn('ONLY_WHEN_FILE_NEVER_CHANGES', model.requests[0][0][1]['content'],
                      '摘要不得只看到本轮新生成的250字符预览而丢失上一轮实际可见的条件')


if __name__ == '__main__':
    unittest.main()
