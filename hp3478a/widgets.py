# -*- coding: utf-8 -*-

"""
Reusable Tkinter UI Widgets and Context Menus with Reflow and Scroll Capabilities.

This module provides adaptive GUI controls designed to flow or scroll dynamically:
DescriptiveCombobox manages pop-up monitor fitting, ScrollableFrame encapsulates 
mouse-wheel scrolling, CommandRow implements conditional horizontal wrapping, 
and ResponsiveButtonBar places items in a grid layout.

Part of the HP 3478A DMM GUI project.
Distributed under the terms of the MIT license.

Author: Lorenzo Mariani <newmarioman@hotmail.it>
"""

import tkinter as tk
from collections.abc import Callable
from tkinter import font, ttk

# Local Packages
from .dialogs import DisplayService
from .layout import LayoutPolicy


class DescriptiveCombobox(ttk.Combobox):
    """Expand the drop-down list to fit descriptions within the active monitor."""

    def __init__(self, parent, **kwargs) -> None:
        self._popup_style = f"Description{id(self)}.TCombobox"
        super().__init__(parent, style=self._popup_style, postcommand=self._fit_popup, **kwargs)

    def _fit_popup(self) -> None:
        text_font = font.Font(root=self, font=self.cget("font"))
        text_width = max((text_font.measure(label) for label in self.cget("values")), default=0)
        
        bounds = DisplayService(self.winfo_toplevel()).bounds()
        monitor_x = bounds[0]
        monitor_width = bounds[2]
        
        width = min(
            max(self.winfo_width(), text_width + text_font.measure("0000")), monitor_width - 16
        )
        x = max(monitor_x + 8, min(self.winfo_rootx(), monitor_x + monitor_width - width - 8))
        
        # Tk's postoffset style grows only the pop-up; the field still fits its grid cell.
        ttk.Style(self).configure(
            self._popup_style,
            postoffset=(
                x - self.winfo_rootx(),
                0,
                width - self.winfo_width(),
                0,
            ),
        )


class ScrollableFrame(ttk.Frame):
    """Keep controls reachable when their requested height exceeds the pane."""

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, width=1, height=1, highlightthickness=0, bg="#f0f0f0")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(self, command=self.canvas.yview)
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.content = ttk.Frame(self.canvas, padding=4)
        self.content.columnconfigure(0, weight=1)
        self._window = self.canvas.create_window(0, 0, window=self.content, anchor="nw")
        self.canvas.bind("<Configure>", self._resize)
        self.content.bind("<Configure>", self._update_scroll)
        
        root = self.winfo_toplevel()
        root.bind("<MouseWheel>", self._wheel, add="+")
        root.bind("<Button-4>", self._wheel, add="+")
        root.bind("<Button-5>", self._wheel, add="+")

    def _resize(self, event) -> None:
        self.canvas.itemconfigure(self._window, width=max(1, event.width))
        self._update_scroll()

    def _update_scroll(self, event=None) -> None:
        height = self.content.winfo_reqheight()
        self.canvas.configure(scrollregion=(0, 0, self.canvas.winfo_width(), height))
        if height > self.canvas.winfo_height() + 1:
            self.scrollbar.grid()
        else:
            self.scrollbar.grid_remove()
            self.canvas.yview_moveto(0)

    def _wheel(self, event) -> None:
        widget = event.widget
        while widget is not None and widget is not self:
            widget = getattr(widget, "master", None)
        if widget is not self:
            return
            
        num = getattr(event, "num", None)
        if num == 4:
            units = -1
        elif num == 5:
            units = 1
        else:
            delta = getattr(event, "delta", 0)
            units = -int(delta / 120) or (-1 if delta > 0 else 1 if delta < 0 else 0)
            
        if units:
            self.canvas.yview_scroll(units, "units")


class CommandRow(ttk.Frame):
    """Put the label above the entry when a single row would be too narrow."""

    def __init__(self, parent, label: str, buttons: tuple[tuple[str, Callable], ...]) -> None:
        super().__init__(parent)
        self.label = ttk.Label(self, text=label)
        self.entry = ttk.Entry(self, width=1)
        self.buttons = tuple(
            ttk.Button(self, text=text, command=command) for text, command in buttons
        )
        self._compact = None
        self.bind("<Configure>", self._resize)
        self._reflow(800)

    def _resize(self, event) -> None:
        self._reflow(event.width)

    def _reflow(self, width: int) -> None:
        needed = (
            self.label.winfo_reqwidth() + sum(b.winfo_reqwidth() + 8 for b in self.buttons) + 120
        )
        compact = width < needed
        if compact == self._compact:
            return
        self._compact = compact
        for widget in (self.label, self.entry, *self.buttons):
            widget.grid_forget()
        for column in range(len(self.buttons) + 2):
            self.columnconfigure(column, weight=0)
        if compact:
            self.label.grid(
                row=0, column=0, columnspan=len(self.buttons) + 1, sticky="w", pady=(0, 2)
            )
            self.columnconfigure(0, weight=1)
            self.entry.grid(row=1, column=0, sticky="ew", pady=2)
            for column, button in enumerate(self.buttons, 1):
                button.grid(row=1, column=column, padx=(6, 0), pady=2, sticky="ew")
        else:
            self.label.grid(row=0, column=0, padx=(0, 8), sticky="w")
            self.columnconfigure(1, weight=1)
            self.entry.grid(row=0, column=1, sticky="ew", pady=2)
            for column, button in enumerate(self.buttons, 2):
                button.grid(row=0, column=column, padx=(6, 0), pady=2, sticky="ew")


class ResponsiveButtonBar(ttk.Frame):
    """Lay out action controls in as many equal-width columns as will fit."""

    def __init__(self, parent) -> None:
        super().__init__(parent)
        self.items = []
        self._columns = 0
        self.bind("<Configure>", self._resize)

    def add(self, widget: ttk.Widget) -> None:
        self.items.append(widget)
        self._columns = 0
        self._reflow(800)

    def _resize(self, event) -> None:
        self._reflow(event.width)

    def _reflow(self, width: int) -> None:
        if not self.items:
            return
        item_width = max(widget.winfo_reqwidth() + 8 for widget in self.items)
        columns = LayoutPolicy.columns(width, item_width, len(self.items))
        if columns == self._columns:
            return
        self._columns = columns
        for column in range(len(self.items)):
            self.columnconfigure(column, weight=0, uniform="")
        for column in range(columns):
            self.columnconfigure(column, weight=1, uniform="actions")
        for index, widget in enumerate(self.items):
            widget.grid(row=index // columns, column=index % columns, padx=3, pady=3, sticky="ew")


class TextContextMenu:
    def __init__(self, root: tk.Tk, clear_console: Callable) -> None:
        self.root = root
        self.menu = tk.Menu(root, tearoff=False)
        self._widget = None
        for label, action in (("Cut", "<<Cut>>"), ("Copy", "<<Copy>>"), ("Paste", "<<Paste>>")):
            self.menu.add_command(
                label=label, command=lambda action=action: self._widget.event_generate(action)
            )
        self.menu.add_separator()
        self.menu.add_command(label="Select all", command=self._select_all)
        self.menu.add_separator()
        self.menu.add_command(label="Clear console", command=clear_console)
        for widget_class in ("Entry", "TEntry", "Text", "TCombobox"):
            root.bind_class(widget_class, "<Button-3>", self._show, add="+")
            root.bind_class(widget_class, "<Control-a>", self._select_event, add="+")

    def _show(self, event) -> str:
        self._widget = event.widget
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()
        return "break"

    def _select_event(self, event) -> str:
        self._widget = event.widget
        self._select_all()
        return "break"

    def _select_all(self) -> None:
        if isinstance(self._widget, tk.Text):
            self._widget.tag_add("sel", "1.0", "end-1c")
        else:
            self._widget.selection_range(0, "end")
