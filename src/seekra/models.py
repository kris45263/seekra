"""DeepSeek 适配器：把 provider 协议转换成核心响应和可确认的用量。"""
from .deepseek import stream_chat_completion
from .runtime import ModelResponse


def normalize_usage(raw: dict) -> dict:
    def number(name):
        value = raw.get(name)
        return value if type(value) is int and value >= 0 else None
    input_tokens, output_tokens = number('prompt_tokens'), number('completion_tokens')
    cached = number('prompt_cache_hit_tokens')
    if cached is None:
        details = raw.get('prompt_tokens_details') or {}
        value = details.get('cached_tokens') if isinstance(details, dict) else None
        cached = value if type(value) is int and value >= 0 else None
    if input_tokens is not None and cached is not None and cached > input_tokens:
        cached = None
    result = {'input_tokens': input_tokens, 'output_tokens': output_tokens,
            'cache_read_tokens': cached,
            'cache_hit_rate': cached / input_tokens if input_tokens and cached is not None else None}
    details = raw.get('completion_tokens_details')
    if isinstance(details, dict) and 'reasoning_tokens' in details:
        value = details['reasoning_tokens']
        result['reasoning_tokens'] = (value if type(value) is int and value >= 0
                                     and (output_tokens is None or value <= output_tokens) else None)
    return result


class DeepSeekModel:
    def __init__(self, client, model: str, api_key: str, *, thinking=True,
                 reasoning_effort='high', max_output_tokens=8192):
        if type(thinking) is not bool or reasoning_effort not in {'low', 'high', 'max'}:
            raise ValueError('thinking 需要布尔值，reasoning_effort 需要 low/high/max')
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 393216:
            raise ValueError('模型输出预算需要 1–393216 个 token')
        self.client, self.name, self.api_key = client, model, api_key
        self.thinking, self.reasoning_effort = thinking, reasoning_effort
        self.max_output_tokens = max_output_tokens
        self.identity = f'deepseek:{model}'
        if thinking or max_output_tokens != 8192:
            self.identity += f':thinking={thinking}:effort={reasoning_effort}:output={max_output_tokens}'

    @property
    def can_rebuild_context(self):
        return self.thinking

    def request_content(self, messages, tools):
        return {'messages': messages, 'tools': tools}

    def project_messages(self, messages, continuations):
        """在预算选择前附加协议字段；缺失数据由执行层明确重建上下文。"""
        if not self.thinking:
            return messages, set()
        projected, missing = [], set()
        for i, (message, continuation) in enumerate(zip(messages, continuations, strict=True)):
            if message['role'] != 'assistant':
                projected.append(message)
                continue
            state = continuation.value
            if continuation.required and state is None:
                missing.add(i)
            projected.append({**message, 'content': message.get('content') or '',
                              'reasoning_content': (state or {}).get('reasoning_content', '')})
        return projected, missing

    def generate(self, messages, tools, *, on_content, on_usage, cancel):
        try:
            result = stream_chat_completion(client=self.client, model=self.name, api_key=self.api_key,
                messages=messages, tools=tools, on_content=on_content, cancel=cancel,
                thinking=self.thinking, reasoning_effort=self.reasoning_effort,
                max_output_tokens=self.max_output_tokens,
                on_usage=lambda raw: on_usage(normalize_usage(raw)))
        except Exception:
            cancel.check()
            raise
        message = result['message']
        reasoning = message.pop('reasoning_content', '')
        continuation = {'reasoning_content': reasoning} if self.thinking and tools else None
        provider = {'configured_model': self.name, 'response_model': result.get('response_model'),
                    'system_fingerprint': result.get('system_fingerprint'), 'thinking': self.thinking,
                    'reasoning_effort': self.reasoning_effort if self.thinking else None,
                    'max_output_tokens': self.max_output_tokens, 'reasoning_characters': len(reasoning)}
        return ModelResponse(message, result['finish_reason'], continuation, provider)
