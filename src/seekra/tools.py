"""模型可调用的工具定义与参数分派，获取实现和终端展示分别维护。"""

from copy import deepcopy
import json

import httpx

from .operations import failure
from .retrieval import read_page, tavily_search


TOOLS = [
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
                                    "description": "本次候选结果上限，默认3；多来源比较可增加，精确定位可减少。"},
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
            "description": "记录简短研究笔记：已有证据、仍缺的信息、下一步搜索或结束。不会获取新事实。",
            "parameters": {
                "type": "object",
                "properties": {"reflection": {"type": "string", "description": "简短的研究进展与下一步"}},
                "required": ["reflection"],
                "additionalProperties": False,
            },
        },
    },
]


TOOLS.append({"type": "function", "function": {
    "name": "read_page",
    "description": "读取明确 URL 的页面正文，不执行搜索。返回来源、范围和读取状态。长文按 next_start 继续读取，start=0 重新获取。",
    "parameters": {"type": "object", "properties": {
        "url": {"type": "string", "description": "要读取的 HTTP(S) 页面 URL"},
        "start": {"type": "integer", "minimum": 0, "description": "字符起点，默认 0；继续时使用返回的 next_start"}},
        "required": ["url"], "additionalProperties": False}}})


def tool_definitions(structured_think: bool = False) -> list[dict]:
    """为当前运行选择笔记形式，不修改全局工具定义。"""
    definitions = deepcopy(TOOLS)
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


def execute_tool(tool_call: dict, *, client: httpx.Client, api_key: str,
                 pages: dict | None = None, structured_think: bool = False,
                 known_operations: dict | None = None) -> str:
    """校验工具参数，执行操作，将同一结果序列化给模型和展示模块。"""
    name = tool_call["function"]["name"]
    try:
        args = json.loads(tool_call["function"]["arguments"])
        if not isinstance(args, dict):
            raise ValueError("参数必须为对象")
        if name == "read_page":
            if not {"url"} <= set(args) <= {"url", "start"}:
                raise ValueError("read_page 需要 url，可选 start")
            result = read_page(**args, client=client, api_key=api_key, pages=pages if pages is not None else {})
        elif name == "think_tool" and structured_think:
            fields = {"goal", "observations", "assessment", "next_step", "references"}
            if set(args) != fields or any(not isinstance(args[k], str) or not args[k].strip() for k in fields - {"references"}):
                raise ValueError("结构化笔记需填写四个非空文本字段及 references")
            refs = args["references"]
            if not isinstance(refs, list) or any(not isinstance(ref, str) or (known_operations or {}).get(ref) not in {"tavily_search", "read_page"} for ref in refs):
                raise ValueError("references 必须引用实际已有的搜索或读取操作编号")
            result = {"operation": name, "status": "success", "content_kind": "model_note", "note": args}
        elif name == 'tavily_search':
            if not {'query'} <= set(args) <= {'query', 'max_results', 'include_domains'}:
                raise ValueError('搜索需要 query，可选 max_results 和 include_domains')
            if 'include_domains' in args and not isinstance(args['include_domains'], list):
                raise ValueError('include_domains 必须为域名列表')
            result = tavily_search(**args, client=client, api_key=api_key)
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

    def register(self, definition, execute):
        name = definition['function']['name']
        if name in self._tools:
            raise ValueError(f'工具重复：{name}')
        self._tools[name] = (deepcopy(definition), execute)

    @property
    def definitions(self):
        return [deepcopy(definition) for definition, _ in self._tools.values()]

    def execute(self, call, pages, known):
        name = call['function']['name']
        if name not in self._tools:
            return json.dumps(failure(name,'invalid_arguments',f'未注册工具：{name}。请从当前工具列表选择。'),ensure_ascii=False)
        return self._tools[name][1](call, pages, known)


def default_registry(client, api_key, session, structured=False, definitions=None):
    """现有工具作为适配器注册；历史读取始终限制在当前会话。"""
    from .context import history_index
    registry = ToolRegistry()
    def execute(call, pages, known):
        return execute_tool(call,client=client,api_key=api_key,pages=pages,
                            structured_think=structured,known_operations=known)
    for definition in definitions if definitions is not None else tool_definitions(structured):
        registry.register(definition, execute)
    history_tools = [
        ('list_history','浏览当前会话的原始执行记录索引；不搜索外部内容。',
         {'cursor':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':50}}, [],
         lambda args: history_index(session, **args)),
        ('read_history','按记录编号和字符范围读取当前会话原文，不把历史笔记当成新证据。',
         {'record_id':{'type':'string'},'start':{'type':'integer','minimum':0},
          'limit':{'type':'integer','minimum':1,'maximum':24000}}, ['record_id'],
         lambda args: session.read_record(args.pop('record_id'), **args)),
    ]
    for name, description, properties, required, reader in history_tools:
        definition={'type':'function','function':{'name':name,'description':description,
                    'parameters':{'type':'object','properties':properties,'required':required,'additionalProperties':False}}}
        def handler(call, pages, known, reader=reader, properties=properties, required=required, name=name):
            try:
                args=json.loads(call['function']['arguments'])
                if not isinstance(args,dict) or not set(required)<=set(args)<=set(properties):
                    raise ValueError('历史读取参数不合法')
                result=reader(args)
            except json.JSONDecodeError as exc:
                result=failure(name,'invalid_arguments',f'参数不是有效 JSON（第 {exc.lineno} 行，第 {exc.colno} 列）。请使用 JSON 对象。')
            except (ValueError,TypeError) as exc:
                result=failure(name,'invalid_arguments',str(exc))
            return json.dumps(result,ensure_ascii=False)
        registry.register(definition, handler)
    return registry
