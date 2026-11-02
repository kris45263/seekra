"""Display projection of SDK v3 streams, never an execution history."""
import asyncio
from dataclasses import dataclass, field
from langchain_core.callbacks import AsyncCallbackHandler


@dataclass(frozen=True)
class Event:
    kind: str
    data: dict = field(default_factory=dict)
    namespace: tuple[str, ...] = ()


async def consume_stream(graph, payload, config, emit):
    # v3 message.node is also 'model' for summary calls. Use the SDK's
    # own callback metadata to distinguish internal prose from user-facing text.
    internal_runs = set()
    class Classification(AsyncCallbackHandler):
        async def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kwargs):
            if (metadata or {}).get('lc_source') == 'summarization':
                internal_runs.add(f'lc_run--{run_id}')
    config = {**config, 'callbacks': [*config.get('callbacks', []), Classification()]}
    async with await graph.astream_events(payload, config, version='v3') as stream:
        async def messages():
            async for message in stream.messages:
                namespace = tuple(message.namespace)
                internal = any((message.message_id or '').startswith(run) for run in internal_runs)
                async for text in message.text:
                    if not internal:
                        emit(Event('text', {'text': text, 'node': message.node}, namespace))
                output = await message
                emit(Event('model_end', {'id': output.id, 'usage': output.usage_metadata,
                                        'node': message.node, 'internal': internal}, namespace))

        async def tools():
            async for call in stream.tool_calls:
                emit(Event('tool_start', {'id': call.tool_call_id, 'name': call.tool_name,
                                         'input': call.input}))
                async for _ in call:
                    pass
                emit(Event('tool_end', {'id': call.tool_call_id, 'name': call.tool_name,
                                       'error': call.error}))

        async with asyncio.TaskGroup() as group:
            group.create_task(messages())
            group.create_task(tools())
        return await stream.output()
