"""工具注册与分派；依赖由注册者绑定，完成结果由循环提交。"""
from copy import deepcopy
from dataclasses import dataclass, field
import json
from .operations import failure


@dataclass(frozen=True)
class ToolResult:
    content: str
    record_fields: dict = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.content, str):
            raise TypeError('工具结果必须是文本')
        if not isinstance(self.record_fields, dict) or {
            'id', 'kind', 'time', 'run_id', 'call_id', 'tool_name', 'result', 'final_output_field'
        } & self.record_fields.keys():
            raise ValueError('工具元数据不能覆盖执行事实字段')
        object.__setattr__(self, 'record_fields', deepcopy(self.record_fields))


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

    def execute(self, call):
        name = call['function']['name']
        if name not in self._tools:
            return ToolResult(json.dumps(failure(name,'invalid_arguments',f'未注册工具：{name}。请从当前工具列表选择。'),ensure_ascii=False))
        result = self._tools[name][1](call)
        return result if isinstance(result, ToolResult) else ToolResult(result)
