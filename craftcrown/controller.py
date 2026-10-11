"""Turns raw crown events into actions, ring navigation and on-screen feedback.

Behaviour (modelled after Logi Options' crown):

* turn             -> rotate action of the active ring tool, or of the profile
* press + turn     -> rotate_pressed action (falls back to rotate)
* tap              -> profile tap action; by default: open the ring
* long press       -> long_press action of the tool / profile
* ring open: turn  -> move the selection, tap -> activate the tool,
                     long press or timeout -> close without changing anything

The active tool is remembered per profile, so every app keeps its own mode.
This module has no Qt dependency; time and UI are injected.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from . import actions
from .actions import ROTATE, TRIGGER
from .activeapp import AppInfo
from .config import Profile, resolve_profile
from .hidpp import CrownEvent

DEFAULT_SLOT_LABEL = "Standaard"
RATCHET_GRACE_S = 0.4
TAP_DEDUP_S = 0.4
TAP_FALLBACK_MS = 150


@dataclass
class RingView:
    title: str
    labels: list[str]
    selected: int
    active: int  # slot that is currently active


class Ui(Protocol):
    def show_ring(self, view: RingView) -> None: ...

    def hide_ring(self) -> None: ...

    def show_hud(self, title: str, detail: str, direction: int) -> None: ...


class Handle(Protocol):
    def cancel(self) -> None: ...


Scheduler = Callable[[int, Callable[[], None]], Handle]


class Controller:
    def __init__(self, cfg: dict, runner: actions.ActionRunner, ui: Ui, schedule: Scheduler,
                 clock: Callable[[], float] = time.monotonic,
                 on_profile: Optional[Callable[[Profile, Optional[AppInfo]], None]] = None):
        self.runner = runner
        self.ui = ui
        self.schedule = schedule
        self.clock = clock
        self.on_profile = on_profile
        self.cfg = cfg
        self.app: Optional[AppInfo] = None
        self.profile: Profile = resolve_profile(cfg, None)
        self.active_tools: dict[str, Optional[int]] = {}
        self.scroll_feel: Optional[str] = None  # "ratchet" / "smooth" as reported by the device

        self.ring_open = False
        self.selection = 0
        self._ring_timer: Optional[Handle] = None

        self._accum = 0
        self._last_ratchet = -1e9
        self._ratchet_seen = False

        self.pressed = False
        self._rotated_while_pressed = False
        self._long_fired = False
        self._tap_seen = False
        self._tapped_serial = -1
        self._press_end_at = -1e9
        self._long_timer: Optional[Handle] = None
        self._press_serial = 0

    # ------------------------------------------------------------------ inputs

    @property
    def general(self) -> dict:
        return self.cfg["general"]

    def set_config(self, cfg: dict) -> None:
        self.cfg = cfg
        self._close_ring()
        self._apply_profile(resolve_profile(cfg, self.app), force=True)

    def set_app(self, app: Optional[AppInfo]) -> None:
        if app is None or app.is_self:
            return  # keep the last real app while our own windows are focused
        if app == self.app:
            return
        self.app = app
        self._apply_profile(resolve_profile(self.cfg, app))

    def set_scroll_feel(self, feel: Optional[str]) -> None:
        self.scroll_feel = feel
        self._accum = 0

    def handle_event(self, ev: CrownEvent) -> None:
        if ev.press_started and not self.pressed:
            self._press_begin()
        ticks = self._ticks(ev)
        if ticks:
            if self.pressed:
                self._rotated_while_pressed = True
                self._cancel(self._long_timer)
            self._rotate(ticks)
        if ev.tap:
            self._tap_event()
        if ev.press_ended:
            self._press_end()

    # ------------------------------------------------------------------ profile / tools

    def _apply_profile(self, profile: Profile, force: bool = False) -> None:
        if not force and profile.key == self.profile.key:
            return
        if self.ring_open:
            self._close_ring()
        self.profile = profile
        active = self.active_tools.get(profile.key)
        if active is not None and active >= len(profile.ring):
            self.active_tools[profile.key] = None
        if self.on_profile is not None:
            self.on_profile(profile, self.app)

    @property
    def active_tool_index(self) -> Optional[int]:
        return self.active_tools.get(self.profile.key)

    @property
    def active_tool(self) -> Optional[dict]:
        idx = self.active_tool_index
        return self.profile.ring[idx] if idx is not None else None

    def _slots(self) -> list[tuple[str, Optional[int]]]:
        slots: list[tuple[str, Optional[int]]] = []
        if self.general.get("ring_default_slot", True):
            slots.append((DEFAULT_SLOT_LABEL, None))
        slots += [(tool.get("label") or "Tool", i) for i, tool in enumerate(self.profile.ring)]
        return slots

    # ------------------------------------------------------------------ rotation

    def _ticks(self, ev: CrownEvent) -> int:
        now = self.clock()
        if ev.ratchet:
            self._ratchet_seen = True
            self._last_ratchet = now
            self._accum = 0
            return ev.ratchet
        if not ev.rotation or self.scroll_feel == "ratchet":
            return 0
        if self.scroll_feel != "smooth" and (self._ratchet_seen or now - self._last_ratchet < RATCHET_GRACE_S):
            return 0
        per = max(1, int(self.general.get("rotation_per_tick", 12)))
        self._accum += ev.rotation
        ticks = int(self._accum / per)  # truncates toward zero for both directions
        self._accum -= ticks * per
        return ticks

    def _rotate(self, ticks: int) -> None:
        if self.ring_open:
            slots = self._slots()
            self.selection = (self.selection + ticks) % len(slots)
            self._show_ring()
            return
        tool = self.active_tool
        if tool is not None:
            action = (tool.get("rotate_pressed") if self.pressed else None) or tool.get("rotate")
            if not action:
                action = self._profile_rotate()
            self.runner.rotate(action, ticks)
            if self.general.get("show_hud", True):
                self.ui.show_hud(tool.get("label") or "Tool", actions.describe(ROTATE, action),
                                 1 if ticks > 0 else -1)
            return
        self.runner.rotate(self._profile_rotate(), ticks)

    def _profile_rotate(self) -> Optional[dict]:
        if self.pressed and self.profile.rotate_pressed:
            return self.profile.rotate_pressed
        return self.profile.rotate

    # ------------------------------------------------------------------ presses

    def _press_begin(self) -> None:
        self.pressed = True
        self._rotated_while_pressed = False
        self._long_fired = False
        self._tap_seen = False
        self._press_serial += 1
        self._cancel(self._long_timer)
        self._long_timer = self.schedule(int(self.general.get("long_press_ms", 500)),
                                         self._long_timeout)

    def _long_timeout(self) -> None:
        self._long_timer = None
        if self.pressed and not self._rotated_while_pressed and not self._long_fired:
            self._long_fired = True
            self._long_press()

    def _press_end(self) -> None:
        if not self.pressed:
            return
        self.pressed = False
        self._press_end_at = self.clock()
        self._cancel(self._long_timer)
        self._long_timer = None
        if self._long_fired or self._rotated_while_pressed or self._tap_seen:
            return
        # Normally the crown sends a separate tap; if it does not, a short
        # press-and-release counts as a tap.
        serial = self._press_serial
        self.schedule(TAP_FALLBACK_MS, lambda: self._tap_fallback(serial))

    def _tap_fallback(self, serial: int) -> None:
        if serial == self._press_serial and not self._tap_seen and not self.pressed:
            self._tapped_serial = serial
            self._tap()

    def _tap_event(self) -> None:
        if self._long_fired or self._rotated_while_pressed:
            return
        # A tap flag arriving shortly after the fallback already handled this
        # press is the same physical tap.
        if (self._tapped_serial == self._press_serial and not self.pressed
                and self.clock() - self._press_end_at < TAP_DEDUP_S):
            return
        self._tap_seen = True
        self._tapped_serial = self._press_serial
        self._tap()

    def _tap(self) -> None:
        if self.ring_open:
            self._select()
        else:
            self._trigger(self.profile.tap)

    def _long_press(self) -> None:
        if self.ring_open:
            self._close_ring()
            return
        tool = self.active_tool
        action = (tool or {}).get("long_press") or self.profile.long_press
        self._trigger(action)

    def _trigger(self, action: Optional[dict]) -> None:
        kind = (action or {}).get("type", "none")
        if kind == "ring":
            self._open_ring()
        elif kind == "reset":
            self.active_tools[self.profile.key] = None
            self._hud_default()
        elif kind != "none":
            self.runner.trigger(action)
            if self.general.get("show_hud", True):
                self.ui.show_hud(actions.describe(TRIGGER, action), "", 0)

    # ------------------------------------------------------------------ ring

    def _open_ring(self) -> None:
        slots = self._slots()
        if not slots:
            return
        active = self.active_tool_index
        self.selection = next((i for i, (_, idx) in enumerate(slots) if idx == active), 0)
        self.ring_open = True
        self._show_ring()

    def _show_ring(self) -> None:
        slots = self._slots()
        active = self.active_tool_index
        active_slot = next((i for i, (_, idx) in enumerate(slots) if idx == active), -1)
        self.ui.show_ring(RingView(self.profile.name, [label for label, _ in slots],
                                   self.selection, active_slot))
        self._cancel(self._ring_timer)
        self._ring_timer = self.schedule(int(self.general.get("ring_timeout_ms", 4000)),
                                         self._ring_timeout)

    def _ring_timeout(self) -> None:
        self._ring_timer = None
        self._close_ring()

    def _close_ring(self) -> None:
        self._cancel(self._ring_timer)
        self._ring_timer = None
        if self.ring_open:
            self.ring_open = False
            self.ui.hide_ring()

    def _select(self) -> None:
        slots = self._slots()
        _, idx = slots[self.selection % len(slots)]
        self._close_ring()
        if idx is None:
            self.active_tools[self.profile.key] = None
            self._hud_default()
            return
        tool = self.profile.ring[idx]
        rotate = tool.get("rotate")
        if not rotate or rotate.get("type", "none") == "none":
            # A "button" tool: run it once and keep the current mode.
            if tool.get("select"):
                self._trigger(tool["select"])
            return
        self.active_tools[self.profile.key] = idx
        self.ui.show_hud(tool.get("label") or "Tool", actions.describe(ROTATE, rotate), 0)

    def _hud_default(self) -> None:
        self.ui.show_hud(DEFAULT_SLOT_LABEL, actions.describe(ROTATE, self.profile.rotate), 0)

    @staticmethod
    def _cancel(handle: Optional[Handle]) -> None:
        if handle is not None:
            handle.cancel()
