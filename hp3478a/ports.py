# -*- coding: utf-8 -*-

"""
Serial Port Hardware Discovery and Adaptive Selection Tracking.

This module provides passive OS-level device enumeration utilizing pySerial's 
list_ports API, natural string sorting for multi-port routing, and logical 
state management for tracking hardware connections across connection updates.

Part of the HP 3478A DMM GUI project.
Distributed under the terms of the MIT license.

Author: Lorenzo Mariani <newmarioman@hotmail.it>
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass


class PortDiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class SerialPort:
    device: str
    description: str = "Serial port"


class SerialPortDiscovery:
    """Enumerate actual OS devices using pySerial's list_ports API."""

    def __init__(self, enumerate_ports: Callable[[], Iterable] | None = None) -> None:
        self._enumerate = enumerate_ports

    def scan(self) -> tuple[SerialPort, ...]:
        enumerate_ports = self._enumerate
        if enumerate_ports is None:
            try:
                from serial.tools.list_ports import comports
            except ImportError as exc:
                raise PortDiscoveryError(
                    "pySerial is not installed. Run: python -m pip install pyserial"
                ) from exc
            enumerate_ports = comports
        try:
            devices = {}
            for port in enumerate_ports():
                device = str(port.device or "").strip()
                if device:
                    description = str(port.description or "").strip() or "Serial port"
                    devices.setdefault(device, SerialPort(device, description))
            return tuple(sorted(devices.values(), key=lambda p: self._natural_key(p.device)))
        except (OSError, RuntimeError) as exc:
            raise PortDiscoveryError(f"Could not enumerate serial ports: {exc}") from exc

    @staticmethod
    def _natural_key(device: str) -> tuple:
        return tuple(
            (1, int(part)) if part.isdigit() else (0, part.casefold())
            for part in re.split(r"(\d+)", device)
        )


class PortSelection:
    """Keep a valid selection while devices are added or removed."""

    def __init__(self) -> None:
        self.ports: tuple[SerialPort, ...] = ()
        self.selected = ""

    def update(self, ports: tuple[SerialPort, ...], connected: bool = False) -> str:
        self.ports = ports
        devices = tuple(port.device for port in ports)
        if self.selected not in devices and not connected:
            self.selected = devices[0] if devices else ""
        return self.selected

    @property
    def description(self) -> str:
        for port in self.ports:
            if port.device == self.selected:
                return port.description
        if self.selected:
            return f"Port no longer detected: {self.selected}"
        return "No serial ports found. \n Connect an adapter and refresh."


SIMULATED_PORT = SerialPort("SIMULATED", "Software controller — No hardware required")
