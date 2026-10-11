"""Test doubles: a simulated Craft behind a Unifying receiver, fake UI and timers."""

from __future__ import annotations

import queue
import time

from craftcrown import hidpp

CRAFT_SLOT = 2
CROWN_INDEX = 0x0E
NAME_INDEX = 0x03
NAME = b"Craft Advanced Keyboard"


class FakeCraftTransport:
    """Answers HID++ requests like a Craft paired on slot 2 of a receiver."""

    def __init__(self, path=b"fake"):
        self.path = path
        self.inbox: "queue.Queue[bytes]" = queue.Queue()
        self.mode = [hidpp.CROWN_DIVERT_HID, hidpp.CROWN_SCROLL_RATCHET]
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        data = bytes(data)
        self.writes.append(data)
        msg = hidpp.parse(data)
        if msg.devnum != CRAFT_SLOT:
            # Receiver: unknown device -> HID++ 1.0 error
            self._reply_short(msg.devnum, 0x8F, msg.sub_id, bytes([msg.address, 0x09]))
            return
        params = msg.data
        if msg.sub_id == 0x00 and msg.function == 0:
            feature = (params[0] << 8) | params[1]
            index = {hidpp.FEATURE_CROWN: CROWN_INDEX, hidpp.FEATURE_DEVICE_NAME: NAME_INDEX}.get(feature, 0)
            self._reply(msg, bytes([index]))
        elif msg.sub_id == NAME_INDEX and msg.function == 0:
            self._reply(msg, bytes([len(NAME)]))
        elif msg.sub_id == NAME_INDEX and msg.function == 1:
            self._reply(msg, NAME[params[0]:params[0] + 16])
        elif msg.sub_id == CROWN_INDEX and msg.function == hidpp.CROWN_FN_GET_MODE:
            self._reply(msg, bytes(self.mode))
        elif msg.sub_id == CROWN_INDEX and msg.function == hidpp.CROWN_FN_SET_MODE:
            if params[0]:
                self.mode[0] = params[0]
            if params[1]:
                self.mode[1] = params[1]
            self._reply(msg, bytes(self.mode))
        else:
            self.inbox.put(bytes([0x11, msg.devnum, 0xFF, msg.sub_id, msg.address, 0x02]).ljust(20, b"\0"))

    def _reply(self, msg, payload: bytes) -> None:
        self.inbox.put(bytes([0x11, msg.devnum, msg.sub_id, msg.address]) + payload.ljust(16, b"\0")[:16])

    def _reply_short(self, devnum, sub_id, address, payload) -> None:
        self.inbox.put(bytes([0x10, devnum, sub_id, address]) + payload.ljust(3, b"\0")[:3])

    def notify_crown(self, rotation=0, ratchet=0, tap=False, press=0) -> None:
        params = bytes([0, rotation & 0xFF, ratchet & 0xFF, 0, 0, 1 if tap else 0, press])
        self.inbox.put(bytes([0x11, CRAFT_SLOT, CROWN_INDEX, 0x00]) + params.ljust(16, b"\0"))

    def read(self, timeout_ms: int) -> bytes:
        try:
            return self.inbox.get(timeout=timeout_ms / 1000)
        except queue.Empty:
            return b""

    def close(self) -> None:
        self.closed = True


RECEIVER_INFO = [{
    "path": b"fake", "vendor_id": hidpp.LOGITECH_VID, "product_id": 0xC52B,
    "usage_page": 0xFF00, "usage": 0x0002, "interface_number": 2, "serial_number": "",
    "product_string": "USB Receiver",
}]


def fake_enumerate(vid, pid):
    return list(RECEIVER_INFO)


class FakeHandle:
    def __init__(self, timers, ms, fn):
        self.timers, self.ms, self.fn, self.cancelled = timers, ms, fn, False

    def cancel(self):
        self.cancelled = True


class FakeTimers:
    """Manual scheduler: ``advance(ms)`` fires due callbacks."""

    def __init__(self, clock):
        self.clock = clock
        self.pending: list[tuple[float, FakeHandle]] = []

    def __call__(self, ms, fn):
        h = FakeHandle(self, ms, fn)
        self.pending.append((self.clock.now + ms / 1000, h))
        return h

    def advance(self, ms):
        target = self.clock.now + ms / 1000
        while True:
            due = sorted((t, h) for t, h in self.pending if t <= target and not h.cancelled)
            if not due:
                break
            t, h = due[0]
            self.pending.remove((t, h))
            self.clock.now = t
            h.fn()
        self.clock.now = target


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FakeUi:
    def __init__(self):
        self.ring = None
        self.huds = []
        self.events = []

    def show_ring(self, view):
        self.ring = view
        self.events.append(("ring", view.selected))

    def hide_ring(self):
        self.ring = None
        self.events.append(("hide",))

    def show_hud(self, title, detail, direction):
        self.huds.append((title, direction))


class FakeBackend:
    def __init__(self):
        self.calls = []

    def tap(self, keys, times=1):
        self.calls.append(("tap", tuple(keys), times))

    def scroll(self, dx, dy):
        self.calls.append(("scroll", dx, dy))

    def type_text(self, text):
        self.calls.append(("text", text))

    def run(self, command):
        self.calls.append(("run", command))


def wait_for(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False
