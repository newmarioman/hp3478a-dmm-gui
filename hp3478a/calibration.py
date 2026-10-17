# -*- coding: utf-8 -*-

"""
HP 3478A Calibration RAM Management, Backup, and Restoration.

This module handles parsing, verification, and checksum checks for the 
256-nibble calibration RAM. It coordinates reading the memory bank, 
validating entry-level checksums, and safely restoring images.

The HP 3478A uses software-based calibration; the data is stored in volatile RAM powered by this battery when the instrument is turned off.
If the battery fully discharges and is removed or replaced without maintaining backup power, the calibration data will be lost.

Lithium battery 3V 2/3 A (standard CR17335 or BR-2/3A dimensions)

Part of the HP 3478A DMM GUI project.
Distributed under the terms of the MIT license.

Author: Lorenzo Mariani <newmarioman@hotmail.it>
"""

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class CalibrationDump:
    nibbles: tuple[int, ...]
    created_utc: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "nibbles", tuple(self.nibbles))
        if len(self.nibbles) != 256 or any(
            not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 15
            for value in self.nibbles
        ):
            raise ValueError("A complete HP 3478A RAM image must contain 256 nibbles (0-15).")
        if not isinstance(self.created_utc, str):
            raise ValueError("Invalid calibration backup timestamp.")

    @property
    def digest(self) -> str:
        return hashlib.sha256(bytes(self.nibbles)).hexdigest()

    @property
    def failed_entries(self) -> tuple[int, ...]:
        failures = []
        for entry in range(19):
            if entry in (5, 16, 18):
                continue  # These ranges are unused and need no valid checksum.
            start = 1 + entry * 13
            values = self.nibbles[start : start + 13]
            if sum(values[:11]) + values[11] * 16 + values[12] != 255:
                failures.append(entry)
        return tuple(failures)

    def require_valid_calibration(self) -> None:
        if self.failed_entries:
            entries = ", ".join(str(item) for item in self.failed_entries)
            raise ValueError(
                f"Calibration checksums failed for entries {entries}. Nothing was written."
            )


class CalibrationRepository:
    FORMAT = "HP3478A-calibration-ram"

    def save(self, path: Path, image: CalibrationDump) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "format": self.FORMAT,
            "version": 1,
            "instrument": "HP 3478A",
            "created_utc": image.created_utc,
            "sha256": image.digest,
            "nibbles": list(image.nibbles),
        }
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
            ) as stream:
                temporary = Path(stream.name)
                json.dump(document, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return path

    def load(self, path: Path) -> CalibrationDump:
        with Path(path).open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("The calibration backup is too large.")
        try:
            document = json.loads(raw)
            if not isinstance(document, dict) or (
                document.get("format") != self.FORMAT
                or type(document.get("version")) is not int
                or document["version"] != 1
                or document.get("instrument") != "HP 3478A"
            ):
                raise ValueError("Select a version 1 HP 3478A calibration JSON backup.")
            image = CalibrationDump(tuple(document["nibbles"]), document["created_utc"])
            if document.get("sha256") != image.digest:
                raise ValueError("Calibration backup integrity check failed. Nothing was written.")
            return image
        except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("The calibration backup is incomplete or invalid.") from exc


class CalibrationManager:
    def __init__(self, instrument, repository: CalibrationRepository | None = None) -> None:
        self.instrument = instrument
        self.repository = repository or CalibrationRepository()

    def backup(
        self, path: Path, progress: Callable[[str, int], None] | None = None
    ) -> CalibrationDump:
        progress = progress or (lambda phase, count: None)
        first = self.instrument.read_calibration_ram(lambda count: progress("Reading", count))
        second = self.instrument.read_calibration_ram(lambda count: progress("Checking", count))
        if first[1:] != second[1:]:
            raise ValueError("Calibration RAM changed between the two reads. No backup was saved.")
        image = CalibrationDump(tuple(second))
        self.repository.save(path, image)
        return image

    def restore(
        self,
        image: CalibrationDump,
        recovery_directory: Path,
        progress: Callable[[str, int], None] | None = None,
    ) -> Path:
        image.require_valid_calibration()
        progress = progress or (lambda phase, count: None)
        if not self.instrument.refresh_status().calibration_enabled:
            raise ValueError("Set the front-panel CAL ENABLE switch to ON before restoring RAM.")
        current = self.instrument.read_calibration_ram(
            lambda count: progress("Recovery backup", count)
        )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        recovery = Path(recovery_directory) / f"HP3478A_before_restore_{stamp}.json"
        self.repository.save(recovery, CalibrationDump(tuple(current)))
        try:
            self.instrument.restore_calibration_ram(bytes(image.nibbles), progress)
        except Exception as exc:
            raise RuntimeError(f"Restore failed: {exc}\nRecovery backup: {recovery}") from exc
        return recovery
