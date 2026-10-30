"""Search Agent 的组装入口；循环由 Harness 提供。"""
import hashlib
from seekra.harness.context import AgentDefinition, PreparedRequest
from seekra.harness.loop import execute_run as execute_harness
from seekra.harness.runtime import CancellationToken
from .compaction import prepare_model_turn
from .prompt import SYSTEM_PROMPT, system_prompt
from .state import SearchSession
from .tools import tool_definitions, default_registry


def prepare_search_request(messages, definitions, session, max_chars, model, *, cancel):
    return PreparedRequest(*prepare_model_turn(
        messages, definitions, session, max_chars, model, cancel=cancel))


SEARCH_AGENT = AgentDefinition(hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
                               system_prompt, prepare_search_request)


def execute_run(question, *, model, registry, session, **options):
    """提供 Search 策略的嵌入入口；普通任务使用 harness.loop.execute_run。"""
    return execute_harness(question, model=model, registry=registry, session=session,
                           agent=SEARCH_AGENT, **options)


def run_search(question, *, model, client, tavily_api_key, session=None,
               structured_think=False, cancel=None, **options):
    """两个模型入口共用的 Search 组装；返回正文，执行失败保留原异常。"""
    cancel = cancel or CancellationToken()
    session = session if session is not None else SearchSession()
    session.secrets = tuple(dict.fromkeys((*session.secrets, tavily_api_key)))
    registry = default_registry(client, tavily_api_key, session, structured_think,
                                definitions=tool_definitions(structured_think), cancel=cancel)
    result = execute_run(question, model=model, registry=registry, session=session,
                         cancel=cancel, **options)
    if result.cause is not None:
        raise result.cause
    return result.answer


def run_agent(question, *, client, model, deepseek_api_key, tavily_api_key,
              session=None, structured_think=False, on_content=None, on_event=None,
              resume=False, cancel=None, max_context_chars=120000, thinking=True,
              reasoning_effort='high', max_output_tokens=8192):
    """兼容原有 Python 入口；新的嵌入调用可直接使用 execute_run 和 Model 接口。"""
    from seekra.providers.deepseek import DeepSeekModel
    session = session if session is not None else SearchSession()
    session.secrets = tuple(dict.fromkeys((*session.secrets, deepseek_api_key, tavily_api_key)))
    return run_search(question, model=DeepSeekModel(client, model, deepseek_api_key, thinking=thinking,
        reasoning_effort=reasoning_effort, max_output_tokens=max_output_tokens),
        client=client, tavily_api_key=tavily_api_key, structured_think=structured_think,
        session=session, resume=resume,
        cancel=cancel, on_content=on_content, on_event=on_event, max_context_chars=max_context_chars)


def run_chatgpt(question, *, client, tavily_api_key, auth, session, resume=False,
                cancel=None, on_content=None, on_event=None, max_context_chars=120000):
    from seekra.providers.chatgpt import ChatGPTModel
    def token():
        value = auth.access_token(client)
        session.secrets = tuple(dict.fromkeys((*session.secrets, value)))
        return value
    return run_search(question, model=ChatGPTModel(client, token),
        client=client, tavily_api_key=tavily_api_key, session=session,
        resume=resume, cancel=cancel, on_content=on_content, on_event=on_event,
        max_context_chars=max_context_chars)
