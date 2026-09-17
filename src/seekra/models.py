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
    return {'input_tokens': input_tokens, 'output_tokens': output_tokens,
            'cache_read_tokens': cached,
            'cache_hit_rate': cached / input_tokens if input_tokens and cached is not None else None}


class DeepSeekModel:
    def __init__(self, client, model: str, api_key: str):
        self.client, self.name, self.api_key = client, model, api_key
        self.identity = f'deepseek:{model}'

    def generate(self, messages, tools, *, on_content, on_usage, cancel):
        try:
            result = stream_chat_completion(client=self.client, model=self.name, api_key=self.api_key,
                messages=messages, tools=tools, on_content=on_content, cancel=cancel,
                on_usage=lambda raw: on_usage(normalize_usage(raw)))
        except Exception:
            cancel.check()
            raise
        return ModelResponse(result['message'], result['finish_reason'])
