"""Verify numeric precision, the actual TrueType font and readout fitting."""

import math
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import ImageFont

from hp3478a.config import AppConfig
from hp3478a.display import DigitalReadoutRenderer
from hp3478a.models import Measurement
from hp3478a.plot import NumericDisplay


class PixelFont:
    def __init__(self, font_path="arial.ttf"):
        self.font_path = font_path
        self.size = 24

    def configure(self, *, size):
        self.size = abs(size)

    def cget(self, key):
        return -self.size

    def measure(self, text):
        face = ImageFont.truetype(str(self.font_path), self.size)
        return math.ceil(face.getlength(text))


class Photo:
    def __init__(self, image, *, master):
        self.image = image

    def width(self):
        return self.image.width


class NumericDisplayTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).absolute().parents[1]
        self.renderer = DigitalReadoutRenderer(self.root / "fonts" / "digital-7.ttf")

    def test_overload_keeps_its_message_for_all_digit_settings(self):
        sample = Measurement(9.99999e9, 0, "Ω", overload=True)
        self.assertEqual(sample.format_value(), "OVERLOAD")

    def test_rendering_loads_digital_7_and_fits_narrow_and_wide_readouts(self):
        self.assertEqual(self.renderer._font(24).getname()[0], "Digital-7")
        for text in ("-0.000", "+1.23456", "-12345678.12345", "OVERLOAD"):
            for width, height in ((1, 1), (48, 24), (160, 40), (600, 80), (1400, 150)):
                with self.subTest(text=text, width=width, height=height):
                    image = self.renderer.render(text, width, height)
                    self.assertLessEqual(image.width, width)
                    self.assertLessEqual(image.height, height)
                    self.assertEqual(image.mode, "RGBA")
                    if width >= 48:
                        self.assertIsNotNone(image.getbbox())

    def make_display(self, width):
        display = NumericDisplay.__new__(NumericDisplay)
        display.renderer = self.renderer
        display._sample = None
        display._photo, display._image_key = None, None
        font_path = self.root / "fonts" / "digital-7.ttf"
        display.unit_font = PixelFont(font_path)
        display.value_image, display.unit_text, display.caption = 1, 2, 3
        display.positions, display.options = {}, {1: {}, 2: {}, 3: {}}
        display.winfo_width, display.winfo_height = lambda: width, lambda: 100
        display.coords = lambda item, x, y: display.positions.update({item: (x, y)})
        display.itemconfigure = lambda item, **options: display.options[item].update(options)
        display.bbox = lambda item: (12, 68, width - 12, 96)
        return display

    def test_display_keeps_the_reading_unit_and_caption_separate(self):
        for width in (120, 200, 640, 1366):
            display = self.make_display(width)
            with patch("hp3478a.plot.ImageTk.PhotoImage", Photo):
                display.show(Measurement(-12345.123456, 1, "Ω"), "Resistance")
                self.assertEqual(display._image_key[0], "-12345.123456")
                self.assertEqual(display.options[display.unit_text]["text"], "Ω")
                image_x, image_y = display.positions[display.value_image]
                image_width, image_height = display._photo.image.size
                unit_x, unit_y = display.positions[display.unit_text]
                unit_width = display.unit_font.measure("Ω")
                self.assertGreaterEqual(image_x - image_width / 2, 0)
                self.assertLessEqual(unit_x + unit_width / 2, width)
                self.assertLessEqual(image_x + image_width / 2, unit_x - unit_width / 2)
                self.assertEqual(image_y, unit_y)
                self.assertLess(image_y + image_height / 2, display.bbox(display.caption)[1])
                

if __name__ == "__main__":
    unittest.main()
