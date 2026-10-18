"""模型进展局部合并与程序生成的阅读导航。"""
from copy import deepcopy
import json

from .documents import canonical

MAX_STATE_CHARS = 8000

OP_FIELDS = {
    'add_finding': ({'summary','conditions','status','references'}, {'summary','conditions','status','references','gap_ids','evidence_quotes'}),
    'revise_finding': ({'id'}, {'id','summary','conditions','status','references','gap_ids','evidence_quotes'}),
    'remove_finding': ({'id'}, {'id'}),
    'add_gap': ({'question'}, {'question','answer_impact'}),
    'revise_gap': ({'id'}, {'id','question','answer_impact'}),
    'resolve_gap': ({'id','resolution','references'}, {'id','resolution','references'}),
    'dismiss_gap': ({'id','resolution'}, {'id','resolution','references'}),
    'reopen_gap': ({'id'}, {'id'}), 'remove_gap': ({'id'}, {'id'}),
    'add_read_target': ({'target','view','purpose'}, {'target','view','purpose','cursor','query','gap_ids'}),
    'revise_read_target': ({'id'}, {'id','target','view','purpose','cursor','query','gap_ids'}),
    'remove_read_target': ({'id'}, {'id'}),
}


def _text(value, name, required=True):
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f'{name} 需要文本' + ('且不能为空' if required else ''))


def _checked_references(session, refs, revision, label):
    """按当前任务已确认的交付范围规范化引用，不生成另一份研究状态。"""
    docs = session.documents
    run = session.last_run
    if not isinstance(refs, list) or not 1 <= len(refs) <= 8:
        raise ValueError(f'{label}.references 需要 1–8 个正文引用；'
                         f'无正文依据的事项请写入 gaps。本次更新未保存，base_revision 仍为 {revision}。')
    checked = []
    for ref_index, ref in enumerate(refs):
        location = f'{label} 的第 {ref_index+1} 个引用 references[{ref_index}]'
        unchanged = f'本次更新未保存，base_revision 仍为 {revision}。'
        try:
            ref = docs.validate_reference(ref)
        except ValueError as exc:
            raise ValueError(f'{location}：{exc}。{unchanged}') from exc
        spans = run.get('delivered', {}).get(ref['document_id'], [])
        if not any(left <= ref['start'] < ref['end'] <= right for left, right in spans):
            position, gaps, nearby = ref['start'], [], []
            for left, right in spans:
                if right <= ref['start'] or left >= ref['end']:
                    continue
                nearby.append((left, right))
                if left > position:
                    gaps.append((position, left))
                position = max(position, min(right, ref['end']))
            if position < ref['end']:
                gaps.append((position, ref['end']))
            def ranges(values):
                return '、'.join(f'[{left},{right})' for left, right in values[:3]) + (
                    f'等 {len(values)} 段' if len(values) > 3 else '')
            raise ValueError(f"{location}：{ref['document_id']} [{ref['start']},{ref['end']})"
                             f'包含尚未交付的正文 {ranges(gaps)}。'
                             f"已交付的相邻范围为 {ranges(nearby) or '无'}。"
                             '请复制实际正文结果的 evidence_ref 分别引用，或先读取未交付范围；'
                             f'不能把不相邻的已读范围合成一个连续引用。{unchanged}')
        checked.append(ref)
    return checked


def _check_findings(session, findings, revision):
    for finding_index, finding in enumerate(findings):
        if not isinstance(finding, dict) or set(finding) != {'summary', 'conditions', 'status', 'references'}:
            raise ValueError('每项发现需要 summary、conditions、status、references')
        _text(finding['summary'], 'summary', True)
        _text(finding['conditions'], 'conditions', False)
        if finding['status'] not in ('supported', 'uncertain', 'conflicting'):
            raise ValueError('发现状态需要 supported、uncertain 或 conflicting')
        finding['references'] = _checked_references(session, finding['references'], revision,
                                                    f'第 {finding_index+1} 项发现 findings[{finding_index}]')


def _replace_research(session, base_revision, goal, findings, gaps, next_step):
    """只生成待提交结果；由 tool_completed 的成功落盘发布最新视图。"""
    run = session.last_run
    if run is None or run['status'] != 'running':
        raise ValueError('没有正在执行的研究任务')
    revision = (run.get('research') or {}).get('revision', 0)
    if type(base_revision) is not int or base_revision != revision:
        raise ValueError(f'进展版本不一致，当前版本为 {revision}；请基于最新完整进展更新')
    state = canonical({'goal': goal, 'findings': findings, 'gaps': gaps, 'next_step': next_step}, session.secrets)
    size=len(json.dumps(state,ensure_ascii=False))
    if size>MAX_STATE_CHARS:
        raise ValueError(f'研究进展超过 8000 字符：实际 {size}，至少需要减少 {size-MAX_STATE_CHARS} 字符；'
                         '请整理为简短发现、条件、引用和缺口后重试')
    _text(state['goal'], 'goal', True)
    _text(state['next_step'], 'next_step', False)
    if not isinstance(state['findings'], list) or len(state['findings']) > 20:
        raise ValueError('findings 需要最多 20 项的列表')
    if not isinstance(state['gaps'], list) or len(state['gaps']) > 20:
        raise ValueError('gaps 需要最多 20 项的列表')
    for gap in state['gaps']:
        _text(gap, 'gap', True)
    _check_findings(session, state['findings'], revision)
    size=len(json.dumps(state,ensure_ascii=False))
    if size>MAX_STATE_CHARS:
        raise ValueError(f'解析正文引用后研究进展超过 8000 字符：实际 {size}，至少需要减少 {size-MAX_STATE_CHARS} 字符；'
                         '请精简发现、条件和缺口后重试')
    return {'operation': 'update_research', 'status': 'success', 'content_kind': 'research_state',
            'run_id': run['id'], 'revision': revision+1, 'state': state,
            'notice': '模型生成的研究进展；程序只校验结构、已交付范围和引用，不认证结论。'}


def update_research(session, base_revision, goal=None, findings=None, gaps=None, next_step=None, operations=None):
    """正式接口使用局部操作；全量参数仅保留旧 Python 调用的兼容语义。"""
    if operations is None:
        if ((session.last_run or {}).get('research') or {}).get('schema_version') == 2:
            raise ValueError('当前进展已使用局部更新格式；请提交 operations，避免重置已有编号和项目')
        return _replace_research(session, base_revision, goal, findings, gaps, next_step)
    if findings is not None or gaps is not None:
        raise ValueError('operations 不能与旧全量 findings/gaps 混用')
    return update_progress(session, base_revision, operations, goal, next_step)


def _initial(previous):
    state = deepcopy((previous or {}).get('state') or {'goal':'','findings':[],'gaps':[], 'next_step':''})
    state.setdefault('read_targets', [])
    for i, finding in enumerate(state['findings'], 1):
        finding.setdefault('id', f'finding-{i}')
    state['gaps'] = [gap if isinstance(gap, dict) else {
        'id':f'gap-{i}','question':gap,'status':'open','resolution':'','references':[]}
        for i, gap in enumerate(state['gaps'], 1)]
    counters = deepcopy((previous or {}).get('counters') or {})
    for kind, field in (('finding','findings'),('gap','gaps'),('target','read_targets')):
        counters[kind] = max(counters.get(kind,0), max((int(x['id'].rsplit('-',1)[1]) for x in state[field]), default=0))
    return state, counters


def _coverage(spans, start, end):
    position, overlap = start, False
    for left, right in spans:
        if right <= start or left >= end:
            continue
        overlap = True
        if left > position:
            return 'part'
        position = max(position, right)
    return 'full' if position >= end else 'part' if overlap else 'none'


def navigation(state, session, visible=None):
    rows = []
    for target in state.get('read_targets', []):
        args = {key: target[key] for key in ('target','view','cursor','query') if key in target}
        row = session.documents.describe_target(**args)
        row.update(id=target['id'], purpose=target['purpose'])
        doc_id, start, end = row.get('document_id'), row.get('start'), row.get('end')
        if doc_id is not None and type(start) is int and type(end) is int and start < end:
            delivered = session.last_run.get('delivered', {}).get(doc_id, [])
            row['ever_delivered'] = _coverage(delivered, start, end)
            row['current_input'] = _coverage((visible or {}).get(doc_id, []), start, end)
            row['first_undelivered'] = session.documents.unread_target(target['target'], target['view'],
                                                                      start, end, delivered)
        else:
            row.update(ever_delivered='none', current_input='none')
        rows.append(row)
    return rows


def present_progress(result, session, visible=None):
    if result.get('schema_version') != 2:
        return result
    nav=navigation(result['state'],session,visible)
    state=result['state']
    links=[{'gap_id':gap['id'],'status':gap['status'],
            'finding_ids':[item['id'] for item in state['findings'] if gap['id'] in item.get('gap_ids',[])],
            'target_ids':[item['id'] for item in state['read_targets'] if gap['id'] in item.get('gap_ids',[])]}
           for gap in state['gaps']]
    return {**result, 'navigation':nav, 'document_coverage':document_coverage(state,session,visible,nav),
            'gap_links':links,
            'coverage_notice':'程序交付概况，最多展示8份资料，其余编号列于 not_displayed_document_ids。ever_delivered 为本任务历史累计交付，current_input 为本次可见正文或字面原句。full 仅指这份资料文本，不保证网页提取完整、模型理解或结论充分；搜索片段的 full 不代表整页正文。当前不可见不等于从未交付，按问题和保存引用决定是否回查，不要求读完所有资料。',
            'navigation_notice':'程序生成阅读入口；ever_delivered 为历史累计交付，current_input 为本次输入，none/part/full 表示覆盖范围。first_undelivered 提供目标内最早尚未交付的范围及阅读动作；根据任务与 purpose 判断是否需要继续，交付完整不证明结论充分。'}


def document_coverage(state,session,visible=None,nav=()):
    """正在引用和选择的资料保持交付概况，不依赖阅读目标继续存在。"""
    owners={}
    def include(doc_id,item_id):
        items=owners.setdefault(doc_id,[])
        if item_id not in items:items.append(item_id)
    for item in [*state.get('findings',[]),*state.get('gaps',[])]:
        if not isinstance(item,dict):continue
        for reference in item.get('references',[]):
            ref=session.documents.validate_reference(reference)
            include(ref['document_id'],item['id'])
    for row in nav:
        if row.get('document_id'):include(row['document_id'],row['id'])
    rows=[]
    for doc_id in list(owners)[:8]:
        doc=session.documents._document(doc_id)
        spans=session.last_run.get('delivered',{}).get(doc_id,[])
        covered=sum(max(0,min(right,len(doc.content))-max(0,left)) for left,right in spans)
        rows.append({'source_id':doc.source_id,'document_id':doc.id,'snapshot_id':doc.snapshot_id,
                     'content_kind':doc.kind,'title':doc.title,'url':doc.url,
                     'total_characters':len(doc.content),'delivered_characters':covered,
                     'ever_delivered':_coverage(spans,0,len(doc.content)),
                     'current_input':_coverage((visible or {}).get(doc_id,[]),0,len(doc.content)),
                     'delivered_ranges':[list(span) for span in spans[:3]],'more_ranges':max(0,len(spans)-3),
                     'used_by':owners[doc_id]})
    return {'documents':rows,'not_displayed_document_ids':list(owners)[8:]}


def _check_links(state):
    gap_ids={gap['id'] for gap in state['gaps']}
    for item in [*state['findings'],*state['read_targets']]:
        ids=item.get('gap_ids',[])
        if not isinstance(ids,list) or len(ids)>4 or any(not isinstance(x,str) for x in ids) or len(set(ids))!=len(ids):
            raise ValueError(f"{item['id']}.gap_ids 需要最多4个不重复的已有缺口编号")
        missing=set(ids)-gap_ids
        if missing:
            raise ValueError(f"{item['id']}.gap_ids 包含不存在或已移除的缺口 {sorted(missing)}；请先在本批修订关联再移除缺口，不猜测尚未返回的编号")


def _check_quotes(finding,documents):
    quotes=finding.get('evidence_quotes',[])
    if not isinstance(quotes,list) or len(quotes)>3:
        raise ValueError(f"{finding['id']}.evidence_quotes 需要最多3条原句")
    for quote in quotes:
        if not isinstance(quote,dict) or set(quote)!={'reference','text'}:
            raise ValueError('每条原句需要 reference 与 text')
        _text(quote['text'],'原句 text')
        if len(quote['text'])>400:
            raise ValueError('每条原句最多400字符，请选择关键措辞而非复制全文')
        ref=documents.validate_reference(quote['reference'])
        if not any(parent['document_id']==ref['document_id'] and parent['start']<=ref['start']<ref['end']<=parent['end']
                   for parent in finding['references']):
            raise ValueError(f"{finding['id']} 的原句引用不在该发现 references 范围内；修改出处时同步修订或移除旧 evidence_quotes")
        try:selected=documents.select_quote(ref,quote['text'])
        except ValueError as exc:raise ValueError(f"{finding['id']}：{exc}") from exc
        quote.update(selected)


def update_progress(session, base_revision, operations, goal=None, next_step=None):
    run = session.last_run
    if run is None or run['status'] != 'running':
        raise ValueError('没有正在执行的研究任务')
    previous = run.get('research')
    revision = (previous or {}).get('revision',0)
    if type(base_revision) is not int or base_revision != revision:
        raise ValueError(f'进展版本不一致，当前版本为 {revision}；请使用当前任务版本')
    if not isinstance(operations,list) or len(operations)>20:
        raise ValueError('operations 需要最多20项列表')
    state, counters = _initial(previous)
    original = deepcopy(state)
    if goal is not None:
        _text(goal,'goal',False); state['goal'] = goal
    if next_step is not None:
        _text(next_step,'next_step',False); state['next_step'] = next_step
    finding_required={'summary','conditions','status','references'}
    for operation in operations:
        if not isinstance(operation,dict) or not isinstance(operation.get('op'),str):
            raise ValueError('每项操作需要 op')
        op = operation['op']
        if op not in OP_FIELDS:
            raise ValueError(f'未知进展操作：{op}')
        required, allowed = OP_FIELDS[op]
        supplied = set(operation)-{'op'}
        if not required<=supplied<=allowed:
            raise ValueError(f'{op} 参数不合法：缺少 {sorted(required-supplied)}，多余 {sorted(supplied-allowed)}。本批未保存，版本仍为 {revision}。')
        kind = 'finding' if op.endswith('finding') else 'target' if op.endswith('read_target') else 'gap'
        field = {'finding':'findings','gap':'gaps','target':'read_targets'}[kind]
        data = {k:v for k,v in operation.items() if k!='op'}
        if op.startswith('add_') and op in {'add_finding','add_gap','add_read_target'}:
            counters[kind] += 1
            item = {'id':f'{kind}-{counters[kind]}', **data}
            if kind=='gap': item.update(status='open',resolution='',references=[])
            state[field].append(item)
        else:
            item_id = data.pop('id', None)
            item = next((x for x in state[field] if x['id']==item_id),None)
            if item is None:
                raise ValueError(f'{op} 的 id 不存在，请复制当前状态中的编号')
            if op in {'remove_finding','remove_gap','remove_read_target'}:
                state[field].remove(item)
            elif op in {'revise_finding','revise_gap','revise_read_target'}:
                if not data: raise ValueError(f'{op} 修订字段不合法')
                item.update(data)
            elif op in {'resolve_gap','dismiss_gap'}:
                _text(data['resolution'],'resolution')
                refs = data.get('references',[])
                if op=='resolve_gap' and not refs:
                    raise ValueError('解决事实缺口需要正文 references；暂未核实请保持 open')
                item.update(status='resolved' if op=='resolve_gap' else 'dismissed',resolution=data['resolution'],references=refs)
            elif op=='reopen_gap':
                item.update(status='open',resolution='',references=[])
            else:
                raise ValueError(f'未知进展操作：{op}')
    state = canonical(state, session.secrets)
    if any(len(state[key])>20 for key in ('findings','gaps')) or len(state['read_targets'])>10:
        raise ValueError('发现/缺口最多20项，阅读目标最多10项；请明确整理已有项目')
    _check_links(state)
    findings=[{k:f[k] for k in finding_required} for f in state['findings']]
    _check_findings(session, findings, revision)
    for finding, checked in zip(state['findings'], findings, strict=True):
        finding['references'] = checked['references']
        _check_quotes(finding,session.documents)
    for gap in state['gaps']:
        _text(gap['question'],'question')
        if 'answer_impact' in gap:
            _text(gap['answer_impact'],'answer_impact',False)
        if gap['references']:
            gap['references'] = _checked_references(session, gap['references'], revision, f"缺口 {gap['id']}")
    for target in state['read_targets']:
        _text(target['purpose'],'purpose')
        if target.get('cursor') is None: target.pop('cursor',None)
        session.documents.describe_target(**{k:target[k] for k in ('target','view','cursor','query') if k in target})
    state_chars=len(json.dumps(state,ensure_ascii=False))
    if state_chars>MAX_STATE_CHARS:
        raise ValueError(f'合并后的研究进展超过8000字符：实际 {state_chars}，至少需要减少 {state_chars-MAX_STATE_CHARS} 字符；'
                         '请保留影响当前答案的发现、条件和出处，修订或移除不再需要的内容，本批未保存')
    result = {'operation':'update_research','status':'success','content_kind':'research_state',
              'schema_version':2,'run_id':run['id'],'revision':revision+1,'state':state,'counters':counters,
              'state_budget':{'characters':state_chars,'limit':MAX_STATE_CHARS,'remaining':MAX_STATE_CHARS-state_chars},
              'content_changed':state!=original,'notice':'模型进展；程序校验引用、结构和阅读入口，不认证结论。'}
    return present_progress(result, session, run.get('visible',{}))
