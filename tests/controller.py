"""Serial AR488 fixture shared by driver, protocol and calibration tests."""

try:
    from PyAR488 import AR488 as AR488
except ImportError:
    from PyAR488.PyAR488 import AR488 as AR488


class Controller:
    """
    This fixture reproduces an unresponsive ++ver after early configuration;
    it does not claim to identify the connected board's physical failure mode.
    """

    VERSION = b"AR488 GPIB controller, ver. 0.53.46, 22/05/2026"

    def __init__(self, *, eos=0, eoi=1, eot=0, eot_enable=1, automatic=0):
        self.config = {
            "++addr": 1,
            "++mode": 1,
            "++eoi": eoi,
            "++eos": eos,
            "++eot_enable": eot_enable,
            "++eot_char": eot,
            "++eor": 0,
            "++idn": 0,
            "++auto": automatic,
        }
        self.port_options, self.writes, self.commands = None, [], []
        self.input, self.partial = bytearray(), bytearray()
        self.escaped, self.stalled, self.identified, self.closed = False, False, False, False
        self.pending_status, self.status_count, self.pending_ram = False, 0, None
        self.ram = bytearray(256)
        self.timeout, self.write_timeout = 1, None

    def open(self, **options):
        self.port_options = options
        self.timeout = options["timeout"]
        return self

    def write(self, payload):
        self.writes.append(payload)
        for value in payload:
            if self.escaped:
                self.partial.append(value)
                self.escaped = False
            elif value == 27:
                self.escaped = True
            elif value in (10, 13):
                if self.partial:
                    command = bytes(self.partial)
                    self.partial.clear()
                    self._handle(command)
            else:
                self.partial.append(value)
        return len(payload)

    def _handle(self, command):
        self.commands.append(command)
        if self.stalled:
            return
        if command == b"++mode 1" and not self.identified:
            self.stalled = True
            return
        if command == b"++ver":
            self.identified = True
            self.input.extend(self.VERSION + b"\r\n")
        elif command in (b"++read", b"++read eoi"):
            if self.pending_status:
                first = 37 if self.status_count == 0 else 45
                self.input.extend(bytes((first, 31, 0, 0, 45)) + b"\r\n")
                self.pending_status = False
                self.status_count += 1
            elif self.pending_ram is not None:
                self.input.extend(bytes((64 + self.ram[self.pending_ram],)))
                self.pending_ram = None
            else:
                self.input.extend(b"-1.20000E+1\r\n")
            if self.config["++eot_enable"]:
                self.input.append(self.config["++eot_char"])
        elif command == b"B":
            self.pending_status = True
        elif len(command) == 2 and command[0] == ord("W"):
            self.pending_ram = command[1]
        elif command.startswith(b"++"):
            words = command.decode("ascii").split()
            if words[0] in self.config:
                if len(words) == 2:
                    self.config[words[0]] = int(words[1])
                else:
                    self.input.extend(f"{self.config[words[0]]}\r\n".encode("ascii"))

    def read(self, size=1):
        result = bytes(self.input[: min(size, 2)])
        del self.input[: len(result)]
        return result

    @property
    def in_waiting(self):
        return len(self.input)

    def readline(self):
        end = self.input.find(b"\n")
        if end == -1:
            result = bytes(self.input)
            self.input.clear()
        else:
            result = bytes(self.input[: end + 1])
            del self.input[: end + 1]
        return result

    def reset_input_buffer(self):
        self.input.clear()

    def close(self):
        self.closed = True
