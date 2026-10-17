# -*- coding: utf-8 -*-

"""
Responsive UI Panels for Instrument Connections and Operational Settings.

This module provides the secondary Tkinter composite widgets, managing layout 
behaviors for the ConnectionPanel (serial port/GPIB address controls) and the 
InstrumentSettingsPanel (function selectors, registers, and calibration options).

Part of the HP 3478A DMM GUI project.
Distributed under the terms of the MIT license.

Author: Lorenzo Mariani <newmarioman@hotmail.it>
"""

import tkinter as tk
from collections.abc import Callable
from idlelib.tooltip import Hovertip
from tkinter import ttk

# Local Packages
from .config import ACQUISITION_INTERVALS, DIGITS, FUNCTIONS, TRIGGERS, AppConfig
from .layout import LayoutPolicy
from .models import DmmStatus
from .plot import LiveTracePlot, NumericDisplay
from .widgets import CommandRow, ResponsiveButtonBar, ScrollableFrame, TextContextMenu


class ConnectionPanel(ttk.Frame):
    def __init__(self, parent, actions: dict[str, Callable], body_font) -> None:
        super().__init__(parent, padding=6)
        self.columnconfigure(0, weight=1)
        for row, text in ((0, "COM port"), (3, "GPIB address")):
            ttk.Label(self, text=text).grid(row=row, column=0, sticky="w", pady=(6, 3))
        self.port = ttk.Combobox(self, values=(), width=1, state="readonly", font=body_font)
        self.port.grid(row=1, column=0, sticky="ew")
        self.port_description = ttk.Label(self, text="Scanning serial ports…", wraplength=200)
        self.port_description.grid(row=2, column=0, sticky="ew", pady=4)
        self.address = ttk.Combobox(
            self,
            values=tuple(str(i) for i in range(1, 31)),
            width=1,
            state="readonly",
            font=body_font,
        )
        self.address.set("23")
        self.address.grid(row=4, column=0, sticky="ew")
        bar = ResponsiveButtonBar(self)
        bar.grid(row=5, column=0, sticky="ew", pady=6)
        self.connect_button = ttk.Button(bar, text="Connect", command=actions["connect"])
        self.disconnect_button = ttk.Button(bar, text="Disconnect", command=actions["disconnect"])
        self.refresh_button = ttk.Button(
            bar, text="Refresh ports", command=actions["refresh_ports"]
        )
        for button in (self.connect_button, self.disconnect_button, self.refresh_button):
            bar.add(button)
        self.controller = ttk.Label(self, text="Controller: —", wraplength=200)
        self.instrument = ttk.Label(self, text="Instrument: —", wraplength=200)
        for row, widget in enumerate((self.controller, self.instrument), 6):
            widget.grid(row=row, column=0, sticky="ew", pady=4)
        self.bind("<Configure>", self._resize)

    def _resize(self, event) -> None:
        for label in (self.port_description, self.controller, self.instrument):
            label.configure(wraplength=max(80, event.width - 16))

    def set_state(self, connected: bool, busy: bool, running: bool) -> None:
        self.port.configure(state="disabled" if connected or busy else "readonly")
        self.address.configure(state="disabled" if connected or busy else "readonly")
        self.connect_button.configure(
            state="normal" if not connected and not busy and bool(self.port.get()) else "disabled"
        )
        self.disconnect_button.configure(
            state="normal" if connected and not busy and not running else "disabled"
        )

    def show_status(self, status: DmmStatus | None, controller: str = "") -> None:
        self.controller.configure(text=f"Controller: {controller or '—'}")
        self.instrument.configure(text="Instrument: HP 3478A" if status else "Instrument: —")


class InstrumentSettingsPanel(ttk.Frame):
    def __init__(self, parent, actions: dict[str, Callable], body_font) -> None:
        super().__init__(parent, padding=6)
        self.columnconfigure(0, weight=1)
        self._actions, self._syncing = actions, False
        self._status: DmmStatus | None = None
        self.measurement_info = ttk.LabelFrame(self, text="Measurement status", padding=5)
        self.measurement_info.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.measurement_info.columnconfigure(0, weight=1)
        self.status_labels = {}
        for row, name in enumerate(("Inputs", "Line", "Autozero", "Autorange", "Trigger")):
            label = ttk.Label(self.measurement_info, text=f"{name}: —", wraplength=200)
            label.grid(row=row, column=0, sticky="ew", pady=1)
            self.status_labels[name] = label
        self.functions = ResponsiveButtonBar(self)
        self.functions.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        self.function_buttons: dict[int, tk.Button] = {}
        for spec in FUNCTIONS:
            button = tk.Button(
                self.functions,
                text=spec.label,
                font=body_font,
                relief="groove",
                background="#f0f0f0",
                activebackground="#d9f8df",
                command=lambda code=int(spec.code): actions["function"](code),
            )
            self.function_buttons[int(spec.code)] = button
            self.functions.add(button)
        self.variables, self.widgets, self.cards = {}, {}, []
        self.fields = ttk.Frame(self)
        self.fields.grid(row=2, column=0, sticky="ew")
        self._columns = 0
        for key, label in (
            ("autorange", "AUTORANGE"),
            ("range", "RANGE"),
            ("digits", "DIGITS"),
            ("autozero", "AUTOZERO"),
            ("trigger", "TRIGGER"),
            ("display", "INSTR DISPLAY"),
        ):
            card = ttk.Frame(self.fields, padding=3)
            card.columnconfigure(0, weight=1)
            ttk.Label(card, text=label).grid(row=0, column=0, sticky="w", pady=(0, 3))
            value = tk.StringVar(self, "—")
            widget = ttk.Combobox(
                card, textvariable=value, values=(), width=1, state="disabled", font=body_font
            )
            widget.grid(row=1, column=0, sticky="ew")
            widget.bind("<<ComboboxSelected>>", lambda event, key=key: self._changed(key))
            self.variables[key], self.widgets[key] = value, widget
            self.cards.append(card)
        Hovertip(
            self.widgets["display"],
            "Display mode is the last command sent; the B response does not report it.",
        )
        Hovertip(
            self.widgets["trigger"],
            "B reports internal/external trigger flags. Fast, Single and Hold retain the last compatible setting.",
        )
        self.fields.bind("<Configure>", self._resize_fields)
        self._arrange_fields(2)
        commands = ResponsiveButtonBar(self)
        commands.grid(row=4, column=0, sticky="ew", pady=4)
        self.status_button = ttk.Button(commands, text="Read status", command=actions["status"])
        self.reset_button = ttk.Button(commands, text="Reset", command=actions["reset"])
        self.clear_errors_button = ttk.Button(
            commands, text="Clear errors", command=actions["clear_errors"]
        )
        for button in (self.status_button, self.reset_button, self.clear_errors_button):
            commands.add(button)
        calibration = ttk.LabelFrame(self, text="Calibration RAM", padding=5)
        calibration.grid(row=6, column=0, sticky="ew", pady=6)
        calibration.columnconfigure(0, weight=1)
        self.calibration_info = ttk.Label(
            calibration, text="Calibration enabled: —", wraplength=200
        )
        self.calibration_info.grid(row=0, column=0, sticky="ew", pady=3)
        self.error_info = ttk.Label(
            calibration, text="Error register: —\nSRQ mask: —", wraplength=200
        )
        self.error_info.grid(row=1, column=0, sticky="ew", pady=3)
        bar = ResponsiveButtonBar(calibration)
        bar.grid(row=2, column=0, sticky="ew", pady=4)
        self.backup_button = ttk.Button(
            bar, text="Backup RAM…", command=actions["backup_calibration"]
        )
        self.restore_button = ttk.Button(
            bar, text="Restore RAM…", command=actions["restore_calibration"]
        )
        bar.add(self.backup_button)
        bar.add(self.restore_button)
        Hovertip(
            self.backup_button,
            "Read all 256 RAM locations twice and save the complete calibration image.",
        )
        Hovertip(
            self.restore_button,
            "Restore a backup from this DMM. The front-panel CAL ENABLE switch must be ON; a recovery backup is saved first.",
        )

    def _changed(self, key: str) -> None:
        if not self._syncing and str(self.widgets[key].cget("state")) == "readonly":
            self._actions["option"](key, self.variables[key].get())

    def apply_status(self, status: DmmStatus | None) -> None:
        self._status, self._syncing = status, True
        try:
            flag_values = {
                "Inputs": ("Front" if status.front_input else "Rear") if status else "—",
                "Line": f"{status.line_hz} Hz" if status else "—",
                "Autozero": ("ON" if status.autozero else "OFF") if status else "—",
                "Autorange": ("ON" if status.autorange else "OFF") if status else "—",
                "Trigger": status.trigger_label if status else "—",
            }
            for name, label in self.status_labels.items():
                label.configure(text=f"{name}: {flag_values[name]}")
            self.calibration_info.configure(
                text=f"Calibration enabled: {'Yes' if status.calibration_enabled else 'No'}"
                if status
                else "Calibration enabled: —"
            )
            self.error_info.configure(
                text=f"Error register: 0x{status.error_register:02X}\nSRQ mask: 0x{status.srq_mask:02X}"
                if status
                else "Error register: —\nSRQ mask: —"
            )
            for code, button in self.function_buttons.items():
                button.configure(
                    background="lawngreen" if status and code == status.function_code else "#f0f0f0"
                )
            choices = {
                "autorange": ("OFF", "ON"),
                "range": tuple(item.label for item in status.function.ranges) if status else (),
                "digits": DIGITS,
                "autozero": ("OFF", "ON"),
                "trigger": TRIGGERS,
                "display": ("ON", "OFF"),
            }
            values = {
                "autorange": "ON" if status and status.autorange else "OFF",
                "range": status.range.label if status else "—",
                "digits": status.digits if status else "—",
                "autozero": "ON" if status and status.autozero else "OFF",
                "trigger": status.trigger_label if status else "—",
                "display": (status.display_mode or "Unknown") if status else "—",
            }
            for key, widget in self.widgets.items():
                widget.configure(values=choices[key] if status else ())
                self.variables[key].set(values[key] if status else "—")
        finally:
            self._syncing = False

    def set_state(self, enabled: bool) -> None:
        for button in (
            *self.function_buttons.values(),
            self.status_button,
            self.reset_button,
            self.clear_errors_button,
            self.backup_button,
            self.restore_button,
        ):
            button.configure(state="normal" if enabled else "disabled")
        for key, widget in self.widgets.items():
            fixed = (
                self._status is not None
                and self._status.range.command is None
                and key in ("range", "autorange")
            )
            widget.configure(state="readonly" if enabled and not fixed else "disabled")

    def _resize_fields(self, event) -> None:
        self._arrange_fields(LayoutPolicy.columns(event.width, 150, 2))
        for label in (*self.status_labels.values(), self.calibration_info, self.error_info):
            label.configure(wraplength=max(80, event.width - 30))

    def _arrange_fields(self, columns: int) -> None:
        if self._columns == columns:
            return
        self._columns = columns
        for column in range(2):
            self.fields.columnconfigure(
                column, weight=int(column < columns), uniform="settings" if column < columns else ""
            )
        for index, card in enumerate(self.cards):
            card.grid(row=index // columns, column=index % columns, sticky="ew")


class ConsolePanel(ttk.Frame):
    def __init__(self, parent, actions: dict[str, Callable], mono_font, body_font) -> None:
        super().__init__(parent, padding=4)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.text = tk.Text(
            self,
            height=1,
            width=1,
            state="disabled",
            background="black",
            foreground="white",
            font=mono_font,
            wrap="word",
        )
        self.text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self, command=self.text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=scroll.set)
        self.clear_button = ttk.Button(self, text="Clear console", command=self.clear)
        self.clear_button.grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=4
        )
        self.command_row = CommandRow(self, "Custom command", (("Send", actions["custom"]),))
        self.command_row.grid(row=2, column=0, columnspan=2, sticky="ew", padx=4, pady=4)
        self.command, self.custom_button = self.command_row.entry, self.command_row.buttons[0]
        self.command.configure(font=body_font)
        self.command.bind("<Return>", lambda event: actions["custom"]())
        self.query_var = tk.BooleanVar(self, False)
        self.query_check = ttk.Checkbutton(
            self, text="Read response after custom command", variable=self.query_var
        )
        self.query_check.grid(row=3, column=0, columnspan=2, sticky="w", padx=4, pady=2)

    def set_state(self, connected: bool, busy: bool, running: bool) -> None:
        enabled = "normal" if connected and not busy and not running else "disabled"
        for widget in (self.command, self.custom_button, self.query_check):
            widget.configure(state=enabled)

    def append(self, direction: str, text: str) -> None:
        self.text.configure(state="normal")
        self.text.insert("end", f"{direction}>> {text.rstrip()}\n")
        lines = int(self.text.index("end-1c").split(".")[0])
        if lines > 2000:
            self.text.delete("1.0", f"{lines - 2000}.0")
        self.text.configure(state="disabled")
        self.text.see("end")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")


class AcquisitionPanel(ttk.Frame):
    def __init__(
        self, parent, actions: dict[str, Callable], body_font, options_parent, display_font_path
    ) -> None:
        super().__init__(parent, padding=4)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self.display = NumericDisplay(self, body_font, display_font_path)
        self.display.grid(row=0, column=0, sticky="ew", padx=4, pady=4)
        bar = ResponsiveButtonBar(self)
        bar.grid(row=1, column=0, sticky="ew", pady=2)
        self.start_button = ttk.Button(bar, text="Start ACQ", command=actions["start"])
        self.stop_button = ttk.Button(bar, text="Stop ACQ", command=actions["stop"])
        self.clear_plot_button = ttk.Button(bar, text="Clear plot", command=actions["clear_plot"])
        self.clear_display_button = ttk.Button(
            bar, text="Clear display", command=actions["clear_display"]
        )
        for widget in (
            self.start_button,
            self.stop_button,
            self.clear_plot_button,
            self.clear_display_button,
        ):
            bar.add(widget)
        options = ttk.LabelFrame(options_parent, text="Acquisition options", padding=4)
        options.grid(row=5, column=0, sticky="ew", pady=6)
        options.columnconfigure(0, weight=1)
        self.rate_var, self.history_var = tk.StringVar(self, "MAX"), tk.StringVar(self, "100")
        for row, (label, variable, values, key) in enumerate(
            (
                ("Interval [s]", self.rate_var, ACQUISITION_INTERVALS, "rate"),
                ("History [samples]", self.history_var, ("100", "250", "500", "1000"), "history"),
            )
        ):
            card = ttk.Frame(options, padding=2)
            card.grid(row=row, column=0, sticky="ew")
            card.columnconfigure(0, weight=1)
            ttk.Label(card, text=label).grid(row=0, column=0, sticky="w")
            combo = ttk.Combobox(
                card,
                textvariable=variable,
                values=values,
                width=1,
                state="readonly",
                font=body_font,
            )
            combo.grid(row=1, column=0, sticky="ew")
            setattr(self, key, combo)
        self.plot_var, self.logger_var = tk.BooleanVar(self, True), tk.BooleanVar(self, True)
        self.graph_check = ttk.Checkbutton(
            options, text="Graph", variable=self.plot_var, command=actions["graph"]
        )
        self.log_check = ttk.Checkbutton(options, text="File log", variable=self.logger_var)
        self.graph_check.grid(row=2, column=0, sticky="w", padx=2, pady=3)
        self.log_check.grid(row=3, column=0, sticky="w", padx=2, pady=3)
        self.plot = LiveTracePlot(self)
        self.plot.grid(row=2, column=0, sticky="nsew", padx=4, pady=4)
        Hovertip(
            self.rate,
            "MAX reads as fast as the DMM allows. Numeric values are sampling intervals in seconds.",
        )
        Hovertip(
            self.history,
            "Keep the latest samples in a scrolling plot. File log records all readings.",
        )

    def set_state(self, connected: bool, busy: bool, running: bool, stopping: bool) -> None:
        idle = not busy and not running
        enabled = "normal" if connected and idle else "disabled"
        self.start_button.configure(state=enabled)
        self.stop_button.configure(state="normal" if running and not stopping else "disabled")
        for combo in (self.rate, self.history):
            combo.configure(state="readonly" if idle else "disabled")
        for widget in (
            self.graph_check,
            self.log_check,
            self.clear_plot_button,
            self.clear_display_button,
        ):
            widget.configure(state="normal" if idle else "disabled")


class ApplicationView(ttk.Frame):
    def __init__(
        self, root: tk.Tk, config: AppConfig, actions: dict[str, Callable], themes
    ) -> None:
        super().__init__(root, padding=4)
        self.grid(row=0, column=0, sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.themes = themes
        self._compact, self._controls_open, self._font_size = None, True, 10
        self._layout_key = None
        self._flash_ids = {}
        toolbar = ResponsiveButtonBar(self)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.controls_button = ttk.Button(toolbar, text="Controls", command=self.toggle_controls)
        self.analysis_button = ttk.Button(
            toolbar, text="Data analysis", command=actions["analysis"]
        )
        self.folder_button = ttk.Button(toolbar, text="Folder", command=actions["folder"])
        self.configuration_button = ttk.Button(
            toolbar, text="Config", command=actions["configuration"]
        )
        self.help_button = ttk.Button(toolbar, text="Help", command=actions["help"])
        for button in (
            self.controls_button,
            self.analysis_button,
            self.folder_button,
            self.configuration_button,
            self.help_button,
        ):
            toolbar.add(button)
        self.body = ttk.Frame(self)
        self.body.grid(row=1, column=0, sticky="nsew")
        self.sidebar = ttk.Frame(self.body)
        self.sidebar.grid_propagate(False)
        self.sidebar.columnconfigure(0, weight=1)
        self.sidebar.rowconfigure(0, weight=1)
        self.notebook = ttk.Notebook(self.sidebar)
        self.notebook.grid(row=0, column=0, sticky="nsew")
        self.connection_page, self.settings_page = (
            ScrollableFrame(self.notebook),
            ScrollableFrame(self.notebook),
        )
        self.console = ConsolePanel(self.notebook, actions, themes.mono_font, themes.body_font)
        for page, label in (
            (self.connection_page, "Connection"),
            (self.settings_page, "Settings"),
            (self.console, "Console"),
        ):
            self.notebook.add(page, text=label)
        self.connection = ConnectionPanel(self.connection_page.content, actions, themes.body_font)
        self.connection.grid(row=0, column=0, sticky="ew")
        self.settings = InstrumentSettingsPanel(
            self.settings_page.content, actions, themes.body_font
        )
        self.settings.grid(row=0, column=0, sticky="ew")
        self.acquisition = AcquisitionPanel(
            self.body, actions, themes.body_font, self.settings, config.display_font_path
        )
        footer = ttk.Frame(self)
        footer.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        footer.columnconfigure(0, weight=1)
        self.status = ttk.Label(footer, text="Ready", wraplength=300)
        self.status.grid(row=0, column=0, sticky="ew", padx=4)
        indicators = ttk.Frame(footer)
        indicators.grid(row=0, column=1, sticky="e")
        self.indicators = {}
        for column, direction in enumerate(("LINK", "TX", "RX", "ACQ")):
            block = ttk.Frame(indicators)
            block.grid(row=0, column=column, padx=3)
            canvas = tk.Canvas(block, width=9, height=9, highlightthickness=0)
            canvas.grid(row=0, column=0, padx=(0, 3))
            rectangle = canvas.create_rectangle(0, 0, 9, 9, fill="black", outline="")
            ttk.Label(block, text=direction).grid(row=0, column=1)
            self.indicators[direction] = canvas, rectangle
        self.context_menu = TextContextMenu(root, self.console.clear)
        self.bind("<Configure>", self._resize)
        self._apply_layout(LayoutPolicy(config.width, config.height, self._dpi()))

    def _dpi(self) -> float:
        return max(1.0, self.winfo_fpixels("1i") / 96)

    def toggle_controls(self) -> None:
        self._controls_open = not self._controls_open
        self._apply_layout(LayoutPolicy(self.winfo_width(), self.winfo_height(), self._dpi()))

    def _resize(self, event) -> None:
        policy = LayoutPolicy(event.width, event.height, self._dpi())
        self.themes.resize_fonts(policy.font_size)
        if policy.font_size != self._font_size:
            self._font_size = policy.font_size
            self.acquisition.plot.renderer.set_font_size(policy.font_size)
            self.acquisition.plot.canvas.draw_idle()
        self._apply_layout(policy)
        self.status.configure(wraplength=max(80, event.width - 230))
        self.acquisition.display.fit()

    def _apply_layout(self, policy: LayoutPolicy) -> None:
        key = (policy.compact, self._controls_open)
        if key == self._layout_key:
            if policy.compact:
                self.sidebar.configure(height=policy.compact_controls_height)
            else:
                self.sidebar.configure(width=policy.sidebar_width)
            return
        self._layout_key, self._compact = key, policy.compact
        if not self._controls_open:
            self.sidebar.grid_remove()
        for index in range(2):
            self.body.columnconfigure(index, weight=0, minsize=0)
            self.body.rowconfigure(index, weight=0, minsize=0)
        self.controls_button.configure(text="Hide controls" if self._controls_open else "Controls")
        if policy.compact:
            self.body.columnconfigure(0, weight=1)
            self.body.rowconfigure(1, weight=1)
            if self._controls_open:
                self.sidebar.configure(width=1, height=policy.compact_controls_height)
                self.sidebar.grid(row=0, column=0, sticky="nsew", pady=(0, 4))
            self.acquisition.grid(row=1, column=0, sticky="nsew")
        else:
            self.body.rowconfigure(0, weight=1)
            self.body.columnconfigure(1, weight=1)
            if self._controls_open:
                self.sidebar.configure(width=policy.sidebar_width, height=1)
                self.sidebar.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
            self.acquisition.grid(row=0, column=1, sticky="nsew")

    def set_state(self, connected: bool, busy: bool, running: bool, stopping: bool) -> None:
        self.connection.set_state(connected, busy, running)
        self.settings.set_state(connected and not busy and not running)
        self.console.set_state(connected, busy, running)
        self.acquisition.set_state(connected, busy, running, stopping)
        self.configuration_button.configure(
            state="normal" if not busy and not running else "disabled"
        )
        self.analysis_button.configure(state="normal" if not busy else "disabled")
        for name, active, color in (("LINK", connected, "lawngreen"), ("ACQ", running, "magenta")):
            canvas, rectangle = self.indicators[name]
            canvas.itemconfigure(rectangle, fill=color if active else "black")

    def flash(self, direction: str) -> None:
        canvas, rectangle = self.indicators[direction]
        canvas.itemconfigure(rectangle, fill="blue" if direction == "TX" else "orange")
        if direction in self._flash_ids:
            self.after_cancel(self._flash_ids[direction])
        self._flash_ids[direction] = self.after(80, lambda: self._end_flash(direction))

    def _end_flash(self, direction: str) -> None:
        canvas, rectangle = self.indicators[direction]
        canvas.itemconfigure(rectangle, fill="black")
        self._flash_ids.pop(direction, None)
