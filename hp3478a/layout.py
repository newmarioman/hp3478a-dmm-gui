# -*- coding: utf-8 -*-

"""
Display-Independent Responsive Layout Policy Calculations.

This module provides the geometric policies and scale-ratio calculations 
necessary to adjust Tkinter widget frames, sidebar dimensions, font scaling, 
and grid column counts dynamically based on window width, height, and DPI scale factors.

Part of the HP 3478A DMM GUI project.
Distributed under the terms of the MIT license.

Author: Lorenzo Mariani <newmarioman@hotmail.it>
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class LayoutPolicy:
    width: int
    height: int
    dpi_scale: float = 1.0

    @property
    def compact(self) -> bool:
        return self.width / max(1.0, self.dpi_scale) < 1000

    @property
    def font_size(self) -> int:
        scale = max(1.0, self.dpi_scale)
        ratio = min(self.width / (1440 * scale), self.height / (900 * scale))
        return max(8, min(14, round(10 * ratio)))

    @property
    def sidebar_width(self) -> int:
        preferred = round(self.width * 0.25)
        minimum = round(260 * max(1.0, self.dpi_scale))
        maximum = round(440 * max(1.0, self.dpi_scale))
        return min(maximum, max(minimum, preferred), max(1, self.width - 280))

    @property
    def compact_controls_height(self) -> int:
        dpi = max(1.0, self.dpi_scale)
        return min(round(180 * dpi), max(round(72 * dpi), round(self.height * 0.20)))

    @staticmethod
    def columns(available_width: int, item_width: int, count: int) -> int:
        return max(1, min(count, max(1, available_width) // max(1, item_width)))
