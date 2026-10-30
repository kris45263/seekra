"""无工具流式开发验收：显式运行会调用 DeepSeek，不是第二套产品 Agent。"""

from copy import deepcopy
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]

from dotenv import dotenv_values
import httpx

from seekra.providers.deepseek_stream import StreamProtocolError, stream_chat_text
from seekra.terminal.display import TextStreamWriter


def read_config(path: Path) -> dict[str, str]:
    """只读项目文件中两个 DeepSeek 字段，拒绝缺失或环境变量引用。"""
    values = dotenv_values(path, interpolate=False, encoding="utf-8-sig")
    config = {}
    for name in ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL"):
        value = (values.get(name) or "").strip()
        if not value or "${" in value:
            raise ValueError(f"请在项目 .env 中直接填写 {name}")
        config[name] = value
    return config


def main() -> int:
    """最小交互验收：成功才保留历史，取消后可再次输入。"""
    try:
        config = read_config(ROOT / ".env")
    except (ValueError, OSError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else "无法读取项目 .env"
        print(f"配置错误：{reason}", file=sys.stderr)
        return 1
    messages = [{"role": "system", "content":
                 "请按用户的语言回答并结合此前对话。当前没有检索工具，不要声称已实时查证。"}]
    print("无工具流式开发验收（使用真实 DeepSeek API）。/exit 退出，生成时 Ctrl+C 取消当前回答。",
          file=sys.stderr, flush=True)
    with httpx.Client() as client:
        while True:
            try:
                print("你：", end="", file=sys.stderr, flush=True)
                question = input().strip()
            except (EOFError, KeyboardInterrupt):
                print("\n验收已结束。", file=sys.stderr)
                return 0
            if question == "/exit":
                return 0
            if not question:
                continue
            pending = deepcopy(messages)
            pending.append({"role": "user", "content": question})
            writer = TextStreamWriter()
            print("正在思考…", file=sys.stderr, flush=True)
            start = time.monotonic()
            first = None
            count = 0

            def show(text: str) -> None:
                """记录真实片段到达时间并立即显示，不记录认证信息。"""
                nonlocal first, count
                count += 1
                writer.write(text)
                if first is None and text.strip():
                    first = time.monotonic() - start

            failure = None
            output_failed = False
            try:
                answer = stream_chat_text(client=client, model=config["DEEPSEEK_MODEL"],
                                          api_key=config["DEEPSEEK_API_KEY"], messages=pending,
                                          on_content=show)
                pending.append({"role": "assistant", "content": answer})
                messages = pending
            except KeyboardInterrupt:
                failure = "已取消，本次回答未完成，本次问答不会加入后续对话。"
            except httpx.HTTPStatusError as exc:
                failure = f"DeepSeek HTTP {exc.response.status_code}，本次回答未完成，本次问答不会加入后续对话。"
            except httpx.RequestError:
                failure = "DeepSeek 网络失败或超时，本次回答未完成，本次问答不会加入后续对话。"
            except (StreamProtocolError, ValueError):
                failure = "DeepSeek 响应不符合无工具流式协议，本次回答未完成，本次问答不会加入后续对话。"
            except (OSError, UnicodeError):
                output_failed = True
                failure = "正文输出失败，本次问答不会加入后续对话，停止验收。"
            # 仅输出失败时不再写同一个坏输出流；清理失败不能覆盖此前失败原因。
            if not output_failed:
                try:
                    writer.finish()
                except (OSError, UnicodeError):
                    output_failed = True
                    failure = (failure + " 终端输出也失败，停止验收。" if failure else
                               "回答已接收并保留，但终端结束行输出失败，停止验收。")
            if failure:
                print(failure, file=sys.stderr, flush=True)
                if output_failed:
                    return 1
                continue
            elapsed = time.monotonic() - start
            print(f"接收完成：首段正文 {first:.3f} 秒，总耗时 {elapsed:.3f} 秒，正文片段 {count} 段。",
                  file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
