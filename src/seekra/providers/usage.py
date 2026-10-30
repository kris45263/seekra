"""供应商 token 报告的归一化；缺失值不当作零。"""
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
