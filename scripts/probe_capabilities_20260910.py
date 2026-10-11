"""本轮小样本真实实验：复用生产 Agent，保存可审计事件，不记录认证或隐藏推理。"""

import argparse
from copy import deepcopy
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from unittest.mock import patch

import httpx

from seekra.agent import run_agent
from seekra.cli import read_config
from seekra.session import Session
from seekra.runtime import CancellationToken
from seekra.tools import legacy_tool_definitions

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=['page', 'boundary', 'think-pair'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('请使用新的输出文件，不覆盖原始记录')
    config = read_config(ROOT / '.env')
    cases = json.loads((ROOT / 'evals/cases.json').read_text(encoding='utf-8'))
    record = {'started_at': datetime.now(timezone.utc).isoformat(), 'case': args.case,
              'model': config['DEEPSEEK_MODEL'], 'runs': [], 'judgment': '待人工核对'}

    def clean(value):
        """只清理保存副本；原始会话在内存中保持不变。"""
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k != 'reasoning_content'}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value

    def save():
        text = json.dumps(clean(record), ensure_ascii=False, indent=2)
        for key in ('DEEPSEEK_API_KEY', 'TAVILY_API_KEY'):
            text = text.replace(json.dumps(config[key], ensure_ascii=False)[1:-1], '[已隐藏密钥]')
        args.output.write_text(text + '\n', encoding='utf-8')

    def run(label, question, session=None, structured=False, limit=8):
        """只用回调和请求钩子采集；循环、工具和 Session 均来自主实现。"""
        session = session if session is not None else Session()
        item = {'label': label, 'question': question, 'structured_think': structured,
                'external_decision_budget': limit, 'requests': [], 'events': []}
        record['runs'].append(item)
        start = time.monotonic()

        def capture(request):
            item['requests'].append({'url': str(request.url), 'body': json.loads(request.content)})

        cancel = CancellationToken()
        def event(name, **details):
            item['events'].append({'event': name, 'seconds': round(time.monotonic()-start, 3), **details})
            if name == 'tool_result' and session.last_run['calls'] >= limit and not session.last_run['pending']:
                cancel.cancel()
            if name in ('model_call', 'tool_call', 'final_answer', 'end'):
                print(label, name, details.get('round', ''), flush=True)

        try:
            compatibility = (patch('seekra.tools.tool_definitions', legacy_tool_definitions)
                             if args.case == 'think-pair' else nullcontext())
            with compatibility, httpx.Client(event_hooks={'request': [capture]}) as client:
                item['answer'] = run_agent(question, client=client, model=config['DEEPSEEK_MODEL'],
                    deepseek_api_key=config['DEEPSEEK_API_KEY'], tavily_api_key=config['TAVILY_API_KEY'],
                    session=session, structured_think=structured, cancel=cancel, on_event=event)
        except (Exception, KeyboardInterrupt) as exc:
            item['error'] = type(exc).__name__
        item['seconds'] = round(time.monotonic()-start, 3)
        save()
        return session, item

    args.output.parent.mkdir(parents=True, exist_ok=True)
    # 独占创建防止覆盖；之后仅更新本次创建的记录。
    with args.output.open('x', encoding='utf-8') as output:
        output.write('{}\n')
    if args.case != 'think-pair':
        case = next(c for c in cases if c['id'] == args.case)
        run(args.case, case['question'], limit=12)
    else:
        seed, seed_run = run('seed', '请读取 https://docs.python.org/3.13/c-api/memory.html 的第一段即可，'
            '不要续读，不要搜索，不做事实判断，最后只回复“已取得第一段”。')
        if seed_run.get('error') or not seed.pages:
            raise RuntimeError('没有真实页面快照，不能做对照')
        record['seed_messages'] = deepcopy(seed.messages)
        record['seed_pages'] = deepcopy(seed.pages)
        question = ('现在只依据这个页面，判断 The mimalloc allocator 小节是否明确规定自由线程构建必须使用 mimalloc。'
                    '先调用 think_tool 整理已有证据和缺口，再决定是否需要续读；不要搜索其他页面。'
                    '最后给出简短结论，区分未记载与不存在，并准确说明实际读取范围。')
        # 交错顺序降低时间漂移；唯一请求差异应是 think 工具参数 schema。
        for index, structured in enumerate((False, True, True, False), 1):
            run(f'pair-{index}-' + ('structured' if structured else 'free'), question,
                deepcopy(seed), structured=structured, limit=6)
        firsts = [deepcopy(r['requests'][0]['body']) for r in record['runs'][1:]]
        for body in firsts:
            next(d['function'] for d in body['tools'] if d['function']['name']=='think_tool').pop('parameters')
        assert all(body == firsts[0] for body in firsts), '实验输入存在非 schema 差异'
        record['same_first_request_except_think_schema'] = True
        record['common_request_sha256'] = hashlib.sha256(json.dumps(firsts[0], sort_keys=True).encode()).hexdigest()
        save()
    print('记录已写入', args.output, flush=True)


if __name__ == '__main__':
    main()
