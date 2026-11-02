"""Terminal entry point for the Deep Agents Search application."""
import argparse
import asyncio
from contextlib import nullcontext
from importlib.metadata import version
from pathlib import Path
import sys
from seekra.config import load_settings, load_secrets
from seekra.app import Seekra
from seekra.terminal.input import ReplInput
from seekra.terminal.display import AgentDisplay, display_text, session_transcript

REPL_HELP = '''/help 查看帮助；/new [模型档案] 新建；/sessions 列表；/open ID 打开
/resume 打开最近对话；/continue 继续未完成任务；/exit 退出
Enter 发送；Shift+Enter 或 Ctrl+J 换行；Ctrl+C 停止任务/清空草稿/退出。'''


def run_question(app, item, question, *, verbose=False, cancel=None):
    secrets = tuple(v for k, v in app.secrets.items() if k.endswith('API_KEY'))
    view = AgentDisplay(verbose, secrets)
    async def execute():
        loop, task = asyncio.get_running_loop(), asyncio.current_task()
        guard = cancel.watch(lambda: loop.call_soon_threadsafe(task.cancel)) if cancel else nullcontext()
        with guard:
            return await app.run(item, question, emit=view.event)
    try:
        result = asyncio.run(execute())
        if result.status != 'completed':
            print('任务已暂停；当前入口未启用人工审批工具。', file=sys.stderr)
            return 1
        return 0
    except (asyncio.CancelledError, KeyboardInterrupt):
        print('\n已停止；已保存进度以 SDK checkpoint 为准，可用 /continue 尝试继续。', file=sys.stderr)
        return 130
    except Exception as exc:
        # ExceptionGroup carries the original SDK stream exception, not just a generic wrapper.
        while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
            exc = exc.exceptions[0]
        print(display_text(f'\n执行失败：{type(exc).__name__}: {exc}', secrets), file=sys.stderr)
        return 1
    finally:
        view.finish()


def main(argv=None):
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='Seekra · Deep Agents 搜索助手')
    parser.add_argument('question', nargs='?')
    parser.add_argument('--version', action='version', version=version('seekra'))
    parser.add_argument('--config', type=Path, default=Path('seekra.toml'))
    parser.add_argument('--env-file', type=Path, default=Path('.env'))
    parser.add_argument('--model', help='新会话使用的模型档案')
    parser.add_argument('--repl', action='store_true')
    parser.add_argument('--verbose', action='store_true')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--session')
    group.add_argument('--resume', nargs='?', const='latest')
    group.add_argument('--continue', dest='continue_id')
    group.add_argument('--sessions', action='store_true')
    args = parser.parse_args(argv)
    if args.question and args.repl:
        parser.error('单次问题与 --repl 二选一')
    try:
        settings = load_settings(args.config)
        app = Seekra(settings, load_secrets(args.env_file))
        listing = app.sessions.list()
        if args.sessions:
            for item in listing:
                print(display_text(f"{item['id']}  {item['model']}  {item['title']}"))
            return 0
        selected = args.session or args.resume or args.continue_id
        if selected == 'latest':
            selected = listing[0]['id'] if listing else None
        item = app.sessions.get(selected) if selected else app.new_session(args.model)
        if selected and args.model and args.model != item['model']:
            raise ValueError('切换模型请新建会话')
        if args.continue_id:
            if args.question:
                parser.error('--continue 不接收新问题')
            return run_question(app, item, None, verbose=args.verbose)
        if args.question:
            return run_question(app, item, args.question, verbose=args.verbose)
        editor = ReplInput()
        editor.set_conversation(item['id'], session_transcript(asyncio.run(app.history(item))))
        def handle(text, cancel):
            nonlocal item
            stripped = text.strip()
            if editor._interactive and stripped.startswith('/'):
                command, _, value = stripped.partition(' ')
                if command == '/exit':
                    return 0
                if command == '/help':
                    print(REPL_HELP)
                elif command == '/new':
                    item = app.new_session(value.strip() or args.model)
                    editor.set_conversation(item['id'], '')
                elif command == '/sessions':
                    for row in app.sessions.list():
                        print(display_text(f"{row['id']}  {row['model']}  {row['title']}"))
                elif command in {'/open', '/resume'}:
                    rows = app.sessions.list()
                    target = value.strip() if command == '/open' else (rows[0]['id'] if rows else '')
                    item = app.sessions.get(target)
                    editor.set_conversation(item['id'], session_transcript(asyncio.run(app.history(item))))
                elif command == '/continue':
                    run_question(app, item, None, verbose=args.verbose, cancel=cancel)
                else:
                    print('未知命令，输入 /help 查看帮助。')
                return None
            if stripped:
                code = run_question(app, item, text, verbose=args.verbose, cancel=cancel)
                if not editor._interactive:
                    return code
            return None
        return editor.run(handle)
    except (ValueError, OSError, TypeError) as exc:
        print(display_text(str(exc)), file=sys.stderr)
        return 1
