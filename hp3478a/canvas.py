# -*- coding: utf-8 -*-

"""
Responsive Tkinter Canvas Wrapper for Matplotlib Figures.

This module provides a specialized canvas subclass to prevent layout breakdown,
flickering, or data-loss during resize events and window movement. It handles
debounced window scaling and aggressive geometry optimizations.

Part of the HP 3478A DMM GUI project.
Distributed under the terms of the MIT license.

Author: Lorenzo Mariani <newmarioman@hotmail.it>
"""

from types import SimpleNamespace

from matplotlib import rc_context
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg


class ResponsiveFigureCanvas(FigureCanvasTkAgg):
    def __init__(self, figure, master=None) -> None:
        self._last_dimensions = None
        self._resize_id = None
        self._pending_dimensions = None
        super().__init__(figure, master=master)
        self.get_tk_widget().bind("<Destroy>", self._destroyed, add="+")

    def resize(self, event) -> None:
        dimensions = (event.width, event.height)
        if min(dimensions) <= 1 or dimensions == self._pending_dimensions:
            return
        if self._resize_id is not None:
            self.get_tk_widget().after_cancel(self._resize_id)
            self._resize_id = None
        self._pending_dimensions = None
        if dimensions == self._last_dimensions:
            return  # Position-only events must not delete the displayed image.
        self._pending_dimensions = dimensions
        self._resize_id = self.get_tk_widget().after(100, self._apply_resize)

    def _apply_resize(self) -> None:
        dimensions = self._pending_dimensions
        self._resize_id, self._pending_dimensions = None, None
        if dimensions is not None:
            self._last_dimensions = dimensions
            super().resize(SimpleNamespace(width=dimensions[0], height=dimensions[1]))

    def draw(self) -> None:
        with rc_context({"agg.path.chunksize": 10000, "path.simplify": True}):
            super().draw()

    def _destroyed(self, event) -> None:
        if event.widget is self.get_tk_widget() and self._resize_id is not None:
            self.get_tk_widget().after_cancel(self._resize_id)
            self._resize_id = None
