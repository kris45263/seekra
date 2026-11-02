"""Deterministic SDK integration, not evidence of real model answer quality."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from typing import Any
from pydantic import Field
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatResult, ChatGeneration
from langgraph.checkpoint.memory import InMemorySaver
from seekra.app import Seekra
from seekra.config import Settings, ModelProfile
from seekra.search.agent import create_search_agent
from seekra.search.sources import Sources
from seekra.sdk.events import consume_stream


class ScriptModel(BaseChatModel):
    replies: list[Any] = Field(default_factory=list)
    seen: list[Any] = Field(default_factory=list)
    tool_names: list[str] = Field(default_factory=list)
    @property
    def _llm_type(self): return 'scripted'
    def bind_tools(self, tools, **kwargs):
        self.tool_names = [t.name for t in tools]
        return self
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(messages)
        response = self.replies.pop(0)
        if isinstance(response, Exception): raise response
        if callable(response): response = response(messages)
        return ChatResult(generations=[ChatGeneration(message=response)])


class FixtureRetrieval:
    async def fetch(self, url):
        return {'results':[{'url':url, 'raw_content':'Pro license required. Shared guest accounts cannot use offline mode.'}]}
    async def search(self, query, **kwargs):
        return {'results':[{'url':'https://example.com/guide','content':'Pro license required.'}]}


def call(name, args, id='t1'):
    return AIMessage(content='', tool_calls=[{'name':name,'args':args,'id':id}])


def profile():
    return ModelProfile('deepseek','litellm','test','KEY',64000)


def app_at(root, model):
    return Seekra(Settings({'test':profile()},'test',Path(root)), {'TAVILY_API_KEY':'fake'},
                  model_factory=lambda *a:model, retrieval_factory=lambda *a:FixtureRetrieval())


class SDKTests(unittest.IsolatedAsyncioTestCase):
    async def test_truncated_text_is_not_announced_as_final(self):
        with tempfile.TemporaryDirectory() as root:
            model=ScriptModel(replies=[AIMessage(content='partial',response_metadata={'finish_reason':'length'})],
                              profile={'max_input_tokens':64000})
            app=app_at(root,model)
            events=[]
            with self.assertRaisesRegex(RuntimeError,'截断'):
                await app.run(app.new_session(),'question',emit=events.append)
            self.assertFalse(any(e.kind=='final' for e in events))

    async def test_fetch_read_only_sources_and_restart(self):
        with tempfile.TemporaryDirectory() as root:
            def read_saved(messages):
                payload = json.loads(messages[-1].content)
                return call('read_file', {'file_path':payload['sources'][0]['path']}, 't2')
            model = ScriptModel(replies=[call('fetch_page',{'url':'https://example.com/guide'}),
                                        read_saved, AIMessage(content='条件仍在')],
                                profile={'max_input_tokens':64000})
            app = app_at(root,model)
            item = app.new_session()
            events=[]
            self.assertEqual((await app.run(item,'读取条件',emit=events.append)).answer,'条件仍在')
            self.assertIn('Shared guest', model.seen[-1][-1].content)
            self.assertNotIn('execute',model.tool_names)
            self.assertNotIn('task',model.tool_names)
            self.assertEqual(len([e for e in events if e.kind=='tool_start']),2)
            saved = Sources(app.sessions.workspace(item)).list()[0]
            second = ScriptModel(replies=[call('edit_file',{'file_path':saved['path'],
                                 'old_string':'Pro','new_string':'Free'}), AIMessage(content='不能改写')],
                                 profile={'max_input_tokens':64000})
            reopened=app_at(root,second)
            await reopened.run(reopened.sessions.get(item['id']),'改写来源')
            self.assertEqual(reopened.sessions.get(item['id'])['title'],'读取条件')
            self.assertTrue(any(isinstance(m,HumanMessage) and m.content=='读取条件' for m in second.seen[0]))
            self.assertIn('denied',str(second.seen[-1][-1].content).lower())
            body=app.sessions.workspace(item)/saved['path'].lstrip('/')
            self.assertTrue(body.read_text().startswith('Pro'))

    async def test_failed_model_resume_and_configuration_guard(self):
        with tempfile.TemporaryDirectory() as root:
            app=app_at(root,ScriptModel(replies=[RuntimeError('connection lost')],profile={'max_input_tokens':64000}))
            item=app.new_session()
            with self.assertRaises(Exception): await app.run(item,'question')
            restored=app_at(root,ScriptModel(replies=[AIMessage(content='resumed')],profile={'max_input_tokens':64000}))
            with self.assertRaisesRegex(ValueError,'未完成'): await restored.run(item,'overwrite')
            self.assertEqual((await restored.run(item)).answer,'resumed')
            with self.assertRaisesRegex(ValueError,'没有需要'): await restored.run(item)
            with self.assertRaisesRegex(ValueError,'已变化'):
                await restored.run({**item,'fingerprint':'changed'},'new')

    async def test_cancellation_propagates_to_running_tool_and_resumes(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        class Blocking(FixtureRetrieval):
            async def fetch(self,url):
                entered.set()
                try: await asyncio.Event().wait()
                finally: cancelled.set()
        with tempfile.TemporaryDirectory() as root:
            model=ScriptModel(replies=[call('fetch_page',{'url':'https://example.com/guide'})],profile={'max_input_tokens':64000})
            app=app_at(root,model)
            app.retrieval_factory=lambda *a:Blocking()
            item=app.new_session()
            task=asyncio.create_task(app.run(item,'fetch'))
            await asyncio.wait_for(entered.wait(),5)
            with self.assertRaisesRegex(ValueError,'正在运行'):
                await app.run(item,'concurrent')
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertTrue(cancelled.is_set())
            restored=app_at(root,ScriptModel(replies=[AIMessage(content='continued')],profile={'max_input_tokens':64000}))
            self.assertEqual((await restored.run(item)).answer,'continued')

    async def test_native_compaction_keeps_source_readable(self):
        from deepagents.middleware.summarization import SummarizationMiddleware
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)
            source=Sources(path).publish(url='https://example.com/guide',content='Only shared guest accounts are excluded.',kind='page')
            summary_model=ScriptModel(replies=[AIMessage(content='A source was saved at '+source['path'])])
            model=ScriptModel(replies=[call('read_file',{'file_path':source['path']}),AIMessage(content='condition found')],profile={'max_input_tokens':64000})
            from seekra.sdk.workspace import workspace_backend
            summary=SummarizationMiddleware(model=summary_model,backend=workspace_backend(path),
                                             trigger=('messages',8),keep=('messages',2))
            graph=create_search_agent(model,FixtureRetrieval(),path,InMemorySaver(),middleware=[summary])
            history=[]
            for i in range(5): history += [HumanMessage(content=f'old {i}'),AIMessage(content='old answer')]
            history.append(HumanMessage(content='Read the saved source'))
            config={'configurable':{'thread_id':'compact'}}
            events=[]
            await consume_stream(graph,{'messages':history},config,events.append)
            self.assertEqual(len(summary_model.seen),1)
            self.assertEqual(len([e for e in events if e.kind=='model_end' and e.data['internal']]),1)
            self.assertFalse(any('A source was saved' in e.data.get('text','') for e in events))
            self.assertIn('Only shared guest',model.seen[-1][-1].content)
            self.assertEqual((path/source['path'].lstrip('/')).read_text(),'Only shared guest accounts are excluded.')
