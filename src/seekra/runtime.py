"""模型与运行的公共接口，不依赖 provider 或终端。"""
from dataclasses import dataclass
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


class Model(Protocol):
    identity: str

    def generate(self, messages: list[dict], tools: list[dict], *,
                 on_content: Callable, on_usage: Callable, cancel: CancellationToken) -> ModelResponse: ...


@dataclass
class RunResult:
    run_id: str
    status: str
    answer: str | None = None
    error: str | None = None
    cause: BaseException | None = None
