"""Exercise the real PyAR488 driver against a serial controller fixture."""

import runpy
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.controller import AR488, Controller

from hp3478a.instrument import HP3478A
from hp3478a.transport import PyAR488Transport, SimulatedTransport, TransportError


class TransportTests(unittest.TestCase):
    def make_device(self, controller):
        return HP3478A(
            PyAR488Transport(), command_delay=0, status_delay=0, settle_delay=0, query_timeout=0.05
        )

    def test_text_responses_use_bus_read_and_status_uses_sized_binary_reads(self):
        controller = Controller(eot_enable=0)
        device = self.make_device(controller)
        with (
            patch.object(AR488.serial, "Serial", controller.open),
            patch.object(AR488.time, "sleep"),
        ):
            try:
                with patch.object(controller, "read", wraps=controller.read) as raw_read:
                    status = device.connect("COM5", 23)
                    self.assertGreater(raw_read.call_count, 0)
                self.assertEqual(status.raw, bytes((45, 31, 0, 0, 45)))
                with patch.object(device.transport, "read_binary", side_effect=AssertionError("Unexpected binary payload read")):
                    self.assertEqual(device.read_measurement().value, -12)
                self.assertEqual(controller.timeout, 1)
                self.assertIsNone(controller.write_timeout)
            finally:
                device.disconnect()

    def test_missing_controller_version_times_out_and_closes_port(self):
        controller = Controller()
        original = controller._handle

        def omit_version(command):
            if command != b"++ver":
                original(command)

        controller._handle = omit_version
        device = self.make_device(controller)
        with (
            patch.object(AR488.serial, "Serial", controller.open),
            patch.object(AR488.time, "sleep"),
        ):
            with self.assertRaisesRegex(
                TransportError, "while reading the controller version.*timed out.*Received: b''"
            ):
                device.connect("COM5", 23)
        self.assertTrue(controller.closed)

    def test_real_pyar488_connects_using_original_serial_parameters_and_wire_commands(self):
        controller = Controller()
        device = self.make_device(controller)
        with (
            patch.object(AR488.serial, "Serial", controller.open),
            patch.object(AR488.time, "sleep"),
        ):
            try:
                status = device.connect("COM5", 23)
                self.assertEqual(
                    device.connection_info.controller_name, controller.VERSION.decode()
                )
                self.assertEqual(status.raw, bytes((45, 31, 0, 0, 45)))
                self.assertEqual(status.range.label, "3 V")
                self.assertEqual(
                    controller.port_options, {"port": "COM5", "baudrate": 115200, "timeout": 1}
                )
                self.assertIn(b"++ver\n\n\r", controller.writes)
                self.assertEqual(controller.status_count, 2)
                self.assertNotIn(b"++mode 1", controller.commands)
                self.assertNotIn(b"++eos 0", controller.commands)
                self.assertIn(b"++eot_enable 0", controller.commands)
                self.assertEqual(device.read_measurement().value, -12)
                self.assertEqual(controller.input, b"")
            finally:
                device.disconnect()
        self.assertTrue(controller.closed)

    def test_ascii_commands_delegate_strings_to_real_pyar488_bus_write(self):
        controller = Controller()
        device = self.make_device(controller)
        original = AR488.bus_write
        commands = []

        def record(adapter, command):
            commands.append(command)
            return original(adapter, command)

        with (
            patch.object(AR488.serial, "Serial", controller.open),
            patch.object(AR488.time, "sleep"),
            patch.object(AR488, "bus_write", record),
        ):
            try:
                device.connect("COM5", 23)
                self.assertIn("++ver\n", commands)
                application_commands = commands[commands.index("++ver\n") :]
                self.assertEqual(
                    application_commands,
                    [
                        "++ver\n",
                        "++auto 0\n",
                        "++eot_enable 0\n",
                        "++auto\n",
                        "++eot_enable\n",
                        "++clr\n",
                        "B\n",
                        "++read eoi\n",
                        "++addr\n",
                        "B\n",
                        "++read eoi\n",
                        "++addr\n",
                    ],
                )
                self.assertTrue(all(isinstance(command, str) for command in commands))
                commands.clear()
                device.select_function(5)
                self.assertEqual(commands, ["F5\n", "RA\n", "B\n", "++read eoi\n", "++addr\n"])
                commands.clear()
                self.assertEqual(device.read_measurement().value, -12)
                self.assertEqual(commands, ["++read\n"])
            finally:
                device.disconnect()

    def test_invalid_ascii_commands_never_reach_pyar488_bus_write(self):
        controller = Controller()
        device = self.make_device(controller)
        with (
            patch.object(AR488.serial, "Serial", controller.open),
            patch.object(AR488.time, "sleep"),
        ):
            try:
                device.connect("COM5", 23)
                with patch.object(AR488, "bus_write") as write:
                    for command in ("", "F5\nRA", "F5\rRA", "F5\n\rN3", "µ", b"F5"):
                        with self.subTest(command=command), self.assertRaises(ValueError):
                            device.transport.write(command)
                    write.assert_not_called()
                self.assertTrue(device.connected)
                self.assertEqual(device.read_measurement().value, -12)
            finally:
                device.disconnect()

    def test_pyar488_bus_write_failure_closes_connection(self):
        controller = Controller()
        device = self.make_device(controller)
        with (
            patch.object(AR488.serial, "Serial", controller.open),
            patch.object(AR488.time, "sleep"),
        ):
            device.connect("COM5", 23)
            with patch.object(AR488, "bus_write", side_effect=OSError("Injected write failure")):
                with self.assertRaisesRegex(TransportError, "Injected write failure"):
                    device.select_function(5)
            self.assertFalse(device.connected)
            self.assertTrue(controller.closed)

    def test_saved_eot_character_cannot_be_mistaken_for_a_negative_sign(self):
        controller = Controller(eot=ord("-"))
        device = self.make_device(controller)
        with (
            patch.object(AR488.serial, "Serial", controller.open),
            patch.object(AR488.time, "sleep"),
        ):
            try:
                device.connect("COM5", 23)
                self.assertEqual(device.read_measurement().value, -12)
                self.assertEqual(controller.input, b"")
            finally:
                device.disconnect()

    def test_ram_read_escapes_binary_addresses_and_restores_saved_framing(self):
        controller = Controller(eos=2, eoi=0, eot=4)
        controller.ram[:] = bytes(i % 16 for i in range(256))
        device = self.make_device(controller)
        with (
            patch.object(AR488.serial, "Serial", controller.open),
            patch.object(AR488.time, "sleep"),
        ):
            try:
                device.connect("COM5", 23)
                self.assertEqual(device.read_calibration_ram(), controller.ram)
                self.assertEqual(
                    [c for c in controller.commands if c.startswith(b"W")],
                    [bytes((87, i)) for i in range(256)],
                )
                self.assertEqual((controller.config["++eos"], controller.config["++eoi"]), (2, 0))
                self.assertEqual(controller.input, b"")
            finally:
                device.disconnect()

    def test_connection_configures_manual_reads_and_binary_eoi_before_dmm_handshake(self):
        controller = Controller()
        device = self.make_device(controller)
        with (
            patch.object(AR488.serial, "Serial", controller.open),
            patch.object(AR488.time, "sleep"),
        ):
            try:
                device.connect("COM5", 23)
                self.assertEqual(
                    controller.commands[controller.commands.index(b"++ver") :],
                    [b"++ver", b"++auto 0", b"++eot_enable 0", b"++auto", b"++eot_enable", b"++clr", b"B", b"++read eoi", b"++addr", b"B", b"++read eoi", b"++addr"],
                )
            finally:
                device.disconnect()

    def test_address_failure_closes_the_pyar488_serial_handle(self):
        controller = Controller()
        device = self.make_device(controller)

        original = AR488.address

        def fail(adapter, address=None):
            if address is not None:
                raise ValueError("Injected address failure")
            return original(adapter)

        with (
            patch.object(AR488.serial, "Serial", controller.open),
            patch.object(AR488.time, "sleep"),
            patch.object(AR488, "address", fail),
        ):
            with self.assertRaises(TransportError):
                device.connect("COM5", 23)
        self.assertTrue(controller.closed)
        self.assertFalse(device.connected)

    def test_all_initial_eot_values_are_disabled_and_preserve_measurement_sign(self):
        bus = SimulatedTransport(sample_delay=0)
        device = HP3478A(bus, command_delay=0, status_delay=0, settle_delay=0)
        device.connect("SIMULATED", 23)
        write = bus.write

        def negative(command):
            is_status = bus._pending_status
            write(command)
            if command == "++read" and not is_status:
                bus._buffer[:] = b"-1.2E+1\r\n" + bus.eot

        bus.write = negative
        try:
            for value in range(256):
                device.disconnect()
                bus.eot = bytes((value,))
                self.assertEqual(device.connect("SIMULATED", 23).raw, bytes((45, 31, 0, 0, 0)))
                self.assertEqual(bus.eot_byte, b"")
                self.assertEqual(device.read_measurement().value, -12)
                self.assertEqual(bus.read(100), b"")
        finally:
            device.disconnect()


if __name__ == "__main__":
    unittest.main()
