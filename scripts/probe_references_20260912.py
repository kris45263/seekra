"""重放真实 think 决策节点，比较协议引用编号与正文中的编号副本。"""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import time

import httpx

from seekra.cli import read_config
from seekra.deepseek import stream_chat_completion
from seekra.tools import execute_tool

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--variant', choices=['original', 'body-id'], required=True)
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.repeat < 1:
        parser.error('使用新的输出文件及正整数 repeat')
    source = ROOT / 'evals/evidence/think-pair-20260910.json'
    base = json.loads(source.read_text(encoding='utf-8'))['runs'][2]['requests'][0]['body']
    expected = deepcopy(base)
    tool_message = next(m for m in expected['messages'] if m['role'] == 'tool')
    if args.variant == 'body-id':
        content = json.loads(tool_message['content'])
        content['source_tool_call_id'] = tool_message['tool_call_id']
        tool_message['content'] = json.dumps(content, ensure_ascii=False)
        restored = deepcopy(expected)
        row = next(m for m in restored['messages'] if m['role'] == 'tool')
        payload = json.loads(row['content'])
        payload.pop('source_tool_call_id')
        assert payload == json.loads(next(m for m in base['messages'] if m['role']=='tool')['content'])
        row['content'] = next(m for m in base['messages'] if m['role']=='tool')['content']
        assert restored == base
    config = read_config(ROOT / '.env')
    assert config['DEEPSEEK_MODEL'] == base['model'], '当前模型与原始实验不同'
    known = {c['id']:c['function']['name'] for m in base['messages'] for c in m.get('tool_calls', [])}
    record = {'started_at': datetime.now(timezone.utc).isoformat(), 'variant': args.variant,
              'source': 'evals/evidence/think-pair-20260910.json',
              'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
              'code_commit': subprocess.check_output(['git','rev-parse','HEAD'], cwd=ROOT,text=True).strip(),
              'request': expected, 'known_operations': known, 'trials': [],
              'scope': '保留原始系统日期和问题，只检查下一次决策；不运行后续研究。'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as output:
        output.write('{}\n')

    def save():
        text = json.dumps(record, ensure_ascii=False, indent=2)
        for key in ['DEEPSEEK_API_KEY','TAVILY_API_KEY']:
            text = text.replace(json.dumps(config[key],ensure_ascii=False)[1:-1], '[已隐藏密钥]')
        args.output.write_text(text+'\n',encoding='utf-8')

    for number in range(args.repeat):
        trial = {'number': number+1, 'evaluations': []}
        record['trials'].append(trial)
        start = time.monotonic()

        def capture(request):
            assert json.loads(request.content) == expected, '发送内容与预定请求不一致'
            trial['wire_request_matches'] = True

        try:
            with httpx.Client(event_hooks={'request':[capture]}) as client:
                choice = stream_chat_completion(client=client,model=expected['model'],
                    api_key=config['DEEPSEEK_API_KEY'], messages=deepcopy(expected['messages']),
                    tools=deepcopy(expected['tools']))
                message = choice['message']
                message.pop('reasoning_content',None)
                trial['response'] = choice
                for call in message.get('tool_calls',[]):
                    if call['function']['name'] != 'think_tool':
                        trial['evaluations'].append({'tool':call['function']['name'],'executed':False})
                        continue
                    result=json.loads(execute_tool(call,client=client,api_key=config['TAVILY_API_KEY'],
                        structured_think=True,known_operations=known))
                    trial['evaluations'].append({'tool':'think_tool','result':result})
        except Exception as exc:
            trial['error'] = type(exc).__name__
        trial['seconds']=round(time.monotonic()-start,3)
        save()
        print(args.variant,number+1,[(e['tool'],e.get('result',{}).get('status')) for e in trial['evaluations']],
              trial.get('error'),flush=True)


if __name__ == '__main__':
    main()
