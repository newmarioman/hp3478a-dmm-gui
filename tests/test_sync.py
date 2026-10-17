"""Real Tcl variables with fake views verify synchronization and thread ownership."""

import threading
import tkinter as tk
import unittest
from types import SimpleNamespace

from hp3478a.app import DmmApplication
from hp3478a.config import FUNCTIONS, TRIGGERS
from hp3478a.instrument import HP3478A
from hp3478a.layout import LayoutPolicy
from hp3478a.ports import PortSelection, SerialPort, SerialPortDiscovery
from hp3478a.status import StatusDecoder
from hp3478a.tasks import BackgroundExecutor
from hp3478a.transport import SimulatedTransport
from hp3478a.ui import InstrumentSettingsPanel


class Widget:
    def __init__(self, **options):
        self.options = options

    def configure(self, **options):
        self.options.update(options)

    def cget(self, name):
        return self.options[name]


class Root:
    def __init__(self):
        self.owner, self.callbacks, self.cancelled = threading.get_ident(), [], []

    def after(self, delay, callback):
        if threading.get_ident() != self.owner:
            raise AssertionError("A worker called root.after")
        self.callbacks.append(callback)
        return len(self.callbacks)

    def after_cancel(self, identifier):
        if threading.get_ident() != self.owner:
            raise AssertionError("A worker called root.after_cancel")
        self.cancelled.append(identifier)


class SettingsSyncTests(unittest.TestCase):
    def setUp(self):
        self.tcl, self.calls = tk.Tcl(), []
        self.panel = InstrumentSettingsPanel.__new__(InstrumentSettingsPanel)
        self.panel._syncing, self.panel._status = False, None
        self.panel._actions = {"option": lambda key, value: self.calls.append((key, value))}
        keys = ("autorange", "range", "digits", "autozero", "trigger", "display")
        self.panel.variables = {key: tk.StringVar(self.tcl) for key in keys}
        self.panel.widgets = {key: Widget(state="readonly") for key in keys}
        self.panel.function_buttons = {int(spec.code): Widget() for spec in FUNCTIONS}
        self.panel.status_labels = {
            name: Widget() for name in ("Inputs", "Line", "Autozero", "Autorange", "Trigger")
        }
        self.panel.calibration_info, self.panel.error_info = Widget(), Widget()
        self.panel.backup_button, self.panel.restore_button = Widget(), Widget()
        self.panel.status_button, self.panel.reset_button, self.panel.clear_errors_button = (
            Widget(),
            Widget(),
            Widget(),
        )

    def test_binary_flags_update_values_and_only_actual_function_is_green(self):
        status = StatusDecoder().decode(bytes((0xAA, 0x5E, 0, 0, 0)), display_mode="OFF")
        self.panel.apply_status(status)
        self.assertEqual(self.panel.variables["range"].get(), "3 A")
        self.assertEqual(self.panel.variables["digits"].get(), "4½")
        self.assertEqual(self.panel.variables["trigger"].get(), "External")
        self.assertEqual(self.panel.variables["display"].get(), "OFF")
        green = [
            code
            for code, button in self.panel.function_buttons.items()
            if button.cget("background") == "lawngreen"
        ]
        self.assertEqual(green, [5])
        self.assertEqual(self.calls, [])

    def test_measurement_labels_in_settings_follow_the_binary_flags(self):
        self.panel.apply_status(StatusDecoder().decode(bytes((45, 31, 0, 0, 0))))
        self.assertEqual(
            {name: label.cget("text") for name, label in self.panel.status_labels.items()},
            {
                "Inputs": "Inputs: Front",
                "Line": "Line: 50 Hz",
                "Autozero": "Autozero: ON",
                "Autorange": "Autorange: ON",
                "Trigger": "Trigger: Internal",
            },
        )
        self.panel.apply_status(None)
        self.assertTrue(
            all(label.cget("text").endswith("—") for label in self.panel.status_labels.values())
        )  

    def test_status_sync_suppresses_even_variable_trace_feedback(self):
        for key, variable in self.panel.variables.items():
            variable.trace_add("write", lambda *args, key=key: self.panel._changed(key))
        self.panel.apply_status(StatusDecoder().decode(bytes((45, 31, 0, 0, 0))))
        self.assertEqual(self.calls, [])
        self.assertFalse(self.panel._syncing)

    def test_only_user_selection_sends_one_setting_immediately(self):
        self.panel.variables["digits"].set("4½")
        self.assertEqual(self.calls, [])
        self.panel._changed("digits")
        self.assertEqual(self.calls, [("digits", "4½")])
        self.panel.set_state(False)
        self.panel._changed("digits")
        self.assertEqual(len(self.calls), 1)

    def test_disconnect_clears_controls_and_extended_ohms_disables_fixed_range(self):
        self.panel.apply_status(StatusDecoder().decode(bytes((229, 31, 0, 0, 0))))
        self.panel.set_state(True)
        self.assertEqual(self.panel.widgets["range"].cget("state"), "disabled")
        self.assertEqual(self.panel.widgets["autorange"].cget("state"), "disabled")
        self.panel.apply_status(None)
        self.assertTrue(all(v.get() == "—" for v in self.panel.variables.values()))
        self.assertTrue(
            all(b.cget("background") != "lawngreen" for b in self.panel.function_buttons.values())
        )

    def test_queued_event_captures_value_before_worker_runs(self):
        jobs, app = [], DmmApplication.__new__(DmmApplication)
        bus = SimulatedTransport(sample_delay=0)
        app.instrument = HP3478A(bus, command_delay=0, status_delay=0, settle_delay=0)
        app.instrument.connect("SIMULATED", 23)
        app._run_task = lambda label, operation: jobs.append(operation)
        self.panel._actions["option"] = app.change_option
        self.panel.variables["digits"].set("4½")
        self.panel._changed("digits")
        self.panel.variables["digits"].set("3½")
        try:
            self.assertEqual(jobs[0]().digits, "4½")
        finally:
            app.instrument.disconnect()


class WorkerTests(unittest.TestCase):
    def test_success_and_errors_return_to_owner_thread(self):
        root, values = Root(), []
        executor = BackgroundExecutor(root)
        owner = threading.get_ident()
        try:
            executor.submit(
                lambda: 7,
                lambda value: values.append((value, threading.get_ident())),
                lambda exc: self.fail(str(exc)),
            )

            def fail():
                raise OSError("Injected error")

            executor.submit(
                fail,
                lambda value: self.fail("Unexpected success"),
                lambda exc: values.append((str(exc), threading.get_ident())),
            )
            executor._executor.submit(lambda: None).result(timeout=1)
            self.assertEqual(values, [])
            executor._drain()
            self.assertEqual(values, [(7, owner), ("Injected error", owner)])
        finally:
            executor.shutdown()

    def test_latest_updates_do_not_accumulate_a_stale_console_queue(self):
        root, values = Root(), []
        executor = BackgroundExecutor(root)
        try:
            for index in range(1000):
                executor.post_latest("progress", values.append, index)
            executor._drain()
            self.assertEqual(values, [999])
        finally:
            executor.shutdown()

    def test_close_reasserts_stop_after_a_queued_start_clears_it(self):
        root, submitted = Root(), []
        app = DmmApplication.__new__(DmmApplication)
        stopped = []
        app.root, app.busy, app._port_scan_pending = root, True, False
        app.acquisition = SimpleNamespace(running=False, stop=lambda: stopped.append(True))
        app._wait_for_close()
        self.assertEqual(len(stopped), 1)
        # The queued startup now begins and clears its earlier stop request.
        app.busy, app.acquisition.running = False, True
        root.callbacks.pop(0)()
        self.assertEqual(len(stopped), 2)
        app.acquisition.running = False
        app.instrument = SimpleNamespace(disconnect=lambda: None)
        app.executor = SimpleNamespace(submit=lambda *args: submitted.append(args))
        root.callbacks.pop(0)()
        self.assertEqual(len(submitted), 1)
        self.assertTrue(app.busy)


class PortLayoutTests(unittest.TestCase):
    def test_actual_ports_are_naturally_sorted_deduplicated_and_kept_when_connected(self):
        ports = [
            SimpleNamespace(device=device, description="USB")
            for device in ("COM10", "COM2", "COM2", "")
        ]
        detected = SerialPortDiscovery(lambda: ports).scan()
        self.assertEqual([p.device for p in detected], ["COM2", "COM10"])
        choice = PortSelection()
        self.assertEqual(choice.update(detected), "COM2")
        self.assertEqual(choice.update((SerialPort("COM10"),), connected=True), "COM2")
        self.assertEqual(choice.update((SerialPort("COM10"),)), "COM10")

    def test_compact_layout_and_dpi_use_bounded_controls_and_fonts(self):
        for width, height, dpi in ((640, 480, 1), (1366, 768, 1), (1920, 1080, 2), (3840, 2160, 2)):
            policy = LayoutPolicy(width, height, dpi)
            self.assertEqual(policy.compact, width / dpi < 1000)
            self.assertLessEqual(policy.compact_controls_height, 180 * dpi)
            self.assertGreaterEqual(policy.font_size, 8)
            self.assertLessEqual(policy.font_size, 14)
        self.assertEqual(LayoutPolicy.columns(100, 150, 2), 1)


if __name__ == "__main__":
    unittest.main()
