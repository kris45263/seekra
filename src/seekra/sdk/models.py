"""Construct existing LangChain integrations; no model wire protocol lives here."""
from langchain_core.language_models import BaseChatModel
from langchain_litellm import ChatLiteLLM

from seekra.config import ModelProfile, require_secret


def create_model(profile: ModelProfile, secrets: dict) -> BaseChatModel:
    key = require_secret(secrets, profile.api_key_env)
    model = ChatLiteLLM(
        model=f'{profile.provider}/{profile.model}', api_key=key,
        api_base=profile.base_url, max_tokens=profile.max_output_tokens,
        model_kwargs=profile.options, streaming=True, max_retries=0,
    )
    model.profile = {**(model.profile or {}), 'max_input_tokens': profile.max_input_tokens,
                     'tool_calling': True}
    return model
