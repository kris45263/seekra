"""显式运行一个真实验收问题，保存答案与操作摘要；默认测试发现不会执行此脚本。"""

import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]

import httpx

from seekra.agent import run_agent
from seekra.cli import read_config
from seekra.session import Session
from seekra.display import AgentDisplay


def main() -> int:
    """每次使用独立 Session；输出位置由调用者明确指定，不覆盖已有记录。"""
    cases = json.loads((ROOT / "evals/cases.json").read_text(encoding="utf-8"))
    parser = argparse.ArgumentParser(description="真实 API 验收（会产生服务调用费用）")
    parser.add_argument("--list", action="store_true", help="列出问题，不调用 API")
    parser.add_argument("--case", choices=[case["id"] for case in cases])
    parser.add_argument("--output", type=Path, help="保存本次记录的新 JSON 文件路径")
    args = parser.parse_args()
    if args.list:
        for case in cases:
            print(f"{case['id']}: {case['question']}")
        return 0
    if not args.case or args.output is None:
        parser.error("需要 --case 和 --output，或使用 --list")
    if args.output.exists():
        parser.error("输出文件已经存在，请选择新路径")
    case = next(case for case in cases if case["id"] == args.case)
    config = read_config(ROOT / ".env")
    latest_messages = []
    model_calls = 0

    def capture(request):
        """只读取模型请求的消息，不记录认证头；失败时也保留已回传的操作。"""
        nonlocal latest_messages, model_calls
        if request.url.host == "api.deepseek.com":
            model_calls += 1
            latest_messages = json.loads(request.content)["messages"]

    session = Session()
    start = time.monotonic()
    answer = None
    error = None
    view = AgentDisplay(secrets=(config['DEEPSEEK_API_KEY'], config['TAVILY_API_KEY']))
    try:
        with httpx.Client(event_hooks={"request": [capture]}) as client:
            answer = run_agent(case["question"], client=client, model=config["DEEPSEEK_MODEL"],
                               deepseek_api_key=config["DEEPSEEK_API_KEY"], tavily_api_key=config["TAVILY_API_KEY"],
                               session=session,
                               on_content=view.content, on_event=view.event)
    except (Exception, KeyboardInterrupt) as exc:
        error = type(exc).__name__
    trace = []
    for message in session.messages or latest_messages:
        for call in message.get("tool_calls") or []:
            trace.append({"call_id": call["id"], "name": call["function"]["name"], "arguments": call["function"]["arguments"]})
        if message["role"] == "tool":
            result = json.loads(message["content"])
            trace.append({"call_id": message["tool_call_id"], "status": result.get("status"),
                          "operation": result.get("operation"), "error_code": result.get("error_code"),
                          "error": result.get("error"), "requested_url": result.get("requested_url"),
                          "url": result.get("url"), "start": result.get("start"), "end": result.get("end"),
                          "next_start": result.get("next_start"),
                          "content": result.get("content"),
                          "query": result.get("query"), "search_parameters": result.get("search_parameters"),
                          "sources": [{"url": row["url"], "title": row["title"]} for row in result.get("results", [])]})
    record = {**case, "model": config["DEEPSEEK_MODEL"], "streaming": True, "model_calls": model_calls,
              "seconds": round(time.monotonic() - start, 2), "answer": answer, "error": error, "trace": trace,
              "judgment": "待人工按 docs/validation.md 核对；正常退出不等于答案正确。"}
    text = json.dumps(record, ensure_ascii=False, indent=2)
    # 记录只是验收产物，隐藏意外回显的本次密钥，不改变实际模型请求。
    for key in (config["DEEPSEEK_API_KEY"], config["TAVILY_API_KEY"]):
        text = text.replace(json.dumps(key, ensure_ascii=False)[1:-1], "[已隐藏密钥]")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(text + "\n")
    print(f"用例：{args.case}；模型调用：{model_calls}；运行错误：{error}；记录：{args.output}")
    return 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())
