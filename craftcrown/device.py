"""Background service: find the Craft, divert its crown, and stream crown events.

All HID I/O happens on one thread.  Other threads talk to it through
``set_scroll_mode`` (queued) and receive data through the callbacks.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import hidpp
from .hidpp import (CROWN_SCROLL_KEEP, CROWN_SCROLL_RATCHET, CROWN_SCROLL_SMOOTH,
                    Crown, CrownEvent, HidppDevice, HidppError, HidppTimeout, Message)

log = logging.getLogger(__name__)

SCROLL_MODES = {"auto": CROWN_SCROLL_KEEP, "ratchet": CROWN_SCROLL_RATCHET,
                "smooth": CROWN_SCROLL_SMOOTH}

RETRY_SECONDS = 3.0
WATCHDOG_SECONDS = 10.0


class HidTransport:
    """Adapter over the two popular Python bindings that are both imported as ``hid``."""

    def __init__(self, path: bytes):
        import hid  # type: ignore

        self.path = path
        if hasattr(hid, "device"):  # 'hidapi' package (cython-hidapi)
            self._dev = hid.device()
            self._dev.open_path(path)
            self._legacy = True
        else:  # 'hid' package (ctypes)
            self._dev = hid.Device(path=path)
            self._legacy = False

    def write(self, data: bytes) -> None:
        if self._legacy:
            written = self._dev.write(list(data))
        else:
            written = self._dev.write(bytes(data))
        if written is not None and written < 0:
            raise OSError("HID write failed")

    def read(self, timeout_ms: int) -> bytes:
        return bytes(self._dev.read(64, timeout_ms) or b"")

    def close(self) -> None:
        try:
            self._dev.close()
        except Exception:  # noqa: BLE001 - closing must never fail
            pass


@dataclass
class Candidate:
    """One physical HID++ interface (a receiver or a directly connected device).

    On Windows the short and long HID++ reports live in separate HID
    collections with their own paths; on macOS/Linux there is just one.
    """

    product_id: int
    product: str
    paths: list[bytes] = field(default_factory=list)
    long_path: Optional[bytes] = None

    @property
    def is_receiver(self) -> bool:
        return self.product_id in hidpp.RECEIVER_PIDS


def enumerate_candidates(enumerate_fn: Optional[Callable] = None) -> list[Candidate]:
    if enumerate_fn is None:
        import hid  # type: ignore
        enumerate_fn = hid.enumerate
    groups: dict[tuple, Candidate] = {}
    for info in enumerate_fn(hidpp.LOGITECH_VID, 0):
        if info.get("usage_page") not in hidpp.HIDPP_USAGE_PAGES:
            continue
        key = (info.get("product_id"), info.get("interface_number"), info.get("serial_number"))
        cand = groups.setdefault(key, Candidate(
            product_id=info.get("product_id", 0),
            product=info.get("product_string") or "",
        ))
        path = info["path"]
        if path not in cand.paths:
            cand.paths.append(path)
        if info.get("usage") in hidpp.LONG_USAGES:
            cand.long_path = path
    for cand in groups.values():
        if cand.long_path is None:
            cand.long_path = cand.paths[0]
    # Receivers first: that's how most Crafts are connected.
    return sorted(groups.values(), key=lambda c: not c.is_receiver)


class CompositeTransport:
    """Writes to the long-report collection, reads from all collections.

    On Windows short replies (e.g. receiver errors, connection notifications)
    arrive on a different HID collection than long ones.
    """

    def __init__(self, writer: hidpp.Transport, readers: list[hidpp.Transport]):
        self.writer = writer
        self.readers = readers

    def write(self, data: bytes) -> None:
        self.writer.write(data)

    def read(self, timeout_ms: int) -> bytes:
        if len(self.readers) == 1:
            return self.readers[0].read(timeout_ms)
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            remaining = int((deadline - time.monotonic()) * 1000)
            slice_ms = max(1, min(10, remaining))
            for reader in self.readers:
                data = reader.read(slice_ms)
                if data:
                    return data
            if remaining <= 0:
                return b""

    def close(self) -> None:
        for reader in self.readers:
            reader.close()


class _Session:
    """An opened candidate with the Craft found on it."""

    def __init__(self, transports: dict[bytes, hidpp.Transport], writer: hidpp.Transport):
        self.transports = transports
        self.io = CompositeTransport(writer, list(transports.values()))
        self.device: Optional[HidppDevice] = None
        self.crown: Optional[Crown] = None
        self.name = ""
        self.backlog: list[Message] = []

    def close(self) -> None:
        for t in self.transports.values():
            t.close()


class CrownService(threading.Thread):
    """Keeps the crown diverted and calls ``on_event`` for every crown notification."""

    def __init__(self, on_event: Callable[[CrownEvent], None],
                 on_status: Callable[[str, bool], None],
                 scroll_mode: str = "auto",
                 on_scroll_feel: Optional[Callable[[Optional[str]], None]] = None,
                 transport_factory: Callable[[bytes], hidpp.Transport] = HidTransport,
                 enumerate_fn: Optional[Callable] = None):
        super().__init__(name="craft-crown-hid", daemon=True)
        self.on_event = on_event
        self.on_status = on_status
        self.on_scroll_feel = on_scroll_feel
        self._feel: Optional[str] = None
        self.scroll_mode = scroll_mode
        self._transport_factory = transport_factory
        self._enumerate_fn = enumerate_fn
        self._stop = threading.Event()
        self._commands: "queue.Queue[tuple]" = queue.Queue()
        self._status: tuple[str, bool] = ("", False)

    # -- public API (any thread) -------------------------------------------------

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self.is_alive():
            self.join(timeout)

    def set_scroll_mode(self, mode: str) -> None:
        self._commands.put(("scroll_mode", mode))

    # -- thread ------------------------------------------------------------------

    def run(self) -> None:
        while not self._stop.is_set():
            session = None
            try:
                session = self._connect()
                if session is None:
                    self._set_status("Craft niet gevonden — is Logi Options+ afgesloten?", False)
                    self._stop.wait(RETRY_SECONDS)
                    continue
                self._serve(session)
            except Exception as exc:  # noqa: BLE001 - keep the service alive
                log.warning("crown session ended: %s", exc)
                self._set_status(f"Verbinding verbroken: {exc}", False)
                self._stop.wait(RETRY_SECONDS)
            finally:
                if session is not None:
                    self._release(session)
                    session.close()

    def _set_status(self, text: str, connected: bool) -> None:
        if (text, connected) != self._status:
            self._status = (text, connected)
            log.info("status: %s", text)
            self.on_status(text, connected)

    def _connect(self) -> Optional[_Session]:
        try:
            candidates = enumerate_candidates(self._enumerate_fn)
        except ImportError:
            self._set_status("Python-module 'hidapi' ontbreekt (pip install hidapi)", False)
            return None
        for cand in candidates:
            transports: dict[bytes, hidpp.Transport] = {}
            try:
                for path in cand.paths:
                    transports[path] = self._transport_factory(path)
            except OSError as exc:
                log.debug("cannot open %s: %s", cand.product, exc)
                for t in transports.values():
                    t.close()
                continue
            session = _Session(transports, transports[cand.long_path])
            slots = hidpp.RECEIVER_SLOTS if cand.is_receiver else (hidpp.DIRECT_DEVICE,)
            for devnum in slots:
                if self._probe(session, devnum):
                    session.name = session.device.name() or cand.product or "Craft"
                    via = "Unifying-ontvanger" if cand.is_receiver else "directe verbinding"
                    session.name = f"{session.name} ({via})"
                    return session
            session.close()
        return None

    def _probe(self, session: _Session, devnum: int) -> bool:
        device = HidppDevice(session.io, devnum, on_message=session.backlog.append)
        try:
            index = device.feature_index(hidpp.FEATURE_CROWN, timeout=0.5)
        except (HidppError, HidppTimeout, OSError):
            return False
        if not index:
            return False
        session.device = device
        session.crown = Crown(device, index)
        return True

    def _divert(self, session: _Session) -> None:
        session.crown.divert(SCROLL_MODES.get(self.scroll_mode, CROWN_SCROLL_KEEP))
        self._set_status(f"Verbonden met {session.name}", True)
        self._report_feel(session.crown.get_mode()[1])

    def _report_feel(self, scroll: int) -> None:
        feel = {CROWN_SCROLL_SMOOTH: "smooth", CROWN_SCROLL_RATCHET: "ratchet"}.get(scroll)
        if self.on_scroll_feel is not None and feel != self._feel:
            self._feel = feel
            self.on_scroll_feel(feel)

    def _release(self, session: _Session) -> None:
        """Hand the crown back to the OS so it keeps working after we quit."""
        if session.crown is None:
            return
        try:
            session.crown.release()
        except Exception:  # noqa: BLE001
            pass

    def _serve(self, session: _Session) -> None:
        try:
            self._divert(session)
        except (HidppError, HidppTimeout):
            # Asleep right now; the watchdog or a reconnect notification retries.
            self._set_status(f"{session.name}: in slaap of niet verbonden", False)
        next_watchdog = time.monotonic() + WATCHDOG_SECONDS
        resetup_at: Optional[float] = None

        while not self._stop.is_set():
            self._run_commands(session)

            for msg in session.backlog:
                resetup_at = self._dispatch(session, msg, resetup_at)
            session.backlog.clear()

            raw = session.io.read(100)
            msg = hidpp.parse(raw) if raw else None
            if msg is not None:
                resetup_at = self._dispatch(session, msg, resetup_at)

            now = time.monotonic()
            if resetup_at is not None and now >= resetup_at:
                resetup_at = None
                next_watchdog = now  # re-divert right away
            if now >= next_watchdog:
                next_watchdog = now + WATCHDOG_SECONDS
                self._watchdog(session)

    def _dispatch(self, session: _Session, msg: Message, resetup_at: Optional[float]):
        event = session.crown.event_from(msg)
        if event is not None:
            self.on_event(event)
            return resetup_at
        link = hidpp.is_connection_notification(msg, session.device.devnum)
        if link is True:
            # The keyboard woke up / reconnected: diversion is lost, redo it.
            return time.monotonic() + 0.5
        if link is False:
            self._set_status(f"{session.name}: in slaap of niet verbonden", False)
        return resetup_at

    def _watchdog(self, session: _Session) -> None:
        try:
            divert, scroll = session.crown.get_mode()
            if divert != hidpp.CROWN_DIVERT_HIDPP:
                self._divert(session)
            else:
                self._set_status(f"Verbonden met {session.name}", True)
                self._report_feel(scroll)
        except (HidppError, HidppTimeout):
            self._set_status(f"{session.name}: in slaap of niet verbonden", False)

    def _run_commands(self, session: _Session) -> None:
        while True:
            try:
                cmd, arg = self._commands.get_nowait()
            except queue.Empty:
                return
            if cmd == "scroll_mode":
                self.scroll_mode = arg
                try:
                    self._divert(session)
                except (HidppError, HidppTimeout) as exc:
                    log.warning("cannot change scroll mode: %s", exc)
