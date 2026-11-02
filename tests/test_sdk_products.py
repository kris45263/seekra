"""Product and adapter contracts; no real credentials or service calls."""
import concurrent.futures
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from langchain_core.messages import AIMessage, AIMessageChunk
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from seekra.config import ModelProfile, load_settings
from seekra.sdk.models import create_model
from seekra.search.sources import Sources
from seekra.search.retrieval import valid_page_url


class ProductTests(unittest.TestCase):
    def test_atomic_idempotent_source_publication(self):
        with tempfile.TemporaryDirectory() as root:
            sources=Sources(Path(root))
            def publish(_): return sources.publish(url='https://example.com/x',content='conditions',kind='page')
            with concurrent.futures.ThreadPoolExecutor(4) as pool:
                refs=list(pool.map(publish,range(8)))
            self.assertEqual(len({r['id'] for r in refs}),1)
            self.assertEqual(len(sources.list()),1)
            changed=sources.publish(url='https://example.com/x',content='new conditions',kind='page')
            self.assertNotEqual(changed['id'],refs[0]['id'])
            (Path(root)/refs[0]['path'].lstrip('/')).write_text('tampered',encoding='utf-8')
            with self.assertRaises(OSError): publish(0)

    def test_urls_reject_credentials_and_local_forms(self):
        for url in ['file:///x','https://u:p@example.com','http://127.0.0.1/x','http://localhost',
                    'https://example.com\\@localhost','https://example.com:bad','https://example.com/a\nb']:
            self.assertFalse(valid_page_url(url),url)
        self.assertTrue(valid_page_url('https://docs.python.org/3.13/'))

    def test_profiles_use_existing_integrations_and_explicit_window(self):
        from litellm.litellm_core_utils.get_llm_provider_logic import get_llm_provider
        from langchain_core.tools import tool
        @tool
        def lookup(query: str) -> str:
            """Look up a source."""
            return query
        settings=load_settings(Path('seekra.example.toml'))
        for profile in settings.models.values():
            model=create_model(profile,{profile.api_key_env:'fake'})
            self.assertEqual(model.profile['max_input_tokens'],profile.max_input_tokens)
            self.assertEqual(model.model,f'{profile.provider}/{profile.model}')
            self.assertEqual(model.api_base,profile.base_url)
            routed,provider,key,base=get_llm_provider(model=model.model,api_base=model.api_base,api_key='fake')
            self.assertEqual((routed,provider,base),(profile.model,profile.provider,profile.base_url))
            self.assertEqual(key,'fake')
            self.assertEqual(model.bind_tools([lookup]).kwargs['tools'][0]['function']['name'],'lookup')
        with self.assertRaises(ValueError):
            ModelProfile('deepseek','litellm','m','KEY',64000,options={'api_key':'override'})
        with self.assertRaises(ValueError):
            ModelProfile('openai','openai','gpt','KEY',64000)

    def test_existing_litellm_reasoning_tool_roundtrip_and_checkpoint(self):
        from langchain_litellm.chat_models.litellm import (
            _convert_dict_to_message,_convert_delta_to_message_chunk,_convert_message_to_dict)
        # These private adapter functions deliberately pin an upstream compatibility seam, not production code.
        raw={'role':'assistant','content':'','reasoning_content':'condition before action',
             'tool_calls':[{'id':'c1','type':'function','function':{'name':'fetch_page','arguments':'{"url":"https://example.com"}'}}]}
        message=_convert_dict_to_message(raw)
        serde=JsonPlusSerializer()
        restored=serde.loads_typed(serde.dumps_typed(message))
        wire=_convert_message_to_dict(restored)
        self.assertEqual(wire['reasoning_content'],raw['reasoning_content'])
        self.assertEqual(wire['tool_calls'][0]['id'],'c1')
        chunk=_convert_delta_to_message_chunk({'role':'assistant','reasoning_content':'first '},AIMessageChunk)
        chunk+=_convert_delta_to_message_chunk({'reasoning_content':'second','content':'answer'},AIMessageChunk)
        from langchain_core.messages import message_chunk_to_message
        self.assertEqual(_convert_message_to_dict(message_chunk_to_message(chunk))['reasoning_content'],'first second')

    def test_empty_reasoning_warning_does_not_imply_lost_nonempty_content(self):
        from langchain_litellm.chat_models.litellm import (
            _convert_delta_to_message_chunk, _convert_message_to_dict)
        from langchain_core.messages import message_chunk_to_message
        from litellm.llms.deepseek.chat.transformation import DeepSeekChatConfig
        from litellm import verbose_logger
        serde=JsonPlusSerializer()
        for reasoning in ('', None, 'preserve these conditions'):
            with self.subTest(reasoning=reasoning):
                # Replay an actual shape: provider emits a tool call with an
                # empty reasoning delta; test the installed adapter and saver.
                chunk=_convert_delta_to_message_chunk({
                    'role':'assistant','content':'','reasoning_content':reasoning,
                    'tool_calls':[{'index':0,'id':'call-test','type':'function',
                                   'function':{'name':'lookup','arguments':'{"query":"rules"}'}}],
                },AIMessageChunk)
                message=message_chunk_to_message(chunk)
                message=serde.loads_typed(serde.dumps_typed(message))
                wire=_convert_message_to_dict(message)
                with patch.object(verbose_logger,'warning') as warning:
                    outgoing=DeepSeekChatConfig()._fill_reasoning_content([wire])[0]
                self.assertEqual(outgoing['tool_calls'][0]['id'],'call-test')
                if reasoning:
                    self.assertEqual(outgoing['reasoning_content'],reasoning)
                    warning.assert_not_called()
                else:
                    self.assertNotIn('reasoning_content',wire)
                    self.assertEqual(outgoing['reasoning_content'],' ')
                    warning.assert_called_once()
