"""Terminal rendering of product events only."""
import json
import sys
import unicodedata
from langchain_core.messages import HumanMessage, AIMessage


def display_text(value, secrets=(), *, single_line=False):
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, '[已隐藏密钥]')
    text = ''.join(c if c in '\n\t' or unicodedata.category(c) not in {'Cc', 'Cf'}
                   else f'\\u{ord(c):04x}' for c in text)
    return ' '.join(text.splitlines()) if single_line else text


def session_transcript(messages, secrets=()):
    lines = []
    for message in messages:
        if isinstance(message, HumanMessage):
            lines.append('\n── 你 ──\n' + display_text(message.text, secrets))
        elif isinstance(message, AIMessage) and not message.tool_calls and message.text:
            lines.append('\n── Seekra ──\n' + display_text(message.text, secrets))
    return '\n'.join(lines)


class AgentDisplay:
    def __init__(self, verbose=False, secrets=()):
        self.verbose, self.secrets = verbose, secrets
        self.input_tokens = self.output_tokens = self.calls = self.missing = 0

    def event(self, event):
        if event.namespace:
            return
        data = event.data
        if event.kind == 'start':
            print('\n── Seekra · 研究中 ──', file=sys.stderr, flush=True)
        elif event.kind == 'text':
            # Intermediate model prose remains a progress display, never the final answer channel.
            print(display_text(data['text'], self.secrets), end='', file=sys.stderr, flush=True)
        elif event.kind == 'tool_start':
            details = json.dumps(data['input'], ensure_ascii=False) if self.verbose else ''
            print(display_text(f"\n[{data['name']}] {details}", self.secrets), file=sys.stderr, flush=True)
        elif event.kind == 'tool_end' and data['error']:
            print(display_text(f"工具失败：{data['name']}", self.secrets), file=sys.stderr)
        elif event.kind == 'model_end':
            self.calls += 1
            usage = data['usage']
            if usage is None:
                self.missing += 1
            else:
                self.input_tokens += usage.get('input_tokens', 0)
                self.output_tokens += usage.get('output_tokens', 0)
        elif event.kind == 'final':
            print('\n── 最终回答 ──', file=sys.stderr)
            print(display_text(data['answer'], self.secrets), flush=True)

    def finish(self):
        print(f'\n已结束模型响应 {self.calls}；已报告输入 {self.input_tokens} / 输出 {self.output_tokens} tokens'
              + (f'；{self.missing} 次用量未知' if self.missing else ''), file=sys.stderr)
