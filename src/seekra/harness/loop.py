"""执行循环：完整结果提交后才推进，不负责具体任务策略。"""
import hashlib
import json
from uuid import uuid4
import httpx
from .context import ContextLimit
from .runtime import CancellationToken, RunResult, ContentOutputError
from .tools import ToolResult
from .operations import failure


def execution_error_message(exc):
    """让终端、执行记录和恢复说明使用相同的已知失败原因。"""
    if isinstance(exc, ContentOutputError):
        return '正文输出失败，当前任务已停止；已保存的执行记录可用于恢复。'
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 401:
            return '模型请求未通过认证（HTTP 401），请检查 API 密钥。'
        if status == 403:
            return '模型服务拒绝了这次请求（HTTP 403）。'
        if status == 429:
            return '模型服务暂时拒绝了请求（HTTP 429），可以稍后再试。'
        if status >= 500:
            return f'模型服务端返回错误（HTTP {status}）。'
        return f'模型请求失败（HTTP {status}）。'
    if isinstance(exc, httpx.TimeoutException):
        return '等待模型服务响应超时。'
    if isinstance(exc, httpx.RequestError):
        return '连接模型服务时发生网络错误。'
    if isinstance(exc, (KeyError, IndexError, TypeError)):
        return '处理这次回答时出现数据处理异常，执行已停止。'
    if isinstance(exc, KeyboardInterrupt):
        return '当前任务已取消。'
    return str(exc) if isinstance(exc, (RuntimeError, ValueError)) else type(exc).__name__


def execute_run(question, *, model, registry, session, agent, resume=False,
                cancel=None, on_content=None, on_event=None, max_context_chars=120000):
    """运行或显式恢复任务，返回状态对象；配置/存储错误可向调用者抛出。"""
    if not resume and (not isinstance(question, str) or not question.strip()):
        raise ValueError("用户问题不能为空")
    if resume and question is not None:
        raise ValueError("恢复任务不能同时提交新问题")
    cancel = cancel or CancellationToken()
    definitions = registry.definitions
    config = {'model': model.identity, 'prompt':agent.prompt_identity, 'tools': hashlib.sha256(
        json.dumps(definitions, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
    if registry.final_outputs:
        config['final_outputs'] = registry.final_outputs

    events_enabled = True
    def emit(event, *, preserve_result=False, **details):
        nonlocal events_enabled
        if on_event is not None and events_enabled:
            try:
                on_event(event, **details)
            except (ContentOutputError, KeyboardInterrupt) as exc:
                events_enabled = False
                if not preserve_result:
                    raise
                session.record('observation', run_id=run_id, event=event,
                               error_code='output_error' if isinstance(exc, ContentOutputError) else 'interrupted',
                               exception_type=type(exc).__name__, error=str(exc) or type(exc).__name__)
            except Exception as exc:
                events_enabled = False
                session.record('observation', run_id=run_id, event=event,
                               error_code='display_error', exception_type=type(exc).__name__,
                               error=execution_error_message(exc))

    with session.exclusive():
        resume_info = ''
        if resume:
            run = session.last_run
            if run is None or run['status'] == 'completed':
                raise ValueError('当前会话没有可继续的未完成任务')
            if config != run['config']:
                raise ValueError('恢复配置与原任务不同，请使用原模型和工具配置，或提交新问题')
            run_id, question = run['id'], run['question']
            resume_info = f"\n恢复信息：上次状态 {run['status']}；原因 {run.get('error') or '进程未记录正常结束'}。已确认结果保留，未完成响应不作为正常答案。"
            if run['started_tools']:
                names = [run['pending'][key]['function']['name'] for key in sorted(run['started_tools']) if key in run['pending']]
                message = f'上次工具结果未知（{", ".join(names) or "未知工具"}），不能自动重做。可以提交新问题。'
                session.record('run_ended', run_id=run_id, status='needs_attention',
                               error=message)
                raise RuntimeError(message)
            session.record('run_resumed', run_id=run_id)
        else:
            run_id = uuid4().hex
            system = agent.system_prompt()
            session.record('run_started', run_id=run_id, question=question, system=system, config=config)
            run = session.runs[run_id]
        messages = run['messages']
        partial = []

        def content(text):
            cancel.check()
            partial.append(text)
            if on_content is not None:
                try:
                    on_content(text)
                except ContentOutputError:
                    raise
                except Exception as exc:
                    raise ContentOutputError('正文回调失败') from exc

        def usage(data):
            session.record('model_usage', run_id=run_id, round=run['calls'], usage=data)

        def completed(answer, answer_record_id=None):
            session.record('run_ended', run_id=run_id, status='completed',
                           **({'answer_record_id':answer_record_id} if answer_record_id else {}))
            return RunResult(run_id, 'completed', answer=answer)

        result = None
        try:
            emit('start', question=question)
            while True:
                cancel.check()
                # 完整响应已持久化但尚未执行的调用，恢复时从这里开始。
                for number, call in enumerate(list(run['pending'].values()), 1):
                    cancel.check()
                    name = call['function']['name']
                    emit('tool_call', round=run['calls'], tool_call=call, number=number)
                    session.record('tool_started', run_id=run_id, call_id=call['id'], tool_name=name)
                    batch = next(m for m in reversed(messages) if m['role']=='assistant')
                    output = (ToolResult(json.dumps(failure(name,'invalid_arguments','终稿工具需单独调用，取得其他操作结果后再提交。'),ensure_ascii=False))
                              if name in registry.final_outputs and len(batch.get('tool_calls') or [])!=1
                              else None)
                    if output is None:
                        output = registry.execute(call)
                    # 取消发生在工具返回后，也先保存已经取得的结果。
                    session.record('tool_completed', run_id=run_id, call_id=call['id'],
                                   tool_name=name, result=output.content, **output.record_fields,
                                   **({'final_output_field':registry.final_outputs[name]} if name in registry.final_outputs else {}))
                    saved = session.records[-1]
                    output = saved['result']
                    emit('tool_result', round=run['calls'], tool_call_id=call['id'],
                          tool_name=name, result=output, written_to_messages=True,
                          message_index=len(messages)-1, message_count=len(messages),
                          diagnostics=saved.get('retrieval_result'))
                    cancel.check()

                # 终稿工具的完整结果已提交；输出失败或进程中断后复用该记录。
                if messages[-1]['role']=='tool':
                    saved = session.records[-1]
                    if saved['kind']!='tool_completed':
                        saved = next((r for r in reversed(session.records) if r['kind']=='tool_completed'
                                      and r.get('run_id')==run_id and r.get('call_id')==messages[-1]['tool_call_id']),None)
                    if saved is not None:
                        answer = registry.final_text(saved.get('tool_name'),saved['result'])
                        if answer is not None:
                            content(answer)
                            partial = []
                            result = completed(answer,saved['id'])
                            break

                # 崩溃前已收齐最终消息但没有写入完成标记，无需重复模型请求。
                if messages[-1]['role'] == 'assistant' and not messages[-1].get('tool_calls'):
                    answer = messages[-1].get('content')
                    if not isinstance(answer, str) or not answer.strip():
                        raise RuntimeError('模型返回空答案，研究未完成。')
                    result = completed(answer)
                    break
                iteration = run['calls']+1
                staged_messages = list(messages)
                staged_messages[0] = {'role':'system','content':agent.system_prompt() + resume_info}
                prepared = agent.prepare_request(
                    staged_messages, definitions, session, max_context_chars, model, cancel=cancel)
                session.record('context_selected', run_id=run_id, **prepared.selection)
                visible_context = {k: v for k, v in session.records[-1].items() if k not in {'id', 'kind', 'time', 'run_id'}}
                emit('context', **visible_context)
                partial = []
                session.record('model_started', run_id=run_id, round=iteration, system=prepared.system)
                emit('model_call', round=iteration, message_count=len(prepared.messages))
                response = model.generate(prepared.messages, prepared.tools, on_content=content,
                                          on_usage=usage, cancel=cancel)
                cancel.check()
                message = response.message
                if not isinstance(message, dict) or message.get('role') != 'assistant':
                    raise ValueError('模型响应格式异常')
                calls = message.get('tool_calls') or []
                if response.finish_reason == 'tool_calls':
                    if not calls or not isinstance(calls, list):
                        raise ValueError('模型工具调用不完整')
                    ids = []
                    for call in calls:
                        function = call.get('function', {})
                        if (not isinstance(call.get('id'),str) or not call['id'] or call.get('type')!='function'
                            or not isinstance(function.get('name'),str) or not function['name']
                            or not isinstance(function.get('arguments'),str)):
                            raise ValueError('模型工具调用结构异常')
                        ids.append(call['id'])
                    if len(ids) != len(set(ids)):
                        raise ValueError('模型工具调用编号重复')
                elif response.finish_reason != 'stop' or calls or not isinstance(message.get('content'),str) or not message['content'].strip():
                    raise RuntimeError('模型没有正常返回完整答案或工具调用')
                session.complete_model(run_id, response)
                partial = []  # 已保存的完整消息不再属于未完成片段。
                emit('model_response', round=iteration, content=message.get('content'),
                     tool_calls=calls, finish_reason=response.finish_reason)
                emit('usage', usage=run['latest_usage'], totals=session.usage_totals())
        except (Exception, KeyboardInterrupt) as exc:
            if session._storage_failed:
                raise  # 存储已不可用，保留原始异常；不能再写结束记录。
            if run['started_tools']:
                status = 'needs_attention'
            elif isinstance(exc, KeyboardInterrupt) or cancel.cancelled:
                status = 'interrupted'
            elif isinstance(exc, ContextLimit):
                status = 'paused'
            else:
                status = 'failed'
            reason = execution_error_message(exc)
            session.record('run_ended', run_id=run_id, status=status, error=reason,
                           error_code=exc.code if isinstance(exc, ContextLimit) else
                                      'output_error' if isinstance(exc, ContentOutputError) else None,
                           exception_type=type(exc).__name__,
                           partial_content=''.join(partial),
                           http_status=exc.response.status_code if isinstance(exc,httpx.HTTPStatusError) else None)
            emit('end', preserve_result=True, end_reason=reason, status=status, run_id=run_id,
                 http_status=exc.response.status_code if isinstance(exc,httpx.HTTPStatusError) else None)
            emit('usage', preserve_result=True, usage=run['latest_usage'], totals=session.usage_totals())
            result = RunResult(run_id, status, error=reason, cause=exc)
        if result.status == 'completed':
            # 答案及完成状态均已落盘；通知故障不能把已提交结果变成失败或取消。
            emit('final_answer', preserve_result=True, round=run['calls'],
                 end_reason='final_tool' if session.records[-1].get('answer_record_id') else 'no_tool_calls',
                 finish_reason='tool_calls' if session.records[-1].get('answer_record_id') else 'stop')
        return result
