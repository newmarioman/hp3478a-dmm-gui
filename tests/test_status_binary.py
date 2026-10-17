"""Exercise unterminated binary B responses through the real PyAR488 driver."""

import unittest
from unittest.mock import patch

from tests.controller import AR488, Controller

from hp3478a.config import OPTION_COMMANDS
from hp3478a.instrument import HP3478A, QueryTimeout
from hp3478a.status import ProtocolError
from hp3478a.transport import AR488Transport


class Clock:
    """Model serial timeouts without spending seconds on each regression case."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class BinaryStatusController(Controller):
    def __init__(self, clock):
        super().__init__(eot_enable=0)
        self.clock = clock
        self.flags = 0x1F
        self.status_header = 45
        self.status_tail = b"\x00\x00\x2d"
        self.status_length = 5
        self.status_terminator = b"\r\n"
        self.deferred = []
        self.eot_encoding = "raw"

    def _handle(self, command):
        if command == b"++addr" and self.deferred:
            # The firmware executes this command only after the ongoing
            # GPIB read has finished, including its delayed final bytes.
            self.commands.append(command)
            self.deferred.append((self.deferred[-1][0], f"{self.config['++addr']}\r\n".encode()))
        elif command in (b"T1", b"T2", b"T3", b"T4", b"T5"):
            self.commands.append(command)
            self.flags = 0x1F if command == b"T1" else 0x5E if command == b"T2" else 0x1E
        elif command in (b"++read", b"++read eoi") and self.pending_status:
            self.commands.append(command)
            payload = bytes((self.status_header, self.flags)) + self.status_tail
            self.input.extend(payload[:self.status_length] + self.status_terminator)
            if self.config["++eot_enable"]:
                if self.eot_encoding == "decimal":
                    # AR488 0.53: uint8_t eot_ch is passed to Print::print(),
                    # so 4 is emitted as ASCII b'4', not the raw byte 0x04.
                    self.input.extend(str(self.config["++eot_char"]).encode())
                else:
                    self.input.append(self.config["++eot_char"])
            self.pending_status = False
            self.status_count += 1
        else:
            super()._handle(command)

    def readline(self):
        result = super().readline()
        if not result.endswith(b"\n"):
            # Like pySerial, readline() waits for LF even if all five binary
            # status bytes have already arrived. The old fixture skipped this.
            self.clock.sleep(self.timeout)
        return result

    def read(self, size=1):
        self._deliver()
        if not self.input and self.deferred:
            delay = max(0, self.deferred[0][0] - self.clock.now)
            self.clock.sleep(min(delay, self.timeout))
            self._deliver()
        result = super().read(size)
        if not result:
            self.clock.sleep(self.timeout)
        return result

    def _deliver(self):
        while self.deferred and self.deferred[0][0] <= self.clock.now:
            _, payload = self.deferred.pop(0)
            self.input.extend(payload)

    @property
    def in_waiting(self):
        self._deliver()
        return len(self.input)


class BinaryStatusTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.controller = BinaryStatusController(self.clock)
        self.device = HP3478A(
            AR488Transport(), query_timeout=1, command_delay=0,
            status_delay=0, settle_delay=0,
        )
        self.patches = [
            patch.object(AR488.serial, "Serial", self.controller.open),
            patch("hp3478a.instrument.time.monotonic", self.clock.monotonic),
            patch("hp3478a.instrument.time.sleep", self.clock.sleep),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self.device.disconnect)
        self.device.connect("COM5", 23)
        self.controller.commands.clear()
        self.controller.status_terminator = b""

    def assert_trigger_response(self, label):
        command = OPTION_COMMANDS["trigger"][label]
        status = self.device.set_option("trigger", label)
        self.assertEqual(status.raw, bytes((45, self.controller.flags)) + self.controller.status_tail)
        self.assertEqual(status.trigger_label, label)
        self.assertTrue(self.device.connected)
        self.assertEqual(self.controller.commands, [command.encode(), b"B", b"++read eoi", b"++addr"])
        self.assertEqual(self.controller.input, b"")
        self.assertEqual(self.controller.timeout, 1)

    def test_internal_status_without_lf(self):
        self.assert_trigger_response("Internal")

    def test_external_status_without_lf(self):
        self.assert_trigger_response("External")

    def test_single_status_without_lf(self):
        self.assert_trigger_response("Single")

    def test_hold_status_without_lf(self):
        self.assert_trigger_response("Hold")

    def test_fast_status_without_lf(self):
        self.assert_trigger_response("Fast")

    def test_binary_cr_lf_eoi_and_zero_are_preserved_inside_status(self):
        for tail in (b"\r\n\x04", b"\n\x00\r", b"\x00\x04\x00"):
            with self.subTest(tail=tail):
                self.controller.status_tail = tail
                status = self.device.refresh_status()
                self.assertEqual(status.raw, bytes((45, self.controller.flags)) + tail)
                self.assertEqual(self.controller.input, b"")

    def test_optional_cr_lf_before_controller_confirmation_is_accepted(self):
        for ending in (b"", b"\r\n"):
            with self.subTest(ending=ending):
                self.controller.status_terminator = ending
                self.assertEqual(len(self.device.refresh_status().raw), 5)
                self.assertEqual(self.controller.input, b"")

    def test_incomplete_binary_status_still_times_out_after_five_attempts(self):
        self.controller.status_length = 2
        with self.assertRaisesRegex(QueryTimeout, "Status B.*5 consecutive timeouts"):
            self.device.refresh_status()
        self.assertEqual(self.controller.commands.count(b"B"), 5)
        self.assertFalse(self.device.connected)
        self.assertTrue(self.controller.closed)
        self.assertEqual(self.controller.timeout, 1)

    def test_status_does_not_require_a_serial_eot_marker(self):
        self.controller.config["++eot_enable"] = 0
        self.assertEqual(self.device.refresh_status().raw, bytes.fromhex("2d 1f 00 00 2d"))
        self.assertTrue(self.device.connected)

    def test_ascii_measurement_from_console_log_is_not_decoded_as_status(self):
        original = self.controller._handle

        def measurement_instead_of_status(command):
            if command in (b"++read", b"++read eoi") and self.controller.pending_status:
                self.controller.commands.append(command)
                self.controller.pending_status = False
                self.controller.input.extend(b"+044.017E-3\r\n\x04")
            else:
                original(command)

        self.controller._handle = measurement_instead_of_status
        with self.assertRaisesRegex(ProtocolError, "Invalid B response.*expected controller address"):
            self.device.refresh_status()
        self.assertFalse(self.device.connected)

    def test_numeric_prefix_without_controller_completion_is_not_accepted(self):
        self.controller.flags = ord("0")
        self.controller.status_tail = b"44."
        self.controller.config["++eot_enable"] = 0
        original = self.controller._handle

        def missing_completion(command):
            if command == b"++addr":
                self.controller.commands.append(command)
            else:
                original(command)

        self.controller._handle = missing_completion
        with self.assertRaises(QueryTimeout):
            self.device.refresh_status()
        self.assertFalse(self.device.connected)

    def test_valid_binary_frame_may_have_ascii_looking_payload(self):
        self.controller.flags = ord("0")
        self.controller.status_tail = b"44."
        for header in (ord("+"), ord("-")):
            with self.subTest(header=header):
                self.controller.status_header = header
                self.assertEqual(self.device.refresh_status().raw, bytes((header,)) + b"044.")

    def test_queued_measurement_is_discarded_before_sending_b(self):
        self.controller.input.extend(b"+044.017E-3\r\n\x04")
        status = self.device.refresh_status()
        self.assertEqual(status.raw, bytes((45, self.controller.flags)) + self.controller.status_tail)
        self.assertEqual(self.controller.input, b"")

    def test_delayed_ascii_tail_is_not_mistaken_for_an_end_of_status(self):
        original = self.controller._handle

        def fragmented_measurement(command):
            if command in (b"++read", b"++read eoi") and self.controller.pending_status:
                self.controller.commands.append(command)
                self.controller.pending_status = False
                self.controller.input.extend(b"+044.")
                self.controller.deferred.append((self.clock.now + 0.2, b"017E-3\r\n\x04"))
            else:
                original(command)

        self.controller._handle = fragmented_measurement
        with self.assertRaisesRegex(ProtocolError, "Invalid B response.*expected controller address"):
            self.device.refresh_status()
        self.assertFalse(self.device.connected)

    def test_connection_disables_saved_automatic_read_mode_before_b(self):
        self.device.disconnect()
        self.controller.config["++auto"] = 1
        original = self.controller._handle

        def require_manual_reads(command):
            if command == b"B" and self.controller.config["++auto"] != 0:
                raise AssertionError("Automatic mode would create a second reply.")
            original(command)

        self.controller._handle = require_manual_reads
        self.device.connect("COM5", 23)
        self.assertEqual(self.controller.config["++auto"], 0)

    def test_calibration_snapshot_preserves_disabled_serial_eot(self):
        self.device.transport.snapshot_framing(self.device._read_controller_line)
        self.assertEqual(self.device.transport.eot_byte, b"")
        self.assertEqual(len(self.device.refresh_status().raw), 5)

    def test_connection_supports_ar488_firmware_with_decimal_eot(self):
        self.device.disconnect()
        self.controller.config["++eot_enable"] = 1
        self.controller.config["++eot_char"] = 4
        self.controller.eot_encoding = "decimal"
        status = self.device.connect("COM5", 23)
        self.assertEqual(status.raw, bytes.fromhex("2d 1f 00 00 2d"))
        self.assertEqual(self.controller.config["++eot_enable"], 0)
        self.assertTrue(self.device.connected)

    def test_wrong_controller_completion_is_rejected(self):
        original = self.controller._handle

        def wrong_completion(command):
            if command == b"++addr":
                self.controller.commands.append(command)
                self.controller.input.extend(b"24\r\n")
            else:
                original(command)

        self.controller._handle = wrong_completion
        with self.assertRaisesRegex(ProtocolError, "expected controller address"):
            self.device.refresh_status()
        self.assertFalse(self.device.connected)


if __name__ == "__main__":
    unittest.main()
