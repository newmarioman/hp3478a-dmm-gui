# -*- coding: utf-8 -*-

"""
Asynchronous Multi-Threaded Task Workers and Tkinter Queue Event Loop.

This module provides thread-safe execution of long-running hardware I/O operations
using a single background ThreadPoolExecutor worker. It decouples core instrument
logic from the Tkinter main loop via task enqueuing and request coalescing.

Part of the HP 3478A DMM GUI project.
Distributed under the terms of the MIT license.

Author: Lorenzo Mariani <newmarioman@hotmail.it>
"""

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from queue import Empty, Queue
from typing import Any


class BackgroundExecutor:
    """Workers never call Tkinter, including root.after()."""

    def __init__(self, root, interval_ms: int = 30) -> None:
        self.root = root
        self.interval_ms = interval_ms
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hp3478a-io")
        self._callbacks: Queue = Queue()
        self._latest: dict[str, tuple[Callable, tuple]] = {}
        self._lock = threading.Lock()
        self._closed = False
        self._after_id = self.root.after(self.interval_ms, self._drain)

    def submit(
        self,
        operation: Callable[[], Any],
        success: Callable[[Any], None],
        error: Callable[[Exception], None],
    ) -> None:
        if self._closed:
            return

        def work() -> None:
            try:
                result = operation()
            except Exception as exc:
                self.post(error, exc)
            else:
                self.post(success, result)

        self._executor.submit(work)

    def post(self, callback: Callable, *args) -> None:
        if not self._closed:
            self._callbacks.put((callback, args))

    def post_latest(self, key: str, callback: Callable, *args) -> None:
        """Coalesce indicator and progress callbacks instead of accumulating stale updates."""
        with self._lock:
            if not self._closed:
                self._latest[key] = (callback, args)

    def shutdown(self) -> None:
        self._closed = True
        self.root.after_cancel(self._after_id)
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _drain(self) -> None:
        if self._closed:
            return
        for _ in range(500):
            try:
                callback, args = self._callbacks.get_nowait()
            except Empty:
                break
            callback(*args)
            if self._closed:
                return
        with self._lock:
            latest = tuple(self._latest.values())
            self._latest.clear()
        for callback, args in latest:
            callback(*args)
        self._after_id = self.root.after(self.interval_ms, self._drain)
