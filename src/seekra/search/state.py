"""Search Session：在统一提交事务中维护页面、研究与交付投影。"""
from copy import deepcopy
from dataclasses import dataclass, field
import json

from seekra.harness.session import Session
from .documents.store import Documents, canonical
from .documents.delivery import remember_delivery


@dataclass
class SearchSession(Session):
    pages: dict[str, dict] = field(default_factory=dict)
    _documents: Documents = field(default_factory=Documents, repr=False)

    def __deepcopy__(self, memo):
        return type(self)(messages=deepcopy(self.messages, memo), pages=deepcopy(self.pages, memo))

    @property
    def documents(self):
        return self._documents.sync(self.records, self.id, self.secrets)

    @staticmethod
    def _validate_search_record(record):
        if record.get('research_version') is not None and record['research_version'] not in {1, 2}:
            raise ValueError('不支持的研究进展版本')
        if record.get('documents_version', 1) != 1:
            raise ValueError('不支持的资料视图版本')
        if record['kind'] == 'tool_completed':
            if not isinstance(record.get('page_updates', {}), dict):
                raise ValueError('页面更新必须是对象')
            if record.get('research_version') is not None:
                result = json.loads(record['result'])
                if not isinstance(result, dict):
                    raise ValueError('研究进展结果必须是对象')

    def _prepare_record(self, record):
        if record['kind'] == 'tool_completed':
            record.setdefault('page_updates', {})
            record = self.documents.prepare(record, self.secrets)
            if record.get('final_output_field') is not None:
                record['result'] = json.dumps(canonical(json.loads(record['result']), self.secrets), ensure_ascii=False)
                record['answer_version'] = 1
            if record.get('tool_name') == 'update_research':
                result = canonical(json.loads(record['result']), self.secrets)
                record['result'] = json.dumps(result, ensure_ascii=False)
                record['research_version'] = result.get('schema_version', 1)
        self._validate_search_record(record)
        return record

    def _persisted_record(self, record):
        persisted = super()._persisted_record(record)
        # 二次文本替换会破坏规范正文坐标或 JSON 数字；这些字段已由提交准备脱敏。
        if record.get('documents_version') == 1:
            persisted.update(result=record['result'], page_updates=record['page_updates'])
        if record.get('research_version') in {1, 2} or record.get('answer_version') == 1:
            persisted['result'] = record['result']
        return persisted

    def _apply(self, record):
        """与加载共用的纯投影；所有可拒绝的版本/结构检查在更改状态前进行。"""
        self._validate_search_record(record)
        kind = record['kind']
        previous = self.last_run
        selection = (previous or {}).get('_context_selection', {})
        super()._apply(record)
        if kind == 'session_reset':
            self.pages = {}
        elif kind == 'run_started':
            if previous is not None:
                for key in ('pages', 'delivered', 'visible', '_request_messages', '_recovery_sources'):
                    previous.pop(key, None)
            self.last_run.update(pages=deepcopy(self.pages), research=None, delivered={})
        elif 'run_id' in record:
            run = self.runs[record['run_id']]
            if kind == 'context_selected' and record.get('checkpoint_record_id'):
                run['checkpoint_record_id'] = record['checkpoint_record_id']
            elif kind == 'model_started':
                if 'progress_stage' in selection:
                    run['progress_stage'] = deepcopy(selection['progress_stage'])
                run['_request_messages'] = [selection.get('replacements', {}).get(str(i), run['messages'][i])
                                            for i in selection.get('message_indices', [])]
                run['_recovery_sources'] = selection.get('recovery_sources', [])
            elif kind == 'model_completed':
                # 发送前或流中失败不能被算作已经交付的证据。
                received = run.pop('_request_messages', []) + [
                    {'role': 'tool', 'content': json.dumps(source, ensure_ascii=False)}
                    for source in run.pop('_recovery_sources', [])]
                run['visible'] = {}
                remember_delivery(run['visible'], received)
                remember_delivery(run['delivered'], received)
            elif kind == 'tool_completed':
                if record.get('research_version') is not None:
                    result = json.loads(record['result'])
                    if result.get('operation') == 'update_research' and result.get('status') == 'success':
                        run['research'] = result
                        run['research_record_id'] = record['id']
                for url, page in record.get('page_updates', {}).items():
                    if page is None:
                        run['pages'].pop(url, None)
                    else:
                        run['pages'][url] = page
            elif kind == 'run_ended':
                run.pop('_request_messages', None)
                run.pop('_recovery_sources', None)
                if record['status'] == 'completed':
                    self.pages = run['pages']
