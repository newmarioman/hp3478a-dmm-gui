# -*- coding: utf-8 -*-

"""
Live Graphics Trace Plot and Numeric Display Widgets.

This module provides Tkinter-integrated UI components for real-time visualization:
LiveTracePlot updates a scrolling Matplotlib trace efficiently without rebuilding 
axes, and NumericDisplay fits the seven-segment digital readout without overlaps.

Part of the HP 3478A DMM GUI project.
Distributed under the terms of the MIT license.

Author: Lorenzo Mariani <newmarioman@hotmail.it>
"""

import tkinter as tk
from tkinter import font, ttk

import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.ticker import MaxNLocator
from PIL import ImageTk

# Local Packages
from .display import DigitalReadoutRenderer
from .models import Measurement


class LiveTraceRenderer:
    """Update one line and move the time axis without rebuilding the axes."""

    def __init__(self, figure: Figure | None = None) -> None:
        self.figure = figure or Figure(figsize=(8, 4), layout="constrained", facecolor="#f0f0f0")
        self.axes = self.figure.add_subplot(111, facecolor="black")
        (self.line,) = self.axes.plot(
            [], [], color="yellow", linewidth=1.2, marker=".", markersize=2
        )
        self.axes.grid(True, color="#b0b0b0", linestyle="--", linewidth=0.4, alpha=0.6)
        self.axes.set_xlabel("Time [s]")
        self.axes.set_ylabel("Measurement")
        self.axes.xaxis.set_major_locator(MaxNLocator(nbins="auto", min_n_ticks=2))
        self.axes.yaxis.set_major_locator(MaxNLocator(nbins="auto", min_n_ticks=2))
        self.axes.ticklabel_format(style="plain", useOffset=False, axis="y")
        self.clear()

    def update(self, samples: tuple[Measurement, ...], unit: str) -> None:
        self.axes.set_ylabel(f"Measurement [{unit}]")
        self.line.set_data(
            [sample.timestamp for sample in samples],
            [np.nan if sample.overload else sample.value for sample in samples],
        )
        if samples:
            first, last = samples[0].timestamp, samples[-1].timestamp
            span = max(0.1, last - first)
            self.axes.set_xlim(max(0.0, first - span * 0.01), max(last + span * 0.01, first + 0.1))
            values = [sample.value for sample in samples if not sample.overload]
            if values:
                low, high = min(values), max(values)
                pad = max((high - low) * 0.12, abs(high) * 1e-6, 1e-12)
                self.axes.set_ylim(low - pad, high + pad)

    def set_font_size(self, size: int) -> None:
        self.axes.tick_params(labelsize=max(8, size - 1))
        self.axes.xaxis.label.set_size(size)
        self.axes.yaxis.label.set_size(size)

    def clear(self) -> None:
        self.line.set_data([], [])
        self.axes.set_ylabel("Measurement")
        self.axes.set_xlim(0, 1)
        self.axes.set_ylim(-1, 1)


class LiveTracePlot(ttk.Frame):
    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.renderer = LiveTraceRenderer()
        self.canvas = FigureCanvasTkAgg(self.renderer.figure, master=self)
        self.canvas.get_tk_widget().configure(width=1, height=1)
        self.canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")

    def update_trace(self, samples: tuple[Measurement, ...], unit: str) -> None:
        self.renderer.update(samples, unit)
        self.canvas.draw_idle()

    def clear(self) -> None:
        self.renderer.clear()
        self.canvas.draw_idle()


class NumericDisplay(tk.Canvas):
    """Fit a Digital-7 reading, Unicode unit and caption without overlap."""

    def __init__(self, parent, body_font, font_path) -> None:
        super().__init__(parent, width=1, height=100, background="black", highlightthickness=0)
        self.renderer = DigitalReadoutRenderer(font_path)
        self._sample, self._photo, self._image_key = None, None, None
        self.unit_font = font.Font(root=self, font=body_font)
        self.value_image = self.create_image(0, 0)
        self.unit_text = self.create_text(0, 0, fill="lawngreen", font=self.unit_font)
        self.caption = self.create_text(
            0, 0, text="Waiting for measurements", fill="#b0b0b0", font=body_font
        )
        self.bind("<Configure>", self.fit)

    def show(self, sample: Measurement, function_label: str) -> None:
        self._sample = sample
        self.itemconfigure(self.caption, text=f"{function_label}  •  {sample.timestamp:.3f} s")
        self.fit()

    def clear(self) -> None:
        self._sample = None
        self.itemconfigure(self.caption, text="Waiting for measurements")
        self.fit()

    def fit(self, event=None) -> None:
        width, height = max(1, self.winfo_width()), max(1, self.winfo_height())
        padding = min(12, width // 20)
        available = max(1, width - 2 * padding)
        self.coords(self.caption, width / 2, height * 0.84)
        self.itemconfigure(self.caption, width=available)
        caption_box = self.bbox(self.caption)
        caption_top = caption_box[1] if caption_box else height * 0.72
        number_height = max(1, min(round(height * 0.6), round(caption_top - 12)))
        unit = self._sample.unit if self._sample and available >= 8 else ""
        self.unit_font.configure(size=-max(1, round(number_height * 0.65)))
        unit_width = self.unit_font.measure(unit)
        unit_limit = max(1, available // 4)
        if unit_width > unit_limit:
            pixels = abs(self.unit_font.cget("size"))
            self.unit_font.configure(size=-max(1, int(pixels * unit_limit / unit_width)))
            unit_width = self.unit_font.measure(unit)
        gap = min(8, max(1, available // 24)) if unit else 0
        number_width = max(1, available - unit_width - gap)
        text = self._sample.format_value() if self._sample else "-"
        key = text, number_width, number_height
        if key != self._image_key:
            image = self.renderer.render(text, number_width, number_height)
            self._photo = ImageTk.PhotoImage(image, master=self)
            self.itemconfigure(self.value_image, image=self._photo)
            self._image_key = key
        group_width = self._photo.width() + gap + unit_width
        left, center_y = (width - group_width) / 2, 8 + number_height / 2
        self.coords(self.value_image, left + self._photo.width() / 2, center_y)
        self.itemconfigure(self.unit_text, text=unit)
        self.coords(self.unit_text, left + self._photo.width() + gap + unit_width / 2, center_y)
