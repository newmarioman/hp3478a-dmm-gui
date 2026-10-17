# -*- coding: utf-8 -*-

"""
HP 3478A Protocol Decoder for Binary Status and ASCII Measurements.

This module provides low-level parsing and validation for the instrument's responses.
It decodes the 5-byte binary status packet into precise operational flags and 
converts raw ASCII measurement strings into validated floating-point numbers.

Part of the HP 3478A DMM GUI project.
Distributed under the terms of the MIT license.

Author: Lorenzo Mariani <newmarioman@hotmail.it>
"""

import math

# Local Packages
from .config import FUNCTION_BY_CODE
from .models import DmmStatus, Measurement


class ProtocolError(RuntimeError):
    pass


class StatusDecoder:
    def decode(
        self,
        payload: bytes,
        *,
        requested_trigger: str | None = None,
        display_mode: str | None = None,
    ) -> DmmStatus:
        if len(payload) != 5:
            raise ProtocolError(f"Expected five status bytes; received {len(payload)}.")
            
        first = payload[0]
        flags = payload[1]
        srq = payload[2]
        error = payload[3]
        dac = payload[4]
        
        function = first >> 5
        range_code = (first >> 2) & 7
        digits = first & 3
        
        if function not in FUNCTION_BY_CODE or digits not in (1, 2, 3) or flags & 0x80:
            raise ProtocolError("Invalid HP 3478A status flags.")
        try:
            FUNCTION_BY_CODE[function].range_from_status(range_code)
        except ValueError as exc:
            raise ProtocolError(str(exc)) from exc
            
        return DmmStatus(
            bytes(payload),
            function,
            range_code,
            digits,
            bool(flags & 2),
            bool(flags & 4),
            50 if flags & 0x10 else 60,
            bool(flags & 8),
            bool(flags & 1),
            bool(flags & 0x40),
            bool(flags & 0x20),
            srq,
            error,
            dac,
            requested_trigger,
            display_mode,
        )


class MeasurementDecoder:
    def decode(
        self, payload: bytes, timestamp: float, unit: str, *, function_code: int | None = None
    ) -> Measurement:
        try:
            raw = payload.decode("ascii").strip()
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError("Nonfinite reading.")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProtocolError(f"Invalid measurement response: {payload!r}.") from exc
        overload = math.isclose(abs(value), 9.99999e9, rel_tol=1e-8)
        return Measurement(value, timestamp, unit, raw, overload, function_code)
