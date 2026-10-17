# -*- coding: utf-8 -*-

"""
Immutable Instrument Definitions and Application Preference Models.

This module defines the specifications for measurement functions, range 
mappings, digit resolutions, and trigger commands for the HP 3478A DMM, 
along with the application runtime configuration and user settings models.

Part of the HP 3478A DMM GUI project.
Distributed under the terms of the MIT license.

Author: Lorenzo Mariani <newmarioman@hotmail.it>
"""

from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from types import MappingProxyType


class MeasurementFunction(IntEnum):
    VOLTAGE_DC = 1
    VOLTAGE_AC = 2
    RESISTANCE_2W = 3
    RESISTANCE_4W = 4
    CURRENT_DC = 5
    CURRENT_AC = 6
    EXTENDED_OHMS = 7


@dataclass(frozen=True)
class RangeSpec:
    label: str
    command: str | None
    status_code: int


@dataclass(frozen=True)
class FunctionSpec:
    code: MeasurementFunction
    label: str
    unit: str
    ranges: tuple[RangeSpec, ...]

    @property
    def command(self) -> str:
        return f"F{int(self.code)}"

    def range_from_status(self, code: int) -> RangeSpec:
        for item in self.ranges:
            if item.status_code == code:
                return item
        raise ValueError(f"Invalid range code {code} for {self.label}.")

    def range_from_label(self, label: str) -> RangeSpec:
        for item in self.ranges:
            if item.label == label:
                return item
        raise ValueError(f"Invalid range for {self.label}: {label}.")


_DC_VOLTAGE_RANGES = (
    RangeSpec("30 mV", "R-2", 1),
    RangeSpec("300 mV", "R-1", 2),
    RangeSpec("3 V", "R0", 3),
    RangeSpec("30 V", "R1", 4),
    RangeSpec("300 V", "R2", 5),
)
_AC_VOLTAGE_RANGES = (
    RangeSpec("300 mV AC", "R-1", 1),
    RangeSpec("3 V AC", "R0", 2),
    RangeSpec("30 V AC", "R1", 3),
    RangeSpec("300 V AC", "R2", 4),
)
_CURRENT_RANGES = (RangeSpec("300 mA", "R-1", 1), RangeSpec("3 A", "R0", 2))
_RESISTANCE_RANGES = tuple(
    RangeSpec(label, f"R{code}", code)
    for code, label in enumerate(("30 Ω", "300 Ω", "3 kΩ", "30 kΩ", "300 kΩ", "3 MΩ", "30 MΩ"), 1)
)
FUNCTIONS = (
    FunctionSpec(MeasurementFunction.VOLTAGE_DC, "Voltage DC", "V", _DC_VOLTAGE_RANGES),
    FunctionSpec(MeasurementFunction.VOLTAGE_AC, "Voltage AC", "V", _AC_VOLTAGE_RANGES),
    FunctionSpec(MeasurementFunction.CURRENT_DC, "Current DC", "A", _CURRENT_RANGES),
    FunctionSpec(MeasurementFunction.CURRENT_AC, "Current AC", "A", _CURRENT_RANGES),
    FunctionSpec(MeasurementFunction.RESISTANCE_2W, "Resistance 2w", "Ω", _RESISTANCE_RANGES),
    FunctionSpec(MeasurementFunction.RESISTANCE_4W, "Resistance 4w", "Ω", _RESISTANCE_RANGES),
    FunctionSpec(
        MeasurementFunction.EXTENDED_OHMS,
        "Extended Ohms",
        "Ω",
        (RangeSpec("Extended Ohms", None, 1),),
    ),
)
FUNCTION_BY_CODE = MappingProxyType({int(item.code): item for item in FUNCTIONS})
DIGITS = ("5½", "4½", "3½")
TRIGGERS = ("Internal", "External", "Single", "Hold", "Fast")
OPTION_COMMANDS = MappingProxyType(
    {
        "digits": MappingProxyType(dict(zip(DIGITS, ("N5", "N4", "N3"), strict=True))),
        "autozero": MappingProxyType({"OFF": "Z0", "ON": "Z1"}),
        "trigger": MappingProxyType({label: f"T{code}" for code, label in enumerate(TRIGGERS, 1)}),
        "display": MappingProxyType({"ON": "D1", "OFF": "D3"}),
    }
)
ACQUISITION_INTERVALS = (
    "MAX",
    *(f"{i / 10:g}" for i in range(1, 10)),
    *(str(i) for i in range(1, 3601)),
)
EOI_MARKER = b"\x04"


@dataclass(frozen=True)
class AppConfig:
    root_dir: Path = Path(__file__).resolve().parents[1]
    tool_name: str = "HP 3478A DMM"
    author: str = "Lorenzo Mariani"
    version_major: int = 1
    version_minor: int = 0
    version_patch: int = 0
    versione_tag: str = "-dev"
    version: str = str(version_major) + "." + str(version_minor) + "." + str(version_patch) + versione_tag
    width: int = 1400
    height: int = 788
    query_timeout: float = 1.0
    command_delay: float = 0.001
    settle_delay: float = 1.0
    status_delay: float = 1.0
    plot_interval_ms: int = 80
    theme_styles: tuple[str, ...] = (
        "winnative",
        "clam",
        "alt",
        "default",
        "classic",
        "vista",
        "xpnative",
    )
    audio_themes: tuple[str, ...] = ("big-sur", "chime", "mario", "material", "zelda")

    @property
    def settings_path(self) -> Path:
        return self.root_dir / "settings.ini"

    @property
    def display_font_path(self) -> Path:
        return self.root_dir / "fonts" / "digital-7.ttf"


@dataclass
class UserSettings:
    program_path: Path = field(default_factory=Path.home)
    theme_style: str = "vista"
    audio_theme: str = "big-sur"
