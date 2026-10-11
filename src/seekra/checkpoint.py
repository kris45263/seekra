"""预算压力下先保存阶段进展；引用回查和直接回答仍可继续。"""
from copy import deepcopy
import json
import re

from .context import build_context, ContextLimit, offloaded_notice
from .operations import failure


NOTICE = ('\n上下文接近上限，当前进入临时整理阶段。若还要继续研究，请先调用 update_research '
          '保存当前阶段，不需要先完成整个任务。用 operations 提交变化，未修改发现和条件由程序保留。'
          '没有正文依据的事项用 add_gap，已知待读入口用 add_read_target；无变化可提交空 operations 确认检查点。'
          '本阶段只提供进展保存和已有引用的局部回查，普通搜索及新资料阅读在保存成功、整理完成后恢复。'
          'read_document 需要已有 evidence_ref/match_ref 和明确的 view：reference 核对引用自身，'
          'text 展开附近正文以修正引用；普通页面续读在保存后进行。更新失败时继续修正，旧进展与旧轮次保留。'
          '阅读动作的 availability=after_progress 表示保存后才可使用，local_reference 表示本阶段局部回查；'
          '这些标记只说明阶段权限，引用范围仍需验证。不要在本阶段调用 after_progress 动作。'
          '信息已经足够时可以直接回答，不能为了保存进展编造结论或提前声称任务已完成。'
          '原始问题、完整执行记录与稳定快照仍可回查。')

RESUME_NOTICE = ('\n阶段进展已保存，普通搜索与阅读已恢复。保存只完成本次上下文整理，不代表研究已完成。'
                 '请结合原始问题、未解决项对回答的影响及阅读目标决定下一步；保存的 next_step 可能已过时。'
                 '信息足够时可以回答，仍缺关键依据时可继续使用已恢复的工具。'
                 '未解决的延伸问题可以保留，不要求清空所有缺口。'
                 '尚未读取的内容或一次查找未命中，不能据此断言原文不存在相关条件。')


def _tools(definitions, final_outputs=()):
    selected = deepcopy([d for d in definitions if d['function']['name'] in {'update_research', 'read_document',*final_outputs}])
    for definition in selected:
        function = definition['function']
        if function['name'] == 'read_document':
            function['description'] = ('整理阶段局部核对已有正文引用，不联网。document_id 复制已读正文的 '
                                       'evidence_ref/match_ref，必须带 @起点-终点；view=reference 只读引用范围，'
                                       '可按其 continuation 续读；view=text 展开附近正文并返回新引用。'
                                       '普通页面续读在保存进展后恢复。')
            properties = function['parameters']['properties']
            reference = {**properties['document_id'], 'pattern':r'^[^@]+@[0-9]+-[0-9]+$',
                         'description':'已交付正文的 evidence_ref/match_ref，包含 @起点-终点'}
            function['parameters'] = {'type':'object', 'anyOf':[
                {'type':'object', 'properties':{'document_id':reference,
                    'view':{'type':'string','enum':['reference']},
                    'cursor':{**properties['cursor'],'description':'仅用于同一引用的 reference 续读'}},
                 'required':['document_id','view'], 'additionalProperties':False},
                {'type':'object', 'properties':{'document_id':reference,
                    'view':{'type':'string','enum':['text']}},
                 'required':['document_id','view'], 'additionalProperties':False}]}
    return selected


def _local_read_args(args):
    if (not isinstance(args,dict) or not {'document_id','view'}<=set(args)<={'document_id','view','cursor'}
            or args['view'] not in {'reference','text'} or (args['view']=='text' and 'cursor' in args)):
        raise ValueError('整理阶段需要 document_id=evidence_ref/match_ref 和 view=reference/text；cursor 仅用于 reference')


def present_actions(payload, stage):
    """当前请求中的程序阅读动作按阶段标记，原正文和获取记录不变。"""
    result=deepcopy(payload)
    def mark(item):
        if not isinstance(item,dict) or not isinstance(item.get('arguments'),dict) or not isinstance(item.get('tool'),str):
            return
        item.pop('availability',None)
        if stage is not None:
            local=False
            if item['tool']=='read_document':
                try:
                    _local_read_args(item['arguments'])
                    ref=item['arguments']['document_id']
                    local=isinstance(ref,str) and re.fullmatch(r'[^@]+@[0-9]+-[0-9]+',ref) is not None
                except (ValueError,TypeError):pass
            item['availability']='local_reference' if local else 'after_progress'
    def visit(value):
        if isinstance(value,list):
            for item in value:visit(item)
        elif isinstance(value,dict):
            if value.get('offloaded') is True:
                value['notice']=offloaded_notice(value,stage is not None)
            for key,item in value.items():
                if key in {'read_action','continuation','read_context_action','open_page_action','outline_continuation',
                           'outline_action','deferred_action','review_action','question_action','source_history_action'}:
                    mark(item)
                elif key=='read_actions' and isinstance(item,list):
                    for action in item:mark(action)
                visit(item)
    visit(result)
    return result


def prepare_context(messages, definitions, session, max_chars, *, message_start=None, extra_sources=(), recovery=None):
    """选择本次消息和工具；阶段在 model_started 落盘时发布。"""
    run = session.last_run
    revision = (run.get('research') or {}).get('revision', 0)
    stage = run.get('progress_stage')
    saved = stage is not None and revision > stage['base_revision']
    if saved:
        stage = None
    threshold = max_chars * 4 // 5
    last_user = max(i for i, m in enumerate(messages) if m['role'] == 'user')
    can_save = (any(m['role'] == 'assistant' for m in messages[last_user+1:])
                and any(d['function']['name'] == 'update_research' for d in definitions))
    latest = next((r for r in reversed(session.records) if r['id'] == run.get('research_record_id')), None)
    just_saved = (latest is not None and messages[-1].get('tool_call_id') == latest['call_id']
                  and messages[-1].get('content') == latest['result'])

    def select(current_stage, compact=False):
        staged = list(messages)
        final_tools=run['config'].get('final_outputs',{})
        staged[0] = {**messages[0], 'content': messages[0]['content'] + (
            NOTICE+('本次还提供终稿提交工具 '+', '.join(final_tools)+'；依据足够时可单独提交终稿，'
                    '引用仍须来自已交付正文，该入口不用于获取新资料。' if final_tools else '')
            if current_stage is not None else RESUME_NOTICE if saved else '')}
        if recovery is not None:
            user, original_content, snapshot = recovery
            staged[user] = {**staged[user], 'content':original_content +
                            '\n\n[harness 提供的恢复资料]\n' + json.dumps(present_actions(snapshot,current_stage),ensure_ascii=False)}
        tools = _tools(definitions,run['config'].get('final_outputs',{})) if current_stage is not None else definitions
        selected, info = build_context(staged, tools, session, max_chars, compact=compact,
                                      message_start=message_start, extra_sources=extra_sources,
                                      payload_transform=lambda payload:present_actions(payload,current_stage),
                                      read_notice=('当前可保存进展或局部核对已有正文引用；'
                                                   '普通资料和历史回查在保存进展后恢复。')
                                      if current_stage is not None else None)
        return selected, tools, info, staged[0]['content']

    try:
        result = select(stage, compact=saved)
    except ContextLimit:
        try:
            result = select(stage, compact=True)
        except ContextLimit:
            if stage is not None or not can_save:
                raise
            stage = {'base_revision': revision}
            result = select(stage, compact=True)
    if result[2]['request_characters'] >= threshold:
        result = select(stage, compact=True)
        if stage is None and result[2]['request_characters'] >= threshold and not just_saved and can_save:
            stage = {'base_revision': revision}
            result = select(stage, compact=True)
    info = result[2]
    info.update(progress_stage=deepcopy(stage), checkpoint_requested=stage is not None,
                checkpoint_threshold=threshold, request_tools=deepcopy(result[1]))
    return result


def _progress_required(name, arguments=None):
    result=failure(name,'progress_required',
        '当前处于临时整理阶段，请先用 update_research 保存阶段进展，再继续普通搜索与阅读。'
        '本次操作未执行；保留待读入口和未核实事项。' +
        ('保存后可使用 deferred_action。' if arguments is not None else '普通工具在保存后恢复。') +
        '可以核对已有引用，或在信息足够时直接回答。')
    if arguments is not None:
        result['deferred_action']={'tool':name,'arguments':arguments,'availability':'after_progress'}
    return json.dumps(result,ensure_ascii=False)


def blocked_call(call, session):
    """执行端也检查阶段边界；完整响应或未执行调用重载后采用同一规则。"""
    if session.last_run.get('progress_stage') is None:
        return None
    name = call['function']['name']
    if name == 'update_research' or name in session.last_run['config'].get('final_outputs',{}):
        return None
    if name == 'read_document':
        args=None
        try:
            args = json.loads(call['function']['arguments'])
            _local_read_args(args)
            reference = session.documents.validate_reference(args['document_id'])
            spans = session.last_run.get('delivered', {}).get(reference['document_id'], [])
            if not any(left <= reference['start'] < reference['end'] <= right for left, right in spans):
                raise ValueError('整理阶段请从已交付正文的 evidence_ref/match_ref 回查；尚未读取的事项先保留在 gaps')
        except (ValueError, TypeError) as exc:
            if isinstance(args,dict):
                try:
                    _local_read_args(args)
                except (ValueError,TypeError):
                    try:
                        # 只核对本地阅读参数，不发布正文、不计为交付。
                        session.documents.read_document(**args)
                    except (ValueError,TypeError):pass
                    else:return _progress_required(name,args)
                else:
                    # 裸资料编号属于普通阅读；引用自身的结构/范围错误仍需纠正。
                    if isinstance(args.get('document_id'),str) and '@' not in args['document_id']:
                        try:session.documents.read_document(**args)
                        except (ValueError,TypeError):pass
                        else:return _progress_required(name,args)
            return json.dumps(failure(name, 'invalid_arguments', str(exc) +
                '。当前普通阅读暂时停用；可以先保存已有发现，将需继续读取的小节或页面记入 gaps/next_step，'
                '保存后再按原小节或续读入口获取正文。不要猜测字符位置来补造已读引用。'), ensure_ascii=False)
        return None
    return _progress_required(name)
