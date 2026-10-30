"""请求准备契约与无业务依赖的基础策略。"""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from typing import Callable


class ContextLimit(RuntimeError):
    def __init__(self, message, *, code='context_limit'):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PreparedRequest:
    messages: list[dict]
    tools: list[dict]
    selection: dict
    system: str


def prepare_request(messages, definitions, session, max_chars, model, *, cancel):
    """保留完整交互；不够容纳或缺少续接时暂停，不擅自丢弃业务状态。"""
    if type(max_chars) is not int or max_chars < 2000:
        raise ValueError('上下文字符上限至少为 2000')
    cancel.check()
    project = getattr(model, 'project_messages', lambda rows, states: (rows, set()))
    projected, missing = project(messages, session.model_continuations(messages))
    if missing:
        raise ContextLimit('缺少供应商续接；当前请求策略不提供业务恢复。', code='missing_continuation')
    request = getattr(model, 'request_content', lambda rows, tools: {'messages': rows, 'tools': tools})
    encoded = json.dumps(request(projected, definitions), ensure_ascii=False)
    if len(encoded) > max_chars:
        raise ContextLimit('完整请求超过上下文字符上限；当前策略不裁剪工具结果。')
    return PreparedRequest(deepcopy(projected), deepcopy(definitions), {
        'message_indices': list(range(len(messages))),
        'request_characters': len(encoded),
        'request_sha256': hashlib.sha256(encoded.encode()).hexdigest(),
    }, messages[0]['content'])


@dataclass(frozen=True)
class AgentDefinition:
    """运行的稳定身份与策略；不绑定供应商、会话或产品界面。"""
    prompt_identity: str
    system_prompt: Callable[[], str]
    prepare_request: Callable = prepare_request

    @classmethod
    def from_prompt(cls, prompt: str):
        return cls(hashlib.sha256(prompt.encode()).hexdigest(), lambda: prompt)
