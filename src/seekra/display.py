"""终端事件展示：只读取操作结果，不生成证据、不更改模型上下文。"""

import json
import sys
import time
import unicodedata

from .runtime import ContentOutputError


def display_text(value: object, secrets: tuple[str, ...] = (), *, single_line: bool = False) -> str:
    """只处理显示副本：遮蔽密钥与终端控制字符，不改证据或模型消息。"""
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, '[已隐藏密钥]')
    text = ''.join(c if c in '\n\t' or unicodedata.category(c) not in {'Cc', 'Cf'}
                   else f'\\u{ord(c):04x}' for c in text)
    return ' '.join(text.splitlines()) if single_line else text


def _usage_totals_text(totals):
    complete = totals['calls'] - totals['missing_calls']
    inputs = totals.get('input_reports', complete)
    outputs = totals.get('output_reports', complete)
    if not inputs and not outputs:
        return '未取得用量报告。'
    incoming = str(totals['input_tokens']) if inputs else '未取得'
    outgoing = str(totals['output_tokens']) if outputs else '未取得'
    text = f'输入 {incoming}，输出 {outgoing}。'
    return text


def session_transcript(session, secrets=()) -> str:
    """从执行记录重建可读对话；不把工具 JSON 或系统提示展示为回答。"""
    parts = []
    committed = {}
    states = {'interrupted':'已停止', 'failed':'回答未完成', 'paused':'已暂停',
              'needs_attention':'需要处理'}
    for record in session.records:
        kind = record['kind']
        if kind == 'session_reset':
            parts.clear()
            committed.clear()
        elif kind == 'model_started':
            committed.pop(record['run_id'], None)
        elif kind == 'run_started':
            parts.append('\n── 你 ──\n' + record['question'] + '\n')
        elif kind == 'model_completed':
            message = record['message']
            committed[record['run_id']] = message.get('content')
            if message.get('content'):
                label = 'Seekra'
                parts.append(f'\n── {label} ──\n{message["content"]}\n')
        elif kind == 'tool_completed' and record.get('final_output_field'):
            payload=json.loads(record['result'])
            if payload.get('status')=='success' and isinstance(payload.get(record['final_output_field']),str):
                text=payload[record['final_output_field']]
                committed[record['run_id']]=text
                parts.append(f'\n── Seekra ──\n{text}\n')
        elif kind == 'run_resumed':
            parts.append('\n── 继续回答 ──\n')
        elif kind == 'run_ended' and record['status'] != 'completed':
            # 兼容旧日志：已保存的完整消息在工具失败时曾被重复记作残稿。
            if record.get('partial_content') and record['partial_content'] != committed.get(record['run_id']):
                parts.append('\n── Seekra · 回答未完成 ──\n' + record['partial_content'] + '\n')
            reason = record.get('error') or ''
            state = states.get(record['status'], record['status'])
            parts.append('\n已停止。\n' if record['status']=='interrupted'
                         else f'\n{state}：{reason}\n' if reason else f'\n{state}。\n')
    return display_text(''.join(parts), secrets)


class TextStreamWriter:
    """正文逐段写入并刷新；与可忽略写入故障的调试展示不同。"""

    def __init__(self):
        """绑定当前 stdout，记录终端行状态。"""
        self.output = sys.stdout
        self.needs_newline = False

    def write(self, text: str) -> None:
        """原样输出非空片段，写入或刷新失败向上传播。"""
        if text:
            try:
                self.output.write(text)
                self.needs_newline = not text.endswith("\n")
                self.output.flush()
            except Exception as exc:
                raise ContentOutputError('正文写入或刷新失败') from exc

    def finish(self) -> None:
        """必要时补换行，重复调用不会重复输出。"""
        if self.needs_newline:
            self.write("\n")


class AgentDisplay:
    """普通状态与模型正文；性能数字仅在 verbose 下显示。"""

    def __init__(self, verbose: bool = False, secrets: tuple[str, ...] = ()):
        self.writer = TextStreamWriter()
        self.verbose = verbose
        self.started = None
        self.first = None
        self.fragments = 0
        self.secrets = secrets

    def content(self, text: str) -> None:
        """立即展示正文；记录首段可见字符写入刷新的近似时刻。"""
        self.writer.write(text)
        self.fragments += 1
        if self.first is None and text.strip():
            self.first = time.monotonic()

    def event(self, event: str, **details) -> None:
        """只由真实模型/工具事件触发状态，不写进模型上下文。"""
        text = None
        if event == 'model_call':
            self.started = time.monotonic()
            self.first = None
            self.fragments = 0
            text = '正在思考…'
        elif event == 'model_response':
            self.writer.finish()
            if self.verbose and self.started is not None:
                elapsed = time.monotonic() - self.started
                first = f'{self.first - self.started:.3f} 秒' if self.first is not None else '无正文'
                text = f'本轮模型：首段正文 {first}，总耗时 {elapsed:.3f} 秒，正文片段 {self.fragments} 段。'
        elif event == 'usage' and self.verbose:
            usage = details.get('usage') or {}
            incoming, outgoing = usage.get('input_tokens'), usage.get('output_tokens')
            show = lambda value: str(value) if value is not None else '未取得'
            text = f'本次请求 token：输入 {show(incoming)}，输出 {show(outgoing)}。'
            rate = usage.get('cache_hit_rate')
            if rate is not None:
                text += f' 缓存命中率：{rate*100:.1f}%。'
            if incoming is None or outgoing is None:
                text += ' 报告不完整。'
        elif event == 'context' and self.verbose:
            text = f"请求长度：{details['request_characters']} 字符（含消息和工具定义）。"
            if details['offloaded']:
                text += ' 部分原文已转为历史记录，可按需读取。'
            if details.get('compacted_messages'):
                text += f" {details['compacted_messages']} 条较早消息已移出当前工作窗口。"
            if details.get('checkpoint_requested'):
                text += ' 已请求保存研究进展，以便继续整理上下文。'
        elif event == 'tool_call':
            name = details['tool_call']['function']['name']
            text = {'tavily_search': '正在搜索…', 'read_page': '正在读取网页…',
                    'think_tool': '正在整理信息…', 'list_history': '正在查找之前的内容…',
                    'update_research': '正在保存研究进展…',
                    'read_history': '正在回看之前的内容…', 'list_documents': '正在查找已保存资料…',
                    'read_section': '正在读取相关小节…', 'read_document': '正在回查正文…',
                    'find_in_documents': '正在定位相关正文…'}.get(name, '正在执行工具…')
            try:
                args = json.loads(details['tool_call']['function']['arguments'])
                if isinstance(args, dict):
                    if name == 'tavily_search':
                        text += '\n搜索词：' + display_text(args.get('query', ''), self.secrets, single_line=True)
                        domains = args.get('include_domains')
                        if isinstance(domains, list) and domains:
                            text += '\n限定来源：' + display_text('、'.join(map(str, domains)), self.secrets, single_line=True)
                    elif name == 'read_page':
                        start = args.get('start', 0)
                        if type(start) is int and start > 0:
                            text = '正在接着读取页面…'
                        text += '\n' + display_text(args.get('url', ''), self.secrets, single_line=True)
                    elif name == 'read_document' and args.get('view') == 'outline':
                        text = '正在读取资料目录…'
            except (ValueError, TypeError):
                pass
        elif event == 'tool_result':
            try:
                result = json.loads(details['result'])
                status = result.get('status')
                if status == 'failed':
                    label = {'tavily_search':'搜索','read_page':'页面读取','think_tool':'笔记记录',
                             'list_history':'历史查找','read_history':'历史读取'}.get(details['tool_name'],'工具调用')
                    text = f"{label}未完成：{result.get('error') or '未取得可用结果。'}"
                elif details['tool_name'] == 'tavily_search':
                    text = '这次没有找到相关结果。' if status == 'empty' else f"找到 {len(result.get('results', []))} 条结果。"
                    for number, row in enumerate(result.get('results', []), 1):
                        title = display_text(row.get('title') or row.get('url', ''), self.secrets, single_line=True)
                        url = display_text(row.get('url', ''), self.secrets, single_line=True)
                        text += f'\n  {number}. {title}\n     {url}'
                elif details['tool_name'] == 'read_page':
                    if status == 'partial':
                        text = ('这次取得的页面内容已读完。' if result.get('next_start') is None
                                else '已读到一部分，后面还有内容。')
                    else:
                        text = '已读到页面内容。'
                    text += '\n' + display_text(result.get('title') or result.get('url', ''), self.secrets, single_line=True)
                elif details['tool_name'] in {'read_section', 'read_document'}:
                    if 'outline' in result:
                        text = f"已列出 {len(result['outline'])} 项目录，" + (
                            '可继续翻页。' if result.get('next_cursor') else '目录已列完。')
                    else:
                        text = ('已取得整段目标正文。' if result.get('complete') else
                                '已取得部分正文，可继续读取。' if result.get('next_cursor') else '已到本次阅读目标末尾。')
                    text += '\n' + display_text(result.get('section_title') or result.get('title', ''), self.secrets, single_line=True)
                elif details['tool_name'] == 'find_in_documents':
                    text = f"找到 {len(result.get('matches', []))} 处字面匹配。"
                elif details['tool_name'] == 'list_documents':
                    text = f"列出 {len(result.get('documents', []))} 份已保存资料。"
                elif details['tool_name'] == 'update_research':
                    text = f"已保存第 {result['revision']} 版研究进展。"
            except (ValueError, TypeError, AttributeError):
                text = '工具结果无法解析。'
        elif event == 'end':
            try:
                self.writer.finish()
            except (OSError, UnicodeError):
                pass  # 保留原始异常，不在异常清理时再次写坏掉的 stdout。
        if text and not (self.verbose and event in {'tool_call','tool_result'}):
            self._write_status(text)
        if self.verbose:
            detail = _verbose_text(event, **details)
            if detail:
                self._write_status(detail)

    def usage_summary(self, before, after):
        calls = after['calls'] - before['calls']
        if not calls:
            return
        # 只计算本视图使用的累计计数；provider 的可选指标可能缺失或未知。
        delta = {key: after[key]-before[key] for key in (
            'calls', 'missing_calls', 'input_tokens', 'output_tokens', 'input_reports', 'output_reports')}
        text = '本次 token：' + _usage_totals_text(delta)
        if before['calls']:
            text += ' 对话累计：' + _usage_totals_text(after)
        if after['missing_calls']:
            if before['calls']:
                text += f" 统计不完整（本次 {delta['missing_calls']} 次、对话累计 {after['missing_calls']} 次请求报告有缺失），数值仅含已报告部分。"
            else:
                text += f" 统计不完整（{after['missing_calls']} 次请求报告有缺失），数值仅含已报告部分。"
        self._write_status(text)

    def _write_status(self, text: str) -> None:
        """诊断只改显示副本；输出失败不改变研究执行。"""
        try:
            print(display_text(text, self.secrets), file=sys.stderr, flush=True)
        except (OSError, UnicodeError):
            pass


def _verbose_text(event: str, **details) -> str | None:
    """将生命周期事件格式化为详细诊断；不写终端，不重复正文。"""
    if event == "model_call":
        text = (f"\n── 第 {details['round']} 轮 ──\n"
                f"模型调用：上下文 {details['message_count']} 条消息")
    elif event == "tool_call":
        function = details["tool_call"]["function"]
        text = f"\n工具 {details['number']}：{function['name']}\n"
        try:
            arguments = json.loads(function["arguments"])
        except (ValueError, TypeError):
            arguments = None
        if isinstance(arguments, dict):
            for key, value in arguments.items():
                label = {"query": "查询", "reflection": "研究笔记"}.get(key, key)
                shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                text += f"{label}：\n{shown}\n"
        else:
            text += f"原始参数（未解析）：{function['arguments']}\n"
        text += "工具执行：开始"
    elif event == "tool_result":
        result = details["result"]
        if details["tool_name"] == "think_tool" and result.startswith("已记录研究笔记："):
            text = "工具执行：研究笔记已记录"
        else:
            try:
                data = json.loads(result)
                if isinstance(data, dict) and "error" in data:
                    text = f"工具执行失败：{data['error']}"
                elif isinstance(data, dict) and data.get("content_kind") == "model_note":
                    text = "工具执行：研究笔记已记录（模型判断，不是外部证据）"
                elif isinstance(data, dict) and data.get('content_kind') == 'research_state':
                    state = data['state']
                    opened = sum(not isinstance(gap,dict) or gap.get('status')=='open' for gap in state['gaps'])
                    text = (f"进展已保存：版本 {data['revision']}，发现 {len(state['findings'])} 项，"
                            f"未解决事项 {opened} 项，阅读目标 {len(state.get('read_targets',[]))} 项。"
                            '程序校验通过，结论仍需正文支持。')
                elif isinstance(data, dict) and data.get("operation") == "read_page":
                    text = (f"页面读取：{data['status']}\n请求地址：{data['requested_url']}\n"
                            f"服务报告地址：{data['url']}\n最终地址：未知\n"
                            f"范围：{data['start']}–{data['end']} / {data['total_characters']} 字符\n"
                            f"继续起点：{data['next_start']}\n{data['coverage']}\n内容：\n{data['content']}")
                elif details["tool_name"] == "tavily_search" and isinstance(data, dict):
                    state = "无结果" if data["status"] == "empty" else "成功"
                    text = f"搜索完成：{state}，返回 {len(data['results'])} 条结果"
                    for number, item in enumerate(data["results"], 1):
                        text += (f"\n\n[结果 {number}] {item['title']}\n"
                                 f"链接：{item['url']}\n内容：\n{item['content']}")
                        if item.get('truncated'):
                            text += f"\n片段仅展示部分，可按 {item['document_id']} 回查。"
                else:
                    text = f"工具结果：\n{result}"
                diagnostic = details.get('diagnostics') or data
                if isinstance(diagnostic, dict) and "processing" in diagnostic:
                    info = diagnostic["processing"]
                    labels = {"invalid_item": "条目不是对象", "invalid_url": "无效 URL",
                              "invalid_fields": "字段类型错误", "empty_content": "空正文",
                              "duplicate": "URL 与正文完全重复"}
                    reasons = "、".join(f"{labels.get(key, key)} {count} 条"
                                        for key, count in info["removed"].items()) or "无"
                    text += (f"\n结果处理：收到 {info['received']} 条，保留 {info['kept']} 条。"
                             f"排除：{reasons}")
            except (ValueError, KeyError, TypeError):
                text = f"工具结果（原文）：\n{result}"
    elif event == "final_answer":
        text = f"运行结束：{details['round']} 次模型调用。"
    elif event == "end":
        text = f"异常结束（{details.get('status', 'failed')}）：{details['end_reason']}"
    else:
        return
    return text
