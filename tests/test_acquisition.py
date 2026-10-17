"""Cancelable acquisition, logging and restart checks without a GUI display."""

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from hp3478a.acquisition import AcquisitionController, AcquisitionOptions
from hp3478a.instrument import HP3478A
from hp3478a.models import Measurement, TraceBuffer
from hp3478a.storage import CsvCaptureLogger, CsvCaptureRepository
from hp3478a.transport import SimulatedTransport, TransportError


class TraceTests(unittest.TestCase):
    def test_last_points_and_total_count_are_independent(self):
        trace = TraceBuffer(5)
        for index in range(20):
            trace.append(Measurement(index, index / 10, "V"))
        self.assertEqual([s.value for s in trace.snapshot()], [15, 16, 17, 18, 19])
        self.assertEqual(trace.count, 20)
        trace.clear()
        self.assertEqual(trace.snapshot(), ())
        self.assertEqual(trace.count, 0)

    def test_invalid_history_and_reverse_timestamps_are_rejected(self):
        for capacity in (0, 1, 10001, 2.5, True):
            with self.assertRaises(ValueError):
                TraceBuffer(capacity)
        trace = TraceBuffer()
        trace.append(Measurement(1, 2, "V"))
        with self.assertRaises(ValueError):
            trace.append(Measurement(1, 1, "V"))

    def test_snapshot_is_not_changed_by_later_samples(self):
        trace = TraceBuffer(2)
        trace.append(Measurement(1, 0, "V"))
        previous = trace.snapshot()
        trace.append(Measurement(2, 1, "V"))
        trace.append(Measurement(3, 2, "V"))
        self.assertEqual([s.value for s in previous], [1])
        self.assertEqual([s.value for s in trace.snapshot()], [2, 3])


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.folder = Path(self.directory.name)
        self.transport = SimulatedTransport(sample_delay=0)
        self.device = HP3478A(self.transport, command_delay=0, status_delay=0, settle_delay=0)
        self.device.connect("SIMULATED", 23)
        self.finished, self.event, self.service = [], threading.Event(), None

    def tearDown(self):
        if self.service:
            self.service.stop()
            self.service.join(2)
        self.device.disconnect()
        self.directory.cleanup()

    def limited(self, limit=5, **kwargs):
        def finished(result):
            self.finished.append(result)
            self.event.set()

        def sample(reading):
            if self.service.buffer.count >= limit:
                self.service.stop()

        self.service = AcquisitionController(
            self.device, on_finished=finished, on_sample=sample, **kwargs
        )
        return self.service

    def test_all_125_readings_are_logged_with_five_point_plot_history(self):
        service, path = self.limited(125), self.folder / "capture.csv"
        service.start(AcquisitionOptions(history=5, log_path=path))
        self.assertTrue(self.event.wait(2))
        service.join(2)
        data = CsvCaptureRepository().load(path)
        self.assertEqual(len(data.values), 125)
        self.assertEqual(self.finished[-1].samples, 125)
        self.assertIsNone(self.finished[-1].error)
        self.assertEqual(service.buffer.count, 125)
        self.assertEqual(tuple(s.value for s in service.buffer.snapshot()), data.values[-5:])
        self.assertEqual(
            tuple(s.timestamp for s in service.buffer.snapshot()), data.timestamps[-5:]
        )
        self.assertTrue(all(a <= b for a, b in zip(data.timestamps, data.timestamps[1:])))

    def test_stop_interrupts_3599_second_wait_without_serial_commands(self):
        ready = threading.Event()
        self.service = AcquisitionController(self.device, on_sample=lambda sample: ready.set())
        self.service.start(AcquisitionOptions(interval=3600))
        self.assertTrue(ready.wait(1))
        before, started = self.transport.commands.copy(), time.monotonic()
        self.service.stop()
        self.service.join(1)
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertFalse(self.service.running)
        self.assertEqual(self.transport.commands, before)

    def test_restart_resets_history_timestamps_and_stop_request(self):
        service = self.limited(3)
        for _ in range(2):
            self.event.clear()
            service.start(AcquisitionOptions())
            self.assertTrue(self.event.wait(1))
            service.join(1)
            self.assertEqual(service.buffer.count, 3)
            self.assertLess(service.buffer.snapshot()[0].timestamp, 0.5)
        self.assertEqual([r.samples for r in self.finished], [3, 3])

    def test_logging_disabled_creates_no_files(self):
        service = self.limited()
        service.start(AcquisitionOptions())
        self.assertTrue(self.event.wait(1))
        service.join(1)
        self.assertIsNone(self.finished[-1].log_path)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_duplicate_start_sends_no_second_status_query(self):
        ready = threading.Event()
        self.service = AcquisitionController(self.device, on_sample=lambda sample: ready.set())
        self.service.start(AcquisitionOptions(interval=3600))
        self.assertTrue(ready.wait(1))
        before = self.transport.commands.copy()
        with self.assertRaises(RuntimeError):
            self.service.start(AcquisitionOptions())
        self.assertEqual(self.transport.commands, before)

    def test_read_error_closes_partial_csv_and_port(self):
        class FailingTransport(SimulatedTransport):
            def write(self, command):
                if (
                    command.startswith("++read")
                    and not self._pending_status
                    and self.sample_count >= 3
                ):
                    raise TransportError("Injected read failure")
                super().write(command)

        self.device.disconnect()
        self.transport = FailingTransport(sample_delay=0)
        self.device = HP3478A(self.transport, command_delay=0, status_delay=0, settle_delay=0)
        self.device.connect("SIMULATED", 23)
        service, path = self.limited(100), self.folder / "partial.csv"
        service.start(AcquisitionOptions(log_path=path))
        self.assertTrue(self.event.wait(1))
        service.join(1)
        self.assertIsInstance(self.finished[-1].error, TransportError)
        self.assertEqual(len(CsvCaptureRepository().load(path).values), 3)
        self.assertFalse(self.device.connected)

    def test_disk_error_stops_and_closes_logger(self):
        created = []

        class FailingLogger:
            def __init__(self, path, unit):
                self.count, self.closed = 0, False
                created.append(self)

            def append(self, sample):
                if self.count == 2:
                    raise OSError("Injected disk error")
                self.count += 1

            def close(self):
                self.closed = True

        service = self.limited(100, logger_factory=FailingLogger)
        service.start(AcquisitionOptions(log_path=self.folder / "unused.csv"))
        self.assertTrue(self.event.wait(1))
        service.join(1)
        self.assertIsInstance(self.finished[-1].error, OSError)
        self.assertEqual(self.finished[-1].samples, 2)
        self.assertTrue(created[0].closed)
        self.assertTrue(self.device.connected)

    def test_callbacks_run_in_worker_thread(self):
        identifiers, owner = [], threading.get_ident()

        def sample(reading):
            identifiers.append(threading.get_ident())
            self.service.stop()

        self.service = AcquisitionController(self.device, on_sample=sample)
        self.service.start(AcquisitionOptions())
        self.service.join(1)
        self.assertEqual(len(identifiers), 1)
        self.assertNotEqual(identifiers[0], owner)

    def test_gui_state_reads_do_not_wait_for_startup_io(self):
        entered, release = threading.Event(), threading.Event()
        original = self.device.refresh_status

        def slow():
            entered.set()
            if not release.wait(1):
                raise RuntimeError("Test gate timed out")
            return original()

        self.device.refresh_status = slow
        service = self.limited(1)
        with ThreadPoolExecutor(max_workers=1) as worker:
            future = worker.submit(service.start, AcquisitionOptions())
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            self.assertFalse(service.running)
            self.assertLess(time.monotonic() - started, 0.05)
            release.set()
            future.result(timeout=1)
        service.join(1)

    def test_stop_during_startup_is_not_lost(self):
        entered, release = threading.Event(), threading.Event()
        original = self.device.refresh_status

        def slow():
            entered.set()
            if not release.wait(1):
                raise RuntimeError("Test gate timed out")
            return original()

        self.device.refresh_status = slow
        service, path = self.limited(100), self.folder / "cancelled.csv"
        with ThreadPoolExecutor(max_workers=1) as worker:
            future = worker.submit(service.start, AcquisitionOptions(log_path=path))
            self.assertTrue(entered.wait(1))
            service.stop()
            release.set()
            future.result(timeout=1)
        service.join(1)
        self.assertFalse(service.running)
        self.assertEqual(self.finished[-1].samples, 0)
        self.assertEqual(len(path.read_text().splitlines()), 1)

    def test_thread_start_failure_closes_csv_and_allows_retry(self):
        created = []

        def logger(path, unit):
            result = CsvCaptureLogger(path, unit)
            created.append(result)
            return result

        service = self.limited(1, logger_factory=logger)
        with patch(
            "hp3478a.acquisition.threading.Thread.start",
            side_effect=RuntimeError("Injected start failure"),
        ):
            with self.assertRaises(RuntimeError):
                service.start(AcquisitionOptions(log_path=self.folder / "failed.csv"))
        self.assertFalse(service.running)
        self.assertTrue(created[0]._handle.closed)
        service.start(AcquisitionOptions())
        self.assertTrue(self.event.wait(1))
        service.join(1)

    def test_intervals_are_validated(self):
        self.assertEqual(AcquisitionOptions.parse_interval("MAX"), 0)
        self.assertEqual(AcquisitionOptions.parse_interval("0.1"), 0.1)
        for value in ("0", "-1", "nan", "inf", "3601", "wrong"):
            with self.assertRaises(ValueError):
                AcquisitionOptions.parse_interval(value)


if __name__ == "__main__":
    unittest.main()
