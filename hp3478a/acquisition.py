# -*- coding: utf-8 -*-

"""
Data Acquisition Controller for the HP 3478A DMM.

This module provides thread-based, cancelable background sampling from the hardware
or simulated device, handles TraceBuffer history sizing, and coordinates CSV logging.

Part of the HP 3478A DMM GUI project.
Distributed under the terms of the MIT license.

Author: Lorenzo Mariani <newmarioman@hotmail.it>
"""

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# Local Packages
from .instrument import HP3478A, QueryTimeout
from .models import DmmStatus, Measurement, TraceBuffer
from .storage import CsvCaptureLogger


@dataclass(frozen=True)
class AcquisitionOptions:
    interval: float = 0.0
    history: int = 100
    log_path: Path | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.interval) or not 0 <= self.interval <= 3600:
            raise ValueError("Acquisition interval must be MAX or less/equal 3600 seconds.")
        TraceBuffer(self.history)

    @staticmethod
    def parse_interval(value: str) -> float:
        if value == "MAX":
            return 0.0
        try:
            interval = float(value)
        except ValueError as exc:
            raise ValueError("Select a valid acquisition interval.") from exc
        if not math.isfinite(interval) or not 0 < interval <= 3600:
            raise ValueError("Select MAX or an interval less/equal 3600 seconds.")
        return interval


@dataclass(frozen=True)
class AcquisitionResult:
    samples: int
    duration: float
    log_path: Path | None
    error: Exception | None = None


class AcquisitionController:
    """Own the sampling thread; callbacks must enqueue work and never touch Tk."""

    MAX_CONSECUTIVE_TIMEOUTS = HP3478A.MAX_CONSECUTIVE_TIMEOUTS

    def __init__(
        self,
        device: HP3478A,
        on_finished: Callable[[AcquisitionResult], None] | None = None,
        on_sample: Callable[[Measurement], None] | None = None,
        logger_factory: Callable = CsvCaptureLogger,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.device = device
        self.on_finished = on_finished or (lambda result: None)
        self.on_sample = on_sample or (lambda sample: None)
        self.logger_factory, self._clock = logger_factory, clock
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._running = False
        self._thread: threading.Thread | None = None
        self.buffer = TraceBuffer()

    @property
    def running(self) -> bool:
        # The GUI reads a state snapshot without waiting for startup I/O.
        return self._running

    @property
    def stopping(self) -> bool:
        return self.running and self._stop.is_set()

    def start(self, options: AcquisitionOptions) -> DmmStatus:
        with self._lock:
            if self._running or (self._thread is not None and self._thread.is_alive()):
                raise RuntimeError("An acquisition is already running.")
            # Clear before startup I/O so a later Stop request remains effective.
            self._stop.clear()
            status = self.device.refresh_status()
            logger = (
                self.logger_factory(options.log_path, status.function.unit)
                if options.log_path
                else None
            )
            self.buffer = TraceBuffer(options.history)
            self._running = True
            self._thread = threading.Thread(
                target=self._run, args=(options, logger), name="hp3478a-acquire", daemon=True
            )
            try:
                self._thread.start()
            except RuntimeError:
                self._running, self._thread = False, None
                if logger is not None:
                    logger.close()
                raise
            return status

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self, options: AcquisitionOptions, logger) -> None:
        start = self._clock()
        count, error, next_due = 0, None, start
        consecutive_timeouts = 0
        try:
            while not self._stop.is_set():
                try:
                    sample = self.device.read_measurement(
                        lambda: max(0.0, self._clock() - start),
                        keep_connection_on_timeout=True,
                    )
                except QueryTimeout as exc:
                    consecutive_timeouts += 1
                    self.device.trace(
                        "WARN",
                        f"++read timeout after {self.device.query_timeout:g} s "
                        f"({consecutive_timeouts}/{self.MAX_CONSECUTIVE_TIMEOUTS} consecutive); "
                        "sample skipped.",
                    )
                    if self._stop.is_set():
                        break
                    if consecutive_timeouts >= self.MAX_CONSECUTIVE_TIMEOUTS:
                        try:
                            self.device.disconnect()
                        finally:
                            raise QueryTimeout(
                                f"No response to {consecutive_timeouts} consecutive ++read queries "
                                f"(timeout {self.device.query_timeout:g} s each). "
                                "Acquisition stopped; connection closed."
                            ) from exc
                    # Retry immediately, without logging or scheduling a missing sample. 
                    continue
                consecutive_timeouts = 0
                if logger is not None:
                    logger.append(sample)
                self.buffer.append(sample)
                count += 1
                self.on_sample(sample)
                next_due += options.interval
                now = self._clock()
                next_due = max(next_due, now)
                if self._stop.wait(max(0.0, next_due - now)):
                    break
        except Exception as exc:
            error = exc
        finally:
            if logger is not None:
                try:
                    logger.close()
                except OSError as exc:
                    error = error or exc
            with self._lock:
                self._running = False
            self.on_finished(
                AcquisitionResult(count, max(0.0, self._clock() - start), options.log_path, error)
            )
