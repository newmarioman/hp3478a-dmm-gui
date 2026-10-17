"""Exercise Console construction and controller wiring without a native display."""

import tkinter as tk
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hp3478a.app import DmmApplication
from hp3478a.ui import ApplicationView


class Surface:
    """Record widget ownership, geometry and callbacks while keeping real Tcl variables."""

    def __init__(self, parent=None, *args, **options):
        self.master = parent
        self.tk = parent.tk if parent is not None else None
        self.options, self.placement, self.bindings = options, {}, {}
        self.columns, self.rows, self.value = {}, {}, ""
        self.content = self

    def _root(self):
        return self.master._root()

    def configure(self, **options):
        self.options.update(options)

    def grid(self, **options):
        self.placement = options

    def grid_forget(self):
        self.placement = {}

    grid_remove = grid_forget

    def grid_propagate(self, value):
        pass

    def columnconfigure(self, column, **options):
        self.columns[column] = options

    def rowconfigure(self, row, **options):
        self.rows[row] = options

    def bind(self, event, callback, **options):
        self.bindings[event] = callback

    def winfo_reqwidth(self):
        return max(40, len(self.options.get("text", "")) * 8)

    def get(self):
        return self.value

    def set(self, value):
        self.value = value

    def delete(self, first, last):
        self.value = ""

    def insert(self, index, value):
        self.value += value

    def index(self, index):
        return f"{len(self.value.splitlines()) + 1}.0"

    def see(self, index):
        pass

    def yview(self, *args):
        pass

    def add(self, page, **options):
        pass

    def create_rectangle(self, *args, **options):
        return 1

    def itemconfigure(self, *args, **options):
        pass

    def set_state(self, *args):
        self.state_args = args

    def invoke(self):
        if self.options.get("state") != "disabled":
            return self.options["command"]()


class DeferredExecutor:
    def __init__(self):
        self.pending = []

    def submit(self, operation, success, failure):
        self.pending.append((operation, success, failure))

    def complete(self):
        operation, success, failure = self.pending.pop(0)
        try:
            result = operation()
        except Exception as error:
            failure(error)
        else:
            success(result)


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for method in (
            "__init__",
            "configure",
            "grid",
            "grid_forget",
            "grid_remove",
            "grid_propagate",
            "columnconfigure",
            "rowconfigure",
            "bind",
        ):
            self.stack.enter_context(patch(f"tkinter.ttk.Frame.{method}", getattr(Surface, method)))
        for name in ("Text", "Canvas"):
            self.stack.enter_context(patch(f"hp3478a.ui.tk.{name}", Surface))
        for name in (
            "Label",
            "Button",
            "Entry",
            "Checkbutton",
            "Scrollbar",
            "Notebook",
            "LabelFrame",
            "Combobox",
        ):
            self.stack.enter_context(patch(f"hp3478a.ui.ttk.{name}", Surface))
        for name in (
            "ScrollableFrame",
            "ConnectionPanel",
            "InstrumentSettingsPanel",
            "NumericDisplay",
            "LiveTracePlot",
            "TextContextMenu",
        ):
            self.stack.enter_context(patch(f"hp3478a.ui.{name}", Surface))
        self.stack.enter_context(patch("hp3478a.ui.Hovertip"))
        self.stack.enter_context(patch.object(ApplicationView, "_dpi", return_value=1))

        self.tcl = tk.Tcl()
        root = Surface(self.tcl)
        self.app = DmmApplication.__new__(DmmApplication)
        self.app.busy, self.app._closing = False, False
        self.app.acquisition = SimpleNamespace(running=False, stopping=False)
        self.app.instrument = Mock(connected=True, status=None, connection_info=None)
        self.app.executor, self.app.dialogs = DeferredExecutor(), SimpleNamespace(error=Mock())
        actions = {
            name: Mock()
            for name in (
                "start",
                "stop",
                "clear_plot",
                "clear_display",
                "graph",
                "analysis",
                "folder",
                "configuration",
                "help",
            )
        }
        actions["custom"] = self.app.send_custom
        self.body_font = object()
        themes = SimpleNamespace(body_font=self.body_font, mono_font=object())
        config = SimpleNamespace(width=1200, height=800, display_font_path=None)
        self.app.view = ApplicationView(root, config, actions, themes)
        self.app.view.settings.apply_status = Mock()
        self.app.view.connection.show_status = Mock()
        self.console = self.app.view.console
        self.app._refresh_state()

    def test_custom_controls_belong_to_console_below_clear_console(self):
        self.assertIs(self.console.clear_button.master, self.console)
        self.assertIs(self.console.command_row.master, self.console)
        self.assertIs(self.console.query_check.master, self.console)
        self.assertIs(self.console.command.master, self.console.command_row)
        self.assertIs(self.console.custom_button.master, self.console.command_row)
        self.assertLess(
            self.console.clear_button.placement["row"], self.console.command_row.placement["row"]
        )
        self.assertLess(
            self.console.command_row.placement["row"], self.console.query_check.placement["row"]
        )
        self.assertEqual(self.console.command_row.label.options["text"], "Custom command")
        self.assertIs(self.console.command.options["font"], self.body_font)
        self.assertFalse(self.console.query_var.get())
        self.assertEqual(self.console.rows[0]["weight"], 1)
        for attribute in ("command", "custom_button", "query_var", "query_check"):
            self.assertFalse(hasattr(self.app.view.acquisition, attribute))

    def test_command_row_adapts_to_narrow_and_wide_console(self):
        row = self.console.command_row
        for width, compact in ((220, True), (800, False)):
            row._reflow(width)
            self.assertEqual(row._compact, compact)
            self.assertEqual(row.entry.placement["row"], row.buttons[0].placement["row"])
            self.assertNotEqual(row.entry.placement["column"], row.buttons[0].placement["column"])
            self.assertEqual(row.label.placement["row"] < row.entry.placement["row"], compact)

    def test_send_and_enter_use_console_values_and_clear_entry_on_success(self):
        for query, enter in ((False, False), (True, True)):
            with self.subTest(query=query, enter=enter):
                self.console.command.set("++ver")
                self.console.query_var.set(query)
                self.assertEqual(self.app.executor.pending, [])
                if enter:
                    self.console.command.bindings["<Return>"](None)
                else:
                    self.console.custom_button.invoke()
                self.assertTrue(self.app.busy)
                self.assertEqual(self.console.command.options["state"], "disabled")
                self.app.executor.complete()
                self.app.instrument.send_custom.assert_called_with("++ver", query)
                self.assertFalse(self.app.busy)
                self.assertEqual(self.console.command.get(), "")
                self.assertEqual(self.console.custom_button.options["state"], "normal")
        self.assertEqual(self.app.instrument.send_custom.call_count, 2)

    def test_failed_command_keeps_entry_and_restores_controls(self):
        self.app.instrument.send_custom.side_effect = TimeoutError("DMM response timed out")
        self.console.command.set("B")
        self.console.custom_button.invoke()
        self.app.executor.complete()
        self.assertEqual(self.console.command.get(), "B")
        self.assertFalse(self.app.busy)
        self.assertEqual(self.console.command.options["state"], "normal")
        self.assertIn("DMM response timed out", self.console.text.get())
        self.app.dialogs.error.assert_called_once()

    def test_empty_command_is_ignored(self):
        self.console.command.set(" \t")
        self.console.custom_button.invoke()
        self.assertEqual(self.app.executor.pending, [])
        self.app.instrument.send_custom.assert_not_called()

    def test_connection_and_acquisition_state_gate_console_controls(self):
        for connected, busy, running, closing in (
            (False, False, False, False),
            (True, False, False, False),
            (True, True, False, False),
            (True, False, True, False),
            (True, False, False, True),
        ):
            with self.subTest(connected=connected, busy=busy, running=running, closing=closing):
                self.app.instrument.connected, self.app.busy = connected, busy
                self.app.acquisition.running, self.app._closing = running, closing
                self.app._refresh_state()
                expected = (
                    "normal"
                    if connected and not busy and not running and not closing
                    else "disabled"
                )
                for widget in (
                    self.console.command,
                    self.console.custom_button,
                    self.console.query_check,
                ):
                    self.assertEqual(widget.options["state"], expected)

    def test_enter_while_busy_does_not_queue_another_operation(self):
        self.console.command.set("B")
        self.console.custom_button.invoke()
        self.console.command.bindings["<Return>"](None)
        self.assertEqual(len(self.app.executor.pending), 1)

    def test_clear_console_keeps_custom_entry_and_read_option(self):
        self.console.command.set("++ver")
        self.console.query_var.set(True)
        self.console.append("INFO", "Previous response")
        self.console.clear_button.invoke()
        self.assertEqual(self.console.text.get(), "")
        self.assertEqual(self.console.command.get(), "++ver")
        self.assertTrue(self.console.query_var.get())


if __name__ == "__main__":
    unittest.main()
