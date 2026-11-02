"""Run live or controlled-retrieval cases through the product application."""
import argparse
import asyncio
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import tempfile
import time

from langchain_core.callbacks import AsyncCallbackHandler

from seekra.app import Seekra
from seekra.config import load_secrets, load_settings
from seekra.terminal.display import display_text

ROOT = Path(__file__).resolve().parents[1]


def cases():
    return json.loads((ROOT / 'evals/cases.json').read_text(encoding='utf-8'))


class FixedRetrieval:
    """Return only the declared document or failure; never call a network service."""

    def __init__(self, case):
        self.url = case['url']
        self.error = case.get('retrieval_error')
        if bool(case.get('fixture')) == bool(self.error):
            raise ValueError('fixed case requires exactly one fixture or retrieval_error')
        self.content = None
        self.sha256 = None
        if case.get('fixture'):
            root = (ROOT / 'evals/fixtures').resolve()
            path = (ROOT / 'evals' / case['fixture']).resolve()
            if not path.is_relative_to(root):
                raise ValueError('fixture must be inside evals/fixtures')
            self.content = path.read_text(encoding='utf-8')
            self.sha256 = hashlib.sha256(self.content.encode('utf-8')).hexdigest()

    async def fetch(self, url):
        if url != self.url:
            return {'error': 'not_in_fixture'}
        if self.error:
            return deepcopy(self.error)
        return {'results': [{'url': url, 'raw_content': self.content}]}

    async def search(self, query, **kwargs):
        if self.error:
            return deepcopy(self.error)
        return {'results': [{'url': self.url, 'content': 'Fixed test document; fetch to read.'}]}


async def evaluate(case, settings, secrets, *, timeout=None, max_model_calls=None,
                   compaction_messages=None):
    case = deepcopy(case)
    if case['mode'] not in {'fixed', 'live'}:
        raise ValueError('unknown case mode')
    if compaction_messages is not None and (
        case['mode'] != 'fixed' or not case.get('fixture') or compaction_messages < 4
    ):
        raise ValueError('compaction diagnostic requires a fixed document and threshold >= 4')
    fixed = FixedRetrieval(case) if case['mode'] == 'fixed' else None
    with tempfile.TemporaryDirectory(prefix='seekra-eval-') as directory:
        settings = replace(settings, data_dir=Path(directory))
        app = Seekra(settings, secrets)
        if compaction_messages:
            from deepagents.middleware.summarization import SummarizationMiddleware
            from seekra.search.agent import create_search_agent
            from seekra.sdk.workspace import workspace_backend

            def compact_agent(model, retrieval, workspace, checkpointer):
                summary = SummarizationMiddleware(
                    model=model, backend=workspace_backend(workspace),
                    trigger=('messages', compaction_messages), keep=('messages', 2),
                )
                return create_search_agent(
                    model, retrieval, workspace, checkpointer, middleware=[summary],
                )

            app.agent_factory = compact_agent
        if fixed:
            app.retrieval_factory = lambda *args: fixed
            app.secrets = {**secrets, settings.tavily_key_env: 'fixed-no-network'}
        item = app.new_session()
        events = []
        started = time.monotonic()

        class CallGuard(AsyncCallbackHandler):
            raise_error = True
            count = 0

            async def on_chat_model_start(self, serialized, messages, **kwargs):
                self.count += 1
                if max_model_calls and self.count > max_model_calls:
                    raise RuntimeError('eval_model_call_limit')

        guard = CallGuard()

        def emit(event):
            events.append(asdict(event))

        result = None
        error = None
        try:
            if timeout:
                async with asyncio.timeout(timeout):
                    result = await app.run(item, case['question'], emit=emit, callbacks=[guard])
            else:
                result = await app.run(item, case['question'], emit=emit, callbacks=[guard])
        except Exception as exc:
            while isinstance(exc, BaseExceptionGroup):
                exc = exc.exceptions[0]
            error = f'{type(exc).__name__}: {exc}'
        from seekra.search.sources import Sources

        workspace = app.sessions.workspace(item)
        snapshots = [
            {**source, 'content': (workspace / source['path'].lstrip('/')).read_text(encoding='utf-8')}
            for source in Sources(workspace).list()
        ]
        return dict(
            case=case['id'], mode=case['mode'], question=case['question'],
            case_definition=case, fixture_sha256=fixed.sha256 if fixed else None,
            model=asdict(settings.models[settings.default_model]),
            seconds=time.monotonic() - started,
            result=asdict(result) if result else None, error=error,
            events=events, sources=snapshots, assessment='unreviewed',
            expected=case.get('expected', []),
            guards={'timeout': timeout, 'max_model_calls': max_model_calls},
            compaction_messages=compaction_messages,
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--case', help='Required unless --list; see evals/cases.json')
    parser.add_argument('--config', type=Path, default=Path('seekra.toml'))
    parser.add_argument('--env-file', type=Path, default=Path('.env'))
    parser.add_argument('--output', type=Path, help='Optional diagnostic record; never overwrite')
    parser.add_argument('--timeout', type=float)
    parser.add_argument('--max-model-calls', type=int)
    parser.add_argument('--compaction-messages', type=int,
                        help='Fixed-document diagnostic: native SDK summary threshold')
    args = parser.parse_args(argv)
    if args.list:
        for case in cases():
            print(f"{case['id']}\t{case['mode']}\t{case['capability']}")
        return 0
    if not args.case:
        parser.error('choose --case explicitly, or use --list')
    case = next((case for case in cases() if case['id'] == args.case), None)
    if case is None:
        parser.error('unknown case')
    if args.compaction_messages is not None and (
        case['mode'] != 'fixed' or not case.get('fixture') or args.compaction_messages < 4
    ):
        parser.error('compaction diagnostic requires a fixed document and threshold >= 4')
    if (args.timeout is not None and args.timeout <= 0) or (
        args.max_model_calls is not None and args.max_model_calls < 1
    ):
        parser.error('limits must be positive')
    if args.output and args.output.exists():
        parser.error('output already exists')
    secrets = load_secrets(args.env_file)
    report = asyncio.run(evaluate(
        case, load_settings(args.config), secrets, timeout=args.timeout,
        max_model_calls=args.max_model_calls, compaction_messages=args.compaction_messages,
    ))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    for name, value in secrets.items():
        if name.endswith('API_KEY') and value:
            text = text.replace(json.dumps(value, ensure_ascii=False)[1:-1], '[redacted]')
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as handle:
            handle.write(text + '\n')
    print(display_text(
        report['result']['answer'] if report['result'] else report['error'],
        tuple(value for name, value in secrets.items() if name.endswith('API_KEY')),
    ))
    print(f"{args.case}: {report['seconds']:.1f}s; assessment=unreviewed")
    return int(report['error'] is not None)


if __name__ == '__main__':
    raise SystemExit(main())
