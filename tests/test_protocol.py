"""Protocol checks with simulated/fragmented responses and injected serial I/O."""

import ast
import math
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from tests.controller import AR488, Controller

from hp3478a.config import EOI_MARKER, FUNCTIONS, OPTION_COMMANDS, TRIGGERS
from hp3478a.instrument import HP3478A, QueryTimeout
from hp3478a.status import MeasurementDecoder, ProtocolError, StatusDecoder
from hp3478a.transport import AR488Transport, SimulatedTransport, TransportError


class StatusTests(unittest.TestCase):
    def test_all_functions_ranges_digits_and_flags(self):
        decoder = StatusDecoder()
        for function in FUNCTIONS:
            for item in function.ranges:
                for digits in (1, 2, 3):
                    for flags in range(128):
                        raw = bytes(
                            (
                                (int(function.code) << 5) | (item.status_code << 2) | digits,
                                flags,
                                13,
                                10,
                                4,
                            )
                        )
                        status = decoder.decode(raw)
                        self.assertEqual(status.function, function)
                        self.assertEqual(status.range, item)
                        self.assertEqual(status.digits_code, digits)
                        self.assertEqual(status.autorange, bool(flags & 2))
                        self.assertEqual(status.autozero, bool(flags & 4))
                        self.assertEqual(status.line_hz, 50 if flags & 16 else 60)
                        self.assertEqual(status.front_input, bool(flags & 8))
                        self.assertEqual(status.calibration_enabled, bool(flags & 32))
                        self.assertEqual(status.internal_trigger, bool(flags & 1))
                        self.assertEqual(status.external_trigger, bool(flags & 64))
                        self.assertEqual(
                            (status.srq_mask, status.error_register, status.dac_value), (13, 10, 4)
                        )

    def test_invalid_status_is_rejected(self):
        invalid = (
            b"",
            b"1234",
            b"123456",
            b"\x00\x00\x00\x00\x00",
            b"\x2c\x00\x00\x00\x00",
            b"\x2d\x80\x00\x00\x00",
            b"\x39\x00\x00\x00\x00",
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ProtocolError):
                StatusDecoder().decode(raw)

    def test_range_one_in_ac_and_resistance_has_correct_label(self):
        for code, label in ((2, "300 mV AC"), (3, "30 Ω"), (4, "30 Ω")):
            status = StatusDecoder().decode(bytes(((code << 5) | 5, 0, 0, 0, 0)))
            self.assertEqual(status.range.label, label)

    def test_leading_zeroes_and_every_status_byte_are_preserved(self):
        raw = bytes((45, 31, 13, 10, 4))
        status = StatusDecoder().decode(raw)
        self.assertEqual(status.raw, raw)
        self.assertEqual(len(status.bit_string.replace(" ", "")), 40)
        self.assertTrue(status.bit_string.startswith("00101101"))


class InstrumentTests(unittest.TestCase):
    def setUp(self):
        self.transport = SimulatedTransport(sample_delay=0)
        self.device = HP3478A(
            self.transport, command_delay=0, status_delay=0, settle_delay=0, query_timeout=0.03
        )
        self.device.connect("SIMULATED", 23)
        self.transport.commands.clear()

    def tearDown(self):
        self.device.disconnect()

    def test_connect_identifies_controller_and_reads_binary_status(self):
        self.device.disconnect()
        self.transport.commands.clear()
        status = self.device.connect("SIMULATED", 23)
        self.assertEqual(
            self.transport.commands,
            ["++ver", "++auto 0", "++eot_enable 0", "++auto", "++eot_enable", "++clr", "B", "++read eoi", "++addr", "B", "++read eoi", "++addr"],
        )
        self.assertEqual(status.function_code, 1)
        self.assertTrue(self.device.connected)
        self.assertNotIn("*IDN?", self.transport.commands)

    def test_all_functions_send_immediately_then_read_actual_status(self):
        for function in FUNCTIONS:
            self.transport.commands.clear()
            status = self.device.select_function(int(function.code))
            self.assertEqual(self.transport.commands, [function.command, "RA", "B", "++read eoi", "++addr"])
            self.assertEqual(status.function, function)

    def test_every_manual_range_sends_the_original_command(self):
        for function in FUNCTIONS:
            self.device.select_function(int(function.code))
            for item in function.ranges:
                if item.command is None:
                    continue
                self.transport.commands.clear()
                status = self.device.set_option("range", item.label)
                self.assertEqual(self.transport.commands, [item.command, "B", "++read eoi", "++addr"])
                self.assertEqual(status.range_code, item.status_code)
                self.assertFalse(status.autorange)

    def test_all_options_refresh_flags(self):
        for key, choices in OPTION_COMMANDS.items():
            for label, command in choices.items():
                self.transport.commands.clear()
                status = self.device.set_option(key, label)
                self.assertEqual(self.transport.commands, [command, "B", "++read eoi", "++addr"])
                if key == "digits":
                    self.assertEqual(status.digits, label)
                elif key == "autozero":
                    self.assertEqual(status.autozero, label == "ON")
                elif key == "display":
                    self.assertEqual(status.display_mode, label)
                else:
                    self.assertEqual(status.trigger_label, label)

    def test_autorange_off_freezes_the_reported_range(self):
        self.device.set_option("range", "30 V")
        self.transport.commands.clear()
        status = self.device.set_option("autorange", "OFF")
        self.assertEqual(self.transport.commands, ["R1", "B", "++read eoi", "++addr"])
        self.assertEqual(status.range.label, "30 V")
        self.assertTrue(self.device.set_option("autorange", "ON").autorange)

    def test_invalid_values_send_nothing_and_keep_connection(self):
        for key, value in (
            ("range", "incorrect"),
            ("trigger", "invalid"),
            ("digits", "6½"),
            ("autorange", "bad"),
        ):
            before = self.transport.commands.copy()
            with self.assertRaises(ValueError):
                self.device.set_option(key, value)
            self.assertEqual(self.transport.commands, before)
            self.assertTrue(self.device.connected)

    def test_readback_function_is_authoritative(self):
        original = self.transport.write

        def write(command):
            original(command)
            if command == "F5":
                self.transport.function_code, self.transport.range_code = 6, 1

        self.transport.write = write
        self.assertEqual(self.device.select_function(5).function_code, 6)

    def test_binary_status_can_be_fragmented_and_contain_cr_lf_eot(self):
        self.transport.srq_mask, self.transport.error_register, self.transport.dac_value = 13, 10, 4
        read = self.transport.read
        self.transport.read = lambda size: read(min(size, 1))
        status = self.device.refresh_status()
        self.assertEqual(status.raw, bytes((45, 31, 13, 10, 4)))
        self.assertEqual(self.transport.read(100), b"")

    def test_delayed_previous_terminators_do_not_become_status_payload(self):
        self.transport._buffer[:] = b"\r\n\x04"
        self.transport.eot = EOI_MARKER
        status = self.device.refresh_status()
        self.assertEqual(status.raw, bytes((45, 31, 0, 0, 0)))
        self.assertEqual(self.transport.read(100), b"")

    def test_partial_status_times_out_and_closes_connection(self):
        original = self.transport.write

        def write(command):
            original(command)
            if command == "++read eoi":
                self.transport._buffer[:] = bytes((45, 31))

        self.transport.write = write
        with self.assertRaises(QueryTimeout):
            self.device.refresh_status()
        self.assertFalse(self.device.connected)
        self.assertFalse(self.transport.is_open)

    def test_status_without_cr_lf_uses_controller_confirmation(self):
        original = self.transport.write

        def write(command):
            original(command)
            if command == "++read eoi":
                self.transport._buffer[:] = bytes((45, 31, 0, 0, 0)) + self.transport.eot_byte

        self.transport.write = write
        status = self.device.refresh_status()
        self.assertEqual(status.raw, bytes((45, 31, 0, 0, 0)))
        self.assertTrue(self.device.connected)
        self.assertEqual(self.transport.read(100), b"")

    def test_extra_data_after_binary_status_is_rejected(self):
        original = self.transport.write

        def trailing_bytes(command):
            original(command)
            if command == "++read eoi":
                self.transport._buffer.extend(b"?\xffextra")

        self.transport.write = trailing_bytes
        with self.assertRaisesRegex(ProtocolError, "Invalid B response.*expected controller address"):
            self.device.refresh_status()
        self.assertFalse(self.device.connected)

    def test_status_waits_before_b_so_delayed_configuration_is_reported_correctly(self):
        bus = SimulatedTransport(sample_delay=0)
        device = HP3478A(bus, command_delay=0, settle_delay=0)
        now, ready_at = [0.0], [math.inf]
        original = bus.write

        def delayed_dmm(command):
            if command == "B" and now[0] >= ready_at[0]:
                bus.digits_code = 3
            original(command)
            if command == "N3":
                bus.digits_code = 1
                ready_at[0] = now[0] + 1

        def advance(seconds):
            now[0] += seconds

        bus.write = delayed_dmm
        with patch("hp3478a.instrument.time.sleep", side_effect=advance):
            try:
                device.connect("SIMULATED", 23)
                self.assertEqual(device.status_delay, 1)
                status = device.set_option("digits", "3½")
                self.assertEqual(status.digits, "3½")
                self.assertGreaterEqual(now[0], ready_at[0])
            finally:
                device.disconnect()

    def test_transport_write_failure_closes_port(self):
        def fail(command):
            raise TransportError("Injected serial error")

        self.transport.write = fail
        with self.assertRaises(TransportError):
            self.device.select_function(5)
        self.assertFalse(self.transport.is_open)

    def test_internal_reads_do_not_trigger_other_modes_do(self):
        for trigger in ("Internal", "Fast", "External", "Single", "Hold"):
            self.device.set_option("trigger", trigger)
            self.transport.commands.clear()
            reading = self.device.read_measurement(lambda: 0.5)
            expected = ["++read"] if trigger in ("Internal", "Fast") else ["++trg", "++read"]
            self.assertEqual(self.transport.commands, expected)
            self.assertEqual(reading.timestamp, 0.5)
            self.assertEqual(reading.function_label, "Voltage DC")

    def test_reading_keeps_its_function_after_configuration_changes(self):
        reading = self.device.read_measurement(1)
        self.device.select_function(5)
        self.assertEqual(reading.function_label, "Voltage DC")
        self.assertEqual(reading.unit, "V")

    def test_reset_clear_errors_and_disconnect(self):
        self.transport.commands.clear()
        self.device.clear_errors()
        self.assertEqual(self.transport.commands, ["K", "B", "++read eoi", "++addr"])
        self.device.select_function(5)
        self.assertEqual(self.device.reset().function_code, 1)
        self.transport.commands.clear()
        self.device.disconnect()
        self.assertEqual(self.transport.commands, ["K", "++clr", "++loc"])
        self.assertIsNone(self.device.status)

    def test_custom_commands_refresh_status_without_leaving_bytes(self):
        self.device.send_custom("F5")
        self.assertEqual(self.device.status.function_code, 5)
        self.assertIsNone(self.device.status.display_mode)
        self.device.send_custom("D3")
        self.assertEqual(self.device.status.display_mode, "OFF")
        self.assertEqual(len(self.device.send_custom("B").replace(" ", "")), 40)
        self.assertTrue(math.isfinite(float(self.device.send_custom("++read"))))
        self.assertEqual(self.transport.read(100), b"")

    def test_framing_changes_and_multiline_commands_are_rejected(self):
        for command in ("F5\nRA", "Ω", "++auto 1", "++eot_char 0", "++addr 24", "++rst"):
            before = self.transport.commands.copy()
            with self.assertRaises(ValueError):
                self.device.send_custom(command)
            self.assertEqual(self.transport.commands, before)

    def test_binary_custom_query_is_shown_as_hex_then_status_is_refreshed(self):
        original = self.transport.write
        pending = []

        def write(command):
            original(command)
            if command == "E":
                pending.append(True)
            elif command == "++read" and pending:
                pending.clear()
                self.transport._buffer[:] = b"\xff\x01\r\n" + EOI_MARKER

        self.transport.write = write
        response = self.device.send_custom("E", query=True)
        self.assertEqual(response, "Binary response: ff 01 0d 0a")
        self.assertTrue(self.device.connected)
        self.assertEqual(self.transport.commands[-3:], ["B", "++read eoi", "++addr"])
        self.assertEqual(self.transport.read(100), b"")

    def test_two_threads_do_not_interleave_transactions(self):
        entered, release = threading.Event(), threading.Event()
        write = self.transport.write

        def blocked(command):
            write(command)
            if command == "N4":
                entered.set()
                if not release.wait(1):
                    raise RuntimeError("Test gate timed out")

        self.transport.write = blocked
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.device.set_option, "digits", "4½")
            self.assertTrue(entered.wait(1))
            second = pool.submit(self.device.set_option, "autozero", "OFF")
            release.set()
            first.result(timeout=1)
            second.result(timeout=1)
        self.assertEqual(self.transport.commands, ["N4", "B", "++read eoi", "++addr", "Z0", "B", "++read eoi", "++addr"])


class MeasurementTests(unittest.TestCase):
    def test_signed_scientific_values_and_negative_zero(self):
        for raw, value in ((b"+1.23E-3\r\n", 0.00123), (b"-1.0E+1", -10), (b"-0", -0.0)):
            reading = MeasurementDecoder().decode(raw, 1, "V")
            self.assertEqual(reading.value, value)
        self.assertTrue(MeasurementDecoder().decode(b"-0", 1, "V").display_text.startswith("-0"))

    def test_invalid_and_nonfinite_readings_are_rejected(self):
        for raw in (b"", b"nan", b"inf", b"-inf", b"\xff", b"not a measurement"):
            with self.assertRaises(ProtocolError):
                MeasurementDecoder().decode(raw, 1, "V")

    def test_overload_is_distinguished_from_normal_value(self):
        self.assertTrue(MeasurementDecoder().decode(b"+9.99999E+9", 1, "V").overload)
        self.assertFalse(MeasurementDecoder().decode(b"+300", 1, "V").overload)


class AdapterTests(unittest.TestCase):
    def test_full_connection_uses_driver_reads_with_embedded_binary_newlines(self):
        controller = Controller(eot_enable=0)
        controller.VERSION = b"AR488 Controller ver. 0.47"
        original = controller._handle

        def binary_status(command):
            if command == b"++read eoi" and controller.pending_status:
                controller.commands.append(command)
                controller.input.extend(bytes((45, 31, 13, 10, 4)) + b"\r\n")
                controller.pending_status = False
            else:
                original(command)

        controller._handle = binary_status
        device = HP3478A(AR488Transport(), command_delay=0, status_delay=0, settle_delay=0)
        with (
            patch.object(AR488.serial, "Serial", controller.open),
            patch.object(AR488.time, "sleep"),
        ):
            try:
                status = device.connect("COM3", 23)
                self.assertEqual(status.raw, bytes((45, 31, 13, 10, 4)))
                self.assertEqual(
                    device.connection_info.controller_name, controller.VERSION.decode()
                )
                self.assertEqual(device.read_measurement().value, -12)
                self.assertEqual(controller.input, b"")
                self.assertIn(b"++read eoi", controller.commands)
            finally:
                device.disconnect()
        self.assertTrue(controller.closed)

    def test_address_readback_failure_in_driver_releases_port(self):
        controller = Controller()
        original = controller._handle

        def wrong_address(command):
            if command != b"++addr 23":
                original(command)

        controller._handle = wrong_address
        device = HP3478A(AR488Transport(), command_delay=0, status_delay=0)
        with (
            patch.object(AR488.serial, "Serial", controller.open),
            patch.object(AR488.time, "sleep"),
        ):
            with self.assertRaisesRegex(
                TransportError, "COM3, GPIB address 23.*opening.*Failed to set interface address"
            ):
                device.connect("COM3", 23)
        self.assertTrue(controller.closed)
        self.assertFalse(device.connected)

    def test_hardware_transport_keeps_driver_timeouts_and_existing_configuration(self):
        controller = Controller()
        transport = AR488Transport()
        with (
            patch.object(AR488.serial, "Serial", controller.open),
            patch.object(AR488.time, "sleep"),
            patch.object(controller, "reset_input_buffer", side_effect=AssertionError("Reset")),
        ):
            transport.open("COM3", 23)
            try:
                self.assertEqual(
                    controller.port_options, {"port": "COM3", "baudrate": 115200, "timeout": 1}
                )
                self.assertEqual(controller.timeout, 1)
                self.assertIsNone(controller.write_timeout)
                self.assertNotIn(b"++mode 1", controller.commands)
                self.assertNotIn(b"++eoi 1", controller.commands)
                self.assertNotIn(b"++eos 0", controller.commands)
                self.assertNotIn(b"++eot_enable 0", controller.commands)
                for command in ("F5", "F5\n", "F5\r\n"):
                    transport.write(command)
                    self.assertEqual(controller.writes[-1], b"F5\n\n\r")
            finally:
                transport.close()
                transport.close()
        self.assertTrue(controller.closed)

    def test_invalid_connection_does_not_open_driver(self):
        def fail(*args, **kwargs):
            self.fail("Invalid arguments opened the serial port")

        transport = AR488Transport(adapter_factory=fail)
        for port, address in (("", 23), ("COM1", -1), ("COM1", 0), ("COM1", 31), ("COM1", True)):
            with self.assertRaises(ValueError):
                transport.open(port, address)


if __name__ == "__main__":
    unittest.main()
