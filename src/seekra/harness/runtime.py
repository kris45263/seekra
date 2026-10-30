"""模型与运行的公共接口，不依赖 provider 或终端。"""
from dataclasses import dataclass, field
from contextlib import contextmanager
import threading
from typing import Protocol, Callable


class RunCancelled(KeyboardInterrupt):
    """取消不是撤销已经发生的外部效果。"""


class ContentOutputError(OSError):
    """正文传递失败，与普通状态展示故障分开处理。"""


class CancellationToken:
    def __init__(self):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks = []

    def cancel(self):
        self._event.set()
        with self._lock:
            callbacks = list(self._callbacks)
        for callback in callbacks:
            try:
                callback()
            except (OSError, RuntimeError):
                pass

    @contextmanager
    def watch(self, callback):
        """适配器登记已取得的响应资源，取消时尽力主动关闭。"""
        with self._lock:
            self._callbacks.append(callback)
        try:
            if self.cancelled:
                callback()
                self.check()
            yield
        finally:
            with self._lock:
                self._callbacks.remove(callback)

    @property
    def cancelled(self):
        return self._event.is_set()

    def check(self):
        if self.cancelled:
            raise RunCancelled('当前任务已取消')


@dataclass(frozen=True)
class ModelResponse:
    message: dict
    finish_reason: str
    continuation: dict | None = None
    provider: dict | None = None


@dataclass(frozen=True)
class ModelContinuation:
    """与一条业务消息对应的续接快照；required 且 value 为 None 表示数据已缺失。"""
    required: bool
    value: dict | None = field(repr=False)


class Model(Protocol):
    identity: str

    def generate(self, messages: list[dict], tools: list[dict], *,
                 on_content: Callable, on_usage: Callable, cancel: CancellationToken) -> ModelResponse: ...


class ContextModel(Model, Protocol):
    """可选的模型上下文接口；投影和请求构造均为纯计算，不访问会话或网络。"""
    @property
    def can_rebuild_context(self) -> bool: ...

    def project_messages(self, messages: list[dict], continuations: tuple[ModelContinuation, ...]
                         ) -> tuple[list[dict], set[int]]:
        """在副本的 reasoning_content 中附加暂态续接，返回缺失续接的消息位置。"""
        ...

    def request_content(self, messages: list[dict], tools: list[dict]) -> dict:
        """返回 generate 实际发送的消息及工具内容，供硬预算检查和摘要计算。"""
        ...


@dataclass
class RunResult:
    run_id: str
    status: str
    answer: str | None = None
    error: str | None = None
    cause: BaseException | None = None
