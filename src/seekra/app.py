"""One SDK invocation per product submission; no model loop or second history."""
from dataclasses import dataclass
from langchain_core.messages import AIMessage, HumanMessage
from seekra.config import require_secret
from seekra.sdk.models import create_model
from seekra.sdk.sessions import Sessions, runtime_fingerprint
from seekra.sdk.events import Event, consume_stream
from seekra.search.agent import create_search_agent
from seekra.search.retrieval import Retrieval


@dataclass(frozen=True)
class Result:
    status: str
    answer: str = ''


class Seekra:
    def __init__(self, settings, secrets, *, model_factory=create_model, retrieval_factory=Retrieval,
                 agent_factory=create_search_agent):
        self.settings, self.secrets = settings, secrets
        self.sessions = Sessions(settings.data_dir)
        self.model_factory, self.retrieval_factory = model_factory, retrieval_factory
        self.agent_factory = agent_factory

    def new_session(self, model_name=None):
        name = model_name or self.settings.default_model
        if name not in self.settings.models:
            raise ValueError(f'没有模型档案：{name}')
        return self.sessions.create(name, self.settings.models[name])

    def _profile(self, item):
        profile = self.settings.models.get(item['model'])
        if profile is None or runtime_fingerprint(profile) != item['fingerprint']:
            raise ValueError('该会话的模型/配置/SDK版本已变化；请使用原配置或新建会话')
        return profile

    async def run(self, item, question=None, *, emit=lambda event: None, callbacks=()):
        profile = self._profile(item)
        async with self.sessions.checkpoint(item) as saver:
            model = self.model_factory(profile, self.secrets)
            retrieval = self.retrieval_factory(require_secret(self.secrets, self.settings.tavily_key_env))
            graph = self.agent_factory(model, retrieval, self.sessions.workspace(item), saver)
            config = {'configurable': {'thread_id': item['id']}, 'callbacks': list(callbacks)}
            state = await graph.aget_state(config)
            if state.next and question is not None:
                raise ValueError('该会话有未完成任务；请 /continue 或 /new，避免覆盖待恢复状态')
            if question is None and not state.next:
                raise ValueError('没有需要继续的执行')
            if question is not None and not question.strip():
                raise ValueError('问题不能为空')
            item = self.sessions.touch(item, question)
            emit(Event('start', {'session_id': item['id']}))
            payload = {'messages': [HumanMessage(content=question)]} if question is not None else None
            await consume_stream(graph, payload, config, emit)
            state = await graph.aget_state(config)
            if state.next:
                return Result('interrupted')
            messages = state.values.get('messages', [])
            last = messages[-1] if messages else None
            if not isinstance(last, AIMessage) or last.tool_calls or last.invalid_tool_calls or not last.text:
                raise RuntimeError('SDK 执行结束但没有完整最终回答')
            if last.response_metadata.get('finish_reason') in {'length', 'max_tokens', 'content_filter'}:
                raise RuntimeError('模型输出被截断或过滤，不能作为完整最终回答')
            emit(Event('final', {'answer': last.text}))
            self.sessions.touch(item)
            return Result('completed', last.text)

    async def history(self, item):
        async with self.sessions.checkpoint(item) as saver:
            # Deep Agents may use delta-backed channels. Only the compiled graph
            # can reconstruct them; checkpoint.channel_values is not the state API.
            model = self.model_factory(self._profile(item), self.secrets)
            graph = self.agent_factory(model, None, self.sessions.workspace(item), saver)
            state = await graph.aget_state({'configurable': {'thread_id': item['id']}})
            return state.values.get('messages', [])
