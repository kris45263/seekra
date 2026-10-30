"""正文工具的真实模型验收；固定资料与真实 Tavily 分开记录。

--list 不访问服务。其他运行使用项目 .env，会产生模型或检索费用。
每次输出使用新路径，保存实际调用与执行记录，供人工核对答案。
"""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import time
import sys

import httpx

from seekra.harness.loop import execution_error_message
from seekra.search.agent import run_agent
from seekra.terminal.cli import read_config
from seekra.search.state import SearchSession as Session


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_URL = 'https://docs.example.org/meridian/2.0/offline'
LONG_PAGE = (
    '# Meridian 2.0 offline guide\n\nThis is a fictional product used for tool evaluation.\n\n'
    '## Supported platforms\nWindows 10 or later and macOS are supported.\n\n'
    '## Offline requirements\nA Pro license is required. Before the first offline use, '
    'connect to the internet and download the resource pack.\n\n'
    + ''.join(f'Operational note {i}: Cached resources stay on the device. '
              'This note does not change licensing requirements.\n\n' for i in range(120))
    + 'The license must be validated online again after 30 consecutive days offline.\n\n'
    '## Limitations\nShared guest accounts cannot use offline mode.\n'
)
PLAIN_PAGE = (
    'Northstar export policy. This is a fictional policy for evaluating document tools.\n\n'
    + ''.join(f'Background paragraph {i}. Records are stored by the responsible team. '
              'This paragraph describes routine maintenance, not export permission.\n\n' for i in range(65))
    + 'Approval: Before exporting records, obtain written approval from the data owner.\n\n'
    'Anonymization: Remove names and email addresses from the exported data.\n\n'
    'Download link expiry: The download link expires 48 hours after creation.\n\n'
    + 'Additional operational background.\n\n' * 30
)

CASES = {
    'long-section': {
        'question': '请读取测试文档 https://docs.example.org/meridian/2.0/offline ，'
                    '核实虚构产品 Meridian 2.0 的 Windows 离线使用条件，'
                    '包括系统要求、许可证、首次准备、离线期限和账户限制。请核对完整的相关内容并给出来源。',
        'mode': 'real_model_fixed_tavily', 'page': LONG_PAGE,
        'snippet': 'Meridian 2.0 supports Windows. See Supported platforms, Offline requirements and Limitations for conditions.',
        'expected': ['Windows 10 or later', 'Pro license', 'initial online download',
                     'online validation after 30 consecutive offline days', 'no shared guest accounts'],
    },
    'no-headings': {
        'question': '请读取测试文档 https://docs.example.org/northstar/export-policy ，'
                    '并在保存的正文中用关键词查找工具定位 Approval、Anonymization、Download link expiry，'
                    '核实导出前需要谁审批、需要删除哪些个人信息，以及下载链接何时失效。给出来源。',
        'mode': 'real_model_fixed_tavily', 'page': PLAIN_PAGE,
        'url': 'https://docs.example.org/northstar/export-policy',
        'snippet': 'Northstar export policy covers Approval, Anonymization and Download link expiry. See the policy for exact requirements.',
        'expected': ['written approval from data owner', 'remove names and email addresses',
                     'link expires 48 hours after creation'],
    },
    'live-page': {
        'question': '请读取 https://docs.python.org/3.13/howto/free-threading-python.html ，'
                    '核实 Immortalization 部分：对象 immortalization 在什么时候触发？'
                    '与主线程以外的新线程有什么关系？请依据该版本原文回答并附链接。',
        'mode': 'real_model_real_tavily',
        'expected': ['triggered when a new thread is started for the first time',
                     'a thread other than the main thread'],
    },
    'migration': {
        'question': '一个 Linux 程序准备从 Python 3.13 升级到 3.14，没有显式设置 multiprocessing 的启动方式。'
                    '父进程会在启动后修改全局配置，子进程需要读取这些配置；程序也使用模块级常量。'
                    '请核实升级后这些用法的适用条件，说明需要检查或调整什么，并提供两个版本的官方依据。',
        'mode': 'real_model_real_tavily',
        'expected': ['Linux default: fork in Python 3.13, forkserver in Python 3.14',
                     'child global values under spawn/forkserver may differ from current parent values',
                     'module-level constants are explicitly distinguished from changing globals',
                     'actionable migration guidance with applicable conditions and versioned official sources'],
    },
    'simple-default': {
        'question': 'Python 3.14 在 Linux 上，未显式设置 multiprocessing 启动方式时，默认采用什么方式？请给出官方依据。',
        'mode': 'real_model_real_tavily',
        'expected': ['forkserver', 'official evidence supports the Linux and version conditions',
                     'stop once sufficient evidence is obtained'],
    },
    'wal-readonly': {
        'question': 'SQLite 数据库已经使用 WAL 模式。一个应用只需读取数据库，数据库所在目录不可写，'
                    '但另一个进程可能继续修改数据库。请依据 https://sqlite.org/wal.html 和 '
                    'https://sqlite.org/uri.html 核实能否这样打开、还缺哪些条件；'
                    '是否可以通过 immutable=1 解决目录不可写的问题？说明适用条件并给出原文依据。',
        'mode': 'real_model_real_tavily',
        'expected': ['read-only WAL requires existing readable wal/shm files, writable directory, or immutable database',
                     'read-only WAL support starts in SQLite 3.22.0',
                     'immutable assumes the file cannot change even by another process',
                     'do not recommend immutable=1 when another process can change the database'],
    },
    'robots-status': {
        'question': '请依据 https://www.rfc-editor.org/rfc/rfc9309.txt 核实：爬虫首次访问一个站点，'
                    '没有缓存过 robots.txt，服务器获取 robots.txt 时分别返回 HTTP 404 和 HTTP 503。'
                    '两种情况下爬虫对该站点资源的访问应如何处理，为什么？给出对应规范原文依据。',
        'mode': 'real_model_real_tavily',
        'expected': ['404 belongs to unavailable 400-499; crawler may access resources',
                     '503 belongs to unreachable 500-599; assume complete disallow',
                     'no previous cached robots rules in this scenario'],
    },
    'version-comparison': {
        'question': '请对比 Python 3.13 与 3.14 的官方 free-threading 说明：'
                    'https://docs.python.org/3.13/howto/free-threading-python.html 和 '
                    'https://docs.python.org/3.14/howto/free-threading-python.html 。'
                    'Immortalization 的触发时机和受影响对象在两个版本中是否相同？'
                    '能否把 3.13 的条件直接用于 3.14？分别给出两个版本的原文依据；无法核实的差异请明确说明。',
        'mode': 'real_model_real_tavily',
        'expected': ['each claim linked to its actual versioned snapshot',
                     '3.13 trigger involves the first thread other than the main thread',
                     'compare affected objects and scope against the acquired 3.14 text',
                     'missing extraction cannot be treated as evidence that versions match'],
    },
    'version-source-comparison': {
        'question': '请对比 Python 3.13 与 3.14 官方 free-threading 说明中的 Immortalization：'
                    'https://docs.python.org/3.13/howto/free-threading-python.html 和 '
                    'https://docs.python.org/3.14/howto/free-threading-python.html 。'
                    '触发时机和受影响对象是否相同，能否把 3.13 的条件直接用于 3.14？'
                    '若 3.13 网站页面无法取得，可用 CPython 官方 3.13 分支的对应原始文档 '
                    'https://raw.githubusercontent.com/python/cpython/3.13/Doc/howto/free-threading-python.rst 。'
                    '分别给出两个版本的原文依据，说明使用了网页还是文档源码。',
        'mode': 'real_model_real_tavily',
        'expected': ['3.13 source is explicitly the CPython 3.13 branch, not another version',
                     '3.13 first non-main thread trigger and wider object categories',
                     '3.14 scope limited to code constants and sys.intern strings',
                     'do not transfer the 3.13 broad object rule or trigger to 3.14 without evidence'],
    },
    'reference-review': {
        'question': '请核对当前会话保存的 RFC 9309 快照中的引用 S1-P1@14667-15078。'
                    '先读取这条引用自身，判断它是否足以支持“缓存不应使用超过24小时，除非 robots.txt 不可达”。'
                    '若引用不足，再展开同一快照的周边正文，给出能支持该声明的正确引用，保存当前核对结果。',
        'mode': 'real_model_prior_real_snapshot',
        'seed_file': 'reference-review.json',
        'expected': ['exact reference read excludes the 24-hour clause',
                     'context expansion acquires the clause with a new reference',
                     'save a reference that actually contains the 24-hour condition and its exception'],
    },
}


def capture_deepseek_request(request, session):
    """按最近的开始记录区分研究与摘要；保存公开请求和实际发送的哈希。"""
    payload = json.loads(request.content)
    public = {**payload, 'messages': [{k: v for k, v in message.items() if k != 'reasoning_content'}
                                    for message in payload['messages']]}
    run_id = session.last_run['id']
    started = next(r for r in reversed(session.records)
                   if r.get('run_id') == run_id and r['kind'] in {'model_started', 'compaction_started'})
    purpose = 'model' if started['kind'] == 'model_started' else 'compaction'
    if purpose == 'model':
        selection = next(r for r in reversed(session.records)
                         if r.get('run_id') == run_id and r['kind'] == 'context_selected')
        if selection.get('request_sha256'):
            encoded = json.dumps({'messages': payload['messages'], 'tools': payload.get('tools', [])},
                                 ensure_ascii=False)
            assert hashlib.sha256(encoded.encode()).hexdigest() == selection['request_sha256']
        assert session.last_run['_request_messages'] == public['messages']
    return {'provider': 'deepseek', 'purpose': purpose,
            'request_sha256': hashlib.sha256(request.content).hexdigest(),
            'request_characters': len(request.content.decode()),
            'message_count': len(payload['messages']), 'payload': public,
            'reasoning_characters_sent': sum(len(m.get('reasoning_content', '')) for m in payload['messages'])}


def run_case(case_name, output, *, seed=None, max_context_chars=120000, thinking=True, reasoning_effort='high', cancel=None):
    """seed 供局部决策实验使用；保留原记录和构造说明，不计作从零自主运行。"""
    if output.exists():
        raise ValueError('输出文件已存在，请选择新路径')
    case = CASES[case_name]
    if seed is None and case.get('seed_file'):
        source = ROOT / 'evals/fixtures' / case['seed_file']
        prior = json.loads(source.read_text(encoding='utf-8'))
        seed = {'records': prior['records'], 'kind': 'prior_real_session_new_task', 'start_new_task': True,
                'source': source.name, 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                'interpretation': 'Explicit citation-verification task over a previous real snapshot; does not prove autonomous citation checking.'}
    new_task = bool(seed and seed.get('start_new_task'))
    config = read_config(ROOT / '.env')
    requests, trace = [], []
    started = time.monotonic()
    source_hashes = {p.relative_to(ROOT / 'src/seekra').as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in (ROOT / 'src/seekra').rglob('*.py')}
    with tempfile.TemporaryDirectory(prefix='seekra-doc-check-') as directory, httpx.Client() as remote:
        if seed is None:
            session = Session.create(Path(directory))
        else:
            path = Path(directory) / 'seed.jsonl'
            path.write_text(''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in seed['records']), encoding='utf-8')
            session = Session.load(path)
            if not new_task and session.last_run['question'] != case['question']:
                raise ValueError('种子记录与当前用例问题不一致')
        initial_calls = 0 if new_task else (session.last_run or {}).get('calls', 0)

        def transport(request):
            payload = json.loads(request.content)
            if request.url.host == 'api.tavily.com':
                requests.append({'provider': 'tavily', 'path': request.url.path,
                                 'parameters':payload,'fixture':'page' in case or 'pages' in case})
                if 'page' in case or 'pages' in case:
                    if request.url.path == '/search':
                        return httpx.Response(200, json={'results': [{
                            'title':case.get('title','Meridian 2.0 guide' if case_name=='long-section' else 'Northstar export policy'),
                            'url': case.get('url', FIXTURE_URL), 'content': case['snippet']}]})
                    if request.url.path == '/extract':
                        if 'pages' in case:
                            return httpx.Response(200,json={'results':[
                                {'url':url,'raw_content':case['pages'][url]} for url in payload['urls']
                                if url in case['pages']], 'failed_results':[
                                {'url':url,'error':'固定测试资料未提供此 URL 的页面；这不是对真实服务的请求。'}
                                for url in payload['urls'] if url not in case['pages']]})
                        return httpx.Response(200, json={'results': [
                            {'url': case.get('url', FIXTURE_URL), 'raw_content': case['page']}], 'failed_results': []})
                    raise AssertionError('测试只提供 Search 和 Extract')
            elif request.url.host == 'api.deepseek.com':
                requests.append(capture_deepseek_request(request, session))
            else:
                raise AssertionError('出现非预期的服务地址')
            return remote.send(request, stream=True)

        def event(name, **details):
            if name == 'context':
                row = {'event': name, **{k:details[k] for k in ('request_characters', 'checkpoint_requested',
                       'checkpoint_record_id', 'compacted_messages', 'research_revision') if k in details}}
                trace.append(row)
                if details.get('checkpoint_requested') or details.get('compacted_messages'):
                    print(json.dumps(row, ensure_ascii=False), flush=True)
            elif name == 'tool_call':
                function = details['tool_call']['function']
                row = {'event': name, 'tool': function['name'], 'arguments': json.loads(function['arguments'])}
                trace.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
            elif name == 'tool_result':
                result = json.loads(details['result'])
                trace.append({'event': name, 'tool': details['tool_name'],
                              **{k: result[k] for k in ('status', 'document_id', 'section_ref', 'start', 'end',
                                                       'complete', 'error_code') if k in result}})

        answer, error = None, None
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            try:
                answer = run_agent(None if seed and not new_task else case['question'], client=client, model=config['DEEPSEEK_MODEL'],
                                   deepseek_api_key=config['DEEPSEEK_API_KEY'], tavily_api_key=config['TAVILY_API_KEY'],
                                   session=session, max_context_chars=max_context_chars,
                                   thinking=thinking, reasoning_effort=reasoning_effort, cancel=cancel,
                                   on_event=event, resume=bool(seed) and not new_task)
            except (Exception, KeyboardInterrupt) as exc:
                error = {'type': type(exc).__name__, 'message': execution_error_message(exc)}
        restored = Session.load(session.path)
        record = {'case': case_name, 'mode': case['mode'], 'question': case['question'],
                  'initial_calls': initial_calls,
                  'max_context_chars': max_context_chars,
                  'seed': {k:v for k,v in seed.items() if k != 'records'} if seed else None,
                  'model': config['DEEPSEEK_MODEL'], 'expected_checks': case['expected'],
                  'thinking':thinking, 'reasoning_effort':reasoning_effort if thinking else None,
                  'status': (session.last_run or {}).get('status'), 'answer': answer, 'error': error,
                  'seconds': round(time.monotonic()-started, 2), 'usage': session.usage_totals(),
                  'source_sha256': source_hashes, 'requests': requests, 'trace': trace,
                  'reload_same_messages': restored.last_run['messages'] == session.last_run['messages'],
                  'records': session.records,
                  'interpretation': 'expected_checks 用于人工核对；completed 不表示答案全部正确。固定资料用例不检验 Tavily 搜索质量；提供 URL 的用例检验读取而非自主发现来源，显式要求查找的用例不证明模型会自主优先查找。requests.payload 保存公开请求投影，省略 private reasoning；purpose 区分主研究 model 与独立摘要 compaction，主研究请求发送前核对完整消息与工具的 canonical request hash；两类请求均保存实际发送的哈希，推理只保留字符数。'}
        encoded = json.dumps(record, ensure_ascii=False, indent=2)
        for key in (config['DEEPSEEK_API_KEY'], config['TAVILY_API_KEY']):
            if key in encoded or json.dumps(key, ensure_ascii=False)[1:-1] in encoded:
                raise RuntimeError('结果中出现凭据，停止保存')
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('x', encoding='utf-8') as file:
            file.write(encoded + '\n')
        print(json.dumps({k: record[k] for k in ('case', 'mode', 'status', 'seconds', 'answer', 'error',
                                                'reload_same_messages', 'usage')}, ensure_ascii=False, indent=2))
        return 0 if record['status'] == 'completed' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--case', choices=CASES)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--max-context-chars', type=int, default=120000)
    args = parser.parse_args()
    if args.list:
        for name, case in CASES.items():
            print(f'{name}: {case["mode"]} — {case["question"]}')
        return
    if args.case is None or args.output is None:
        parser.error('运行需要 --case 和新的 --output 路径')
    return run_case(args.case, args.output, max_context_chars=args.max_context_chars)


if __name__ == '__main__':
    sys.exit(main())
