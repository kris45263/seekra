"""显式终稿提交：引用保存原文，判断与建议保持模型身份；默认不注册。"""
from copy import deepcopy
import json
import re

from seekra.search.documents.store import canonical, READ_CHARS
from seekra.harness.operations import failure


NAME='submit_answer'
PENDING_ADVICE='候选方案（关键前提尚未确认，不能据此保证可行）'


class AnswerLimit(ValueError):
    def __init__(self,message,details):
        super().__init__(message)
        self.details=details


def _object(properties, required=None):
    return {'type':'object','properties':properties,'required':list(properties) if required is None else required,
            'additionalProperties':False}


def _array(items, maximum=20):
    return {'type':'array','items':items,'maxItems':maximum}


TEXT={'type':'string'}
INDICES=_array({'type':'integer','minimum':1})
CONTENT_FIELDS=('evidence','judgments','recommendations','unknowns')
LAYOUT_FIELDS={'evidence':'evidence','judgment':'judgments',
               'recommendation':'recommendations','unknown':'unknowns'}
PROPERTIES={
    'evidence':_array(_object({'reference':{'type':'string','description':'复制实际正文结果的 evidence_ref；不是目录或关键词 match_ref。'},
                               'quote':{'type':'string','maxLength':400,'description':'逐字选取引用范围内的关键原句，作为定位线索；程序展示其原段，不接受改写或翻译。'}}),12),
    'judgments':_array(_object({'text':TEXT,'evidence_indices':INDICES,'conditions':TEXT})),
    'recommendations':_array(_object({'text':TEXT,'judgment_indices':INDICES,
        'unknown_indices':{**INDICES,'description':'所有相关未知的序号，包括建议要调查的问题和方案依赖的未确认前提。关联不表示该行动必须等待。'},
        'dependency_indices':{**INDICES,'description':'unknown_indices 的子集：其结果决定这项方案能否成立的未确认前提。存在此类前提时，程序先标明候选方案尚不能保证可行；文本也须保持条件式。仅建议询问或核实某问题，不依赖该问题得到肯定答案，应填空列表。确定行动与有条件方案混在一句时先拆开；不得为避免候选标记而漏填关键依赖。省略只兼容旧调用，不自动推断为没有依赖。'}},
        ('text','judgment_indices','unknown_indices')),10),
    'unknowns':_array(_object({'question':TEXT,'answer_impact':TEXT})),
    'layout':{'type':'array','minItems':1,'maxItems':100,
              'description':'按阅读顺序安排标题和内容。heading 只写单行标题，正文写在 judgments/recommendations/unknowns 中，可用 Markdown。其余节点的 index 是对应列表从1开始的序号。每项判断和建议恰好安排一次；每组原文至少安排一次，合并后的重复原文只在首次位置展开。未知项必须单独安排或关联到建议，程序在该建议旁展示其问题和影响。省略仅兼容旧固定栏目调用。',
              'items':{'anyOf':[
                  _object({'type':{'type':'string','enum':['heading']},'text':TEXT,
                           'level':{'type':'integer','minimum':1,'maximum':6}}),
                  _object({'type':{'type':'string','enum':list(LAYOUT_FIELDS)},
                           'index':{'type':'integer','minimum':1}})]}},
}
DEFINITION={'type':'function','function':{'name':NAME,
    'description':'研究依据足够或无法核实的边界已明确时，单独提交终稿。用 layout 自主安排标题、判断、原文、建议和未知事项的顺序，不必分成固定栏目。evidence 以已交付正文引用和原句定位，程序填入保存原文并标明展示范围；逐字引文由程序提供，翻译和转述属于模型表达。judgments 关联 evidence 序号，conditions 保留对象、时点、前提及确定程度；使用 layout 时每项 recommendations 至少关联一项 judgments 或 unknowns，关键未知必须在建议文字中表述为尚待满足的前提，不能一面保证可行一面列为未知。程序在建议旁展示关联判断的条件及未知项的影响；不识别遗漏依赖，也不认证文字与条件是否一致。列表可空，未知不冒充已调查。失败按具体反馈修正或回查，成功由程序交付终稿并结束，无需再重写。',
    'parameters':_object(PROPERTIES,CONTENT_FIELDS)}}


def _fields(value, names, label):
    if not isinstance(value,dict) or set(value)!=set(names):
        raise ValueError(f'{label} 需要且只接受字段：{", ".join(names)}')


def _text(value,label,required=True):
    if not isinstance(value,str) or (required and not value.strip()):
        raise ValueError(f'{label} 需要文本'+('且不能为空' if required else ''))


def _indices(values,count,label,required=False):
    if (not isinstance(values,list) or (required and not values)
            or any(type(i) is not int or not 1<=i<=count for i in values)
            or len(values)!=len(set(values))):
        raise ValueError(f'{label} 需要实际从1开始的唯一整数序号，范围1–{count}')


def _covered(session,reference):
    return any(start<=reference['start']<reference['end']<=end
               for start,end in session.last_run.get('delivered',{}).get(reference['document_id'],[]))


def _validate_layout(draft, source_indices):
    layout=draft['layout']
    if not isinstance(layout,list) or not 1<=len(layout)<=100:
        raise ValueError('layout 需要1至100项标题或内容节点')
    seen={kind:set() for kind in LAYOUT_FIELDS}
    for position,node in enumerate(layout,1):
        label=f'layout[{position}]'
        if not isinstance(node,dict) or not isinstance(node.get('type'),str):
            raise ValueError(f'{label} 需要 type')
        kind=node['type']
        if kind=='heading':
            _fields(node,('type','text','level'),label)
            _text(node['text'],label+'.text')
            if '\n' in node['text'] or '\r' in node['text'] or type(node['level']) is not int or not 1<=node['level']<=6:
                raise ValueError(f'{label} 标题需单行文本和1至6的 level')
            continue
        if kind not in LAYOUT_FIELDS:
            raise ValueError(f'{label} 未知类型；使用 heading/evidence/judgment/recommendation/unknown')
        _fields(node,('type','index'),label)
        index=node['index']
        _indices([index],len(draft[LAYOUT_FIELDS[kind]]),label+'.index')
        if kind!='evidence' and index in seen[kind]:
            raise ValueError(f'{label} 重复安排 {kind}[{index}]；正文只安排一次')
        seen[kind].add(index)
    for index,row in enumerate(draft['recommendations'],1):
        if not row['judgment_indices'] and not row['unknown_indices']:
            raise ValueError(f'recommendations[{index}] 需要关联至少一项判断或未确定事项，不要为通过检查编造依据')
    attached={i for row in draft['recommendations'] for i in row['unknown_indices']}
    missing=[]
    for kind,field in LAYOUT_FIELDS.items():
        if kind=='evidence':
            shown={source_indices[i-1] for i in seen[kind]}
            absent=[i for i,target in enumerate(source_indices,1) if target not in shown]
        else:
            shown=seen[kind]|(attached if kind=='unknown' else set())
            absent=sorted(set(range(1,len(draft[field])+1))-shown)
        if absent:missing.append(f'{field} 序号 {absent}')
    if missing:
        raise ValueError('layout 遗漏 '+ '；'.join(missing)+'。补齐对应节点；未知项也可关联到建议以就近展示。')


def _render_layout(draft, sources, source_indices):
    """内容关系独立于布局；条件与未知依赖不由排版节点决定是否展示。"""
    def basis(indices):
        numbers=list(dict.fromkeys(source_indices[i-1] for i in indices))
        return '、'.join(str(i) for i in numbers)
    def unknown(index):
        row=draft['unknowns'][index-1]
        return row['question']+(' 对回答的影响：'+row['answer_impact'] if row['answer_impact'] else '')
    chunks=[];displayed=set()
    for node in draft['layout']:
        kind=node['type']
        if kind=='heading':
            chunks.append('#'*node['level']+' '+node['text'].strip())
            continue
        index=node['index']
        row=draft[LAYOUT_FIELDS[kind]][index-1]
        if kind=='evidence':
            number=source_indices[index-1]
            if number in displayed:continue
            displayed.add(number)
            source=sources[number-1]
            label=' / '.join(source['path']) or source['title']
            scope=('保存文本中的完整小节' if source.get('complete_section') else
                   {'paragraph':'原段','reference_context':'引用上下文','excerpt':'局部原文（原段未完整展示）'}[source['scope']])
            quote='\n'.join('> '+line if line else '>' for line in source['text'].strip().splitlines())
            chunks.append(f'原文 {number} · {label} · 展示范围：{scope}（不代表整页已核实）\n\n'
                          +quote+f'\n\n[来源]({source["url"]})')
        elif kind=='judgment':
            refs=basis(row['evidence_indices'])
            chunks.append(row['text']+('（依据 '+refs+'）' if refs else '')+
                          ('\n\n条件与边界：'+row['conditions'] if row['conditions'] else ''))
        elif kind=='recommendation':
            related=[draft['judgments'][i-1] for i in row['judgment_indices']]
            conditions=list(dict.fromkeys(j['conditions'] for j in related if j['conditions'].strip()))
            refs=basis([i for j in related for i in j['evidence_indices']])
            dependencies=row.get('dependency_indices',[])
            parts=([PENDING_ADVICE] if dependencies else [])
            parts.append(row['text']+('（相关依据 '+refs+'）' if refs else ''))
            parts.extend('适用前提：'+condition for condition in conditions)
            parts.extend(('尚未确认的前提：' if i in dependencies else '待核实事项：')+unknown(i)
                         for i in row['unknown_indices'])
            chunks.append('\n\n'.join(parts))
        else:
            chunks.append('未确定：'+unknown(index))
    return '\n\n'.join(chunks)+'\n'


def compile_answer(session, draft):
    """不发布状态，不修改资料；完整校验后返回可保存的终稿。"""
    if session.last_run is None:
        raise ValueError('没有当前研究任务')
    _fields(draft,PROPERTIES if isinstance(draft,dict) and 'layout' in draft else CONTENT_FIELDS,'终稿')
    if len(json.dumps(draft,ensure_ascii=False))>16000:
        raise ValueError('终稿参数超过16000字符，请缩减重复判断与建议，保留关键条件和未知依赖')
    limits={'evidence':12,'judgments':20,'recommendations':10,'unknowns':20}
    for key,limit in limits.items():
        if not isinstance(draft[key],list) or len(draft[key])>limit:
            raise ValueError(f'{key} 需要最多{limit}项列表')
    if not draft['judgments'] and not draft['unknowns']:
        raise ValueError('终稿需有场景判断或明确未知事项')
    normalized=deepcopy(draft)
    sources=[]
    for index,item in enumerate(draft['evidence'],1):
        _fields(item,('reference','quote'),f'evidence[{index}]')
        _text(item['quote'],f'evidence[{index}].quote')
        if len(item['quote'])>400:
            raise ValueError('关键原句最多400字符')
        reference=session.documents.validate_reference(item['reference'])
        if not _covered(session,reference):
            raise ValueError('终稿引用必须在本任务已确认交付的连续范围内；先回查实际正文，不用两端引用跨越未读内容')
        quote=session.documents.select_quote(reference,item['quote'])
        paragraph=session.documents.paragraph_for(quote['reference'])
        parent=paragraph['reference']
        scope='paragraph'
        if parent['end']-parent['start']>READ_CHARS:
            if reference['end']-reference['start']>READ_CHARS:
                raise ValueError('原段很长，请引用实际工具单次交付范围，终稿将明确展示局部原文')
            paragraph={**paragraph,'reference':reference,'text':session.documents.read_document(
                f"{reference['document_id']}@{reference['start']}-{reference['end']}",view='reference')['content']}
            scope='excerpt'
        elif not _covered(session,parent):
            token=f"{parent['document_id']}@{parent['start']}-{parent['end']}"
            raise ValueError(f'关键原句所在原段尚未完整交付；普通阶段用 read_document(document_id="{token}", view="reference") 补读；'
                             '整理阶段可先保存进展，或用已交付引用的 view=text 展开纠错')
        else:
            context={**reference,'start':min(reference['start'],parent['start']),
                     'end':max(reference['end'],parent['end'])}
            if context['end']-context['start']>READ_CHARS:
                context=reference
                if context['end']-context['start']>READ_CHARS:
                    raise ValueError('请使用实际工具单次交付的引用范围，终稿不能静默裁掉引用中的条件')
                scope='excerpt'
            elif context!=parent:
                scope='reference_context'
            token=f"{context['document_id']}@{context['start']}-{context['end']}"
            material=session.documents.read_document(token,view='reference')
            paragraph={**paragraph,'reference':context,'text':material['content'],
                       'path':session.documents.paragraph_for(context)['path']}
        substantive=[line for line in paragraph['text'].splitlines()
                     if line.strip() and not re.match(r'^ {0,3}#{1,6}(?:\s|$)',line)]
        if not substantive:
            raise ValueError('标题仅作导航，终稿需引用实际正文')
        sources.append({'reference':paragraph['reference'],'text':paragraph['text'],'anchor':quote,
                        'source_id':paragraph['source_id'],'document_id':paragraph['document_id'],
                        'title':paragraph['title'],'path':paragraph['path'],'url':paragraph['url'],
                        'snapshot_id':paragraph['snapshot_id'],'record_id':paragraph['record_id'],
                        'scope':scope,'read_action':{'tool':'read_document','arguments':{
                            'document_id':f"{paragraph['document_id']}@{paragraph['reference']['start']}-{paragraph['reference']['end']}",'view':'reference'}}})
        normalized['evidence'][index-1]={'reference':reference,'quote':quote['text']}
    for index,row in enumerate(draft['judgments'],1):
        _fields(row,('text','evidence_indices','conditions'),f'judgments[{index}]')
        _text(row['text'],'判断');_text(row['conditions'],'适用条件',False)
        _indices(row['evidence_indices'],len(sources),'evidence_indices',bool(sources))
    for row in draft['unknowns']:
        _fields(row,('question','answer_impact'),'unknowns')
        _text(row['question'],'未知事项');_text(row['answer_impact'],'回答影响',False)
    for row in draft['recommendations']:
        fields=('text','judgment_indices','unknown_indices')
        _fields(row,fields+('dependency_indices',) if isinstance(row,dict) and 'dependency_indices' in row else fields,'recommendations')
        _text(row['text'],'建议')
        _indices(row['judgment_indices'],len(draft['judgments']),'judgment_indices')
        _indices(row['unknown_indices'],len(draft['unknowns']),'unknown_indices')
        dependencies=row.get('dependency_indices',[])
        _indices(dependencies,len(draft['unknowns']),'dependency_indices')
        if not set(dependencies)<=set(row['unknown_indices']):
            raise ValueError('dependency_indices 必须属于本建议的 unknown_indices；先关联该未知事项')
    doc_order={row['document_id']:i for i,row in enumerate(sources)}
    ordered=sorted(sources,key=lambda row:(doc_order[row['document_id']],row['reference']['start']))
    unique=[]
    for row in ordered:
        ref=row['reference']
        previous=unique[-1] if unique else None
        if (previous and previous['document_id']==row['document_id']
                and ref['start']<=previous['reference']['end']):
            old=previous['reference']
            if ref['end']>old['end']:
                previous['text']+=row['text'][old['end']-ref['start']:]
                old['end']=ref['end']
            previous['anchors'].append(row['anchor'])
            common=[]
            for left,right in zip(previous['path'],row['path']):
                if left!=right:break
                common.append(left)
            previous['path']=common
            previous['scope']='excerpt' if 'excerpt' in {previous['scope'],row['scope']} else 'reference_context'
            previous['read_action']['arguments']['document_id']=f"{old['document_id']}@{old['start']}-{old['end']}"
        else:
            unique.append(deepcopy({**row,'anchors':[row['anchor']]}))
    source_indices=[next(i for i,item in enumerate(unique,1)
                         if item['document_id']==row['document_id']
                         and item['reference']['start']<=row['reference']['start']
                         and row['reference']['end']<=item['reference']['end']) for row in sources]
    sources=unique
    characters=sum(len(row['text']) for row in sources)
    if characters>12000:
        costs=[{'source_index':i,'document_id':row['document_id'],'reference':row['reference'],
                'characters':len(row['text']),'title':row['title'],'path':row['path'],
                'evidence_indices':[j for j,target in enumerate(source_indices,1) if target==i]}
               for i,row in enumerate(sources,1)]
        raise AnswerLimit(f'原文展示共{characters}字符，上限12000，需减少{characters-12000}；'
                          '按 citation_costs 精简重复或无关引用，保留回答所需的版本、条件及例外，不能只删必需依据。',
                          {'display_budget':{'characters':characters,'limit':12000,'reduce_by':characters-12000},
                           'citation_costs':costs})
    normalized=canonical(normalized,session.secrets)
    if 'layout' in normalized:
        _validate_layout(normalized,source_indices)
        for source in sources:
            source['complete_section']=session.documents.complete_section(source['reference'])
        answer=_render_layout(normalized,sources,source_indices)
        return {'operation':NAME,'status':'success','content_kind':'structured_answer','schema_version':1,
                'run_id':session.last_run['id'],'draft':normalized,'source_quotes':sources,
                'evidence_source_indices':source_indices,'answer':answer,
                'notice':'来源、结构和排版覆盖已校验，不认证模型判断、条件一致性、隐含依赖齐全或研究完整。'}
    lines=[]
    if sources:
        lines=['## 原文依据','']
        for index,row in enumerate(sources,1):
            label=' > '.join(row['path']) or row['title']
            lines.extend([f'### {index}. {label}'+('（局部原文）' if row['scope']=='excerpt' else ''),'',
                          '> '+row['text'].strip().replace('\n','\n> '),'',f"[来源]({row['url']})",''])
    if normalized['judgments']:
        lines.extend(['## 场景判断',''])
        for row in normalized['judgments']:
            basis='、'.join(str(i) for i in dict.fromkeys(source_indices[index-1] for index in row['evidence_indices']))
            lines.append('- '+row['text']+('（依据 '+basis+'）' if basis else '')+
                         (' 条件与边界：'+row['conditions'] if row['conditions'] else ''))
    if normalized['recommendations']:
        lines.extend(['','## 调整建议',''])
        for row in normalized['recommendations']:
            dependencies=row.get('dependency_indices',[])
            if dependencies:lines.extend([PENDING_ADVICE,''])
            lines.append('- '+row['text'])
            for i in row['unknown_indices']:
                prefix=('尚未确认的前提：' if i in dependencies else
                        '待核实事项：' if 'dependency_indices' in row else '待确认条件：')
                lines.append('  - '+prefix+normalized['unknowns'][i-1]['question'])
    if normalized['unknowns']:
        lines.extend(['','## 未确定事项',''])
        for row in normalized['unknowns']:
            lines.append('- '+row['question']+(' 对回答的影响：'+row['answer_impact'] if row['answer_impact'] else ''))
    return {'operation':NAME,'status':'success','content_kind':'structured_answer','schema_version':1,
            'run_id':session.last_run['id'],'draft':normalized,'source_quotes':sources,
            'evidence_source_indices':source_indices,
            'answer':'\n'.join(lines)+'\n','notice':'来源与结构已校验，不认证模型判断、隐含依赖齐全或研究完整。'}


def register_answer_tool(registry,session):
    """显式嵌入调用入口；CLI 和 default_registry 暂不启用。"""
    def execute(call):
        try:
            result=compile_answer(session,json.loads(call['function']['arguments']))
        except (ValueError,TypeError) as exc:
            result=failure(NAME,'invalid_arguments',str(exc),**getattr(exc,'details',{}))
        return json.dumps(result,ensure_ascii=False)
    registry.register(DEFINITION,execute,final_output='answer')
