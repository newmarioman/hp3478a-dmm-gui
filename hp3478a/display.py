# -*- coding: utf-8 -*-

"""
Digital Seven-Segment Readout Image Rendering Engine.

This module provides high-performance rendering of numerical displays using
the bundled Digital-7 TrueType font file via PIL (Pillow), computing precise
bounding box constraints and scales without system-wide font installation.

Part of the HP 3478A DMM GUI project.
Distributed under the terms of the MIT license.

Author: Lorenzo Mariani <newmarioman@hotmail.it>
"""

from functools import lru_cache
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


class DigitalReadoutRenderer:
    """Fit Digital-7 glyphs into a pixel rectangle without installing a system font."""

    def __init__(self, font_path: Path) -> None:
        self.font_path = Path(font_path)
        self._font(24)  # Validate the font once during display construction.

    @lru_cache(maxsize=24)
    def _font(self, size: int) -> ImageFont.FreeTypeFont:
        return ImageFont.truetype(str(self.font_path), size)

    def render(self, text: str, width: int, height: int) -> Image.Image:
        width = max(1, int(width))
        height = max(1, int(height))
        text = text or "-"
        size = max(1, round(height * 1.5))
        face = self._font(size)
        
        left, top, right, bottom = face.getbbox(text)
        scale = min(1.0, width / max(1, right - left), height / max(1, bottom - top))
        if scale < 1:
            face = self._font(max(1, int(size * scale)))
            left, top, right, bottom = face.getbbox(text)
            
        image = Image.new("RGBA", (max(1, right - left), max(1, bottom - top)))
        ImageDraw.Draw(image).text((-left, -top), text, font=face, fill="lawngreen")
        if image.width > width or image.height > height:
            image.thumbnail((width, height), Image.Resampling.LANCZOS)
            
        return image