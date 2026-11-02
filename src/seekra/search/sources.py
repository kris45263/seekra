"""Immutable source snapshots. A reference is published only after both files exist."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone
from filelock import FileLock
from seekra.sdk.workspace import atomic_json


class Sources:
    def __init__(self, workspace: Path):
        self.root = workspace / 'sources'
        self.root.mkdir(parents=True, exist_ok=True)

    def publish(self, *, url: str, content: str, kind: str, title: str = '', requested_url: str = '') -> dict:
        if kind not in {'snippet', 'page'} or not content.strip():
            raise ValueError('来源必须包含非空内容及明确的 snippet/page 类型')
        digest = hashlib.sha256(content.encode()).hexdigest()
        identity = json.dumps([url, requested_url, kind, digest], ensure_ascii=False)
        source_id = hashlib.sha256(identity.encode()).hexdigest()
        meta_path = self.root / f'{source_id}.json'
        body_path = self.root / f'{source_id}.md'
        with FileLock(str(self.root / '.publish.lock')):
            if meta_path.exists():
                metadata = json.loads(meta_path.read_text(encoding='utf-8'))
                if not body_path.exists() or hashlib.sha256(body_path.read_bytes()).hexdigest() != digest:
                    raise OSError('来源快照损坏，拒绝返回可用引用')
                return metadata
            # An interrupted publication may leave an unreferenced body; repair only exact content.
            if body_path.exists() and body_path.read_bytes() != content.encode():
                raise OSError('来源快照内容冲突')
            if not body_path.exists():
                fd, temporary = tempfile.mkstemp(dir=self.root, suffix='.tmp')
                try:
                    with os.fdopen(fd, 'wb') as handle:
                        handle.write(content.encode())
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, body_path)
                finally:
                    Path(temporary).unlink(missing_ok=True)
            metadata = dict(id=source_id, url=url, requested_url=requested_url or url,
                            title=title, kind=kind, sha256=digest,
                            retrieved_at=datetime.now(timezone.utc).isoformat(),
                            path=f'/sources/{source_id}.md')
            atomic_json(meta_path, metadata)
            return metadata

    def list(self):
        return [json.loads(path.read_text(encoding='utf-8')) for path in sorted(self.root.glob('*.json'))]
