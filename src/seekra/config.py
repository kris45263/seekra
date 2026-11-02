"""Named model profiles and local settings; credentials never enter checkpoints."""
from dataclasses import dataclass, field, asdict
import hashlib
import json
import os
from pathlib import Path
import tomllib

from dotenv import dotenv_values


@dataclass(frozen=True)
class ModelProfile:
    provider: str
    adapter: str
    model: str
    api_key_env: str
    max_input_tokens: int
    base_url: str | None = None
    max_output_tokens: int = 8192
    options: dict = field(default_factory=dict)

    def __post_init__(self):
        if (self.adapter, self.provider) not in {
            ('litellm', 'deepseek'), ('litellm', 'moonshot'),
            ('litellm', 'zai'),
        }:
            raise ValueError('不支持的 provider / adapter 组合')
        if not self.model or not self.api_key_env:
            raise ValueError('model 和 api_key_env 不能为空')
        if self.max_input_tokens < 4096 or self.max_output_tokens < 1:
            raise ValueError('必须配置有效的输入窗口与输出 token 上限')
        # Options are provider parameters, not a backdoor to change endpoint/identity.
        allowed = {'thinking', 'reasoning_effort', 'temperature', 'top_p', 'extra_body'}
        if set(self.options) - allowed:
            raise ValueError(f'未知模型选项：{sorted(set(self.options) - allowed)}')

    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Settings:
    models: dict[str, ModelProfile]
    default_model: str
    data_dir: Path = Path('.seekra/v2')
    tavily_key_env: str = 'TAVILY_API_KEY'


def load_settings(path: Path) -> Settings:
    with path.open('rb') as handle:
        data = tomllib.load(handle)
    app = data.get('app', {})
    if app.get('capability', 'search') != 'search':
        raise ValueError('当前版本只启用 search 能力')
    models = {name: ModelProfile(**values) for name, values in data.get('models', {}).items()}
    default = app.get('default_model', '')
    if default not in models:
        raise ValueError('app.default_model 必须指向一个模型档案')
    root = Path(app.get('data_dir', '.seekra/v2'))
    if not root.is_absolute():
        root = path.resolve().parent / root
    return Settings(models, default, root, data.get('search', {}).get('api_key_env', 'TAVILY_API_KEY'))


def load_secrets(path: Path) -> dict[str, str]:
    """Explicit env-file takes precedence; absent names may use process environment."""
    values = dict(os.environ)
    if path.is_file():
        values.update(dotenv_values(path, interpolate=False, encoding='utf-8-sig'))
    return {key: value for key, value in values.items() if isinstance(value, str)}


def require_secret(values: dict, name: str) -> str:
    value = values.get(name, '').strip()
    if not value or '${' in value:
        raise ValueError(f'请在指定 .env 或进程环境中填写 {name}')
    return value
