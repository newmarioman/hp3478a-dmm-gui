# -*- coding: utf-8 -*-

"""
Instrument Snapshots and Bounded Acquisition Data Independent of Tkinter.

This module contains the core thread-safe models and containers for processing
the HP 3478A status, active connection metadata, individual parsed measurements,
and the rolling historical TraceBuffer for plotting.

Part of the HP 3478A DMM GUI project.
Distributed under the terms of the MIT license.

Author: Lorenzo Mariani <newmarioman@hotmail.it>
"""

import math
import threading
from collections import deque
from dataclasses import dataclass

# Local Packages
from .config import DIGITS, FUNCTION_BY_CODE, FunctionSpec, RangeSpec


@dataclass(frozen=True)
class DmmStatus:
    raw: bytes
    function_code: int
    range_code: int
    digits_code: int
    autorange: bool
    autozero: bool
    line_hz: int
    front_input: bool
    internal_trigger: bool
    external_trigger: bool
    calibration_enabled: bool
    srq_mask: int
    error_register: int
    dac_value: int
    requested_trigger: str | None = None
    display_mode: str | None = None

    @property
    def function(self) -> FunctionSpec:
        return FUNCTION_BY_CODE[self.function_code]

    @property
    def range(self) -> RangeSpec:
        return self.function.range_from_status(self.range_code)

    @property
    def digits(self) -> str:
        return DIGITS[self.digits_code - 1]

    @property
    def trigger_flags(self) -> str:
        if self.internal_trigger and not self.external_trigger:
            return "Internal"
        if self.external_trigger and not self.internal_trigger:
            return "External"
        return "Other"

    @property
    def trigger_label(self) -> str:
        # B reports only the internal/external bits, not all five trigger modes.
        if self.external_trigger:
            return "External"
        if self.internal_trigger:
            return (
                self.requested_trigger
                if self.requested_trigger in ("Internal", "Fast")
                else "Internal"
            )
        return self.requested_trigger if self.requested_trigger in ("Single", "Hold") else "Fast"

    @property
    def bit_string(self) -> str:
        return " ".join(f"{byte:08b}" for byte in self.raw)


@dataclass(frozen=True)
class ConnectionInfo:
    controller_name: str
    instrument_name: str = "HP 3478A"


@dataclass(frozen=True)
class Measurement:
    value: float
    timestamp: float
    unit: str
    raw: str = ""
    overload: bool = False
    function_code: int | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.value) or not math.isfinite(self.timestamp) or self.timestamp < 0:
            raise ValueError("Measurements require a finite value and a non-negative timestamp.")

    @property
    def function_label(self) -> str:
        spec = FUNCTION_BY_CODE.get(self.function_code)
        return spec.label if spec else "Measurement"

    @property
    def display_text(self) -> str:
        return f"{self.format_value()} {self.unit}"

    def format_value(self) -> str:
        if self.overload:
            return "OVERLOAD"
        formatted = f"{self.value:+.{9}f}"
        if "." in formatted:
            parts = formatted.split(".")
            integer_part = parts[0]
            decimal_part = parts[1].rstrip("0")
            if decimal_part:
                return f"{integer_part}.{decimal_part}"
            return integer_part
        return formatted

class TraceBuffer:
    """Retain the latest samples while counting every acquired reading."""

    def __init__(self, capacity: int = 100) -> None:
        if (
            not isinstance(capacity, int)
            or isinstance(capacity, bool)
            or not 2 <= capacity <= 10000
        ):
            raise ValueError("Plot history must contain from 2 to 10000 samples.")
        self.capacity = capacity
        self._samples: deque[Measurement] = deque(maxlen=capacity)
        self._count = 0
        self._lock = threading.Lock()

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    def append(self, sample: Measurement) -> None:
        with self._lock:
            if self._samples and sample.timestamp < self._samples[-1].timestamp:
                raise ValueError("Acquisition timestamps must be non-decreasing.")
            self._samples.append(sample)
            self._count += 1

    def snapshot(self) -> tuple[Measurement, ...]:
        with self._lock:
            return tuple(self._samples)

    def clear(self) -> None:
        with self._lock:
            self._samples.clear()
            self._count = 0
