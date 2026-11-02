"""Bridge a terminal thread's Ctrl+C to the application's asyncio task."""
from contextlib import contextmanager
import threading


class CancellationToken:
    def __init__(self):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks = []

    @property
    def cancelled(self):
        return self._event.is_set()

    def cancel(self):
        self._event.set()
        with self._lock:
            callbacks = list(self._callbacks)
        for callback in callbacks:
            callback()

    @contextmanager
    def watch(self, callback):
        with self._lock:
            self._callbacks.append(callback)
        try:
            if self.cancelled:
                callback()
            yield
        finally:
            with self._lock:
                self._callbacks.remove(callback)
