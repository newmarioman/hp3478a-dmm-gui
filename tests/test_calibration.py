"""Check complete RAM transfers, protected writes, recovery and corrupt files."""

import json
import tempfile
import unittest
from pathlib import Path

from hp3478a.calibration import CalibrationDump, CalibrationManager
from hp3478a.instrument import HP3478A
from hp3478a.transport import AR488Transport, SimulatedTransport, TransportError


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.bus = SimulatedTransport(sample_delay=0, calibration_enabled=True)
        self.device = HP3478A(
            self.bus, command_delay=0, status_delay=0, settle_delay=0, query_timeout=0.05
        )
        self.device.connect("SIMULATED", 23)
        self.manager = CalibrationManager(self.device)
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "backup.json"
        self.bus.commands.clear()

    def tearDown(self):
        self.device.disconnect()
        self.directory.cleanup()

    def test_backup_reads_all_addresses_twice_without_any_write(self):
        image = self.manager.backup(self.path)
        self.assertEqual(bytes(image.nibbles), bytes(self.bus.ram))
        reads = [command for command in self.bus.commands if isinstance(command, bytes)]
        self.assertEqual(reads, [bytes((87, i)) for i in range(256)] * 2)
        self.assertEqual(self.manager.repository.load(self.path), image)
        self.assertIn("++eos 3", self.bus.commands)
        self.assertEqual(self.bus.commands[-1], "++eos 0")
        self.assertTrue(self.device.connected)
        self.assertEqual(self.bus.read(100), b"")

    def test_restore_saves_original_ram_and_verifies_all_data(self):
        before = bytes(self.bus.ram)
        target = bytearray(before)
        target[1:14] = bytes((10, 13, 15, 0, 1, 2, 3, 4, 5, 6, 7, 11, 13))
        # 66 calibration nibbles plus checksum 0xBD == 255.
        self.assertEqual(sum(target[1:12]), 66)
        target[255] = 15
        image = CalibrationDump(tuple(target))
        image.require_valid_calibration()
        recovery = self.manager.restore(image, self.path.parent)
        self.assertEqual(bytes(self.manager.repository.load(recovery).nibbles), before)
        self.assertEqual(self.bus.ram, target)
        writes = [c for c in self.bus.commands if isinstance(c, bytes) and c[0] == 88]
        self.assertEqual(writes, [bytes((88, i, target[i])) for i in range(256)])
        self.assertTrue(self.device.connected)

    def test_calibration_lock_blocks_restore_before_any_ram_write(self):
        self.bus.flags &= ~32
        with self.assertRaisesRegex(ValueError, "CAL ENABLE"):
            self.manager.restore(CalibrationDump(tuple(self.bus.ram)), self.path.parent)
        self.assertFalse(any(isinstance(c, bytes) and c[0] == 88 for c in self.bus.commands))
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_corrupted_digest_and_wrong_model_are_rejected(self):
        self.manager.backup(self.path)
        document = json.loads(self.path.read_text())
        for changed in (
            {**document, "instrument": "HP 34401A"},
            {**document, "sha256": "0" * 64},
            {**document, "nibbles": document["nibbles"][:-1]},
            {**document, "version": True},
        ):
            self.path.write_text(json.dumps(changed))
            with self.assertRaises(ValueError):
                self.manager.repository.load(self.path)

    def test_invalid_active_checksum_is_saved_but_cannot_be_restored(self):
        self.bus.ram[1] = 1
        image = self.manager.backup(self.path)
        self.assertEqual(image.failed_entries, (0,))
        self.bus.commands.clear()
        with self.assertRaisesRegex(ValueError, "checksums"):
            self.manager.restore(image, self.path.parent)
        self.assertEqual(self.bus.commands, [])

    def test_unused_entries_need_no_valid_checksum(self):
        for entry in (5, 16, 18):
            self.bus.ram[entry * 13 + 1] = 3
        CalibrationDump(tuple(self.bus.ram)).require_valid_calibration()

    def test_partial_write_failure_retains_recovery_backup_and_closes_port(self):
        write = self.bus.write_binary

        def fail(payload):
            if payload[:2] == b"X\x0d":
                raise TransportError("Injected write failure")
            write(payload)

        self.bus.write_binary = fail
        with self.assertRaisesRegex(RuntimeError, "Recovery backup"):
            self.manager.restore(CalibrationDump(tuple(self.bus.ram)), self.path.parent)
        self.assertEqual(len(list(self.path.parent.glob("HP3478A_before_restore_*.json"))), 1)
        self.assertFalse(self.device.connected)

    def test_readback_mismatch_is_detected_instead_of_claiming_success(self):
        write = self.bus.write_binary

        def ignore_one_write(payload):
            if payload[:2] != b"X\xff":
                write(payload)

        self.bus.write_binary = ignore_one_write
        target = bytearray(self.bus.ram)
        target[255] = 9
        with self.assertRaisesRegex(RuntimeError, "verification failed.*0xFF"):
            self.manager.restore(CalibrationDump(tuple(target)), self.path.parent)
        self.assertFalse(self.device.connected)

    def test_unstable_read_does_not_create_a_backup(self):
        read = self.device.read_calibration_ram

        def changing(progress=None):
            result = read(progress)
            self.bus.ram[255] ^= 1
            return result

        self.device.read_calibration_ram = changing
        with self.assertRaisesRegex(ValueError, "changed"):
            self.manager.backup(self.path)
        self.assertFalse(self.path.exists())

    def test_invalid_ram_byte_closes_connection_and_restores_ascii_eos(self):
        write = self.bus.write

        def malformed(command):
            write(command)
            if command == "++read eoi":
                self.bus._buffer[:] = b"?"

        self.bus.write = malformed
        with self.assertRaisesRegex(Exception, "Invalid calibration reply"):
            self.device.read_calibration_ram()
        self.assertEqual(self.bus.commands[-1], "++eos 0")
        self.assertFalse(self.device.connected)


class BinaryEscapingTests(unittest.TestCase):
    def test_every_binary_address_and_nibble_survive_ar488_serial_escaping(self):
        class Serial:
            def __init__(self):
                self.writes = []

            def write(self, payload):
                self.writes.append(payload)
                return len(payload)

            def close(self):
                pass

        serial = Serial()

        class Adapter:
            def __init__(self, port, *, timeout, debug):
                self._ser = serial

            def address(self, address):
                pass

            def close(self):
                serial.close()

        transport = AR488Transport(adapter_factory=Adapter)
        transport.open("COM4", 23)
        try:
            for address in range(256):
                for value in range(16):
                    expected = bytes((88, address, value))
                    transport.write_binary(expected)
                    wire = serial.writes[-1]
                    self.assertEqual(wire[-3:], b"\n\n\r")
                    restored, escaped = bytearray(), False
                    for byte in wire[:-3]:
                        if escaped:
                            restored.append(byte)
                            escaped = False
                        elif byte == 27:
                            escaped = True
                        else:
                            self.assertNotIn(byte, (10, 13, 43))
                            restored.append(byte)
                    self.assertFalse(escaped)
                    self.assertEqual(bytes(restored), expected)
        finally:
            transport.close()


if __name__ == "__main__":
    unittest.main()
