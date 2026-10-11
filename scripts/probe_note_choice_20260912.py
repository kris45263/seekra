"""同一真实 Session 下比较不提供笔记工具、自由笔记和结构化笔记。"""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import httpx

from seekra.agent import run_agent
from seekra.cli import read_config
from seekra.session import Session
from seekra.runtime import CancellationToken
from seekra.tools import legacy_tool_definitions as tool_definitions

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('拒绝覆盖已有记录')
    data = json.loads((ROOT/'evals/evidence/think-pair-20260910.json').read_text(encoding='utf-8'))
    seed = Session(messages=data['seed_messages'], pages=data['seed_pages'])
    config = read_config(ROOT/'.env')
    question = ('现在只依据这个页面，判断 The mimalloc allocator 小节是否明确规定自由线程构建必须使用 mimalloc。'
                '已有内容不足时可继续读取同一页面，不要搜索其他页面。'
                '最后给出简短结论，区分未记载和不存在，并准确说明实际读取范围。')
    record = {'started_at': datetime.now(timezone.utc).isoformat(), 'question': question,
              'code_commit': subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
              'seed_source': 'evals/evidence/think-pair-20260910.json', 'trials': [],
              'scope': '只改变工具可用性或笔记 schema；使用真实服务与生产循环，不改生产文件。'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x',encoding='utf-8') as f:
        f.write('{}\n')

    def clean(value):
        if isinstance(value,dict):return {k:clean(v) for k,v in value.items() if k!='reasoning_content'}
        if isinstance(value,list):return [clean(v) for v in value]
        return value

    def save():
        text = json.dumps(clean(record),ensure_ascii=False,indent=2)
        for key in ['DEEPSEEK_API_KEY','TAVILY_API_KEY']:
            text = text.replace(json.dumps(config[key],ensure_ascii=False)[1:-1],'[已隐藏密钥]')
        args.output.write_text(text+'\n',encoding='utf-8')

    for mode in ['none','free','structured','structured','free','none']:
        trial={'mode':mode,'requests':[],'events':[],'external_decision_budget':6}
        record['trials'].append(trial)
        session=deepcopy(seed)

        def capture(request):
            trial['requests'].append({'url':str(request.url),'body':json.loads(request.content)})

        cancel = CancellationToken()
        def event(name,**details):
            trial['events'].append({'event':name,**details})
            if name == 'tool_result' and session.last_run['calls'] >= 6 and not session.last_run['pending']:
                cancel.cancel()
            if name in ['model_call','tool_call','final_answer','end']:
                print(mode,name,details.get('round'),flush=True)

        definitions=tool_definitions(mode=='structured')
        if mode=='none':
            definitions=[t for t in definitions if t['function']['name']!='think_tool']
        try:
            # 只替换实验中注册的工具列表；其余生产循环/校验/快照读取保持原样。
            with patch('seekra.agent.tool_definitions',return_value=definitions), httpx.Client(
                    event_hooks={'request':[capture]}) as client:
                trial['answer']=run_agent(question,client=client,model=config['DEEPSEEK_MODEL'],
                    deepseek_api_key=config['DEEPSEEK_API_KEY'],tavily_api_key=config['TAVILY_API_KEY'],
                    session=session,structured_think=mode=='structured',cancel=cancel,on_event=event)
        except (Exception, KeyboardInterrupt) as exc:
            trial['error']=type(exc).__name__
        save()
    requests=[deepcopy(t['requests'][0]['body']) for t in record['trials']]
    for request in requests:
        request['tools']=[t for t in request['tools'] if t['function']['name']!='think_tool']
    assert all(r==requests[0] for r in requests), '首请求存在非笔记工具差异'
    record['same_initial_request_except_think_definition']=True
    save()


if __name__=='__main__':
    main()
