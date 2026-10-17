# -*- coding: utf-8 -*-

"""
Main Application Coordinator and Component Orchestrator.

This module wires together the hardware transport layers, data acquisition 
threads, user preferences persistence, and Tkinter application views 
without maintaining a global state.

Part of the HP 3478A DMM GUI project.
Distributed under the terms of the MIT license.

Author: Lorenzo Mariani <newmarioman@hotmail.it>
"""

import tkinter as tk
from collections.abc import Callable

# Local Packages
from .acquisition import AcquisitionController, AcquisitionOptions, AcquisitionResult
from .analysis_ui import AnalysisWindow
from .calibration import CalibrationManager, CalibrationRepository
from .config import AppConfig, UserSettings
from .dialogs import ConfigurationDialog, DialogService, SoundNotifier, ThemeManager
from .instrument import HP3478A
from .ports import SIMULATED_PORT, PortSelection, SerialPortDiscovery
from .storage import CsvCaptureRepository, OutputManager, SettingsRepository
from .tasks import BackgroundExecutor
from .transport import AR488Transport, InstrumentTransport, SimulatedTransport
from .ui import ApplicationView


class DmmApplication:
    def __init__(
        self,
        root: tk.Tk,
        config: AppConfig,
        *,
        demo: bool = False,
        sound: bool = True,
        maximize: bool = True,
        transport: InstrumentTransport | None = None,
        port_discovery: SerialPortDiscovery | None = None,
    ) -> None:
        self.root, self.config, self.demo = root, config, demo
        self.busy, self._closing, self._port_scan_pending = False, False, False
        self._port_error_message, self._last_render_count = "", -1
        self._live_after_id, self._ports_after_id = None, None
        self.executor = BackgroundExecutor(root)
        self.sounds = SoundNotifier(sound)
        self.themes = ThemeManager(root, self.sounds)
        self.dialogs = DialogService(root, config, self.sounds)
        self.settings_repository = SettingsRepository(config)
        self.settings = self.settings_repository.load()
        self.output_manager, self.csv_repository = OutputManager(config), CsvCaptureRepository()
        self.port_discovery, self.port_selection = (
            port_discovery or SerialPortDiscovery(),
            PortSelection(),
        )
        self.instrument = HP3478A(
            transport or (
                SimulatedTransport(calibration_enabled=True) 
                if demo 
                else AR488Transport(timeout=config.query_timeout)
            ),
            query_timeout=config.query_timeout,
            command_delay=0 if demo else config.command_delay,
            settle_delay=0 if demo else config.settle_delay,
            status_delay=0 if demo else config.status_delay,
            trace=self._trace,
            activity=self._activity,
        )
        self.acquisition = AcquisitionController(
            self.instrument,
            on_finished=lambda result: self.executor.post(self._acquisition_finished, result),
        )
        self.calibration_repository = CalibrationRepository()
        self.calibration = CalibrationManager(self.instrument, self.calibration_repository)
        root.title(config.tool_name)
        self.dialogs.set_icon(root)
        self.view = ApplicationView(
            root,
            config,
            {
                "connect": self.connect,
                "disconnect": self.disconnect,
                "refresh_ports": self.refresh_ports,
                "function": self.select_function,
                "option": self.change_option,
                "status": self.refresh_status,
                "reset": self.reset_instrument,
                "clear_errors": self.clear_errors,
                "backup_calibration": self.backup_calibration,
                "restore_calibration": self.restore_calibration,
                "custom": self.send_custom,
                "start": self.start_acquisition,
                "stop": self.stop_acquisition,
                "graph": self.toggle_graph,
                "clear_plot": self.clear_plot,
                "clear_display": self.clear_display,
                "analysis": self.open_analysis,
                "folder": self.open_folder,
                "configuration": self.configuration,
                "help": self.dialogs.show_help,
            },
            self.themes,
        )
        self.themes.apply(self.settings)
        width, height = self.dialogs.display.dimensions()
        dpi = self.dialogs.display.scale_factor()
        root.minsize(
            min(round(640 * dpi), round(width * 0.95)), min(round(520 * dpi), round(height * 0.85))
        )
        try:
            self.output_manager.ensure_directory(self.settings)
            if not config.settings_path.exists():
                self.settings_repository.save(self.settings)
        except OSError as exc:
            self.view.console.append("INFO", f"Could not initialize output preferences: {exc}")
        if demo:
            self.view.console.append(
                "INFO", "Demo mode: measurements are simulated; no serial device is opened."
            )
        self._sync_from_instrument()
        self.dialogs.display.fit_main_window(config.width, config.height, maximize=maximize)
        root.protocol("WM_DELETE_WINDOW", self.request_exit)
        root.deiconify()
        self.refresh_ports()
        self._ports_after_id = root.after(2000, self._poll_ports)
        self._live_after_id = root.after(config.plot_interval_ms, self._poll_live)

    def run(self) -> None:
        self.root.mainloop()

    def _trace(self, direction: str, text: str) -> None:
        if self.acquisition.running and direction in ("OUT", "IN"):
            self.executor.post_latest(
                f"trace-{direction}", self.view.console.append, direction, text
            )
        else:
            self.executor.post(self.view.console.append, direction, text)

    def _activity(self, direction: str) -> None:
        self.executor.post_latest(f"activity-{direction}", self.view.flash, direction)

    def _refresh_state(self) -> None:
        self.view.set_state(
            self.instrument.connected,
            self.busy or self._closing,
            self.acquisition.running,
            self.acquisition.stopping or self._closing,
        )

    def _sync_from_instrument(self) -> None:
        status, info = self.instrument.status, self.instrument.connection_info
        self.view.settings.apply_status(status)
        self.view.connection.show_status(status, info.controller_name if info else "")
        self._refresh_state()

    def _run_task(
        self,
        label: str,
        operation: Callable,
        success: Callable | None = None,
        *,
        allow_acquisition: bool = False,
    ) -> None:
        if self.busy or self._closing or (self.acquisition.running and not allow_acquisition):
            return
        self.busy = True
        self.view.status.configure(text=label)
        self._refresh_state()

        def succeeded(result):
            self.busy = False
            if self._closing:
                return
            self._sync_from_instrument()
            self.view.status.configure(text="Acquiring…" if self.acquisition.running else "Ready")
            if success:
                success(result)

        def failed(error):
            self.busy = False
            if self._closing:
                self.view.console.append("ERROR", str(error))
                return
            self._sync_from_instrument()
            self.view.status.configure(text=f"Error: {error}")
            self.view.console.append("ERROR", str(error))
            self.dialogs.error("DMM operation", str(error))

        self.executor.submit(operation, succeeded, failed)

    def connect(self) -> None:
        port = self.view.connection.port.get()
        try:
            address = int(self.view.connection.address.get())
        except ValueError:
            self.dialogs.error("Connection", "Select a valid GPIB address.")
            return

        def connected(status):
            self.view.notebook.select(self.view.settings_page)
            self.sounds.play("success")

        if isinstance(self.instrument.transport, AR488Transport):
            self.view.console.append(
                "INFO",
                "AR488 connection, "
                f"serial timeout {self.instrument.transport.timeout:g} s.",
            )
        self._run_task(
            "Connecting and reading DMM status…",
            lambda: self.instrument.connect(port, address),
            connected,
        )

    def disconnect(self) -> None:
        self._run_task("Disconnecting…", self.instrument.disconnect)

    def select_function(self, code: int) -> None:
        self._run_task(
            "Selecting function and reading DMM status…",
            lambda: self.instrument.select_function(code),
        )

    def change_option(self, key: str, value: str) -> None:
        # Capture the GUI value before queuing I/O; workers never read Tk variables.
        self._run_task(
            "Applying setting and reading DMM status…",
            lambda: self.instrument.set_option(key, value),
        )

    def refresh_status(self) -> None:
        self._run_task("Reading DMM status…", self.instrument.refresh_status)

    def reset_instrument(self) -> None:
        self._run_task("Resetting and reading DMM status…", self.instrument.reset)

    def clear_errors(self) -> None:
        self._run_task("Clearing errors and reading DMM status…", self.instrument.clear_errors)

    def _calibration_progress(self, phase: str, count: int) -> None:
        self.executor.post_latest(
            "calibration-progress",
            self._show_calibration_progress,
            phase,
            count,
        )

    def _show_calibration_progress(self, phase: str, count: int) -> None:
        if self.busy and not self._closing:
            self.view.status.configure(text=f"Calibration RAM: {phase.lower()} {count}/256…")

    def backup_calibration(self) -> None:
        try:
            directory = self.output_manager.ensure_directory(self.settings)
            path = self.dialogs.ask_calibration_file(directory, save=True)
        except OSError as exc:
            self.dialogs.error("Calibration backup", str(exc))
            return
        if not path:
            return

        def saved(image):
            text = f"Complete calibration RAM saved: {path}"
            self.view.console.append("INFO", text)
            if image.failed_entries:
                text += f"\nInvalid calibration checksums in entries: {image.failed_entries}. "
                text += "The complete RAM was saved, but this image cannot be restored."
                self.dialogs.warning("Calibration backup", text)
            else:
                self.dialogs.info("Calibration backup", text)

        self._run_task(
            "Reading complete calibration RAM…",
            lambda: self.calibration.backup(path, self._calibration_progress),
            saved,
        )

    def restore_calibration(self) -> None:
        try:
            directory = self.output_manager.ensure_directory(self.settings)
            path = self.dialogs.ask_calibration_file(directory, save=False)
        except OSError as exc:
            self.dialogs.error("Calibration restore", str(exc))
            return
        if not path:
            return

        def validated(image):
            if not self.dialogs.confirm_calibration_restore(path):
                return

            def restored(recovery):
                text = (
                    f"Calibration RAM restored and verified.\nRecovery backup: {recovery}\n"
                    "Return the front-panel CAL ENABLE switch to OFF."
                )
                self.view.console.append("INFO", text)
                self.dialogs.info("Calibration restore", text)

            self._run_task(
                "Saving recovery backup and restoring calibration RAM…",
                lambda: self.calibration.restore(image, directory, self._calibration_progress),
                restored,
            )

        def load():
            image = self.calibration_repository.load(path)
            image.require_valid_calibration()
            return image

        self._run_task("Checking calibration backup…", load, validated)

    def send_custom(self) -> None:
        command, query = self.view.console.command.get(), self.view.console.query_var.get()
        if command.strip():
            self._run_task(
                "Sending custom command and reading DMM status…",
                lambda: self.instrument.send_custom(command, query),
                lambda result: self.view.console.command.delete(0, "end"),
            )

    def start_acquisition(self) -> None:
        if self.busy or self.acquisition.running or self._closing:
            return
        try:
            panel = self.view.acquisition
            interval, history = (
                AcquisitionOptions.parse_interval(panel.rate_var.get()),
                int(panel.history_var.get()),
            )
            status = self.instrument.status
            if status is None:
                raise ValueError("Connect to the DMM first.")
            path = (
                self.output_manager.capture_path(self.settings, status.function.unit)
                if panel.logger_var.get()
                else None
            )
            options = AcquisitionOptions(interval, history, path)
        except (OSError, ValueError) as exc:
            self.dialogs.error("Acquisition", str(exc))
            return
        self._last_render_count = -1
        panel.display.clear()
        panel.plot.clear()
        self._run_task("Starting acquisition…", lambda: self.acquisition.start(options))

    def stop_acquisition(self) -> None:
        if self.acquisition.running:
            self.acquisition.stop()
            self.view.status.configure(text="Stopping acquisition…")
            self.acquisition.device._clean_autorange_queue()
            self._refresh_state()

    def _acquisition_finished(self, result: AcquisitionResult) -> None:
        if self._closing:
            return
        if self.busy:
            self.root.after(50, lambda: self._acquisition_finished(result))
            return
        self._update_live(force=True)
        self._sync_from_instrument()
        summary = f"Stopped: {result.samples} samples in {result.duration:.3f} s."
        if result.log_path:
            summary += f" CSV: {result.log_path}"
        self.view.console.append("INFO", summary)
        if result.error:
            self.view.status.configure(text=f"Acquisition stopped: {result.error}")
            self.view.console.append("ERROR", str(result.error))
            self.dialogs.error("Acquisition", str(result.error))
        elif self.instrument.connected:
            self._run_task(
                "Reading final DMM status…",
                self.instrument.refresh_status,
                lambda status: self.view.status.configure(text=summary),
            )
        else:
            self.view.status.configure(text=summary)

    def _poll_live(self) -> None:
        if not self._closing:
            if self.acquisition.running:
                self._update_live()
            self._live_after_id = self.root.after(self.config.plot_interval_ms, self._poll_live)

    def _update_live(self, force: bool = False) -> None:
        count = self.acquisition.buffer.count
        if count == self._last_render_count and not force:
            return
        samples = self.acquisition.buffer.snapshot()
        if not samples:
            return
        self._last_render_count = count
        latest = samples[-1]
        self.view.acquisition.display.show(latest, latest.function_label)
        if self.view.acquisition.plot_var.get():
            self.view.acquisition.plot.update_trace(samples, latest.unit)
        if self.acquisition.running and not self.acquisition.stopping:
            self.view.status.configure(
                text=f"Acquiring… {count} samples  •  {latest.timestamp:.3f} s"
            )

    def toggle_graph(self) -> None:
        if self.view.acquisition.plot_var.get():
            self._update_live(force=True)
        else:
            self.view.acquisition.plot.clear()

    def clear_plot(self) -> None:
        if not self.acquisition.running:
            self.acquisition.buffer.clear()
            self._last_render_count = -1
            self.view.acquisition.plot.clear()

    def clear_display(self) -> None:
        if not self.acquisition.running:
            self.view.acquisition.display.clear()

    def refresh_ports(self) -> None:
        if self._port_scan_pending or self._closing:
            return
        self._port_scan_pending = True

        def scanned(ports):
            self._port_scan_pending = False
            if self._closing:
                return
            if self.demo:
                ports = (*ports, SIMULATED_PORT)
            self.port_selection.selected = self.view.connection.port.get()
            selected = self.port_selection.update(
                ports, connected=self.instrument.connected or self.busy
            )
            self.view.connection.port.configure(values=tuple(port.device for port in ports))
            self.view.connection.port.set(selected)
            self.view.connection.port_description.configure(text=self.port_selection.description)
            self._refresh_state()

        def failed(error):
            self._port_scan_pending = False
            if self._closing:
                return
            if self.demo:
                scanned(())
            else:
                self.view.connection.port_description.configure(text=str(error))
            if str(error) != self._port_error_message:
                self._port_error_message = str(error)
                self.view.console.append("INFO", str(error))
            self._refresh_state()

        self.executor.submit(self.port_discovery.scan, scanned, failed)

    def _poll_ports(self) -> None:
        if not self._closing:
            self.refresh_ports()
            self._ports_after_id = self.root.after(2000, self._poll_ports)

    def open_analysis(self) -> None:
        try:
            path = self.dialogs.ask_csv(self.output_manager.ensure_directory(self.settings))
        except OSError as exc:
            self.dialogs.error("Data analysis", str(exc))
            return
        if path:
            self._run_task(
                "Loading acquisition CSV…",
                lambda: self.csv_repository.load(path),
                lambda data: AnalysisWindow(
                    self.root, data, self.dialogs, self.themes, self.output_manager, self.settings
                ),
                allow_acquisition=True,
            )

    def open_folder(self) -> None:
        try:
            self.output_manager.open_folder(self.settings)
        except OSError as exc:
            self.dialogs.error("Output folder", str(exc))

    def configuration(self) -> None:
        ConfigurationDialog(
            self.root,
            self.config,
            self.settings,
            self.themes.available_themes(),
            self.dialogs,
            self.apply_preferences,
        )

    def apply_preferences(self, settings: UserSettings) -> None:
        self.output_manager.ensure_directory(settings)
        self.settings_repository.save(settings)
        self.settings = settings
        self.themes.apply(settings)

    def request_exit(self, force: bool = False) -> None:
        if self._closing or (not force and not self.dialogs.confirm_exit()):
            return
        self._closing = True
        self.acquisition.stop()
        self.view.status.configure(text="Closing and releasing the serial port…")
        self._refresh_state()
        self._wait_for_close()

    def _wait_for_close(self) -> None:
        # Startup can be queued when closing begins. Keep the stop request active.
        self.acquisition.stop()
        if self.busy or self._port_scan_pending or self.acquisition.running:
            self.root.after(50, self._wait_for_close)
            return
        self.busy = True
        self.executor.submit(
            self.instrument.disconnect,
            lambda result: self._destroy(),
            lambda error: self._destroy(),
        )

    def _destroy(self) -> None:
        for identifier in (self._live_after_id, self._ports_after_id):
            if identifier is not None:
                self.root.after_cancel(identifier)
        self.executor.shutdown()
        self.root.quit()
        self.root.destroy()
