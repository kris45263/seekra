"""模型可调用的工具定义与参数分派，获取实现和终端展示分别维护。"""

from copy import deepcopy
import json

import httpx

from .operations import failure
from .retrieval import read_page, tavily_search
from .documents import READ_CHARS
from .research import DESCRIPTION, PROPERTIES, LEGACY_PROPERTIES, update_research


_LEGACY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "tavily_search",
            "description": "搜索网络获取新信息。根据当前信息缺口提出一个查询，返回标题、链接及内容片段。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "本次搜索词"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 10,
                                    "description": "本次搜索返回的候选结果数量上限。"},
                    "chunks_per_source": {"type": "integer", "minimum": 1, "maximum": 3,
                                          "description": "每个来源的相关片段数量上限，与 max_results 的来源数量不同。精确定位可用 1，需要更多相关内容可用 2–3；省略沿用 Tavily 默认。"},
                    "include_domains": {"type": "array", "maxItems": 10, "items": {"type": "string"},
                                        "description": "可选来源域名限制，例如 api-docs.deepseek.com；仅明确域名，不含协议/路径/通配符。省略或空列表不限制；不要猜测官网域名。"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "think_tool",
            "description": "记录临时思考笔记，不获取新事实，也不保证后续上下文保留。需持续保留的发现、条件、出处和缺口使用 update_research。",
            "parameters": {
                "type": "object",
                "properties": {"reflection": {"type": "string", "description": "简短的研究进展与下一步"}},
                "required": ["reflection"],
                "additionalProperties": False,
            },
        },
    },
]


_LEGACY_TOOLS.append({"type": "function", "function": {
    "name": "read_page",
    "description": "获取明确 URL 的页面并保存新快照，返回来源、实际小节目录与开头正文。只接受 url。已有快照用 read_section 或 read_document 在本地读取，按返回的 continuation 续读；再次调用本工具会重新联网获取。",
    "parameters": {"type": "object", "properties": {
        "url": {"type": "string", "description": "要获取的 HTTP(S) 页面 URL"}},
    "required": ["url"], "additionalProperties": False}}})
TOOLS = [deepcopy(d) for d in _LEGACY_TOOLS if d['function']['name']!='think_tool']


def legacy_tool_definitions(structured_think: bool = False) -> list[dict]:
    """旧实验的显式工具列表；正式默认集合不提供 think。"""
    definitions = deepcopy(_LEGACY_TOOLS)
    if structured_think:
        fields = {"goal": "当前目标", "observations": "实际观察，不把推测写成观察",
                  "assessment": "当前判断及不确定性", "next_step": "下一步及希望获得的信息"}
        definitions[1]["function"]["parameters"] = {
            "type": "object", "properties": {
                **{key: {"type": "string", "description": value} for key, value in fields.items()},
                "references": {"type": "array", "items": {"type": "string"},
                               "description": "相关搜索或读取结果的 tool_call_id，可为空；不表示内容已被认证"}},
            "required": [*fields, "references"], "additionalProperties": False}
    return definitions


def tool_definitions(structured_think: bool = False) -> list[dict]:
    """默认使用研究进展；旧 Python 实验参数保留受控重放入口。"""
    if structured_think:
        return legacy_tool_definitions(True)
    return deepcopy(TOOLS)


def execute_tool(tool_call: dict, *, client: httpx.Client, api_key: str,
                 pages: dict | None = None, structured_think: bool = False,
                 known_operations: dict | None = None, cancel=None) -> str:
    """校验工具参数，执行操作，将同一结果序列化给模型和展示模块。"""
    name = tool_call["function"]["name"]
    try:
        args = json.loads(tool_call["function"]["arguments"])
        if not isinstance(args, dict):
            raise ValueError("参数必须为对象")
        if name == "read_page":
            if set(args) != {"url"}:
                raise ValueError("read_page 只接受 url 并获取新快照；已有快照请用 read_document 或 read_section，按其 continuation 续读")
            result = read_page(**args, client=client, api_key=api_key, pages=pages if pages is not None else {}, cancel=cancel)
        elif name == "think_tool" and structured_think:
            fields = {"goal", "observations", "assessment", "next_step", "references"}
            if set(args) != fields or any(not isinstance(args[k], str) or not args[k].strip() for k in fields - {"references"}):
                raise ValueError("结构化笔记需填写四个非空文本字段及 references")
            refs = args["references"]
            if not isinstance(refs, list) or any(not isinstance(ref, str) or (known_operations or {}).get(ref) not in {
                    "tavily_search", "read_page", "read_document", "read_section", "find_in_documents"} for ref in refs):
                raise ValueError("references 必须引用实际已有的搜索或读取操作编号")
            result = {"operation": name, "status": "success", "content_kind": "model_note", "note": args}
        elif name == 'tavily_search':
            if not {'query'} <= set(args) <= {'query', 'max_results', 'include_domains', 'chunks_per_source'}:
                raise ValueError('搜索需要 query，可选 max_results、include_domains 和 chunks_per_source')
            if 'chunks_per_source' in args and type(args['chunks_per_source']) is not int:
                raise ValueError('chunks_per_source 必须为 1–3 的整数')
            if 'include_domains' in args and not isinstance(args['include_domains'], list):
                raise ValueError('include_domains 必须为域名列表')
            result = tavily_search(**args, client=client, api_key=api_key, cancel=cancel)
        elif name == "think_tool":
            key = "reflection"
            if set(args) != {key} or not isinstance(args[key], str) or not args[key].strip():
                raise ValueError(f"参数必须且只能包含非空字符串 {key}")
            result = {"operation": name, "status": "success", "content_kind": "model_note", "note": args[key]}
        else:
            raise ValueError(f"未注册工具：{name}。请从当前工具列表选择。")
    except json.JSONDecodeError as exc:
        result = failure(name, "invalid_arguments", f"参数不是有效 JSON（第 {exc.lineno} 行，第 {exc.colno} 列）。请使用双引号，并检查括号和逗号。")
    except (ValueError, TypeError) as exc:
        result = failure(name, "invalid_arguments", f"参数无效：{exc}")
    return json.dumps(result, ensure_ascii=False)


class ToolRegistry:
    """统一工具集合；执行核心不依赖 provider、工具名称或接入方式。"""
    def __init__(self):
        self._tools = {}
        self._final_outputs = {}

    def register(self, definition, execute, *, final_output=None):
        name = definition['function']['name']
        if name in self._tools:
            raise ValueError(f'工具重复：{name}')
        if final_output is not None and (not isinstance(final_output,str) or not final_output):
            raise ValueError('终稿字段需要非空字符串')
        self._tools[name] = (deepcopy(definition), execute)
        if final_output is not None:
            self._final_outputs[name] = final_output

    @property
    def final_outputs(self):
        return dict(self._final_outputs)

    def final_text(self, name, output):
        field = self._final_outputs.get(name)
        if field is None:
            return None
        payload = json.loads(output)
        if not isinstance(payload,dict) or payload.get('status')!='success':
            return None
        answer = payload.get(field)
        if not isinstance(answer,str) or not answer.strip():
            raise ValueError('终稿工具成功结果没有有效正文')
        return answer

    @property
    def definitions(self):
        return [deepcopy(definition) for definition, _ in self._tools.values()]

    def execute(self, call, pages, known):
        name = call['function']['name']
        if name not in self._tools:
            return json.dumps(failure(name,'invalid_arguments',f'未注册工具：{name}。请从当前工具列表选择。'),ensure_ascii=False)
        return self._tools[name][1](call, pages, known)


def default_registry(client, api_key, session, structured=False, definitions=None, *, cancel=None):
    """现有工具作为适配器注册；历史读取受 Session 的最近 reset 边界约束。"""
    from .context import history_index, read_history
    registry = ToolRegistry()
    def execute(call, pages, known):
        return execute_tool(call,client=client,api_key=api_key,pages=pages,
                            structured_think=structured,known_operations=known,cancel=cancel)
    for definition in definitions if definitions is not None else tool_definitions(structured):
        registry.register(definition, execute)
    history_tools = [
        ('list_history','浏览当前会话的原始执行记录索引；不搜索外部内容。',
         {'cursor':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':50}}, [],
         lambda args: history_index(session, **args)),
        ('read_history','按记录编号查看历史。view=text（默认）返回消息、失败原因及资料入口；view=review 核对 model_completed 消息，绑定原任务问题、响应前已提交进展和当时资料快照，不能拿当前进展否定旧事件；事实主张仍须读原文，保存事件查当时进展。view=raw 查看原事件 JSON。历史模型文字不是已核实事实。start 只属于所选 view，按 continuation 续读。',
         {'record_id':{'type':'string'},'start':{'type':'integer','minimum':0},
          'limit':{'type':'integer','minimum':1,'maximum':24000},
          'view':{'type':'string','enum':['text','review','raw']}}, ['record_id'],
          lambda args: read_history(session, **args)),
    ]
    history_tools.extend([
        ('update_research', DESCRIPTION, PROPERTIES, ['base_revision','operations'],
         lambda args: update_research(session, **args)),
        ('list_documents', '查看当前会话已保存的资料目录；同一 source_id 关联同一页面的搜索片段和提取快照。可按来源筛选，用 next_cursor 翻页。',
         {'source_id': {'type': 'string'}, 'cursor': {'type': 'string'},
          'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20}}, [],
         lambda args: session.documents.list_documents(**args)),
        ('read_section', '读取目录给出的 section_ref，无需指定字符数。程序按小节边界分页；按本工具结果的 continuation 继续同一节。complete 只说明本次是否包含整节；evidence_ref 可直接用于保存正文引用。',
         {'section_ref': {'type': 'string'}, 'cursor': {'type': 'string'}}, ['section_ref'],
         lambda args: session.documents.read_section(**args)),
        ('find_in_documents', '在已保存文本中精确查找关键词，区分大小写，不联网。每个窗口最多2000字符，优先完整文本块和同节相邻段落；窗口内重复命中合并。match_range/match_ref 只定位代表命中；content 与 start/end 是实际正文，evidence_ref 只引用这段。complete 仅表示目标窗口完整，不保证整页条件已覆盖。truncated 时用 read_context_action 读取完整目标，或按 section_ref 继续读小节；read_action 可展开命中周边。未命中仅限于已查文本。',
         {'query': {'type': 'string'}, 'document_id': {'type': 'string'}, 'cursor': {'type': 'string'}}, ['query'],
         lambda args: session.documents.find_in_documents(**args)),
        ('read_document', '本地读取已保存资料。核对引用：document_id 填 evidence_ref，view=reference 只返回该引用范围，不扩展，程序分页。展开周边：view=text（默认），把 match_ref/evidence_ref 填入 document_id，返回附近正文及新引用；周边正文不属于原引用。view=outline 读目录。按 continuation 续读；start/limit 仅用于 text 模式精确读取。读取搜索片段不会自动取得网页。',
         {'document_id': {'type': 'string'}, 'cursor': {'type': 'string'},
          'view': {'type': 'string', 'enum': ['text', 'outline', 'reference']},
          'start': {'type': 'integer', 'minimum': 0},
          'limit': {'type': 'integer', 'minimum': 1, 'maximum': READ_CHARS}}, ['document_id'],
         lambda args: session.documents.read_document(**args)),
    ])
    for name, description, properties, required, reader in history_tools:
        definition={'type':'function','function':{'name':name,'description':description,
                    'parameters':{'type':'object','properties':properties,'required':required,'additionalProperties':False}}}
        def handler(call, pages, known, reader=reader, properties=properties, required=required, name=name):
            try:
                args=json.loads(call['function']['arguments'])
                legacy = name=='update_research' and isinstance(args,dict) and set(args)==set(LEGACY_PROPERTIES)
                if not isinstance(args,dict) or (not legacy and not set(required)<=set(args)<=set(properties)):
                    raise ValueError('读取参数不合法')
                result=reader(args)
            except json.JSONDecodeError as exc:
                result=failure(name,'invalid_arguments',f'参数不是有效 JSON（第 {exc.lineno} 行，第 {exc.colno} 列）。请使用 JSON 对象。')
            except (ValueError,TypeError) as exc:
                result=failure(name,'invalid_arguments',str(exc))
            return json.dumps(result,ensure_ascii=False)
        registry.register(definition, handler)
    return registry
