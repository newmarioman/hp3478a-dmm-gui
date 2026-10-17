# -*- coding: utf-8 -*-

"""
Preferences Persistence, Complete CSV Logging, and Platform-Independent Output Paths.

Part of the HP 3478A DMM GUI project.
Distributed under the terms of the MIT license.

Author: Lorenzo Mariani <newmarioman@hotmail.it>
"""

import csv
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Local Packages
from .config import AppConfig, UserSettings
from .models import Measurement


class SettingsRepository:
    """Read and write settings.ini."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def load(self) -> UserSettings:
        settings = UserSettings()
        try:
            payload = self.config.settings_path.read_bytes()
            try:
                lines = payload.decode("utf-8-sig").splitlines()
            except UnicodeDecodeError:
                lines = payload.decode("cp1252").splitlines()
            prefixes = ("Program Path: ", "Theme Style: ", "Audio : ")
            if len(lines) < 3 or any(not lines[i].startswith(p) for i, p in enumerate(prefixes)):
                raise ValueError("Invalid settings file format.")
            settings = UserSettings(
                Path(lines[0][len(prefixes[0]) :].strip()),
                lines[1][len(prefixes[1]) :].strip(),
                lines[2][len(prefixes[2]) :].strip(),
            )
        except (OSError, ValueError):
            pass
        if not settings.program_path.is_dir():
            settings.program_path = Path.home()
        if settings.theme_style not in self.config.theme_styles:
            settings.theme_style = "vista"
        if settings.audio_theme not in self.config.audio_themes:
            settings.audio_theme = "big-sur"
        return settings

    def save(self, settings: UserSettings) -> None:
        path = self.config.settings_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".ini.tmp")
        temporary.write_text(
            f"Program Path: {settings.program_path}\nTheme Style: {settings.theme_style}\nAudio : {settings.audio_theme}",
            encoding="utf-8",
        )
        temporary.replace(path)


class OutputManager:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def output_directory(self, settings: UserSettings) -> Path:
        return settings.program_path / self.config.tool_name.replace(" ", "_") / "OUTPUT"

    def ensure_directory(self, settings: UserSettings) -> Path:
        path = self.output_directory(settings)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def sanitize_name(value: str) -> str:
        return (
            re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value.replace("Ω", "Ohm")).strip(" .")
            or "capture"
        )

    def capture_path(self, settings: UserSettings, unit: str) -> Path:
        stamp = datetime.now().strftime("%d-%b-%Y (%H.%M.%S.%f)")
        return self.ensure_directory(settings) / f"ACQ_DATA_{self.sanitize_name(unit)}_{stamp}.csv"

    def plot_path(self, settings: UserSettings, plot_type: str, title: str) -> Path:
        directory = self.ensure_directory(settings) / "PLOTTER" / plot_type
        directory.mkdir(parents=True, exist_ok=True)
        stem, index = self.sanitize_name(title), 1
        path = directory / f"{stem}.png"
        while path.exists():
            path = directory / f"{stem}_{index}.png"
            index += 1
        return path

    def open_folder(self, settings: UserSettings) -> None:
        directory = self.ensure_directory(settings)
        if sys.platform == "win32":
            os.startfile(directory)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(directory)])


class CsvCaptureLogger:
    """Log every sample independently of plot refresh and close on stop/error."""

    def __init__(self, path: Path, unit: str) -> None:
        self.path, self.unit = Path(path), unit
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("x", encoding="utf-8", newline="")
        self._writer = csv.writer(self._handle, lineterminator="\n")
        self._writer.writerow((f"MEASURE[{unit.replace('Ω', 'Ohm')}]", "TIMESTAMP[s]"))
        self._handle.flush()
        self._flushed_at = time.monotonic()

    def append(self, sample: Measurement) -> None:
        if sample.unit != self.unit:
            raise ValueError("A capture file cannot contain different measurement units.")
        self._writer.writerow((float(repr(sample.value)), repr(sample.timestamp)))
        if time.monotonic() - self._flushed_at >= 1:
            self._handle.flush()
            self._flushed_at = time.monotonic()

    def close(self) -> None:
        self._handle.close()


@dataclass(frozen=True)
class CaptureData:
    values: tuple[float, ...]
    timestamps: tuple[float, ...]
    unit: str
    title: str = "Acquisition"

    def __post_init__(self) -> None:
        if not self.values or len(self.values) != len(self.timestamps):
            raise ValueError("A capture needs matching, nonempty values and timestamps.")
        if any(not math.isfinite(value) for value in (*self.values, *self.timestamps)) or any(
            t < 0 for t in self.timestamps
        ):
            raise ValueError("Capture data needs finite values and nonnegative timestamps.")


class CsvCaptureRepository:
    """Load captures."""

    def load(self, path: Path) -> CaptureData:
        values, timestamps = [], []
        with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, [])
            match = re.fullmatch(r"MEASURE\[(.+)\]", header[0]) if len(header) == 2 else None
            if not match or header[1] != "TIMESTAMP[s]":
                raise ValueError("Expected MEASURE[unit],TIMESTAMP[s] in the CSV header.")
            for row_number, row in enumerate(reader, 2):
                if not row:
                    continue
                try:
                    if len(row) != 2:
                        raise ValueError("Expected two columns.")
                    value, timestamp = map(float, row)
                    if not math.isfinite(value) or not math.isfinite(timestamp) or timestamp < 0:
                        raise ValueError("Invalid numeric data.")
                except ValueError as exc:
                    raise ValueError(
                        f"CSV row {row_number}: enter two finite numbers and a nonnegative timestamp."
                    ) from exc
                values.append(value)
                timestamps.append(timestamp)
        return CaptureData(
            tuple(values), tuple(timestamps), match[1].replace("Ohm", "Ω"), Path(path).stem
        )
