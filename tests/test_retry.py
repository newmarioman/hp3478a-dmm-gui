"""Timeout recovery, exact retry limits and unchanged writes during reads."""

import tempfile
import threading
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from tests.controller import AR488, Controller

from hp3478a.acquisition import AcquisitionController, AcquisitionOptions
from hp3478a.calibration import CalibrationManager
from hp3478a.instrument import HP3478A, QueryTimeout
from hp3478a.status import ProtocolError
from hp3478a.transport import AR488Transport, SimulatedTransport, TransportError


class ReadRetryTests(unittest.TestCase):
    def setUp(self):
        self.bus = SimulatedTransport(sample_delay=0, calibration_enabled=True)
        self.messages = []
        self.device = HP3478A(
            self.bus, query_timeout=0.006, command_delay=0, status_delay=0,
            settle_delay=0, trace=lambda direction, text: self.messages.append((direction, text)),
        )
        self.device.connect("SIMULATED", 23)
        self.bus.commands.clear()

    def tearDown(self):
        self.device.disconnect()

    def drop_replies(self, kind, should_drop, partial=b""):
        """Let commands run, but remove selected complete or partial replies."""
        original = self.bus.write
        attempts = Counter()

        def write(command):
            address = self.bus._pending_ram
            status = self.bus._pending_status
            selected = (
                command.startswith("++read") and (
                    (kind == "ram" and address is not None)
                    or (kind == "status" and status)
                    or (kind == "measurement" and address is None and not status)
                )
            ) or command == kind
            original(command)
            if selected:
                key = address if kind == "ram" else kind
                attempts[key] += 1
                if should_drop(key, attempts[key]):
                    self.bus._buffer[:] = partial

        self.bus.write = write
        return attempts

    def test_partial_status_recovers_on_fifth_request_without_replaying_setting(self):
        attempts = self.drop_replies("status", lambda key, count: count < 5, b"\x2d\x3f")
        with patch.object(self.bus, "discard_input", wraps=self.bus.discard_input) as discard:
            status = self.device.select_function(5)
        self.assertEqual(status.function_code, 5)
        self.assertEqual(attempts["status"], 5)
        self.assertEqual(self.bus.commands.count("B"), 5)
        self.assertEqual(self.bus.commands.count("F5"), 1)
        self.assertEqual(self.bus.commands.count("RA"), 1)
        self.assertEqual(discard.call_count, 5)
        self.assertTrue(self.device.connected)
        self.assertEqual(self.bus.read(100), b"")

    def test_fifth_status_timeout_aborts_without_a_sixth_request(self):
        attempts = self.drop_replies("status", lambda key, count: count <= 5, b"\x2d")
        with self.assertRaisesRegex(QueryTimeout, "Status B.*5 consecutive timeouts"):
            self.device.refresh_status()
        self.assertEqual(attempts["status"], 5)
        self.assertFalse(self.device.connected)
        self.assertFalse(self.bus.is_open)

    def test_status_success_resets_counter_before_next_operation(self):
        attempts = self.drop_replies("status", lambda key, count: count % 5 != 0)
        for _ in range(2):
            self.device.refresh_status()
        self.assertEqual(attempts["status"], 10)
        self.assertTrue(self.device.connected)

    def test_malformed_status_is_rejected_immediately(self):
        attempts = self.drop_replies(
            "status", lambda key, count: True, b"\xff\x3f\x00\x00\x00\r\n"
        )
        with self.assertRaises(ProtocolError):
            self.device.refresh_status()
        self.assertEqual(attempts["status"], 1)
        self.assertFalse(self.device.connected)

    def test_write_failure_is_not_retried_as_a_timeout(self):
        original = self.bus.write
        attempts = []

        def write(command):
            if command == "++read eoi":
                attempts.append(command)
                raise TransportError("Injected serial failure")
            original(command)

        self.bus.write = write
        with self.assertRaisesRegex(TransportError, "Injected serial failure"):
            self.device.refresh_status()
        self.assertEqual(len(attempts), 1)
        self.assertFalse(self.device.connected)

    def test_console_frame_recovers_and_does_not_repeat_custom_command(self):
        attempts = self.drop_replies("measurement", lambda key, count: count < 5, b"+1.2")
        response = self.device.send_custom("D3", query=True)
        self.assertTrue(response.startswith("+"))
        self.assertEqual(attempts["measurement"], 5)
        self.assertEqual(self.bus.commands.count("D3"), 1)
        self.assertEqual(self.device.status.display_mode, "OFF")
        self.assertTrue(self.device.connected)

    def test_console_frame_stops_at_five_timeouts(self):
        attempts = self.drop_replies("measurement", lambda key, count: count <= 5)
        with self.assertRaisesRegex(QueryTimeout, "Console.*5 consecutive timeouts"):
            self.device.send_custom("++read")
        self.assertEqual(attempts["measurement"], 5)
        self.assertNotIn("B", self.bus.commands)
        self.assertFalse(self.device.connected)

    def test_partial_controller_line_is_restarted_instead_of_concatenated(self):
        attempts = self.drop_replies("++ver", lambda key, count: count < 5, b"AR488 partial")
        self.assertEqual(self.device.send_custom("++ver", query=True), "AR488 software simulator")
        self.assertEqual(attempts["++ver"], 5)
        self.assertTrue(self.device.connected)

    def test_connect_recovers_controller_version_on_fifth_attempt(self):
        self.device.disconnect()
        attempts = self.drop_replies("++ver", lambda key, count: count < 5, b"AR488 partial")
        self.device.connect("SIMULATED", 23)
        self.assertEqual(attempts["++ver"], 5)
        self.assertEqual(self.device.connection_info.controller_name, "AR488 software simulator")
        self.assertTrue(self.device.connected)

    def test_connect_aborts_after_five_missing_versions(self):
        self.device.disconnect()
        attempts = self.drop_replies("++ver", lambda key, count: count <= 5)
        with self.assertRaisesRegex(TransportError, "reading the controller version.*5 consecutive"):
            self.device.connect("SIMULATED", 23)
        self.assertEqual(attempts["++ver"], 5)
        self.assertFalse(self.device.connected)
        self.assertFalse(self.bus.is_open)

    def test_connect_status_counter_resets_after_a_complete_status(self):
        self.device.disconnect()
        attempts = self.drop_replies("status", lambda key, count: count % 5 != 0, b"\x2d")
        self.device.connect("SIMULATED", 23)
        self.assertEqual(attempts["status"], 10)
        self.assertTrue(self.device.connected)

    def test_connect_aborts_on_fifth_status_timeout(self):
        self.device.disconnect()
        attempts = self.drop_replies("status", lambda key, count: count <= 5)
        with self.assertRaisesRegex(TransportError, "binary DMM status bytes.*5 consecutive"):
            self.device.connect("SIMULATED", 23)
        self.assertEqual(attempts["status"], 5)
        self.assertFalse(self.bus.is_open)

    def test_ram_retries_same_address_and_resets_after_each_valid_byte(self):
        addresses = {0, 1, 13, 43, 255}
        attempts = self.drop_replies(
            "ram", lambda address, count: address in addresses and count < 5, b"\r\n\x04"
        )
        progress = []
        self.assertEqual(self.device.read_calibration_ram(progress.append), bytes(self.bus.ram))
        self.assertEqual(progress, list(range(1, 257)))
        for address in range(256):
            self.assertEqual(attempts[address], 5 if address in addresses else 1)
        self.assertFalse(any(isinstance(c, bytes) and c[0] == 88 for c in self.bus.commands))
        self.assertTrue(self.device.connected)

    def test_ram_timeout_aborts_backup_and_restores_framing_before_disconnect(self):
        attempts = self.drop_replies("ram", lambda address, count: address == 66)
        self.bus.send_eos, self.bus.send_eoi = 2, False
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "backup.json"
            with self.assertRaisesRegex(QueryTimeout, "RAM 0x42.*5 consecutive timeouts"):
                CalibrationManager(self.device).backup(path)
            self.assertFalse(path.exists())
        self.assertEqual(attempts[66], 5)
        self.assertNotIn(67, attempts)
        self.assertEqual(self.bus.commands[-2:], ["++eos 2", "++eoi 0"])
        self.assertFalse(self.device.connected)
        self.assertFalse(self.bus.is_open)

    def test_restore_verification_retries_reads_without_repeating_ram_writes(self):
        attempts = self.drop_replies("ram", lambda address, count: address == 13 and count < 5)
        with patch("hp3478a.instrument.time.sleep"):
            self.device.restore_calibration_ram(bytes(self.bus.ram))
        writes = [c for c in self.bus.commands if isinstance(c, bytes) and c[0] == 88]
        self.assertEqual(len(writes), 256)
        self.assertEqual([c[1] for c in writes], list(range(256)))
        self.assertEqual(attempts[13], 5)
        self.assertTrue(self.device.connected)

    def test_direct_measurement_recovers_on_fifth_read(self):
        attempts = self.drop_replies("measurement", lambda key, count: count < 5)
        self.assertGreater(self.device.read_measurement().value, 0)
        self.assertEqual(attempts["measurement"], 5)
        self.assertTrue(self.device.connected)

    def test_acquisition_still_stops_after_five_timeouts_without_nested_retries(self):
        attempts = self.drop_replies("measurement", lambda key, count: count <= 5)
        finished, event = [], threading.Event()

        def complete(result):
            finished.append(result)
            event.set()

        service = AcquisitionController(self.device, on_finished=complete)
        try:
            service.start(AcquisitionOptions())
            self.assertTrue(event.wait(2))
            service.join(2)
        finally:
            service.stop()
            service.join(2)
        self.assertEqual(attempts["measurement"], 5)
        self.assertEqual(finished[0].samples, 0)
        self.assertIsInstance(finished[0].error, QueryTimeout)
        self.assertFalse(self.device.connected)

    def test_acquisition_success_resets_its_existing_timeout_counter(self):
        attempts = self.drop_replies("measurement", lambda key, count: count % 5 != 0)
        finished, event = [], threading.Event()

        def complete(result):
            finished.append(result)
            event.set()

        def sample(reading):
            if service.buffer.count == 2:
                service.stop()

        service = AcquisitionController(self.device, on_sample=sample, on_finished=complete)
        try:
            service.start(AcquisitionOptions(history=2))
            self.assertTrue(event.wait(2))
            service.join(2)
        finally:
            service.stop()
            service.join(2)
        self.assertEqual(attempts["measurement"], 10)
        self.assertEqual(finished[0].samples, 2)
        self.assertIsNone(finished[0].error)
        self.assertTrue(self.device.connected)


class HardwareConnectRetryTests(unittest.TestCase):
    def setUp(self):
        self.controller = Controller(eos=2, eoi=0, eot=4)
        self.controller.ram[:] = bytes(i % 16 for i in range(256))
        self.bus = AR488Transport()
        self.device = HP3478A(
            self.bus, query_timeout=0.006, command_delay=0, status_delay=0, settle_delay=0
        )
        original_open = self.controller.open

        def open_serial(**options):
            self.controller.closed = False
            return original_open(**options)

        self.serial = patch.object(AR488.serial, "Serial", side_effect=open_serial).start()
        self.addCleanup(patch.stopall)
        patch.object(AR488.time, "sleep").start()
        self.close = patch.object(self.controller, "close", wraps=self.controller.close).start()
        self.addCleanup(self.device.disconnect)

    def omit_controller_replies(self, commands, limit):
        original = self.controller._handle
        attempts = Counter()

        def handle(command):
            if command in commands:
                attempts[command] += 1
                if attempts[command] <= limit:
                    self.controller.commands.append(command)
                    return
            original(command)

        self.controller._handle = handle
        return attempts

    def immediate_trailers(self):
        # The fixture queues complete replies synchronously; no USB wait is needed.
        return patch.object(
            self.bus, "read_available",
            side_effect=lambda size: self.controller.read(size) if self.controller.input else b"",
        )

    def test_connect_reopens_after_startup_timeouts_and_closes_each_failed_handle(self):
        attempts = self.omit_controller_replies({b"++mode"}, 4)
        self.device.connect("COM5", 23)
        self.assertEqual(attempts[b"++mode"], 5)
        self.assertEqual(self.serial.call_count, 5)
        self.assertEqual(self.close.call_count, 4)
        self.assertEqual(self.controller.commands.count(b"++ver"), 1)
        self.assertTrue(self.device.connected)
        self.assertFalse(self.controller.closed)

    def test_connect_exhaustion_during_driver_initialization_closes_all_five_handles(self):
        attempts = self.omit_controller_replies({b"++addr"}, 5)
        with self.assertRaisesRegex(TransportError, "opening the serial port.*5 consecutive"):
            self.device.connect("COM5", 23)
        self.assertEqual(attempts[b"++addr"], 5)
        self.assertEqual(self.serial.call_count, 5)
        self.assertEqual(self.close.call_count, 5)
        self.assertNotIn(b"++ver", self.controller.commands)
        self.assertFalse(self.device.connected)
        self.assertTrue(self.controller.closed)

    def test_invalid_address_readback_is_not_retried(self):
        original = self.controller._handle

        def handle(command):
            if command != b"++addr 23":
                original(command)

        self.controller._handle = handle
        with self.assertRaisesRegex(TransportError, "Failed to set interface address"):
            self.device.connect("COM5", 23)
        self.assertEqual(self.serial.call_count, 1)
        self.assertTrue(self.controller.closed)

    def test_all_calibration_framing_reads_recover_on_fifth_request(self):
        self.device.connect("COM5", 23)
        attempts = self.omit_controller_replies({b"++eos", b"++eoi", b"++eot_enable"}, 4)
        with self.immediate_trailers():
            self.assertEqual(self.device.read_calibration_ram(), bytes(self.controller.ram))
        self.assertEqual(list(attempts.values()), [5, 5, 5])
        self.assertEqual((self.controller.config["++eos"], self.controller.config["++eoi"]), (2, 0))
        self.assertTrue(self.device.connected)

    def test_framing_read_timeout_aborts_before_any_ram_or_framing_write(self):
        self.device.connect("COM5", 23)
        self.controller.commands.clear()
        attempts = self.omit_controller_replies({b"++eos"}, 5)
        with self.assertRaisesRegex(TransportError, "framing.*5 consecutive timeouts"):
            self.device.read_calibration_ram()
        self.assertEqual(attempts[b"++eos"], 5)
        self.assertNotIn(b"++eos 3", self.controller.commands)
        self.assertFalse(any(c.startswith(b"W") for c in self.controller.commands))
        self.assertFalse(self.device.connected)
        self.assertTrue(self.controller.closed)

    def test_hardware_ram_retry_preserves_binary_address_escaping(self):
        self.device.connect("COM5", 23)
        original = self.controller._handle
        attempts = []

        def handle(command):
            address = self.controller.pending_ram
            original(command)
            if command == b"++read eoi" and address == 13:
                attempts.append(address)
                if len(attempts) < 5:
                    self.controller.input.clear()

        self.controller._handle = handle
        with self.immediate_trailers():
            self.assertEqual(self.device.read_calibration_ram(), bytes(self.controller.ram))
        self.assertEqual(len(attempts), 5)
        self.assertEqual(self.controller.commands.count(b"W\x0d"), 5)
        self.assertTrue(self.device.connected)


if __name__ == "__main__":
    unittest.main()
