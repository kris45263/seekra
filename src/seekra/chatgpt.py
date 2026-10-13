"""ChatGPT 订阅授权的 Responses 适配器，复用 Seekra 执行循环。"""
from copy import deepcopy
import json

from .deepseek import _sse_data, StreamProtocolError
from .models import normalize_usage
from .runtime import ModelResponse


class ChatGPTModel:
    def __init__(self, client, access_token, *, model='gpt-6.1-sol', reasoning_effort='medium'):
        self.client, self.access_token = client, access_token
        self.name, self.reasoning_effort = model, reasoning_effort
        self.thinking = True
        self.identity = f'chatgpt-plan:{model}:effort={reasoning_effort}:responses-v1'

    def project_messages(self, messages, session):
        projected, missing = [], set()
        for index, message in enumerate(messages):
            if message['role'] != 'assistant':
                projected.append(message)
                continue
            record_id = session._message_records.get(id(message))
            state = session._provider_states.get(record_id)
            if state and state.get('kind') != 'openai_responses':
                state = None
            if record_id in session._continuation_required and state is None:
                missing.add(index)
            # 复用供应商续接字段的预算与脱敏路径；只含不透明密文，不含推理全文。
            projected.append({**message, 'reasoning_content': json.dumps((state or {}).get('items', []))})
        return projected, missing

    def request_content(self, messages, tools):
        instructions, inputs = [], []
        for message in messages:
            role = message['role']
            if role == 'system':
                instructions.append(message['content'])
            elif role == 'tool':
                inputs.append({'type':'function_call_output', 'call_id':message['tool_call_id'],
                               'output':message['content']})
            elif role in {'user', 'assistant'}:
                if role == 'assistant':
                    inputs.extend(json.loads(message.get('reasoning_content') or '[]'))
                if message.get('content'):
                    inputs.append({'role':role, 'content':message['content']})
                for call in message.get('tool_calls') or []:
                    inputs.append({'type':'function_call', 'call_id':call['id'], 'namespace':'seekra',
                                   'name':call['function']['name'], 'arguments':call['function']['arguments']})
            else:
                raise ValueError('不支持的模型消息角色')
        definitions = [{'type':'function', **deepcopy(t['function']), 'strict':False} for t in tools]
        return {'instructions':'\n'.join(instructions), 'input':inputs,
                'tools':[{'type':'namespace','name':'seekra','description':'Seekra research tools',
                          'tools':definitions}] if definitions else []}

    def generate(self, messages, tools, *, on_content, on_usage, cancel):
        cancel.check()
        token = self.access_token()
        payload = {**self.request_content(messages, tools), 'model':self.name,
                   'reasoning':{'effort':self.reasoning_effort}, 'store':False, 'stream':True,
                   'include':['reasoning.encrypted_content']}
        emitted = []
        completed_items, added_items = {}, set()
        with self.client.stream('POST', 'https://api.openai.com/v1/responses',
                headers={'Authorization':f'Bearer {token}', 'Accept':'text/event-stream'}, json=payload, timeout=60) as response, \
                cancel.watch(response.close):
            response.raise_for_status()
            media_type = response.headers.get('content-type', '').split(';')[0].strip().lower()
            # 已观察到订阅响应缺少此头；完整事件校验仍是成功条件。
            if media_type and media_type != 'text/event-stream':
                raise StreamProtocolError('OpenAI 未返回 SSE 流。')
            response.encoding = 'utf-8'
            for data in _sse_data(response):
                cancel.check()
                try:
                    event = json.loads(data)
                except ValueError:
                    raise StreamProtocolError('OpenAI 流事件不是有效 JSON。') from None
                if not isinstance(event, dict):
                    raise StreamProtocolError('OpenAI 流事件格式异常。')
                kind = event.get('type')
                if kind in {'response.output_item.added', 'response.output_item.done'}:
                    index = event.get('output_index')
                    if type(index) is not int or index < 0 or not isinstance(event.get('item'), dict):
                        raise StreamProtocolError('OpenAI 输出项事件格式异常。')
                    if kind == 'response.output_item.added':
                        if index in added_items:
                            raise StreamProtocolError('OpenAI 输出项编号重复。')
                        added_items.add(index)
                    else:
                        if index in completed_items:
                            raise StreamProtocolError('OpenAI 输出项完成事件重复。')
                        completed_items[index] = event['item']
                if kind in {'response.output_text.delta', 'response.refusal.delta'}:
                    text = event.get('delta')
                    if not isinstance(text, str):
                        raise StreamProtocolError('OpenAI 正文片段格式异常。')
                    emitted.append(text)
                    on_content(text)
                result = event.get('response') or {}
                if isinstance(result, dict) and isinstance(result.get('usage'), dict):
                    raw = result['usage']
                    on_usage(normalize_usage({'prompt_tokens':raw.get('input_tokens'),
                        'completion_tokens':raw.get('output_tokens'),
                        'prompt_tokens_details':raw.get('input_tokens_details'),
                        'completion_tokens_details':raw.get('output_tokens_details')}))
                if kind in {'response.failed', 'response.incomplete', 'error'}:
                    error = result.get('error') or event.get('error') or event
                    code = error.get('code') if isinstance(error, dict) else None
                    if code in {'subscription_sharing_usage_limit_exceeded', 'subscription_sharing_usage_unavailable'}:
                        raise RuntimeError('ChatGPT 订阅额度不足或当前不可用；请在 ChatGPT 设置中检查应用用量。')
                    raise StreamProtocolError('OpenAI 未完成本次响应；未将残稿作为答案或执行工具。')
                if kind == 'response.completed':
                    if isinstance(result, dict) and not result.get('output') and completed_items:
                        indices = set(completed_items)
                        if (indices != set(range(len(completed_items))) or
                                (added_items and indices != added_items)):
                            raise StreamProtocolError('OpenAI 输出项未完整结束。')
                        result = {**result, 'output':[completed_items[i] for i in range(len(completed_items))]}
                    return self._completed(result, emitted, on_content)
        raise StreamProtocolError('OpenAI 流缺少 response.completed，研究未完成。')

    def _completed(self, response, emitted, on_content):
        if not isinstance(response, dict) or response.get('status') != 'completed' or not isinstance(response.get('output'), list):
            raise StreamProtocolError('OpenAI 完成事件格式异常。')
        text, calls, reasoning = [], [], []
        for item in response['output']:
            if not isinstance(item, dict):
                raise StreamProtocolError('OpenAI 输出项格式异常。')
            kind = item.get('type')
            if kind == 'message':
                if (item.get('role') != 'assistant' or not isinstance(item.get('content'), list)
                        or item.get('status', 'completed') != 'completed'):
                    raise StreamProtocolError('OpenAI 消息格式异常。')
                for part in item['content']:
                    if not isinstance(part, dict):
                        raise StreamProtocolError('OpenAI 正文格式异常。')
                    value = part.get('text') if part.get('type') == 'output_text' else part.get('refusal')
                    if not isinstance(value, str):
                        raise StreamProtocolError('OpenAI 正文格式异常。')
                    text.append(value)
            elif kind == 'function_call':
                if (item.get('namespace') not in {None, 'seekra'} or
                    item.get('status', 'completed') != 'completed' or
                    any(not isinstance(item.get(k), str) or not item[k] for k in ('call_id','name','arguments'))):
                    raise StreamProtocolError('OpenAI 工具调用不完整或命名空间不匹配。')
                calls.append({'id':item['call_id'], 'type':'function',
                              'function':{'name':item['name'], 'arguments':item['arguments']}})
            elif kind == 'reasoning':
                if item.get('encrypted_content'):
                    if not isinstance(item['encrypted_content'], str):
                        raise StreamProtocolError('OpenAI 推理续接数据格式异常。')
                    reasoning.append({k:deepcopy(item[k]) for k in ('type','id','encrypted_content') if k in item} | {'summary':[]})
            else:
                raise StreamProtocolError('OpenAI 返回了未注册的输出类型。')
        answer = ''.join(text)
        if emitted and ''.join(emitted) != answer:
            raise StreamProtocolError('OpenAI 完整正文与流式正文不一致。')
        if len({c['id'] for c in calls}) != len(calls) or (not calls and not answer.strip()):
            raise StreamProtocolError('OpenAI 返回重复调用或空答案。')
        if not emitted and answer:
            on_content(answer)
        message = {'role':'assistant', 'content':answer or None}
        if calls:
            message['tool_calls'] = calls
        return ModelResponse(message, 'tool_calls' if calls else 'stop',
            {'kind':'openai_responses','items':reasoning} if reasoning else None,
            {'configured_model':self.name,'response_model':response.get('model'),
             'reasoning_effort':self.reasoning_effort,'auth_mode':'chatgpt_plan'})


def run_chatgpt(question, *, client, tavily_api_key, auth, session, resume=False,
                cancel=None, on_content=None, on_event=None, max_context_chars=120000):
    from .agent import execute_run
    from .runtime import CancellationToken
    from .tools import default_registry
    cancel = cancel or CancellationToken()
    session.secrets = tuple(set(session.secrets + (tavily_api_key,)))
    def token():
        value = auth.access_token(client)
        session.secrets = tuple(set(session.secrets + (value,)))
        return value
    result = execute_run(question, model=ChatGPTModel(client, token),
        registry=default_registry(client, tavily_api_key, session, cancel=cancel), session=session,
        resume=resume, cancel=cancel, on_content=on_content, on_event=on_event,
        max_context_chars=max_context_chars)
    if result.cause is not None:
        raise result.cause
    return result.answer
