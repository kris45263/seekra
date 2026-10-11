"""模型提交研究进展；程序检查大小、版本和已交付正文的引用。"""
import json

from .documents import canonical


MAX_STATE_CHARS = 8000
REFERENCE = {'type': 'object', 'properties': {
    'source_id': {'type': 'string'}, 'document_id': {'type': 'string'},
    'start': {'type': 'integer', 'minimum': 0}, 'end': {'type': 'integer', 'minimum': 1}},
    'required': ['source_id', 'document_id', 'start', 'end'], 'additionalProperties': False}
PROPERTIES = {
    'base_revision': {'type': 'integer', 'minimum': 0,
                      'description': '当前任务最新进展版本；尚未保存时为 0。更新提交完整下一版。'},
    'goal': {'type': 'string', 'description': '简短的任务目标与约束，不替代用户原始要求。'},
    'findings': {'type': 'array', 'maxItems': 20, 'items': {'type': 'object', 'properties': {
        'summary': {'type': 'string', 'description':'仅概括引用实际支持的事实，保留必要限定，不扩大因果、版本或保证。'},
        'conditions': {'type': 'string', 'description': '适用前提、版本、例外与证据未确定的边界；需与 summary 一致，回答和建议也应保留。'},
        'status': {'type': 'string', 'enum': ['supported', 'uncertain', 'conflicting'],
                   'description': '模型对证据的判断，程序不会认证结论。'},
        'references': {'type': 'array', 'minItems': 1, 'maxItems': 8, 'items': {'anyOf': [
            {'type': 'string', 'description': '直接复制正文结果的 evidence_ref'}, REFERENCE]},
                       'description': '优先直接复制正文结果的 evidence_ref；也兼容 reference 对象。关键词 match_ref 只定位词语，不能代替支持发现的附近正文；仅有目录或保存但未交付的正文不能充当证据。'}},
        'required': ['summary', 'conditions', 'status', 'references'], 'additionalProperties': False}},
    'gaps': {'type': 'array', 'maxItems': 20, 'items': {'type': 'string'},
             'description': '未核实事项或冲突。没有正文依据的假设应放在这里。'},
    'next_step': {'type': 'string', 'description': '下一步要解决的缺口；足以回答时可写准备回答。'},
}
DESCRIPTION = ('保存当前任务的完整研究进展，后续请求保留最新有效版本；不联网。'
               '保留仍相关的旧发现、条件和出处，再补充新发现、缺口与下一步。'
               '总长度最多 8000 个序列化字符；引用必须指向已交付的正文。'
               '失败不会替换旧进展；成功只表示保存和引用检查通过，不表示结论正确。')
LEGACY_PROPERTIES = PROPERTIES
PROPERTIES = {
    'base_revision': {'type':'integer','minimum':0,'description':'当前任务进展版本；尚未保存为0。'},
    'goal': {'type':'string','description':'可选的简短工作目标；原用户要求由程序保留。'},
    'next_step': {'type':'string','description':'可选，当前下一步及其对原问题的作用；解决缺口或改变方向时同步更新，准备回答可说明保留哪些未确定项。空文本清除旧计划；省略保留原值。具体阅读入口用 add_read_target 保存。'},
    'operations': {'type':'array','maxItems':20,'description':'仅提交变化，未修改项目继续保留；空列表确认现有状态并保存检查点。',
        'items': {'type':'object','properties': {
            'op': {'type':'string','enum':['add_finding','revise_finding','remove_finding','add_gap','revise_gap',
                   'resolve_gap','dismiss_gap','reopen_gap','remove_gap','add_read_target','revise_read_target','remove_read_target'],
                   'description':'发现需正文引用；导航和未读事项使用 gap/read_target。修改或删除时复制已有id。'},
            'id': {'type':'string','description':'修改/删除对象的已有id；新增时由程序分配。'},
            **LEGACY_PROPERTIES['findings']['items']['properties'],
            'gap_ids':{'type':'array','maxItems':4,'uniqueItems':True,'items':{'type':'string'},
                       'description':'可选，关联已有缺口编号；复制已返回的 gap-N，不猜测新编号。发现与缺口关联不自动解决缺口。'},
            'evidence_quotes':{'type':'array','maxItems':3,'items':{'type':'object','properties':{
                'reference':LEGACY_PROPERTIES['findings']['items']['properties']['references']['items'],
                'text':{'type':'string','minLength':1,'maxLength':400}},
                'required':['reference','text'],'additionalProperties':False},
                'description':'可选，为关键结论选取短原句；reference 必须在本发现 references 内，text 保持原文措辞。可省略内联 Markdown 链接/转义并折叠空白，程序限定范围定位后保存实际原文与坐标。改写或翻译不接受，匹配不认证结论。空列表移除旧原句。'},
            'question': {'type':'string','description':'未核实事项或假设，add_gap 必填。'},
            'answer_impact': {'type':'string','description':'可选，说明缺口会改变原问题的哪项结论、条件或明确要求；若仅是延伸问题，说明为何不影响本次回答。模型判断，程序不认证；省略保留旧值，空文本清除旧说明。'},
            'resolution': {'type':'string','description':'解决/不再需要核查的理由；resolve_gap 还需要正文 references。'},
            'target': {'type':'string','description':'阅读目标：实际 section_ref/document_id/evidence_ref，或 page 模式的明确 URL/source_id。'},
            'view': {'type':'string','enum':['section','text','reference','outline','find','page']},
            'purpose': {'type':'string','description':'该目标要核实的具体事项。'},
            'cursor': {'type':['string','null'],'description':'可选，复制对应工具的 continuation 游标。'},
            'query': {'type':'string','description':'find 模式查找词，区分大小写。'}},
            'required':['op'],'additionalProperties':False}},
}
DESCRIPTION = ('局部更新当前研究进展，不联网。使用 base_revision 和 operations，仅提交新增、修订、解决或移除；'
               '程序保留未修改内容，返回完整状态和稳定id。发现需要实际正文 evidence_ref；'
               '找到目录但尚未读正文时保存 gap 和 read_target，无需借用别处引用。'
               '可选 gap_ids 关联已有缺口，evidence_quotes 保留关键短原句并校验字面出处；关联不自动解决缺口，原句匹配不认证结论。'
               '缺口可带 answer_impact 说明对原问题的作用；未解决项可保留，回答无需清空所有缺口。next_step 省略保留旧值，改变方向时请更新。'
               '程序补齐引用资料的交付概况和关联视图，阅读目标移除后仍保留被引用资料。'
               '空 operations 可确认现有进展以继续整理；content_changed 标明内容是否变化。'
               '模型状态最多8000字符，返回 state_budget；程序导航另计请求预算。缩减时保留当前答案所需的事实、条件和出处。'
               '失败整批不保存，成功不认证结论。')
from .progress import OP_FIELDS
_OP_PROPERTIES = PROPERTIES['operations']['items']['properties']
PROPERTIES['operations']['items'] = {'anyOf': [
    {'type':'object','properties': {'op':{'type':'string','enum':[op]},
        **{name:_OP_PROPERTIES[name] for name in sorted(allowed)}},
     'required':['op',*sorted(required)],'additionalProperties':False}
    for op,(required,allowed) in OP_FIELDS.items()]}


def _text(value, name, required=False):
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f'{name} 需要文本' + ('且不能为空' if required else ''))


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
    _text(state['next_step'], 'next_step')
    if not isinstance(state['findings'], list) or len(state['findings']) > 20:
        raise ValueError('findings 需要最多 20 项的列表')
    if not isinstance(state['gaps'], list) or len(state['gaps']) > 20:
        raise ValueError('gaps 需要最多 20 项的列表')
    for gap in state['gaps']:
        _text(gap, 'gap', True)
    docs = session.documents
    for finding_index, finding in enumerate(state['findings']):
        if not isinstance(finding, dict) or set(finding) != {'summary', 'conditions', 'status', 'references'}:
            raise ValueError('每项发现需要 summary、conditions、status、references')
        _text(finding['summary'], 'summary', True)
        _text(finding['conditions'], 'conditions')
        if finding['status'] not in ('supported', 'uncertain', 'conflicting'):
            raise ValueError('发现状态需要 supported、uncertain 或 conflicting')
        refs = finding['references']
        if not isinstance(refs, list) or not 1 <= len(refs) <= 8:
            raise ValueError(f'第 {finding_index+1} 项发现 findings[{finding_index}].references 需要 1–8 个正文引用；'
                             f'无正文依据的事项请写入 gaps。本次更新未保存，base_revision 仍为 {revision}。')
        for ref_index, ref in enumerate(refs):
            location = f'第 {finding_index+1} 项发现 findings[{finding_index}] 的第 {ref_index+1} 个引用 references[{ref_index}]'
            unchanged = f'本次更新未保存，base_revision 仍为 {revision}。'
            try:
                ref = docs.validate_reference(ref)
            except ValueError as exc:
                raise ValueError(f'{location}：{exc}。{unchanged}') from exc
            refs[ref_index] = ref
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
    from .progress import update_progress
    return update_progress(session, base_revision, operations, goal, next_step)


def quote_projection(progress):
    """恢复资料中已校验原句的交付投影；正文只在实际请求中展示一次。"""
    if not isinstance(progress,dict) or progress.get('schema_version')!=2:
        return None
    findings=[{'evidence_quotes':finding['evidence_quotes']}
              for finding in progress.get('state',{}).get('findings',[])
              if isinstance(finding,dict) and finding.get('evidence_quotes')]
    return {'operation':'update_research','schema_version':2,'content_kind':'source_quotes',
            'state':{'findings':findings}} if findings else None


def remember_delivery(delivered, messages):
    """合并实际请求中的正文范围；目录和被外置的正文不计为已交付。"""
    for message in messages:
        if message.get('role') != 'tool':
            continue
        try:
            payload = json.loads(message['content'])
        except (ValueError, TypeError, KeyError):
            continue
        if not isinstance(payload, dict) or payload.get('offloaded'):
            continue
        operation = payload.get('operation')
        if operation in {'read_page', 'read_document', 'read_section'}:
            rows = [payload]
        elif operation == 'tavily_search':
            rows = payload.get('results', [])
        elif operation == 'find_in_documents':
            rows = payload.get('matches', [])
        elif operation=='update_research' and payload.get('schema_version')==2:
            state=payload.get('state',{})
            rows=[{'reference':q.get('reference'),'content':q.get('text')}
                  for finding in state.get('findings',[]) if isinstance(finding,dict)
                  for q in finding.get('evidence_quotes',[]) if isinstance(q,dict)]
        elif operation=='submit_answer' and payload.get('content_kind')=='structured_answer' and payload.get('schema_version')==1:
            rows=[{'reference':q.get('reference'),'content':q.get('text')}
                  for q in payload.get('source_quotes',[]) if isinstance(q,dict)]
        else:
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            ref = row.get('reference')
            if not isinstance(ref, dict) or not isinstance(row.get('content'), str):
                continue
            start, end, doc_id = ref.get('start'), ref.get('end'), ref.get('document_id')
            if (not isinstance(doc_id, str) or type(start) is not int or type(end) is not int
                    or start < 0 or end <= start or end-start != len(row['content'])):
                continue
            spans = sorted([*delivered.get(doc_id, []), (start, end)])
            merged = []
            for left, right in spans:
                if merged and left <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], right))
                else:
                    merged.append((left, right))
            delivered[doc_id] = merged
