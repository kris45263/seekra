"""Light product catalog; SQLite checkpoints own agent state."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import uuid
from filelock import FileLock, Timeout
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from seekra.sdk.workspace import atomic_json


def runtime_fingerprint(profile):
    dependencies = '|'.join(version(name) for name in (
        'deepagents', 'langchain-core', 'langchain-litellm', 'litellm', 'langgraph'))
    return hashlib.sha256(f'seekra-search-v2-1|{dependencies}|{profile.fingerprint()}'.encode()).hexdigest()


class Sessions:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.catalog = self.root / 'sessions'
        self.catalog.mkdir(parents=True, exist_ok=True)

    def create(self, model_name, profile):
        item = dict(id=uuid.uuid4().hex, model=model_name, fingerprint=runtime_fingerprint(profile),
                    capability='search', title='', updated_at=datetime.now(timezone.utc).isoformat())
        atomic_json(self.catalog / f'{item["id"]}.json', item)
        return item

    def list(self):
        return sorted((json.loads(p.read_text(encoding='utf-8')) for p in self.catalog.glob('*.json')),
                      key=lambda x: x['updated_at'], reverse=True)

    def get(self, prefix):
        if not isinstance(prefix, str) or len(prefix) < 8 or any(c not in '0123456789abcdef' for c in prefix):
            raise ValueError('请输入至少 8 位的新会话 ID；旧 JSONL 会话不能原地续跑')
        matches = [item for item in self.list() if item['id'].startswith(prefix)]
        if len(matches) != 1:
            raise ValueError('会话不存在或前缀不唯一；旧 JSONL 不会自动迁移')
        return matches[0]

    def workspace(self, item):
        return self.root / 'workspaces' / item['id']

    def touch(self, item, question=None):
        # Callers may retain the original catalog object over multiple turns.
        current = json.loads((self.catalog / f'{item["id"]}.json').read_text(encoding='utf-8'))
        item = {**item, 'title': current['title']}
        item = {**item, 'updated_at': datetime.now(timezone.utc).isoformat()}
        if question and not item['title']:
            item['title'] = question[:80]
        atomic_json(self.catalog / f'{item["id"]}.json', item)
        return item

    @asynccontextmanager
    async def checkpoint(self, item):
        lock = FileLock(str(self.catalog / f'{item["id"]}.lock'))
        try:
            lock.acquire(timeout=0)
        except Timeout:
            raise ValueError('该会话正在运行，请等待结束或新建会话') from None
        try:
            async with AsyncSqliteSaver.from_conn_string(str(self.root / 'checkpoints.sqlite')) as saver:
                yield saver
        finally:
            lock.release()
