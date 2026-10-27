"""请求预算准备：先外置旧正文，不改变研究工具权限。"""
from copy import deepcopy
from .context import build_context


def present_actions(payload, stage=None):
    """旧记录中的阶段标记不再代表当前权限；原始执行记录保持不变。"""
    result = deepcopy(payload)
    def visit(value):
        if isinstance(value, list):
            for item in value: visit(item)
        elif isinstance(value, dict):
            if isinstance(value.get('tool'), str) and isinstance(value.get('arguments'), dict):
                value.pop('availability', None)
            for item in value.values(): visit(item)
    visit(result)
    return result


def prepare_context(messages, definitions, session, max_chars, *, message_start=None,
                    extra_sources=(), recovery=None, allow_overflow=False):
    threshold = max_chars * 4 // 5
    selected, info = build_context(messages, definitions, session, max_chars,
        offload_target=threshold-1, message_start=message_start, extra_sources=extra_sources,
        payload_transform=present_actions, allow_overflow=allow_overflow)
    info.update(progress_stage=None, checkpoint_requested=False,
                checkpoint_threshold=threshold, request_tools=deepcopy(definitions))
    return selected, definitions, info, selected[0]['content']
