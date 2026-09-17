"""追加式会话记录：执行事实可恢复，完成历史只是其中一个视图。"""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
from uuid import uuid4

RECORD_KINDS = {
    'session_created', 'session_reset', 'run_started', 'run_resumed', 'context_selected',
    'model_started', 'model_usage', 'model_completed', 'tool_started', 'tool_completed',
    'run_ended', 'observation',
}


def _redact_text(value, secrets):
    """只处理文本值，不在序列化 JSON 上替换数字和结构字段。"""
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, '[已隐藏密钥]')
        return value
    if isinstance(value, list):
        return [_redact_text(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: item if key in {'id', 'tool_call_id', 'role', 'type', 'name'}
                else _redact_text(item, secrets) for key, item in value.items()}
    return value


def _clean(value):
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if k != 'reasoning_content'}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


class UnsupportedSessionVersion(ValueError):
    pass


@dataclass
class Session:
    """messages/pages 是完成历史的兼容视图；records 同时保留失败研究。"""
    messages: list[dict] = field(default_factory=list)
    pages: dict[str, dict] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid4().hex)
    path: Path | None = None
    records: list[dict] = field(default_factory=list)
    runs: dict[str, dict] = field(default_factory=dict)
    recovered_tail: bool = False
    secrets: tuple[str, ...] = field(default=(), repr=False)
    _valid_bytes: int = field(default=0, repr=False)
    _owner: int | None = field(default=None, repr=False)
    _storage_failed: bool = field(default=False, repr=False)
    _busy: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __deepcopy__(self, memo):
        """实验副本独立，不共享文件和锁。"""
        return Session(messages=deepcopy(self.messages, memo), pages=deepcopy(self.pages, memo))

    @classmethod
    def create(cls, directory: Path):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        session = cls()
        session.path = directory / f'{session.id}.jsonl'
        with session.path.open('xb'):
            pass
        session.record('session_created', version=1, session_id=session.id)
        return session

    @classmethod
    def load(cls, path: Path):
        path = Path(path)
        raw = path.read_bytes()
        session = cls(path=path, id=path.stem)
        lines = raw.splitlines(keepends=True)
        for index, line in enumerate(lines):
            if not line.endswith(b'\n') and index == len(lines)-1:
                session.recovered_tail = True
                break
            try:
                record = json.loads(line)
                if not isinstance(record, dict) or record.get('id') != str(index+1):
                    raise ValueError('记录编号不连续')
                if index == 0:
                    if record.get('kind') != 'session_created' or 'version' not in record:
                        raise ValueError('缺少会话格式头')
                    if record['version'] != 1:
                        raise UnsupportedSessionVersion(f"不支持会话格式版本 {record['version']}，当前支持版本 1。")
                session._apply(record)
                session.records.append(record)
                session._valid_bytes += len(line)
            except UnsupportedSessionVersion:
                raise
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise ValueError(f'会话记录第 {index+1} 行损坏，停止读取。') from exc
        if not session.records:
            raise ValueError('会话没有完整头记录')
        return session

    @classmethod
    def list_saved(cls, directory: Path):
        rows = []
        for path in sorted(Path(directory).glob('*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                s = cls.load(path)
                run = s.last_run or {}
                rows.append({'id': s.id, 'status': run.get('status', 'ready'), 'question': run.get('question', '')})
            except UnsupportedSessionVersion:
                rows.append({'id':path.stem,'status':'unsupported','question':''})
            except ValueError:
                rows.append({'id': path.stem, 'status': 'corrupt', 'question': ''})
        return rows

    @property
    def last_run(self):
        return next(reversed(self.runs.values()), None)

    @contextmanager
    def exclusive(self):
        """进程内及跨进程排他；崩溃由操作系统释放文件锁。"""
        if not self._busy.acquire(blocking=False):
            raise RuntimeError('会话正在运行，不能并发使用。')
        handle, acquired = None, False
        try:
            if self.path:
                handle = self.path.with_suffix('.lock').open('a+b')
                if handle.tell() == 0:
                    handle.write(b'0')
                    handle.flush()
                handle.seek(0)
                try:
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise RuntimeError('会话正由另一进程使用。') from exc
                acquired = True
                raw = self.path.read_bytes()
                if len(raw) != self._valid_bytes and not self.recovered_tail:
                    raise RuntimeError('会话文件已更新，请重新打开会话。')
                if self.recovered_tail:
                    current = Session.load(self.path)
                    if current.records != self.records:
                        raise RuntimeError('会话文件已更新，请重新打开会话。')
                    tail = raw[self._valid_bytes:]
                    if tail:
                        self.path.with_suffix(f'.partial-{uuid4().hex}').write_bytes(tail)
                    with self.path.open('r+b') as file:
                        file.truncate(self._valid_bytes)
                        file.flush()
                        os.fsync(file.fileno())
                    self.recovered_tail = False
            self._owner = threading.get_ident()
            yield
        finally:
            self._owner = None
            if handle:
                if acquired:
                    handle.seek(0)
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
            self._busy.release()

    def record(self, kind: str, **data) -> str:
        if kind not in RECORD_KINDS or {'id', 'kind', 'time'} & data.keys():
            raise ValueError('未知记录类型或覆盖记录元数据')
        if self._storage_failed:
            raise RuntimeError('会话存储失败，已停止写入；请重新打开并核对记录。')
        if self._owner != threading.get_ident():
            with self.exclusive():
                return self.record(kind, **data)
        record = _clean({'id': str(len(self.records)+1), 'kind': kind,
                         'time': datetime.now(timezone.utc).isoformat(), **deepcopy(data)})
        if self.path:
            persisted = dict(record)
            for key in {'question', 'system', 'message', 'result', 'page_updates', 'error', 'partial_content', 'content'}:
                if key in persisted:
                    persisted[key] = _redact_text(persisted[key], self.secrets)
            text = json.dumps(persisted, ensure_ascii=False)
            encoded = (text+'\n').encode('utf-8')
            try:
                with self.path.open('ab') as file:
                    file.write(encoded)
                    file.flush()
                    os.fsync(file.fileno())
            except OSError:
                self._storage_failed = True
                raise
            self._valid_bytes += len(encoded)
        self.records.append(record)
        self._apply(record)
        return record['id']

    def _apply(self, record):
        kind = record['kind']
        if kind not in RECORD_KINDS:
            raise ValueError('未知记录类型')
        if kind == 'session_created':
            self.id = record['session_id']
        elif kind == 'session_reset':
            self.messages, self.pages, self.runs = [], {}, {}
        elif kind == 'run_started':
            # 仅最近任务需要完整运行投影；旧任务的事实仍保留在 records 中。
            previous = self.last_run
            if previous is not None:
                previous.pop('messages', None)
                previous.pop('pages', None)
                previous.pop('pending', None)
                previous.pop('started_tools', None)
            messages = list(self.messages)
            system = {'role': 'system', 'content': record['system']}
            if messages:
                messages[0] = system
            else:
                messages = [system]
            messages.append({'role': 'user', 'content': record['question']})
            self.runs[record['run_id']] = {
                'id': record['run_id'], 'question': record['question'], 'config': record['config'],
                'status': 'running', 'messages': messages, 'pages': deepcopy(self.pages),
                'calls': 0, 'pending': {}, 'started_tools': set(), 'latest_usage': None}
        elif 'run_id' in record:
            run = self.runs[record['run_id']]
            if kind == 'run_resumed':
                run['status'] = 'running'
            elif kind == 'model_started':
                run['calls'] += 1
                run['latest_usage'] = None
                run['messages'][0] = {'role':'system','content':record['system']}
            elif kind == 'model_usage':
                run['latest_usage'] = record['usage']
            elif kind == 'model_completed':
                message = record['message']
                run['messages'].append(message)
                run['pending'] = {c['id']: c for c in message.get('tool_calls') or []}
            elif kind == 'tool_started':
                run['started_tools'].add(record['call_id'])
            elif kind == 'tool_completed':
                run['messages'].append({'role': 'tool', 'tool_call_id': record['call_id'], 'content': record['result']})
                run['pending'].pop(record['call_id'], None)
                run['started_tools'].discard(record['call_id'])
                for url, page in record['page_updates'].items():
                    if page is None:
                        run['pages'].pop(url, None)
                    else:
                        run['pages'][url] = page
            elif kind == 'run_ended':
                run['status'] = record['status']
                run['error'] = record.get('error')
                run['error_code'] = record.get('error_code')
                if record['status'] == 'completed':
                    self.messages, self.pages = run['messages'], run['pages']

    def usage_totals(self):
        """每次请求只取最后一次累计 usage，不把缺失报告当作零消耗。"""
        calls = {}
        for record in self.records:
            key = (record.get('run_id'), record.get('round'))
            if record['kind'] == 'model_started':
                calls[key] = None
            elif record['kind'] == 'model_usage':
                calls[key] = record['usage']
        return {'input_tokens': sum((u or {}).get('input_tokens') or 0 for u in calls.values()),
                'output_tokens': sum((u or {}).get('output_tokens') or 0 for u in calls.values()),
                'input_reports': sum(u is not None and u.get('input_tokens') is not None for u in calls.values()),
                'output_reports': sum(u is not None and u.get('output_tokens') is not None for u in calls.values()),
                'missing_calls': sum(not u or u.get('input_tokens') is None or u.get('output_tokens') is None for u in calls.values()),
                'calls': len(calls)}

    def clear(self):
        self.record('session_reset')

    def read_record(self, entry_id: str, start: int = 0, limit: int = 24000):
        if not isinstance(entry_id, str) or not entry_id.isdecimal():
            raise ValueError('记录编号必须是当前会话中的数字字符串')
        index = int(entry_id)-1
        if index < 0 or index >= len(self.records):
            raise ValueError('没有该记录')
        if type(start) is not int or type(limit) is not int or start < 0 or not 1 <= limit <= 24000:
            raise ValueError('读取范围不合法')
        text = json.dumps(self.records[index], ensure_ascii=False)
        if start >= len(text):
            raise ValueError('起点超出记录范围')
        end = min(start+limit, len(text))
        return {'operation': 'read_history', 'status': 'success' if start == 0 and end == len(text) else 'partial',
                'content_kind': 'session_record', 'record_id': entry_id, 'content': text[start:end],
                'start': start, 'end': end, 'total_characters': len(text),
                'next_start': end if end < len(text) else None}
