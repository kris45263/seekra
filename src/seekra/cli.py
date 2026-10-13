"""命令行入口：从项目 .env 读取配置，运行单次搜索或复用会话连续输入。"""

import argparse
from importlib.metadata import version
import io
from pathlib import Path
import re
import sys

from dotenv import dotenv_values
import httpx

from .agent import run_agent, execution_error_message
from .session import Session
from .display import AgentDisplay, display_text, session_transcript
from .input import ReplInput
from .deepseek import StreamProtocolError
from .runtime import ContentOutputError
from .retrieval import TavilyTransport


REPL_HELP = """/help  查看帮助
/new   开始新对话
/sessions  查看对话列表
/open ID或序号   打开一段对话
/resume    回到最近聊过的对话
/continue  继续上次未完成的回答
/exit  退出 Seekra

Enter 发送；Shift+Enter 换行，终端不支持时可用 Ctrl+J。
Ctrl+C：运行时停止，空闲时清空输入，输入为空时退出。空闲且输入为空时也可用 Ctrl+D 退出。
滚轮查看，PageUp/PageDown 翻页；Ctrl+Home/End 跳到输入内容首尾。
Windows Terminal：Shift 拖选后 Ctrl+Shift+C 复制。"""


def read_config(path: Path, *, provider='deepseek') -> dict[str, str]:
    """仅从指定 .env 读取所选服务需要的配置，不回退到系统环境。"""
    if not path.is_file():
        raise ValueError(f"没有找到配置文件：{path.resolve()}。请复制 .env.example 为 .env 并填写配置，"
                         "或使用 --env-file 指定文件。")
    values = dotenv_values(path, interpolate=False, encoding="utf-8-sig")
    config = {}
    names = ('TAVILY_API_KEY',) if provider == 'chatgpt' else ('DEEPSEEK_API_KEY', 'DEEPSEEK_MODEL', 'TAVILY_API_KEY')
    for name in names:
        value = (values.get(name) or "").strip()
        if not value or "${" in value:
            raise ValueError(f"配置项 {name} 未填写或格式无效。请检查 {path}。")
        config[name] = value
    return config


def run_question(question: str, *, client: httpx.Client, config: dict,
                 session: Session, verbose: bool, structured_think: bool = False,
                 resume: bool = False, max_context_chars: int = 120000, cancel=None) -> int:
    """执行一个问题并报告错误；失败时由调用者决定退出还是继续输入。"""
    if resume and (session.last_run is None or session.last_run['status'] == 'completed'):
        print('这段对话没有需要继续的回答，可以直接提问。', file=sys.stderr)
        return 1
    view = AgentDisplay(verbose, secrets=(config.get('DEEPSEEK_API_KEY', ''), config['TAVILY_API_KEY']))
    usage_before = session.usage_totals()
    started = False
    storage_or_output_failed = False
    def event(name, **details):
        nonlocal started
        if name == 'start':
            started = True
            print('\n── Seekra ──', file=sys.stderr, flush=True)
        view.event(name, **details)
    try:
        if config.get('MODEL_PROVIDER') == 'chatgpt':
            from .chatgpt import run_chatgpt
            from .chatgpt_auth import ChatGPTAuth
            answer = run_chatgpt(question, client=client, session=session,
                tavily_api_key=config['TAVILY_API_KEY'], auth=ChatGPTAuth(profile=config.get('CHATGPT_PROFILE', 'default')),
                on_content=view.content, on_event=event, resume=resume,
                max_context_chars=max_context_chars, cancel=cancel)
        else:
            answer = run_agent(
                question, client=client, session=session, model=config["DEEPSEEK_MODEL"],
                deepseek_api_key=config["DEEPSEEK_API_KEY"], tavily_api_key=config["TAVILY_API_KEY"],
                structured_think=structured_think,
                thinking=True,
                on_content=view.content, on_event=event,
                resume=resume, max_context_chars=max_context_chars, cancel=cancel,
            )
        # 崩溃前最终响应已落盘，恢复只补完成标记时没有新流片段。
        if resume and view.fragments == 0 and answer:
            view.content(answer)
            view.writer.finish()
        return 0
    except StreamProtocolError:
        print('这次没能收到有效的完整回答。', file=sys.stderr)
    except (ValueError, RuntimeError) as exc:
        if session._storage_failed:
            storage_or_output_failed = True
            print('会话保存失败，部分执行记录可能未保存，已停止当前任务。', file=sys.stderr)
            return 74
        print(f"错误：{exc}", file=sys.stderr)
    except httpx.HTTPStatusError as exc:
        print(f'错误：{execution_error_message(exc)}', file=sys.stderr)
    except (httpx.RequestError, KeyError, IndexError, TypeError) as exc:
        print(f'错误：{execution_error_message(exc)}', file=sys.stderr)
    except KeyboardInterrupt:
        print("已停止。", file=sys.stderr)
        return 130
    except ContentOutputError as exc:
        storage_or_output_failed = True
        print(execution_error_message(exc), file=sys.stderr)
        return 74
    except (OSError, UnicodeError):
        storage_or_output_failed = True
        print('会话保存失败，部分执行记录可能未保存，已停止当前任务。' if session._storage_failed
              else '本地读写或终端输出失败，已停止当前任务。', file=sys.stderr)
        return 74
    finally:
        if started:
            view.usage_summary(usage_before, session.usage_totals())
            if not storage_or_output_failed and (session.last_run or {}).get('status') != 'interrupted':
                hint = continuation_hint(session)
                if hint:
                    print(hint, file=sys.stderr)
    return 1


def continuation_hint(session):
    run = session.last_run
    if run is None or run['status'] == 'completed':
        return ''
    if run.get('started_tools'):
        names = [run['pending'][key]['function']['name'] for key in sorted(run['started_tools']) if key in run['pending']]
        return f'上次工具结果未知（{", ".join(names) or "未知工具"}），不能自动重做。可以提交新问题。'
    messages = run.get('messages', [])
    if messages and messages[-1]['role'] == 'assistant' and not messages[-1].get('tool_calls') and messages[-1].get('content'):
        return '回答已保存，输入 /continue 完成恢复即可。'
    if run['status'] == 'paused':
        code = run.get('error_code')
        # 旧记录没有错误码，只在明确的历史原因上识别次数限制。
        if code == 'iteration_limit' or (not code and (run.get('error') or '').startswith('达到 ')):
            return '上次因旧版调用次数限制暂停。当前不设次数限制，但续接仍要求原模型、提示词和工具配置一致；配置已变时请使用原版本续接，或提交新问题回查保存资料。'
        if code == 'context_limit' or (not code and '上下文字符上限' in (run.get('error') or '')):
            if session.path is None:
                return '请求内容超过上下文限制；可以提交较短的新问题。当前未保存到磁盘，退出后无法续接。'
            return f'请求内容超过上下文限制。可退出后调整 --max-context-chars，并用 --continue {session.id} 续接；调大本地上限不保证模型服务能够接受。'
        return '上次回答已暂停。请先处理上方提示的问题。'
    return '回答还没完成。输入 /continue 继续，或直接提出新问题。'


def main() -> int:
    """启动单次问答或 REPL，共用配置、客户端和当前内存会话。"""
    # 命令入口统一使用 UTF-8 处理文本输入输出。
    # 仅配置真实文本流；嵌入调用和测试提供的自定义输出对象保持原样。
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        prog="seekra", description="Seekra · 终端搜索研究助手", add_help=False,
        epilog='连续对话：seekra --repl  |  单次提问：seekra "你的问题"',
    )
    parser.add_argument("-h", "--help", action="help", help="查看命令帮助并退出")
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('seekra')}",
                        help="查看版本并退出")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), metavar="PATH",
                        help="配置文件路径，默认读取当前工作目录的 .env")
    parser.add_argument("question", nargs="?", help="单次研究问题，请用引号包住")
    parser.add_argument("--repl", action="store_true", help="启动连续对话，默认自动保存会话")
    parser.add_argument("--verbose", action="store_true", help="显示工具参数、用量和计时等详细诊断")
    parser.add_argument('--provider', choices=('deepseek', 'chatgpt'), default='deepseek',
                        help='模型服务；chatgpt 使用已授权订阅的 gpt-6.1-sol / medium')
    parser.add_argument('--chatgpt-login', action='store_true', help='在浏览器中授权 Seekra 使用 ChatGPT 套餐')
    parser.add_argument('--chatgpt-profile', default='default', help='本地 ChatGPT 账号标签，默认 default')
    parser.add_argument("--session-dir", type=Path, default=Path('.seekra/sessions'), help="本地会话目录，默认启动目录下 .seekra/sessions")
    parser.add_argument("--session", metavar="ID", help="打开已有会话后提问或进入 REPL")
    parser.add_argument("--resume", nargs='?', const='', metavar="ID", help="打开最近有内容的会话进入 REPL，也可指定 ID")
    parser.add_argument("--continue", dest='continue_session', metavar="ID", help="接续指定会话最后一次未完成执行")
    parser.add_argument("--sessions", action="store_true", help="列出本地会话，无需密钥")
    parser.add_argument("--no-session", action="store_true", help="不保存到磁盘，仅使用内存会话")
    parser.add_argument("--max-context-chars", type=int, default=120000, help="请求消息和工具定义的字符上限（非 token），默认 120000")
    args = parser.parse_args()
    if args.chatgpt_login:
        if args.question or args.repl or args.session or args.resume is not None or args.continue_session or args.sessions:
            parser.error('--chatgpt-login 请单独使用，不与研究或会话操作混用')
        from .chatgpt_auth import ChatGPTAuth
        try:
            auth = ChatGPTAuth(profile=args.chatgpt_profile)
            print('Continue with ChatGPT：将打开浏览器，请确认 Seekra 的套餐使用权限。', file=sys.stderr)
            with httpx.Client() as login_client:
                auth.login(login_client)
            print(f'ChatGPT 授权已验证，账号标签：{auth.profile}。凭据保存在 {auth.path}。', file=sys.stderr)
            print('应用用量与撤销授权：https://chatgpt.com/settings/usage', file=sys.stderr)
            return 0
        except (ValueError, RuntimeError, OSError, httpx.HTTPError) as exc:
            print(f'ChatGPT 登录未完成：{exc if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__}', file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print('登录已取消。', file=sys.stderr)
            return 130
    if args.resume is not None:
        if args.question is not None or args.sessions or args.continue_session or args.session or args.no_session:
            parser.error("--resume 不能与问题、--sessions、--continue、--session 或 --no-session 一起使用")
        args.repl = True
    if sum((args.repl,args.question is not None,args.continue_session is not None,args.sessions)) != 1:
        parser.error("问题、--repl、--resume、--continue、--sessions 请选择一种运行方式")
    if args.no_session and (args.session or args.continue_session or args.sessions):
        parser.error("--no-session 不能与 --session、--continue 或 --sessions 一起使用")
    if args.session and (args.continue_session or args.sessions):
        parser.error("--session 不能与 --continue 或 --sessions 一起使用")
    if args.max_context_chars < 2000:
        parser.error("上下文字符上限至少为 2000")

    listed_session_ids = []
    def list_sessions():
        rows=[row for row in Session.list_saved(args.session_dir) if row['question'] or row['status'] in {'corrupt','unsupported'}]
        listed_session_ids[:] = [row['id'] for row in rows]
        if not rows:
            print('还没有聊过的对话。',file=sys.stderr)
        for number, row in enumerate(rows, 1):
            state = {'completed':'已完成','interrupted':'已停止','failed':'未完成','paused':'已暂停',
                     'needs_attention':'需要处理','running':'未记录结束','corrupt':'记录损坏',
                     'unsupported':'格式不兼容','ready':'尚未提问'}.get(row['status'],row['status'])
            print(display_text(f"{number}. {row['id']}  {state}  {row['question']}",single_line=True),file=sys.stderr)
        if rows and args.repl:
            print('输入 /open 序号 打开对话，也可以使用 ID。', file=sys.stderr)

    def open_session(identifier):
        if not re.fullmatch(r'[a-f0-9]{8,32}',identifier):
            raise ValueError('这个对话编号无法识别。请使用至少 8 位的 ID，或从 /sessions 中选择序号。')
        matches=list(args.session_dir.glob(identifier+'*.jsonl'))
        if not matches:
            raise ValueError('没有找到这段对话，请用 /sessions 查看现有记录')
        if len(matches)>1:
            raise ValueError('编号前缀对应多段对话，请多输入几位或使用列表序号')
        return Session.load(matches[0])

    def recent_session_id():
        for row in Session.list_saved(args.session_dir):
            if row['question'] and row['status'] != 'corrupt':
                return row['id']
        return None

    if args.sessions:
        list_sessions()
        return 0
    try:
        config = read_config(args.env_file, provider=args.provider) if args.provider == 'chatgpt' else read_config(args.env_file)
        if args.provider == 'chatgpt':
            from .chatgpt_auth import ChatGPTAuth
            auth = ChatGPTAuth(profile=args.chatgpt_profile)
            saved = auth.read()
            if not saved.get('access_token'):
                raise ValueError('尚未登录 ChatGPT，请先运行 seekra --chatgpt-login。')
            config.update(MODEL_PROVIDER='chatgpt', CHATGPT_PROFILE=args.chatgpt_profile)
            print(f'模型：ChatGPT / gpt-6.1-sol · medium · 账号标签：{auth.profile}', file=sys.stderr)
    except (ValueError, OSError) as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 1
    try:
        identifier = (args.resume or recent_session_id()) if args.resume is not None else (args.session or args.continue_session)
        session = open_session(identifier) if identifier else (
            Session() if args.no_session else Session.create(args.session_dir))
    except (OSError,ValueError,RuntimeError) as exc:
        print(f'会话打开失败：{exc}',file=sys.stderr)
        return 1
    if session.path and not args.repl:
        print(f'会话：{session.id}（自动保存）',file=sys.stderr)
    with httpx.Client(mounts={'https://api.tavily.com': TavilyTransport()}) as client:
        if not args.repl:
            return run_question(args.question, client=client, config=config, session=session,
                                verbose=args.verbose,
                                resume=args.continue_session is not None, max_context_chars=args.max_context_chars)
        editor = ReplInput()
        def show_session(message=''):
            secrets = tuple(config.get(key, '') for key in ('DEEPSEEK_API_KEY', 'TAVILY_API_KEY'))
            transcript = session_transcript(session, secrets)
            if session.recovered_tail:
                transcript += '\n已载入完整记录；末尾一段未写完整的记录未载入。\n'
            hint = continuation_hint(session)
            if hint and hint != (session.last_run or {}).get('error'):
                transcript += '\n' + hint + '\n'
            if message:
                transcript += '\n' + message + '\n'
            if not transcript.strip():
                transcript = ('想查什么？输入问题开始吧。\n' if args.no_session
                              else '想查什么？输入问题开始，或用 /resume 接着上次聊。\n')
            editor.set_conversation(session.id, transcript)
        show_session('还没有可以接着聊的对话。直接输入问题开始吧。'
                     if args.resume == '' and identifier is None else '')
        def handle(question, cancel):
            nonlocal session
            if not question.strip():
                return None
            command = question.strip()
            if '\n' in command or '\r' in command:
                command = ''  # 多行资料中出现 /new 等字符串不能执行会话命令。
            if command == "/exit":
                return 0
            if command == "/help":
                print(REPL_HELP + "\n", file=sys.stderr)
                return None
            if command == "/new":
                try:
                    session = Session() if args.no_session else Session.create(args.session_dir)
                    show_session('新对话准备好了。')
                except OSError as exc:
                    print(f'创建会话失败：{type(exc).__name__}',file=sys.stderr)
                return None
            if command == '/sessions':
                list_sessions()
                return None
            if command == '/open':
                print('要打开哪段对话？先用 /sessions 查看，再输入 /open 序号或ID。', file=sys.stderr)
                return None
            if command == '/resume':
                if args.no_session:
                    print('当前是临时对话模式。退出后去掉 --no-session，即可打开保存的对话。', file=sys.stderr)
                    return None
                try:
                    identifier = recent_session_id()
                    if identifier is None:
                        print('还没有可以接着聊的对话。直接输入问题开始吧。', file=sys.stderr)
                        return None
                    session = open_session(identifier)
                    show_session('回到上次的对话了。' if session.last_run and session.last_run['status']!='completed'
                                 else '回到上次的对话了，接着聊吧。')
                except (ValueError, OSError, RuntimeError) as exc:
                    print(f'恢复会话失败：{exc}', file=sys.stderr)
                return None
            if command.startswith('/open '):
                if args.no_session:
                    print('当前是临时对话模式。退出后去掉 --no-session，即可打开保存的对话。', file=sys.stderr)
                    return None
                try:
                    identifier = command.split(maxsplit=1)[1]
                    if identifier.isdecimal() and len(identifier) < 8:
                        index = int(identifier)-1
                        if not 0 <= index < len(listed_session_ids):
                            raise ValueError('当前列表中没有这个序号。输入 /sessions 重新查看。')
                        identifier = listed_session_ids[index]
                    session=open_session(identifier)
                    show_session('已打开这段对话。')
                except (ValueError,OSError) as exc:
                    print(f'会话打开失败：{exc}',file=sys.stderr)
                return None
            if command.startswith("/") and command != '/continue':
                print("Seekra 不认识这个命令。输入 /help 看看可用命令。", file=sys.stderr)
                return None
            # run_question 报告失败后返回；Session 保留此前成功历史，继续等待输入。
            if command != '/continue' and session.last_run and session.last_run['status'] != 'completed':
                print('开始新问题后，/continue 将不再接续之前未完成的回答。', file=sys.stderr)
            status = run_question(None if command == '/continue' else question, client=client, config=config, session=session,
                         verbose=args.verbose,
                         resume=command == '/continue', max_context_chars=args.max_context_chars, cancel=cancel)
            if status == 74:
                return status
            return None

        result = editor.run(handle)
        print("\n会话已结束。", file=sys.stderr)
        return result


if __name__ == "__main__":
    raise SystemExit(main())
