"""Search composition; execution and context management belong to Deep Agents."""
from deepagents import create_deep_agent
from deepagents.profiles import HarnessProfile, GeneralPurposeSubagentProfile, register_harness_profile
from seekra.sdk.workspace import workspace_backend, search_permissions
from seekra.search.prompt import SEARCH_PROMPT
from seekra.search.sources import Sources
from seekra.search.tools import search_tools


def create_search_agent(model, retrieval, workspace, checkpointer, *, middleware=()):
    provider = model._get_ls_params()['ls_provider']
    identifier = getattr(model, 'model_name', None) or getattr(model, 'model', None)
    key = f'{provider}:{identifier}' if identifier else provider
    register_harness_profile(key, HarnessProfile(
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        excluded_tools=frozenset({'execute', 'task'}),
    ))
    return create_deep_agent(model, tools=search_tools(retrieval, Sources(workspace)),
                             system_prompt=SEARCH_PROMPT, backend=workspace_backend(workspace),
                             permissions=search_permissions(), checkpointer=checkpointer,
                             skills=['/skills/'], middleware=middleware, name='seekra-search')
