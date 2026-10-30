"""DeepSeek 流式响应：正文立即通知，工具调用完整组装后才返回。"""

from collections.abc import Callable
from contextlib import nullcontext
import json

import httpx


from .sse import iter_sse_data, StreamProtocolError


def stream_chat_completion(
    *, client: httpx.Client, model: str, api_key: str,
    messages: list[dict], on_content: Callable[[str], None] | None = None,
    tools: list[dict] | None = None,
    on_usage: Callable[[dict], None] | None = None,
    cancel=None,
    thinking: bool = False,
    reasoning_effort: str = 'high',
    max_output_tokens: int = 8192,
) -> dict:
    """接收一轮完整 assistant 响应，不修改历史、不打印、不重试。

    回调同步执行并允许异常向上传播；任何退出路径都关闭当前响应。
    正文与工具分别累计；结束原因、结束标记及工具结构全部校验后返回。
    """
    if not messages or any(
        not isinstance(message, dict)
        or message.get("role") not in {"system", "user", "assistant", "tool"}
        or (not tools and (message.get("role") == "tool" or message.get("tool_calls")))
        or message.get("function_call")
        for message in messages
    ):
        raise ValueError("流式接口需要非空且与工具配置一致的消息历史。")
    if cancel is not None:
        cancel.check()
    parts = []
    usage = None
    reasoning = []
    calls = {}
    finished = None
    reported_model, fingerprint = None, None
    with client.stream(
        "POST", "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "messages": messages,
              "thinking": {"type": "enabled" if thinking else "disabled"},
              "max_tokens": max_output_tokens, "stream": True,
              **({"reasoning_effort": reasoning_effort} if thinking else {}),
              **({"tools": tools, **({} if thinking else {"tool_choice": "auto"})} if tools else {})},
        timeout=60,
    ) as response, (cancel.watch(response.close) if cancel is not None else nullcontext()):
        response.raise_for_status()
        media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media != "text/event-stream":
            raise StreamProtocolError("DeepSeek 未返回 SSE 流。")
        response.encoding = "utf-8"
        for data in iter_sse_data(response):
            if cancel is not None:
                cancel.check()
            if data == "[DONE]":
                answer = "".join(parts)
                if finished == 'tool_calls' and calls:
                    ordered = [calls[k] for k in sorted(calls)]
                    if sorted(calls) != list(range(len(calls))) or any(
                        not c.get('id') or c.get('type') != 'function'
                        or not c['function'].get('name') or 'arguments' not in c['function']
                        for c in ordered
                    ) or len({c['id'] for c in ordered}) != len(ordered):
                        raise StreamProtocolError('DeepSeek 工具调用结构不完整。')
                elif finished == 'stop' and not calls and answer.strip():
                    ordered = []
                else:
                    raise StreamProtocolError("DeepSeek 缺少正常结束原因或返回空答案。")
                message = {'role': 'assistant', 'content': answer or None}
                if ordered:
                    message['tool_calls'] = ordered
                if reasoning and tools:
                    message['reasoning_content'] = ''.join(reasoning)
                return {'message': message, 'finish_reason': finished, 'usage': usage,
                        'response_model': reported_model, 'system_fingerprint': fingerprint}
            try:
                chunk = json.loads(data)
            except ValueError:
                raise StreamProtocolError("DeepSeek 流事件不是合法 JSON。") from None
            if not isinstance(chunk, dict) or "error" in chunk:
                raise StreamProtocolError("DeepSeek 返回异常流事件。")
            if isinstance(chunk.get('model'), str):
                reported_model = chunk['model']
            if isinstance(chunk.get('system_fingerprint'), str):
                fingerprint = chunk['system_fingerprint']
            if isinstance(chunk.get('usage'), dict):
                usage = chunk['usage']
                if on_usage is not None:
                    on_usage(usage)
            choices = chunk.get("choices")
            if choices == [] and isinstance(chunk.get("usage"), dict):
                continue
            if not isinstance(choices, list) or len(choices) != 1:
                raise StreamProtocolError("DeepSeek 流 choice 格式异常。")
            choice = choices[0]
            if (not isinstance(choice, dict) or type(choice.get("index")) is not int
                    or choice["index"] != 0):
                raise StreamProtocolError("DeepSeek 流 choice 编号异常。")
            delta = choice.get("delta")
            if not isinstance(delta, dict) or delta.get("role") not in (None, "assistant"):
                raise StreamProtocolError("DeepSeek 流消息格式异常。")
            if (delta.get("tool_calls") and not tools) or delta.get("function_call"):
                raise StreamProtocolError("当前流式正文接口不支持工具调用。")
            fragments = delta.get('tool_calls')
            if fragments is not None and not isinstance(fragments, list):
                raise StreamProtocolError('DeepSeek 工具片段格式异常。')
            if finished and (fragments or delta.get('reasoning_content')):
                raise StreamProtocolError('DeepSeek 在结束后继续发送消息。')
            for fragment in fragments or []:
                if not isinstance(fragment, dict) or type(fragment.get('index')) is not int or fragment['index'] < 0:
                    raise StreamProtocolError('DeepSeek 工具编号异常。')
                call = calls.setdefault(fragment['index'], {'function': {}})
                function = fragment.get('function') or {}
                if not isinstance(function, dict):
                    raise StreamProtocolError('DeepSeek 工具函数格式异常。')
                for target, source, key in ((call, fragment, 'id'), (call, fragment, 'type'),
                                             (call['function'], function, 'name')):
                    value = source.get(key)
                    if value is not None:
                        if not isinstance(value, str) or (key in target and target[key] != value):
                            raise StreamProtocolError('DeepSeek 工具身份字段冲突。')
                        target[key] = value
                arguments = function.get('arguments')
                if arguments is not None:
                    if not isinstance(arguments, str):
                        raise StreamProtocolError('DeepSeek 工具参数片段类型异常。')
                    call['function']['arguments'] = call['function'].get('arguments', '') + arguments
            thought = delta.get('reasoning_content')
            if thought is not None:
                if not isinstance(thought, str):
                    raise StreamProtocolError('DeepSeek 推理字段类型异常。')
                reasoning.append(thought)
            content = delta.get("content")
            if content is not None and not isinstance(content, str):
                raise StreamProtocolError("DeepSeek 流正文类型异常。")
            finish = choice.get("finish_reason")
            if finished and (content or finish is not None):
                raise StreamProtocolError("DeepSeek 在结束后继续发送正文或结束原因。")
            if content:
                parts.append(content)
                if on_content is not None:
                    on_content(content)
            if finish is not None:
                if finish not in ({'stop', 'tool_calls'} if tools else {'stop'}):
                    raise StreamProtocolError("DeepSeek 输出未正常完成（可能已截断）。")
                finished = finish
    raise StreamProtocolError("DeepSeek 流已断开，缺少结束标记。")


def stream_chat_text(*, client: httpx.Client, model: str, api_key: str,
                     messages: list[dict], on_content: Callable[[str], None] | None = None) -> str:
    """无工具开发验收的薄包装，共用主 Agent 的接收器。"""
    return stream_chat_completion(client=client, model=model, api_key=api_key,
                                  messages=messages, on_content=on_content)['message']['content']
