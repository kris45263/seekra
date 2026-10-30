"""供应商共用的 SSE 事件边界解析。"""
from collections.abc import Iterator
import httpx

class StreamProtocolError(RuntimeError):
    """只携带本地说明，不携带模型原始数据的协议错误。"""


def iter_sse_data(response: httpx.Response) -> Iterator[str]:
    """以空行完成事件，支持多行 data、注释和网络任意分片。"""
    data = []
    for line in response.iter_lines():
        if line == "":
            if data:
                yield "\n".join(data)
                data.clear()
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if field == "data":
            data.append(value.removeprefix(" ") if separator else "")
    # 未以空行终结的事件不是完整事件，尤其不能接受残缺的 DONE。
    if data:
        raise StreamProtocolError("SSE 流事件未完整结束。")
