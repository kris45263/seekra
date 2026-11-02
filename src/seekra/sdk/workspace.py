"""Native filesystem configuration for a Search workspace."""
from pathlib import Path
import json
import os
import tempfile
from deepagents import FilesystemPermission
from deepagents.backends import FilesystemBackend


def workspace_backend(root: Path):
    for folder in ('sources', 'notes', 'outputs', 'skills'):
        (root / folder).mkdir(parents=True, exist_ok=True)
    return FilesystemBackend(root_dir=root, virtual_mode=True)


def search_permissions():
    return [
        FilesystemPermission(operations=['write'], paths=['/notes/**', '/outputs/**'], mode='allow'),
        FilesystemPermission(operations=['write'], paths=['/**'], mode='deny'),
    ]


def atomic_json(path: Path, value: dict):
    """Publish shared storage metadata atomically, not a second agent state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
