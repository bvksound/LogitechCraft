"""Minimal HID++ 2.0 implementation: just enough to talk to the Craft crown.

The Craft keyboard exposes its crown through HID++ feature 0x4600 (CROWN).
By default the crown sends normal HID scroll/volume events.  When the feature
is put in *diverted* mode the keyboard instead sends HID++ notifications that
contain the raw rotation, ratchet steps, taps and presses.  That is exactly
what Logi Options does, and what this module does as well.

Report layout (long report, 20 bytes):

    [0x11, device_index, feature_index, (function << 4) | sw_id, params...]

Notifications sent by the device carry sw_id == 0; replies to our own
requests carry the sw_id we used, which is how the two are told apart.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Protocol

LOGITECH_VID = 0x046D

# Vendor usage pages carrying HID++: 0xFF00 for receivers/USB, 0xFF43 for BLE.
HIDPP_USAGE_PAGES = (0xFF00, 0xFF43)
LONG_USAGES = (0x0002, 0x0202)

REPORT_SHORT = 0x10
REPORT_LONG = 0x11
SHORT_LEN = 7
LONG_LEN = 20

SW_ID = 0x0A  # any non-zero value; 0 is reserved for device notifications

FEATURE_ROOT = 0x0000
FEATURE_DEVICE_NAME = 0x0005
FEATURE_CROWN = 0x4600

# Crown function ids (feature 0x4600).
CROWN_FN_GET_MODE = 1
CROWN_FN_SET_MODE = 2
CROWN_DIVERT_KEEP = 0x00
CROWN_DIVERT_HID = 0x01
CROWN_DIVERT_HIDPP = 0x02
CROWN_SCROLL_KEEP = 0x00
CROWN_SCROLL_SMOOTH = 0x01
CROWN_SCROLL_RATCHET = 0x02

# Crown press states (params[6] of a crown notification).
PRESS_NONE = 0x00
PRESS_START = 0x01
PRESS_END = 0x05

# Device index used to address a device directly (Bluetooth / USB cable).
DIRECT_DEVICE = 0xFF
RECEIVER_SLOTS = range(1, 7)

# Known Logitech receivers (Unifying, Bolt, Nano, Lightspeed).  The Craft
# uses Unifying, but probing the others is harmless.
RECEIVER_PIDS = frozenset({
    0xC517, 0xC52B, 0xC52F, 0xC531, 0xC532, 0xC534, 0xC539, 0xC53A,
    0xC53D, 0xC53F, 0xC541, 0xC545, 0xC547, 0xC548,
})

# HID++ 1.0 receiver notification: device (dis)connected.
SUB_ID_CONNECTION = 0x41
SUB_ID_ERROR_10 = 0x8F
FEATURE_INDEX_ERROR_20 = 0xFF


class HidppError(Exception):
    """The device answered with an HID++ error code."""

    def __init__(self, code: int, message: str = ""):
        super().__init__(message or f"HID++ error 0x{code:02X}")
        self.code = code


class HidppTimeout(Exception):
    """No reply arrived in time (device asleep, absent, ...)."""


class Transport(Protocol):
    """A single opened HID interface."""

    def write(self, data: bytes) -> None: ...

    def read(self, timeout_ms: int) -> bytes: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class Message:
    report_id: int
    devnum: int
    sub_id: int  # feature index (HID++ 2.0) or sub id (HID++ 1.0)
    address: int  # (function << 4) | sw_id
    data: bytes

    @property
    def function(self) -> int:
        return self.address >> 4

    @property
    def sw_id(self) -> int:
        return self.address & 0x0F

    @property
    def is_notification(self) -> bool:
        return self.sw_id == 0


def parse(raw: bytes | Iterable[int]) -> Optional[Message]:
    raw = bytes(raw)
    if len(raw) < 4 or raw[0] not in (REPORT_SHORT, REPORT_LONG):
        return None
    return Message(raw[0], raw[1], raw[2], raw[3], raw[4:])


def build_long(devnum: int, feature_index: int, function: int, params: bytes = b"",
               sw_id: int = SW_ID) -> bytes:
    if len(params) > LONG_LEN - 4:
        raise ValueError("too many parameters for a long report")
    msg = bytes([REPORT_LONG, devnum, feature_index, ((function & 0x0F) << 4) | (sw_id & 0x0F)])
    return (msg + bytes(params)).ljust(LONG_LEN, b"\x00")


def signed8(value: int) -> int:
    return value - 256 if value >= 128 else value


@dataclass(frozen=True)
class CrownEvent:
    """One decoded crown notification.

    rotation: fine-grained rotation delta (positive = clockwise)
    ratchet:  number of ratchet notches passed (positive = clockwise)
    tap:      the crown was tapped (short press)
    press:    press state, PRESS_START .. PRESS_END, 0 when not pressed
    """

    rotation: int = 0
    ratchet: int = 0
    tap: bool = False
    press: int = PRESS_NONE

    @classmethod
    def from_params(cls, params: bytes) -> "CrownEvent":
        p = bytes(params).ljust(7, b"\x00")
        return cls(
            rotation=signed8(p[1]),
            ratchet=signed8(p[2]),
            tap=p[5] == 0x01,
            press=p[6] if p[6] <= PRESS_END else PRESS_NONE,
        )

    @property
    def press_started(self) -> bool:
        return self.press == PRESS_START

    @property
    def press_ended(self) -> bool:
        return self.press == PRESS_END

    @property
    def pressing(self) -> bool:
        return PRESS_START <= self.press < PRESS_END


class HidppDevice:
    """One HID++ 2.0 device reachable through a transport (directly or via a receiver).

    Notifications that arrive while waiting for a reply are handed to
    ``on_message`` so that nothing gets lost.
    """

    def __init__(self, transport: Transport, devnum: int,
                 on_message: Optional[Callable[[Message], None]] = None):
        self.transport = transport
        self.devnum = devnum
        self.on_message = on_message
        self._features: dict[int, int] = {FEATURE_ROOT: 0}

    def request(self, feature_index: int, function: int, params: bytes = b"",
                timeout: float = 1.0) -> bytes:
        self.transport.write(build_long(self.devnum, feature_index, function, params))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HidppTimeout(
                    f"no reply from device {self.devnum:#04x} "
                    f"(feature {feature_index:#04x}, fn {function})")
            raw = self.transport.read(max(1, int(remaining * 1000)))
            if not raw:
                continue
            msg = parse(raw)
            if msg is None:
                continue
            if self._is_error_for(msg, feature_index, function):
                raise HidppError(msg.data[1])
            if (msg.devnum == self.devnum and msg.sub_id == feature_index
                    and msg.function == function and msg.sw_id == SW_ID):
                return msg.data
            if self.on_message is not None:
                self.on_message(msg)

    def _is_error_for(self, msg: Message, feature_index: int, function: int) -> bool:
        if msg.devnum != self.devnum or len(msg.data) < 2:
            return False
        expected_address = ((function & 0x0F) << 4) | SW_ID
        if msg.report_id == REPORT_LONG and msg.sub_id == FEATURE_INDEX_ERROR_20:
            return msg.address == feature_index and msg.data[0] == expected_address
        if msg.sub_id == SUB_ID_ERROR_10:
            return msg.address == feature_index and msg.data[0] == expected_address
        return False

    def feature_index(self, feature_id: int, timeout: float = 1.0) -> int:
        """Return the index of a feature, or 0 when the device lacks it."""
        if feature_id not in self._features:
            reply = self.request(0, 0, bytes([feature_id >> 8, feature_id & 0xFF]), timeout)
            self._features[feature_id] = reply[0]
        return self._features[feature_id]

    def name(self) -> Optional[str]:
        try:
            index = self.feature_index(FEATURE_DEVICE_NAME)
            if not index:
                return None
            length = self.request(index, 0)[0]
            chars = b""
            while len(chars) < length:
                chunk = self.request(index, 1, bytes([len(chars)]))
                if not chunk:
                    break
                chars += chunk
            return chars[:length].rstrip(b"\x00").decode("utf-8", "replace") or None
        except (HidppError, HidppTimeout):
            return None


class Crown:
    """The CROWN feature (0x4600) of a device."""

    def __init__(self, device: HidppDevice, index: int):
        self.device = device
        self.index = index

    def get_mode(self) -> tuple[int, int]:
        reply = self.device.request(self.index, CROWN_FN_GET_MODE)
        return reply[0], reply[1]

    def set_mode(self, divert: int = CROWN_DIVERT_KEEP, scroll: int = CROWN_SCROLL_KEEP) -> None:
        self.device.request(self.index, CROWN_FN_SET_MODE, bytes([divert, scroll]))

    def divert(self, scroll: int = CROWN_SCROLL_KEEP) -> None:
        self.set_mode(CROWN_DIVERT_HIDPP, scroll)

    def release(self) -> None:
        self.set_mode(CROWN_DIVERT_HID, CROWN_SCROLL_KEEP)

    def event_from(self, msg: Message) -> Optional[CrownEvent]:
        if (msg.devnum == self.device.devnum and msg.sub_id == self.index
                and msg.is_notification and msg.function == 0):
            return CrownEvent.from_params(msg.data)
        return None


def is_connection_notification(msg: Message, devnum: int) -> Optional[bool]:
    """For a receiver 0x41 notification: True = link up, False = link lost."""
    if msg.devnum != devnum or msg.sub_id != SUB_ID_CONNECTION or len(msg.data) < 2:
        return None
    return not bool(msg.data[1] & 0x40)
