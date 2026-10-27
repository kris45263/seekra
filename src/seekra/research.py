"""研究进展工具的参数描述；更新与校验由 progress 实现。"""
from .progress import OP_FIELDS, update_research

REFERENCE = {'type': 'object', 'properties': {
    'source_id': {'type': 'string'}, 'document_id': {'type': 'string'},
    'start': {'type': 'integer', 'minimum': 0}, 'end': {'type': 'integer', 'minimum': 1}},
    'required': ['source_id', 'document_id', 'start', 'end'], 'additionalProperties': False}
LEGACY_PROPERTIES = {
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
PROPERTIES = {
    'base_revision': {'type':'integer','minimum':0,'description':'当前任务进展版本；尚未保存为0。'},
    'goal': {'type':'string','description':'可选的简短工作目标；原用户要求由程序保留。'},
    'next_step': {'type':'string','description':'可选，当前下一步及其对原问题的作用；解决缺口或改变方向时同步更新，准备回答可说明保留哪些未确定项。空文本清除旧计划；省略保留原值。具体阅读入口用 add_read_target 保存。'},
    'operations': {'type':'array','maxItems':20,'description':'仅提交变化，未修改项目继续保留；空列表仅确认现有状态，不触发上下文整理。',
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
_OP_PROPERTIES = PROPERTIES['operations']['items']['properties']
PROPERTIES['operations']['items'] = {'anyOf': [
    {'type':'object','properties': {'op':{'type':'string','enum':[op]},
        **{name:_OP_PROPERTIES[name] for name in sorted(allowed)}},
     'required':['op',*sorted(required)],'additionalProperties':False}
    for op,(required,allowed) in OP_FIELDS.items()]}
