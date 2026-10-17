# -*- coding: utf-8 -*-

"""
HP 3478A Digital Multimeter Command Serializer and Protocol Decoder.

This module encapsulates the core logical layer for managing transaction states,
sending safe ASCII instrument sequences, tracking trigger/display modes, and 
handling communication session contexts for the HP 3478A DMM hardware.

Part of the HP 3478A DMM GUI project.
Distributed under the terms of the MIT license.

Author: Lorenzo Mariani <newmarioman@hotmail.it>
"""

import math
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import TypeVar

# Local Packages
from .config import EOI_MARKER, FUNCTION_BY_CODE, OPTION_COMMANDS, TRIGGERS
from .models import ConnectionInfo, DmmStatus, Measurement
from .status import MeasurementDecoder, ProtocolError, StatusDecoder
from .transport import InstrumentTransport, TransportError, TransportTimeout


_ReadResult = TypeVar("_ReadResult")


class QueryTimeout(TimeoutError):
    pass


class HP3478A:
    MAX_CONSECUTIVE_TIMEOUTS = 5

    def __init__(
        self,
        transport: InstrumentTransport,
        *,
        query_timeout: float = 1.0,
        command_delay: float = 0.001,
        settle_delay: float = 1.0,
        status_delay: float = 1.0,
        trace: Callable[[str, str], None] | None = None,
        activity: Callable[[str], None] | None = None,
    ) -> None:
        if not math.isfinite(query_timeout) or query_timeout <= 0:
            raise ValueError("Query timeout must be a finite positive number.")
        if not math.isfinite(status_delay) or status_delay < 0:
            raise ValueError("Status delay must be a finite nonnegative number.")
        self.transport = transport
        self.status_delay = status_delay
        self.query_timeout, self.command_delay, self.settle_delay = (
            query_timeout,
            command_delay,
            settle_delay,
        )
        self.trace, self.activity = (
            trace or (lambda direction, text: None),
            activity or (lambda direction: None),
        )
        self._lock = threading.RLock()
        self._status: DmmStatus | None = None
        self._info: ConnectionInfo | None = None
        self._connected = False
        self._display_mode: str | None = None
        self._trigger: str | None = None
        self._gpib_address: int | None = None
        self.status_decoder, self.measurement_decoder = StatusDecoder(), MeasurementDecoder()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def status(self) -> DmmStatus | None:
        return self._status

    @property
    def connection_info(self) -> ConnectionInfo | None:
        return self._info

    @contextmanager
    def _session(self, require_connection: bool = True, *, keep_connection_on_timeout: bool = False):
        with self._lock:
            if require_connection and not self._connected:
                raise TransportError("Connect to the DMM first.")
            try:
                yield
            except QueryTimeout:
                if not keep_connection_on_timeout:
                    self._release()
                raise
            except (TransportError, ProtocolError):
                self._release()
                raise

    def _release(self) -> None:
        self._connected, self._status, self._info = False, None, None
        self._display_mode, self._trigger = None, None
        self._gpib_address = None
        self.transport.close()

    def connect(self, port: str, address: int) -> DmmStatus:
        with self._session(False):
            if self._connected:
                raise ValueError("The DMM is already connected.")
            stage = "opening the serial port"
            try:
                self._retry_read(
                    lambda: self.transport.open(port, address),
                    "Connection initialization",
                    discard_input=False,
                )
                stage = "reading the controller version"
                version = self._read_controller_line("++ver").decode("ascii", errors="replace").strip()
                if not version or "unrecognized command" in version.lower():
                    raise ProtocolError(f"Invalid AR488 response: {version!r}")
                self._received(f"Controller: {version}")
                stage = "configuring explicit GPIB responses"
                # The application sends every ++read explicitly. A saved
                # automatic-read mode would create additional responses.
                self._write("++auto 0")
                # AR488 0.53 emits a numeric EOT with Print::print(uint8_t),
                # producing ASCII digits instead of a raw marker byte.
                # Disable EOT and use a controller reply to confirm that the
                # preceding GPIB transaction has completed.
                self._write("++eot_enable 0")
                self.transport.eot_byte = b""
                self.transport.discard_input()
                automatic = self._read_controller_line("++auto")
                eot_enabled = self._read_controller_line("++eot_enable")
                if automatic != b"0" or eot_enabled != b"0":
                    raise ProtocolError(
                        f"Controller framing was not applied: auto={automatic!r}, "
                        f"eot_enable={eot_enabled!r}."
                    )
                self._gpib_address = address
                self._received("Controller: explicit reads; serial EOT disabled.")
                self._write("++clr")
                self._display_mode, self._trigger = "ON", None
                stage = "reading the five binary DMM status bytes"
                status = self._read_status()
                if status.autorange:
                    if self.settle_delay:
                        time.sleep(self.settle_delay)
                    status = self._read_status()
            except (TransportError, ProtocolError, QueryTimeout) as exc:
                raise TransportError(
                    f"Connection failed on {port}, GPIB address {address}, while {stage}: {exc}"
                ) from exc
            self._info, self._connected = ConnectionInfo(version), True
            return status

    def disconnect(self) -> None:
        with self._lock:
            try:
                if self._connected:
                    self._write("K")
                    self._write("++clr")
                    self._write("++loc")
            finally:
                self._release()

    def refresh_status(self) -> DmmStatus:
        with self._session():
            return self._read_status()

    def select_function(self, code: int) -> DmmStatus:
        if code not in FUNCTION_BY_CODE:
            raise ValueError("Select a valid measurement function.")
        with self._session():
            self._write(FUNCTION_BY_CODE[code].command)
            self._write("RA")
            if self.settle_delay:
                time.sleep(self.settle_delay)
            return self._read_status()

    def set_option(self, key: str, value: str) -> DmmStatus:
        with self._session():
            status = self._status
            if status is None:
                raise ProtocolError("Read the DMM status before changing settings.")
            if key == "range":
                command = status.function.range_from_label(value).command
                if command is None:
                    raise ValueError("Extended Ohms does not have a selectable manual range.")
            elif key == "autorange":
                if value not in ("ON", "OFF"):
                    raise ValueError("Select ON or OFF for autorange.")
                if status.range.command is None:
                    raise ValueError("Extended Ohms has a fixed range.")
                command = "RA" if value == "ON" else status.range.command
            else:
                try:
                    command = OPTION_COMMANDS[key][value]
                except KeyError as exc:
                    raise ValueError(f"Invalid {key} setting: {value}.") from exc
            self._write(command)
            if key == "display":
                self._display_mode = value
            if key == "trigger":
                self._trigger = value
            if command == "RA" and self.settle_delay:
                time.sleep(self.settle_delay)
            return self._read_status()

    def reset(self) -> DmmStatus:
        with self._session():
            self._write("++clr")
            self._display_mode, self._trigger = "ON", None
            return self._read_status()

    def clear_errors(self) -> DmmStatus:
        with self._session():
            self._write("K")
            return self._read_status()

    def read_measurement(self, timestamp: float | Callable[[], float] = 0.0, *, keep_connection_on_timeout: bool = False) -> Measurement:
        with self._session(keep_connection_on_timeout=keep_connection_on_timeout):
            status = self._status
            if status is None:
                raise ProtocolError("Read the DMM status before acquisition.")

            def read() -> bytes:
                if not status.internal_trigger or status.external_trigger:
                    self._write("++trg")
                deadline = time.monotonic() + self.query_timeout
                self._write("++read")
                with self.transport.measurement_reader() as reader:
                    return self._read_frame(deadline, reader)

            try:
                # Acquisition owns its counter and Stop handling. Do not nest
                # another five attempts inside each acquisition attempt.
                payload = (
                    read() if keep_connection_on_timeout
                    else self._retry_read(read, "Measurement ++read")
                )
            except QueryTimeout:
                if keep_connection_on_timeout:
                    # A partial reply must not become part of the next sample.
                    self.transport.discard_input()
                raise
            self._received(payload.decode("ascii", errors="replace").strip())
            elapsed = timestamp() if callable(timestamp) else timestamp
            return self.measurement_decoder.decode(
                payload, elapsed, status.function.unit, function_code=status.function_code
            )

    def send_custom(self, command: str, query: bool = False) -> str | None:
        command = command.strip()
        if not command or any(char in command for char in "\r\n") or not command.isascii():
            raise ValueError("Enter one ASCII command without line breaks.")
        head = command.split()[0].lower()
        if head in {
            "++auto",
            "++eot_enable",
            "++eot_char",
            "++eos",
            "++eoi",
            "++eor",
            "++mode",
            "++addr",
            "++rst",
        }:
            raise ValueError(
                "Connection and message framing settings are managed by the application."
            )
        with self._session():
            if command == "B":
                return self._read_status().bit_string
            response = None
            if head == "++read":
                response = self._describe_response(self._query_response())
            elif head == "++ver" or (head.startswith("++") and query):
                response = self._describe_response(self._read_controller_line(command))
            else:
                self._write(command)
                if query:
                    response = self._describe_response(self._query_response())
                if command in ("D1", "D3"):
                    self._display_mode = "ON" if command == "D1" else "OFF"
                elif command in ("T1", "T2", "T3", "T4", "T5"):
                    self._trigger = next(
                        label
                        for label, value in OPTION_COMMANDS["trigger"].items()
                        if value == command
                    )
                elif command == "++clr":
                    self._display_mode, self._trigger = "ON", None
                elif not head.startswith("++"):
                    self._display_mode, self._trigger = None, None
            if response is not None:
                self._received(response)
            self._read_status()
            return response

    @staticmethod
    def _describe_response(payload: bytes) -> str:
        try:
            return payload.decode("ascii").strip()
        except UnicodeDecodeError:
            return f"Binary response: {payload.hex(' ')}"

    def _write(self, command: str) -> None:
        self.activity("TX")
        self.transport.write(command)
        self.trace("OUT", command)
        if self.command_delay:
            time.sleep(self.command_delay)

    def _received(self, text: str) -> None:
        self.activity("RX")
        self.trace("IN", text)

    def _clean_autorange_queue(self):
        time.sleep(0.20)
        self.transport.read(4096)
        for _ in range(3):
            self._write("++read")
            time.sleep(0.05)
            self.transport.read(4096) 
        self.transport.read(4096) 
        if hasattr(self, '_buffer'):
            self._buffer.clear()
        time.sleep(0.20)

    def _retry_read(
        self, read: Callable[[], _ReadResult], description: str, *, discard_input: bool = True
    ) -> _ReadResult:
        """Retry a complete request; abort on the fifth consecutive timeout."""
        for consecutive_timeouts in range(1, self.MAX_CONSECUTIVE_TIMEOUTS + 1):
            try:
                return read()
            except (QueryTimeout, TransportTimeout) as exc:
                exhausted = consecutive_timeouts >= self.MAX_CONSECUTIVE_TIMEOUTS
                self.trace(
                    "WARN",
                    f"{description} timeout after {self.query_timeout:g} s "
                    f"({consecutive_timeouts}/{self.MAX_CONSECUTIVE_TIMEOUTS} consecutive); "
                    + ("read aborted." if exhausted else "retrying read."),
                )
                if exhausted:
                    # The surrounding session restores framing, if needed,
                    # and closes the connection while propagating this error.
                    raise QueryTimeout(
                        f"{description} failed after {consecutive_timeouts} consecutive timeouts "
                        f"(timeout {self.query_timeout:g} s each). {exc}"
                    ) from exc
                # A partial reply must not become part of the next attempt.
                if discard_input:
                    self.transport.discard_input()

    def _query_response(self) -> bytes:
        def read() -> bytes:
            self._write("++read")
            return self._read_frame(time.monotonic() + self.query_timeout)

        # Retry only the read request, not an arbitrary console command that
        # may change the instrument or write calibration data.
        return self._retry_read(read, "Console ++read")

    def _read_status(self) -> DmmStatus:
        def read() -> DmmStatus:
            # Wait before B so the returned flags reflect the settled state.
            if self.status_delay:
                time.sleep(self.status_delay)
            # No previous response belongs to the B request about to be sent.
            self.transport.discard_input()
            self._write("B")
            self._write("++read eoi")
            deadline = time.monotonic() + self.query_timeout
            reader = self.transport.read_binary
            payload = self._read_first_byte(deadline, reader) + self._read_exact(4, deadline, reader)
            try:
                status = self.status_decoder.decode(
                    payload, requested_trigger=self._trigger, display_mode=self._display_mode
                )
            except ProtocolError as exc:
                raise ProtocolError(f"Invalid B response ({payload.hex(' ')}): {exc}") from exc
            self._write("++addr")
            boundary = self._read_frame(deadline, reader).strip()
            expected = str(self._gpib_address).encode("ascii")
            if boundary != expected:
                raise ProtocolError(
                    f"Invalid B response ({payload.hex(' ')}): expected controller "
                    f"address {expected!r} after five binary bytes; received {boundary!r}. "
                    "Additional response data follows the five-byte prefix."
                )
            return status

        # Each attempt clears its input immediately before sending B.
        status = self._retry_read(read, "Status B", discard_input=False)
        self._status = status
        self._received(f"Status: {status.bit_string}")
        return status

    def _read_exact(
        self, length: int, deadline: float, reader: Callable[[int], bytes] | None = None
    ) -> bytes:
        reader = reader or self.transport.read
        result = bytearray()
        while len(result) < length:
            if time.monotonic() >= deadline:
                raise QueryTimeout(f"DMM response timed out after {self.query_timeout:g} seconds.")
            data = reader(length - len(result))
            if data:
                result.extend(data)
            else:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(0.001, remaining))
        if len(result) != length:
            raise ProtocolError("The transport returned more bytes than requested.")
        return bytes(result)

    def _read_frame(
        self, deadline: float, reader: Callable[[int], bytes] | None = None
    ) -> bytes:
        result = bytearray()
        while len(result) <= 4096:
            byte = self._read_exact(1, deadline, reader)
            if byte == EOI_MARKER or (byte == b"\x00" and byte == self.transport.eot_byte):
                if not result:
                    continue
                self._drain_terminators()
                return bytes(result)
            result.extend(byte)
            if byte == b"\n":
                if result.strip(b"\r\n"):
                    self._drain_terminators()
                    return bytes(result)
                result.clear()
        raise ProtocolError("DMM response exceeded the message size limit.")

    def _read_first_byte(
        self, deadline: float, reader: Callable[[int], bytes] | None = None
    ) -> bytes:
        # Neither a status header nor a RAM reply can be CR, LF or EOT.
        # Skip delayed terminators from a previous USB packet before reading
        # the fixed-length binary payload; never strip its internal bytes.
        for _ in range(32):
            byte = self._read_exact(1, deadline, reader)
            if byte not in (b"\r", b"\n", EOI_MARKER, b"\x00"):
                return byte
        raise ProtocolError("Too many leading response terminators.")

    def _drain_terminators(self) -> None:
        # Clear extra response bytes without changing the driver's timeout.
        # Some controllers append bytes beyond CR/LF/EOT; discard them after
        # consuming the complete response instead of rejecting the connection.
        for _ in range(32):
            byte = self.transport.read_available(1)
            if not byte:
                return
        raise ProtocolError("Too many response terminators.")

    def _write_binary(self, payload: bytes) -> None:
        self.activity("TX")
        self.transport.write_binary(payload)
        self.trace("OUT", f"Calibration: {payload.hex(' ')}")
        if self.command_delay:
            time.sleep(self.command_delay)

    def _read_ram(self, progress: Callable[[int], None] | None = None) -> bytes:
        values = bytearray()
        for address in range(256):
            def read() -> bytes:
                self._write_binary(bytes((ord("W"), address)))
                # RAM replies contain exactly one ASCII byte, without CR/LF.
                self._write("++read eoi")
                payload = self._read_first_byte(
                    time.monotonic() + self.query_timeout, self.transport.read_binary
                )
                self._drain_terminators()
                return payload

            reply = self._retry_read(read, f"Calibration RAM 0x{address:02X}")[0]
            if not 64 <= reply <= 79:
                raise ProtocolError(f"Invalid calibration reply at 0x{address:02X}: 0x{reply:02X}")
            values.append(reply - 64)
            self.activity("RX")
            if progress:
                progress(address + 1)
        return bytes(values)

    @contextmanager
    def _calibration_session(self):
        with self._session():
            self.transport.snapshot_framing(self._read_controller_line)
            try:
                self._write("++eoi 1")
                self._write("++eos 3")
                yield
            finally:
                try:
                    self._write(f"++eos {self.transport.send_eos}")
                    if not self.transport.send_eoi:
                        self._write("++eoi 0")
                except TransportError:
                    self._release()
                    raise

    def read_calibration_ram(self, progress: Callable[[int], None] | None = None) -> bytes:
        with self._calibration_session():
            return self._read_ram(progress)

    def restore_calibration_ram(
        self, values: bytes, progress: Callable[[str, int], None] | None = None
    ) -> None:
        from .calibration import CalibrationDump

        CalibrationDump(tuple(values)).require_valid_calibration()
        progress = progress or (lambda phase, count: None)
        with self._calibration_session():
            if not self._read_status().calibration_enabled:
                raise ValueError(
                    "Set the front-panel CAL ENABLE switch to ON before restoring RAM."
                )
            for address, value in enumerate(values):
                self._write_binary(bytes((ord("X"), address, value)))
                progress("Writing", address + 1)
                time.sleep(0.05)
            actual = self._read_ram(lambda count: progress("Verifying", count))
            # Address zero is the hardware write-protection test nibble and can
            # change independently. All 255 remaining locations are verified.
            differences = [i for i in range(1, 256) if actual[i] != values[i]]
            if differences:
                raise ProtocolError(
                    f"Calibration verification failed at address 0x{differences[0]:02X}. "
                    "The RAM may be partially restored. Keep the recovery backup."
                )
        self.refresh_status()

    def _read_controller_line(self, command: str = "++ver") -> bytes:
        def read() -> bytes:
            self._write(command)
            result = bytearray()
            deadline = time.monotonic() + self.query_timeout
            while time.monotonic() < deadline:
                # Wait for a complete LF-terminated response within the deadline.
                fragment = self.transport.read_text()
                if not fragment:
                    time.sleep(0.005)
                    continue
                result.extend(fragment.encode("utf-8"))
                if len(result) > 4096:
                    raise ProtocolError("Controller response exceeded the message size limit.")
                if b"\n" in result:
                    line = bytes(result).split(b"\n", 1)[0].strip(b"\r\x04")
                    if line:
                        return line
                    result.clear()
            raise QueryTimeout(
                f"Controller response timed out after {self.query_timeout:g} seconds. "
                f"Received: {bytes(result[:128])!r}"
            )

        return self._retry_read(read, f"Controller {command}")
