# -*- coding: utf-8 -*-

"""
Data Analysis Window Layout and Event Handlers for CSV Logs.

This module manages the independent Toplevel windows containing the Matplotlib
visualizations, responsive layouts, sidebar options, and CSV data plotting controls.

Part of the HP 3478A DMM GUI project.
Distributed under the terms of the MIT license.

Author: Lorenzo Mariani <newmarioman@hotmail.it>
"""

import tkinter as tk
from tkinter import ttk

from matplotlib.colors import CSS4_COLORS

# Local Packages
from .analysis import PLOT_TYPES, AnalysisRenderer, PlotOptions
from .canvas import ResponsiveFigureCanvas
from .layout import LayoutPolicy
from .storage import CaptureData
from .widgets import DescriptiveCombobox, ResponsiveButtonBar, ScrollableFrame


class AnalysisWindow:
    def __init__(self, parent, data: CaptureData, dialogs, themes, output, preferences) -> None:
        self.data, self.dialogs, self.output, self.preferences = data, dialogs, output, preferences
        self.themes = themes
        self.renderer = AnalysisRenderer()
        self._limits, self._compact = (None, None), None
        self._layout_key, self._last_size, self._resize_id = None, None, None
        self._fullscreen = False
        self.window = tk.Toplevel(parent)
        self.window.withdraw()
        self.window.title("Data analysis")
        self.window.resizable(True, True)
        self.window.columnconfigure(0, weight=1)
        self.window.rowconfigure(1, weight=1)
        dialogs.set_icon(self.window)
        toolbar = ResponsiveButtonBar(self.window)
        toolbar.grid(row=0, column=0, sticky="ew", padx=4, pady=4)
        for widget in (
            ttk.Button(toolbar, text="Save PNG", command=self._save),
            ttk.Button(toolbar, text="Plot guide", command=self._guide),
            ttk.Button(toolbar, text="Maximize", command=self._maximize),
            ttk.Button(toolbar, text="Fullscreen (F11)", command=self._toggle_fullscreen),
            ttk.Button(toolbar, text="Close", command=self._close),
        ):
            toolbar.add(widget)
        self.body = ttk.Frame(self.window)
        self.body.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        self.sidebar = ScrollableFrame(self.body)
        self.sidebar.grid_propagate(False)
        fields = self.sidebar.content
        fields.columnconfigure(0, weight=1)
        self.kind, self.color = tk.StringVar(self.window, "Line"), tk.StringVar(self.window, "red")
        self._last_kind = self.kind.get()
        self.title, self.bins = tk.StringVar(self.window, data.title), tk.StringVar(self.window, "")
        self.option_vars = {
            key: tk.BooleanVar(self.window, key == "grid")
            for key in (
                "grid",
                "mean",
                "std",
                "median",
                "mode",
                "minimum",
                "maximum",
                "percentage",
                "kde",
                "vertical",
                "quartiles",
            )
        }
        self.option_checks = {}
        row = 0
        for label, variable, choices in (
            ("Plot type", self.kind, PLOT_TYPES),
            ("Color", self.color, tuple(CSS4_COLORS)),
        ):
            ttk.Label(fields, text=label).grid(row=row, column=0, sticky="w", pady=(6, 3))
            combo = DescriptiveCombobox(
                fields,
                textvariable=variable,
                values=choices,
                width=1,
                state="readonly",
                font=themes.body_font,
            )
            combo.grid(row=row + 1, column=0, sticky="ew")
            combo.bind("<<ComboboxSelected>>", self.redraw)
            row += 2
        for key, label in (
            ("grid", "Grid"),
            ("minimum", "Minimum"),
            ("maximum", "Maximum"),
            ("mean", "Mean"),
            ("std", "Standard deviation"),
            ("median", "Median"),
            ("mode", "Mode"),
            ("percentage", "Percentage"),
            ("kde", "KDE overlay"),
            ("vertical", "Vertical"),
            ("quartiles", "Quartiles"),
        ):
            check = ttk.Checkbutton(
                fields, text=label, variable=self.option_vars[key], command=self.redraw
            )
            check.grid(row=row, column=0, sticky="w", pady=2)
            self.option_checks[key] = check
            row += 1
        ttk.Label(fields, text="Histogram intervals (10-200; empty = Auto)", wraplength=220).grid(
            row=row, column=0, sticky="w", pady=(6, 3)
        )
        self.bins_entry = ttk.Entry(fields, textvariable=self.bins, width=1, font=themes.body_font)
        self.bins_entry.grid(row=row + 1, column=0, sticky="ew")
        self.bins_entry.bind("<Return>", self.redraw)
        row += 2
        ttk.Label(fields, text="Title").grid(row=row, column=0, sticky="w", pady=(6, 3))
        title_entry = ttk.Entry(fields, textvariable=self.title, width=1, font=themes.body_font)
        title_entry.grid(row=row + 1, column=0, sticky="ew")
        title_entry.bind("<Return>", self.redraw)
        row += 2
        bar = ResponsiveButtonBar(fields)
        bar.grid(row=row, column=0, sticky="ew", pady=4)
        for widget in (
            ttk.Button(bar, text="Update plot", command=self.redraw),
            ttk.Button(bar, text="Reset title", command=self._reset_title),
        ):
            bar.add(widget)
        row += 1
        self.limit_vars = {
            key: tk.StringVar(self.window, "") for key in ("x_low", "x_high", "y_low", "y_high")
        }
        for key, label in (
            ("x_low", "X lower limit"),
            ("x_high", "X upper limit"),
            ("y_low", "Y lower limit"),
            ("y_high", "Y upper limit"),
        ):
            ttk.Label(fields, text=label).grid(row=row, column=0, sticky="w", pady=(6, 3))
            ttk.Entry(
                fields, textvariable=self.limit_vars[key], width=1, font=themes.body_font
            ).grid(row=row + 1, column=0, sticky="ew")
            row += 2
        bar = ResponsiveButtonBar(fields)
        bar.grid(row=row, column=0, sticky="ew", pady=4)
        for widget in (
            ttk.Button(bar, text="Apply limits", command=self._apply_limits),
            ttk.Button(bar, text="Reset limits", command=self._reset_limits),
        ):
            bar.add(widget)
        self.statistics_label = ttk.Label(fields, text="", wraplength=220)
        self.statistics_label.grid(row=row + 1, column=0, sticky="ew", pady=8)
        self.canvas = ResponsiveFigureCanvas(self.renderer.figure, master=self.body)
        self.canvas.get_tk_widget().configure(width=1, height=1)
        self.canvas.mpl_connect("resize_event", self._plot_resized)
        self.saved = ttk.Label(self.window, text="", wraplength=600)
        self.saved.grid(row=2, column=0, sticky="ew", padx=8, pady=4)
        self.window.bind("<Configure>", self._resize)
        self.window.bind("<F11>", self._toggle_fullscreen)
        self.window.bind("<Escape>", self._leave_fullscreen)
        self.window.protocol("WM_DELETE_WINDOW", self._close)
        width, height = dialogs.display.dimensions()
        self.window.minsize(min(600, round(width * 0.95)), min(420, round(height * 0.85)))
        self._layout(
            LayoutPolicy(min(1100, round(width * 0.9)), min(750, round(height * 0.9)), self._dpi())
        )
        self.window.geometry(f"{min(1100, round(width * 0.9))}x{min(750, round(height * 0.9))}")
        self.redraw()
        self.window.deiconify()
        # An independent Toplevel keeps the native maximize control on Windows.

    def _close(self) -> None:
        if self._resize_id is not None:
            self.window.after_cancel(self._resize_id)
            self._resize_id = None
        self.window.destroy()

    def _maximize(self) -> None:
        self._leave_fullscreen()
        try:
            self.window.state("zoomed")
        except tk.TclError:
            try:
                self.window.attributes("-zoomed", True)
            except tk.TclError:
                width, height = self.dialogs.display.dimensions()
                self.window.geometry(f"{width}x{height}+0+0")

    def _toggle_fullscreen(self, event=None) -> str:
        self._fullscreen = not self._fullscreen
        self.window.attributes("-fullscreen", self._fullscreen)
        return "break"

    def _leave_fullscreen(self, event=None) -> str:
        self._fullscreen = False
        self.window.attributes("-fullscreen", False)
        return "break"

    def _plot_resized(self, event=None) -> None:
        pixels = self.themes.body_font.metrics("linespace")
        self.renderer.set_font_size(pixels * 72 / self.renderer.figure.dpi)

    def _dpi(self) -> float:
        return max(1.0, self.window.winfo_fpixels("1i") / 96)

    def _resize(self, event) -> None:
        size = (event.width, event.height)
        if event.widget is not self.window or min(size) <= 1 or size == self._last_size:
            return
        self._last_size = size
        if self._resize_id is not None:
            self.window.after_cancel(self._resize_id)
        self._resize_id = self.window.after(100, self._finish_resize)

    def _finish_resize(self) -> None:
        self._resize_id = None
        width, height = self._last_size
        self._layout(LayoutPolicy(width, height, self._dpi()))
        self.saved.configure(wraplength=max(80, width - 24))

    def _layout(self, policy: LayoutPolicy) -> None:
        compact = policy.width / max(1.0, policy.dpi_scale) < 720
        key = compact
        if key == self._layout_key:
            if compact:
                self.sidebar.configure(height=policy.compact_controls_height)
            else:
                self.sidebar.configure(width=policy.sidebar_width)
            return
        self._layout_key, self._compact = key, compact
        # Reconfigure grid positions without unmapping the plot widget.
        for index in range(2):
            self.body.columnconfigure(index, weight=0, minsize=0)
            self.body.rowconfigure(index, weight=0, minsize=0)
        if compact:
            self.body.columnconfigure(0, weight=1)
            self.body.rowconfigure(1, weight=1)
            self.sidebar.configure(width=1, height=policy.compact_controls_height)
            self.sidebar.grid(row=0, column=0, sticky="nsew")
            self.canvas.get_tk_widget().grid(row=1, column=0, sticky="nsew")
        else:
            self.body.rowconfigure(0, weight=1)
            self.body.columnconfigure(1, weight=1)
            self.sidebar.configure(width=policy.sidebar_width, height=1)
            self.sidebar.grid(row=0, column=0, sticky="nsew")
            self.canvas.get_tk_widget().grid(row=0, column=1, sticky="nsew")

    def _options(self) -> PlotOptions:
        kind, enabled = self.kind.get(), {"grid"}
        if kind != self._last_kind:
            self._clear_limits()
            self._last_kind = kind
        if kind in ("Line", "KDE"):
            enabled.add("mean")
            if self.option_vars["mean"].get():
                enabled.add("std")
        for kinds, keys in (
            ({"Line"}, {"minimum", "maximum"}),
            ({"KDE"}, {"median", "mode"}),
            ({"Histogram"}, {"kde", "percentage"}),
            ({"Box", "Violin"}, {"vertical"}),
            ({"Violin"}, {"quartiles"}),
        ):
            if kind in kinds:
                enabled |= keys
        for key, check in self.option_checks.items():
            check.configure(state="normal" if key in enabled else "disabled")
            if key not in enabled:
                self.option_vars[key].set(False)
        self.bins_entry.configure(state="normal" if kind == "Histogram" else "disabled")
        bins = int(self.bins.get()) if kind == "Histogram" and self.bins.get().strip() else None
        return PlotOptions(
            kind=kind,
            title=self.title.get(),
            color=self.color.get(),
            bins=bins,
            x_limits=self._limits[0],
            y_limits=self._limits[1],
            **{key: variable.get() for key, variable in self.option_vars.items()},
        )

    def redraw(self, event=None) -> bool:
        try:
            options = self._options()
            self._plot_resized()
            self.renderer.render(self.data, options)
            self._update_statistics()
            self.canvas.draw_idle()
            self.last_options = options
            self.saved.configure(text="")
            return True
        except (ValueError, ArithmeticError) as exc:
            self.dialogs.error("Data analysis", str(exc))
            return False

    def _reset_title(self) -> None:
        self.title.set(self.data.title)
        self.redraw()

    def _apply_limits(self) -> None:
        try:
            limits = []
            for axis in ("x", "y"):
                low, high = (
                    self.limit_vars[f"{axis}_{end}"].get().strip() for end in ("low", "high")
                )
                if bool(low) != bool(high):
                    raise ValueError(f"Enter both {axis.upper()} limits (or leave both empty).")
                limits.append((float(low), float(high)) if low else None)
            PlotOptions(x_limits=limits[0], y_limits=limits[1])
            self._limits = tuple(limits)
            self.redraw()
        except ValueError as exc:
            self.dialogs.error("Plot limits", str(exc))

    def _reset_limits(self) -> None:
        self._clear_limits()
        self.redraw()

    def _clear_limits(self) -> None:
        for variable in self.limit_vars.values():
            variable.set("")
        self._limits = (None, None)

    def _update_statistics(self) -> None:
        stats = self.renderer.last_statistics
        if stats is None:
            text = "Samples: 0\nNo samples in the selected area."
        else:
            text = (
                f"Samples: {stats.count}\nMinimum: {stats.minimum:.6g}\nMaximum: {stats.maximum:.6g}\n"
                f"Mean: {stats.mean:.6g}\nSD: {stats.std:.6g}\nMedian: {stats.median:.6g}"
            )
        self.statistics_label.configure(text=text)

    def _save(self) -> None:
        if not self.redraw():
            return
        try:
            path = self.output.plot_path(
                self.preferences, self.last_options.kind, self.last_options.title
            )
            self.renderer.save_png(path)
            self.canvas.draw_idle()
            self.saved.configure(text=f"Saved: {path}")
        except OSError as exc:
            self.dialogs.error("Save PNG", str(exc))

    def _guide(self) -> None:
        guides = {
            "Line": "Measurements against elapsed time. Minimum, maximum, mean and sample standard deviation use only samples inside all applied X/Y limits, including the bounds. Reset limits restores the full capture. No statistics are shown when the selected area contains no samples.",
            "Histogram": "Measurement counts or percentages grouped into bins. Choose 10-200 intervals or leave empty for Auto. KDE overlay uses the same count/percentage scale.",
            "KDE": "A smooth estimate of the measurement distribution. Mean, median and mode mark their positions. Density needs at least two distinct readings.",
            "Box": "The box shows the interquartile range and median. Whiskers extend up to 1.5 times the interquartile range; isolated points are outliers.",
            "Violin": "The width shows estimated density. The median is drawn and quartiles can be added. A constant or single reading is shown as a box plot.",
        }
        self.dialogs.info(
            f"{self.kind.get()} plot guide",
            guides[self.kind.get()]
            + "\n\nApply limits to set axis bounds; Reset limits restores automatic scaling. Changing the plot type clears all limits and their entry fields. Save PNG exports at 300 dpi.",
        )
