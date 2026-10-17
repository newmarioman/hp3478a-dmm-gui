# -*- coding: utf-8 -*-

"""
Hardware Transport Layer and PyAR488 GPIB-to-Serial Communication.

This module defines the abstract base transport architecture and implements 
the real-time serial interface wrapper for AR488 controllers. It handles bus writes, raw binary data stream fragments, 
and low-level EOS/EOI framing options.

Part of the HP 3478A DMM GUI project.
Distributed under the terms of the MIT license.

Author: Lorenzo Mariani <newmarioman@hotmail.it>
"""

import math
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import contextmanager

# Local Packages
from .config import FUNCTION_BY_CODE


class TransportError(RuntimeError):
    pass


class TransportTimeout(TransportError):
    """A read timed out while the hardware transport was being opened."""


class InstrumentTransport(ABC):
    eot_byte: bytes = b""
    send_eos: int = 0
    send_eoi: bool = True

    @abstractmethod
    def open(self, port: str, address: int) -> None:
        pass

    @abstractmethod
    def write(self, command: str) -> None:
        pass

    @abstractmethod
    def read(self, size: int = 1) -> bytes:
        pass

    def read_text(self) -> str:
        return self.read(4096).decode("utf-8", errors="replace")

    def read_binary(self, size: int = 1) -> bytes:
        return self.read(size)

    def read_available(self, size: int = 1) -> bytes:
        return self.read(size)

    @contextmanager
    def measurement_reader(self):
        """Yield a reader that cooperates with the instrument's response deadline."""
        yield self.read

    def discard_input(self) -> None:
        """Discard queued bytes from an incomplete response before retrying."""
        while self.read_available(4096):
            pass

    def snapshot_framing(self, controller_query: Callable[[str], bytes] | None = None) -> None:
        """Save controller framing before a calibration operation changes it."""

    def write_binary(self, payload: bytes) -> None:
        raise TransportError("This transport does not support binary calibration commands.")

    @abstractmethod
    def close(self) -> None:
        pass


class AR488Transport(InstrumentTransport):
    """
    Controller text uses bus_read(); ASCII DMM replies use bus_read(decode=False).
    A byte buffer preserves response bytes across readline fragments. Fixed-
    length binary status and calibration replies use sized serial reads;
    they must not wait for a line terminator.
    """

    def __init__(self, timeout: float = 1.0, *, adapter_factory: Callable | None = None) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Serial timeout must be a finite positive number.")
        self.timeout = timeout
        self._adapter_factory = adapter_factory
        self._interface = None
        self._buffer = bytearray()

    @property
    def is_open(self) -> bool:
        return self._interface is not None

    def open(self, port: str, address: int) -> None:
        self.close()
        if not port.strip() or type(address) is not int or not 1 <= address <= 30:
            raise ValueError("Select a serial port and a GPIB address from 1 to 30.")
        factory = self._adapter_factory
        if factory is None:
            try:
                try:
                    from PyAR488 import AR488
                except ImportError:
                    from PyAR488.PyAR488 import AR488
            except ImportError as exc:
                raise TransportError(
                    "PyAR488 is not installed. Run: python -m pip install -r requirements.txt"
                ) from exc
            class ConnectingAR488(AR488):
                def __init__(self, *args, **kwargs):
                    try:
                        super().__init__(*args, **kwargs)
                    except Exception:
                        # A constructor failure happens before _interface is
                        # assigned, so release its serial handle here.
                        serial = getattr(self, "_ser", None)
                        if serial is not None:
                            try:
                                serial.close()
                            except Exception:
                                pass
                            self._ser = None
                        raise

                def query(self, message, payload=False, decode=True):
                    response = super().query(message, payload=payload, decode=decode)
                    if not response or not response.strip():
                        raise TimeoutError(f"Controller {message} response timed out.")
                    return response

            factory = ConnectingAR488
        try:
            self._interface = factory(port, timeout=self.timeout, debug=False)
            self._interface.address(address)
            # The constructor already read this character. Do not add queries
            # or modify serial timeouts before the first ++ver exchange.
            character = getattr(self._interface, "_eot_char", "")
            self.eot_byte = character.encode("latin-1") if character else b""
            if len(self.eot_byte) > 1:
                raise ValueError("The controller EOT character must be exactly one byte.")
        except TimeoutError as exc:
            self.close()
            raise TransportTimeout(f"Could not open AR488 on {port}, GPIB {address}: {exc}") from exc
        except Exception as exc:
            self.close()
            raise TransportError(f"Could not open AR488 on {port}, GPIB {address}: {exc}") from exc

    def write(self, command: str) -> None:
        if self._interface is None:
            raise TransportError("The AR488 port is not open.")
        if not isinstance(command, str):
            raise ValueError("Instrument commands must be ASCII strings.")
        # The driver receives a string and owns its ASCII conversion and LF/CR ending.
        command = command.rstrip("\r\n")
        if not command or any(char in command for char in "\r\n"):
            raise ValueError("Send one ASCII command without embedded line breaks.")
        if not command.isascii():
            raise ValueError("Instrument commands must contain ASCII characters.")
        try:
            self._interface.bus_write(command + "\n")
        except Exception as exc:
            raise TransportError(f"AR488 write error: {exc}") from exc

    def read_text(self) -> str:
        if self._interface is None:
            raise TransportError("The AR488 port is not open.")
        try:
            if self._buffer:
                response = bytes(self._buffer)
                self._buffer.clear()
            else:
                response = self._interface.bus_read()
            return response.decode("utf-8") if isinstance(response, bytes) else str(response or "")
        except Exception as exc:
            raise TransportError(f"AR488 read error: {exc}") from exc

    def read(self, size: int = 1) -> bytes:
        if self._interface is None:
            raise TransportError("The AR488 port is not open.")
        try:
            if not self._buffer:
                response = self._interface.bus_read(decode=False)
                if isinstance(response, str):
                    response = response.encode("latin-1")
                self._buffer.extend(response or b"")
            result = bytes(self._buffer[:size])
            del self._buffer[: len(result)]
            return result
        except Exception as exc:
            raise TransportError(f"AR488 binary read error: {exc}") from exc

    def read_available(self, size: int = 1) -> bytes:
        if self._interface is None:
            raise TransportError("The AR488 port is not open.")
        if self._buffer:
            result = bytes(self._buffer[:size])
            del self._buffer[: len(result)]
            return result
        serial = getattr(self._interface, "_ser", None)
        if serial is None:
            return b""
        # A short, bounded availability check handles delayed USB trailers.
        # It leaves both serial timeout properties exactly as PyAR488 opened them.
        deadline = time.monotonic() + 0.05
        try:
            while time.monotonic() < deadline:
                waiting = serial.in_waiting
                if waiting:
                    return bytes(serial.read(min(size, waiting)))
                time.sleep(0.001)
            return b""
        except Exception as exc:
            raise TransportError(f"AR488 trailer read error: {exc}") from exc

    @contextmanager
    def measurement_reader(self):
        if self._interface is None:
            raise TransportError("The AR488 port is not open.")
        try:
            yield self.read
        finally:
            pass

    def discard_input(self) -> None:
        if self._interface is None:
            raise TransportError("The AR488 port is not open.")
        self._buffer.clear()
        try:
            self._interface._ser.reset_input_buffer()
        except Exception as exc:
            raise TransportError(f"AR488 input reset error: {exc}") from exc

    def read_binary(self, size: int = 1) -> bytes:
        if self._interface is None:
            raise TransportError("The AR488 port is not open.")
        if self._buffer:
            result = bytes(self._buffer[:size])
            del self._buffer[: len(result)]
            return result
        # Status and calibration replies have known lengths and need not
        # contain LF. Binary CR/LF values are data, not message terminators.
        try:
            return bytes(self._interface._ser.read(size))
        except Exception as exc:
            raise TransportError(f"AR488 binary read error: {exc}") from exc

    def snapshot_framing(self, controller_query: Callable[[str], bytes] | None = None) -> None:
        if self._interface is None:
            raise TransportError("The AR488 port is not open.")
        try:
            # Query only when calibration is requested. Trim the raw text to
            # avoid boolean-parsing differences between PyAR488 releases.
            query = self._interface.query
            if controller_query is not None:
                # Use the instrument's deadline and retry policy for all
                # controller reads, before changing any calibration framing.
                def query(command: str) -> str:
                    return controller_query(command).decode("ascii")

            eos = query("++eos").strip()
            eoi = query("++eoi").strip()
            eot = query("++eot_enable").strip()
            character = int(query("++eot_char").strip())
            if (
                eos not in ("0", "1", "2", "3") or eoi not in ("0", "1")
                or eot not in ("0", "1") or not 0 <= character <= 255
            ):
                raise ValueError("Invalid controller EOS/EOI/EOT flags.")
            self.send_eos, self.send_eoi = int(eos), eoi == "1"
            # The session may have changed the character since PyAR488's
            # constructor cached it. Preserve the active controller setting.
            self.eot_byte = bytes((character,)) if eot == "1" else b""
        except Exception as exc:
            raise TransportError(f"Could not read AR488 calibration framing: {exc}") from exc

    def write_binary(self, payload: bytes) -> None:
        if self._interface is None:
            raise TransportError("The AR488 port is not open.")
        if not isinstance(payload, bytes) or not payload:
            raise ValueError("Binary commands must be nonempty bytes.")
        # AR488 reserves CR, LF, ESC and '+', even inside a binary address.
        escaped = bytearray()
        for value in payload:
            if value in (10, 13, 27, 43):
                escaped.append(27)
            escaped.append(value)
        wire = bytes(escaped) + b"\n\n\r"
        try:
            if self._interface._ser.write(wire) != len(wire):
                raise TransportError("Incomplete serial write.")
        except Exception as exc:
            raise TransportError(f"AR488 calibration write error: {exc}") from exc

    def close(self) -> None:
        interface, self._interface = self._interface, None
        self._buffer.clear()
        self.eot_byte, self.send_eos, self.send_eoi = b"", 0, True
        if interface is not None:
            try:
                close = getattr(interface, "close", None)
                if callable(close):
                    close()
                else:
                    interface._ser.close()
            except Exception:
                # Keep cleanup idempotent and preserve the original I/O error.
                pass


# Preserve imports used by the previous modular release; there is one hardware path.
PyAR488Transport = AR488Transport


class SimulatedTransport(InstrumentTransport):
    """Exercise the same binary protocol, commands and acquisition without hardware."""

    def __init__(
        self, *, sample_delay: float = 0.01, eot: bytes = b"", calibration_enabled: bool = False
    ) -> None:
        self.sample_delay = sample_delay
        self.eot = eot
        self.calibration_enabled = calibration_enabled
        self.commands: list[str | bytes] = []
        self.ram = bytearray(256)
        for entry in range(19):
            self.ram[1 + entry * 13 + 11 : 1 + entry * 13 + 13] = b"\x0f\x0f"
        self._pending_ram = None
        self.is_open = False
        self.sample_count = 0
        self._buffer = bytearray()
        self._pending_status = False
        self._pending_ram = None
        self._reset()

    def _reset(self) -> None:
        self.function_code, self.range_code, self.digits_code = 1, 3, 1
        self.flags, self.error_register, self.srq_mask, self.dac_value = 0x1F, 0, 0, 0
        if self.calibration_enabled:
            self.flags |= 32
        self.display_mode = "ON"

    @property
    def eot_byte(self) -> bytes:
        return self.eot

    @eot_byte.setter
    def eot_byte(self, value: bytes) -> None:
        self.eot = value

    def open(self, port: str, address: int) -> None:
        if not isinstance(address, int) or isinstance(address, bool) or not 1 <= address <= 30:
            raise ValueError("Select a GPIB address from 1 to 30.")
        if self.is_open:
            raise TransportError("The simulated controller is already open.")
        self.is_open = True
        self.address = address
        self.automatic = 0
        self._buffer.clear()
        self._pending_status = False
        self._reset()

    def write(self, command: str) -> None:
        if not self.is_open:
            raise TransportError("The simulated controller is closed.")
        self.commands.append(command)
        if command == "++ver":
            self._buffer.extend(b"AR488 software simulator\r\n")
        elif command == "++addr":
            self._buffer.extend(f"{self.address}\r\n".encode("ascii"))
        elif command == "++auto":
            self._buffer.extend(f"{self.automatic}\r\n".encode("ascii"))
        elif command == "++auto 0":
            self.automatic = 0
        elif command.startswith("++eot_char "):
            self.eot = bytes((int(command.split()[1]),))
        elif command == "++eot_enable 1" and not self.eot:
            self.eot = b"\x04"
        elif command == "++eot_enable 0":
            self.eot = b""
        elif command == "++eot_enable":
            self._buffer.extend(b"1\r\n" if self.eot else b"0\r\n")
        elif command == "++clr":
            self._reset()
        elif command == "B":
            self._pending_status = True
        elif command.startswith("++read"):
            if self._pending_ram is not None:
                self._buffer.extend(bytes((64 + self.ram[self._pending_ram],)) + self.eot)
                self._pending_ram = None
            elif self._pending_status:
                first = (self.function_code << 5) | (self.range_code << 2) | self.digits_code
                self._buffer.extend(
                    bytes((first, self.flags, self.srq_mask, self.error_register, self.dac_value))
                    + b"\r\n"
                    + self.eot
                )
                self._pending_status = False
                self.error_register = 0
            else:
                if self.sample_delay:
                    time.sleep(self.sample_delay)
                self.sample_count += 1
                unit = FUNCTION_BY_CODE[self.function_code].unit
                base = {"V": 1.2, "A": 0.012, "Ω": 1000.0}[unit]
                value = base * (1 + 0.15 * math.sin(self.sample_count / 12))
                self._buffer.extend(f"{value:+.9E}\r\n".encode("ascii") + self.eot)
        elif (
            command.startswith("F")
            and command[1:].isdigit()
            and int(command[1:]) in FUNCTION_BY_CODE
        ):
            self.function_code = int(command[1:])
            self._auto_range()
        elif command == "RA":
            self.flags |= 2
            self._auto_range()
        elif command.startswith("R"):
            ranges = FUNCTION_BY_CODE[self.function_code].ranges
            selected = next((item for item in ranges if item.command == command), None)
            if selected:
                self.range_code = selected.status_code
                self.flags &= ~2
        elif command in ("N3", "N4", "N5"):
            self.digits_code = 6 - int(command[1:])
        elif command in ("Z0", "Z1"):
            self.flags = self.flags | 4 if command == "Z1" else self.flags & ~4
        elif command in ("T1", "T2", "T3", "T4", "T5"):
            self.flags &= ~0x41
            if command in ("T1", "T5"):
                self.flags |= 1
            elif command == "T2":
                self.flags |= 0x40
        elif command in ("D1", "D3"):
            self.display_mode = "ON" if command == "D1" else "OFF"
        elif command == "K":
            self.error_register = 0

    def _auto_range(self) -> None:
        self.range_code = {1: 3, 2: 2, 3: 3, 4: 3, 5: 1, 6: 1, 7: 1}[self.function_code]

    def write_binary(self, payload: bytes) -> None:
        if not self.is_open:
            raise TransportError("The simulated controller is closed.")
        if len(payload) == 2 and payload[0] == ord("W"):
            self.commands.append(payload)
            self._pending_ram = payload[1]
        elif len(payload) == 3 and payload[0] == ord("X") and payload[2] <= 15:
            self.commands.append(payload)
            if self.flags & 32:
                self.ram[payload[1]] = payload[2]
        else:
            raise ValueError("Invalid calibration command.")

    def read(self, size: int = 1) -> bytes:
        if not self.is_open:
            raise TransportError("The simulated controller is closed.")
        result = bytes(self._buffer[:size])
        del self._buffer[:size]
        return result

    def close(self) -> None:
        self.is_open = False
        self._buffer.clear()
        self._pending_status = False
