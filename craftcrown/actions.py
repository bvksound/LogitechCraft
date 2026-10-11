"""Actions that the crown can perform, and the code that performs them.

An action in the configuration is a small dict: ``{"type": "volume"}`` or
``{"type": "keys", "cw": "mod+=", "ccw": "mod+-"}``.  ``ACTION_TYPES``
describes every type (label + parameters) so the settings window can build
its editors from it.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from typing import Optional, Protocol

from .keys import parse_combo

log = logging.getLogger(__name__)

ROTATE = "rotate"
TRIGGER = "trigger"


@dataclass(frozen=True)
class Param:
    name: str
    label: str
    kind: str  # "int", "str", "combo", "bool"
    default: object


@dataclass(frozen=True)
class ActionType:
    id: str
    label: str
    kind: str  # ROTATE or TRIGGER
    params: tuple[Param, ...] = field(default_factory=tuple)
    cw: str = ""  # preset shortcut for clockwise
    ccw: str = ""  # preset shortcut for counter-clockwise


_STEP = Param("step", "Herhalingen per klik", "int", 1)

ROTATE_TYPES = [
    ActionType("none", "Niets", ROTATE),
    ActionType("volume", "Volume", ROTATE, (_STEP,), "media_volume_up", "media_volume_down"),
    ActionType("scroll", "Verticaal scrollen", ROTATE,
               (Param("lines", "Regels per klik", "int", 2), Param("invert", "Omkeren", "bool", False))),
    ActionType("hscroll", "Horizontaal scrollen", ROTATE,
               (Param("lines", "Kolommen per klik", "int", 2), Param("invert", "Omkeren", "bool", False))),
    ActionType("zoom", "Zoomen", ROTATE, (_STEP,), "mod+=", "mod+-"),
    ActionType("tabs", "Tabbladen wisselen", ROTATE, (_STEP,), "ctrl+tab", "ctrl+shift+tab"),
    ActionType("undo_redo", "Ongedaan maken / opnieuw", ROTATE, (_STEP,),
               "win=ctrl+y; mac=cmd+shift+z", "mod+z"),
    ActionType("arrows_h", "Pijltjes links / rechts", ROTATE, (_STEP,), "right", "left"),
    ActionType("arrows_v", "Pijltjes op / neer", ROTATE, (_STEP,), "down", "up"),
    ActionType("pages", "Page up / down", ROTATE, (_STEP,), "page_down", "page_up"),
    ActionType("media_track", "Volgend / vorig nummer", ROTATE, (), "media_next", "media_previous"),
    ActionType("keys", "Eigen sneltoetsen", ROTATE,
               (Param("cw", "Rechtsom", "combo", ""), Param("ccw", "Linksom", "combo", ""), _STEP)),
]

TRIGGER_TYPES = [
    ActionType("none", "Niets", TRIGGER),
    ActionType("ring", "Ring openen", TRIGGER),
    ActionType("reset", "Terug naar standaardfunctie", TRIGGER),
    ActionType("play_pause", "Afspelen / pauze", TRIGGER, (), "media_play_pause"),
    ActionType("mute", "Geluid dempen", TRIGGER, (), "media_volume_mute"),
    ActionType("next_track", "Volgend nummer", TRIGGER, (), "media_next"),
    ActionType("prev_track", "Vorig nummer", TRIGGER, (), "media_previous"),
    ActionType("keys", "Sneltoets", TRIGGER, (Param("combo", "Sneltoets", "combo", ""),)),
    ActionType("text", "Tekst typen", TRIGGER, (Param("text", "Tekst", "str", ""),)),
    ActionType("command", "Commando uitvoeren", TRIGGER, (Param("cmd", "Commando", "str", ""),)),
]

ACTION_TYPES = {ROTATE: {t.id: t for t in ROTATE_TYPES}, TRIGGER: {t.id: t for t in TRIGGER_TYPES}}

# Trigger types the controller handles itself.
CONTROLLER_TRIGGERS = {"none", "ring", "reset"}


def action_type(kind: str, action: Optional[dict]) -> Optional[ActionType]:
    if not action:
        return None
    return ACTION_TYPES[kind].get(action.get("type", "none"))


def describe(kind: str, action: Optional[dict]) -> str:
    t = action_type(kind, action)
    if t is None:
        return "Niets"
    if t.id == "keys":
        if kind == ROTATE:
            return f"{action.get('ccw', '')} / {action.get('cw', '')}"
        return action.get("combo", "") or t.label
    return t.label


def param(action: dict, t: ActionType, name: str):
    for p in t.params:
        if p.name == name:
            return action.get(name, p.default)
    return action.get(name)


class Backend(Protocol):
    def tap(self, keys: list[str], times: int = 1) -> None: ...

    def scroll(self, dx: int, dy: int) -> None: ...

    def type_text(self, text: str) -> None: ...

    def run(self, command: str) -> None: ...


class PynputBackend:
    """Synthesizes input on Windows, macOS and Linux (X11) through pynput."""

    def __init__(self):
        from pynput import keyboard, mouse  # imported lazily: needs a display

        self._kb = keyboard.Controller()
        self._mouse = mouse.Controller()
        self._Key = keyboard.Key
        self._KeyCode = keyboard.KeyCode

    def _key(self, name: str):
        if len(name) == 1:
            return self._KeyCode.from_char(name)
        try:
            return getattr(self._Key, name)
        except AttributeError:
            raise ValueError(f"toets {name!r} bestaat niet op dit platform") from None

    def tap(self, keys: list[str], times: int = 1) -> None:
        resolved = [self._key(k) for k in keys]
        for _ in range(max(0, times)):
            for k in resolved:
                self._kb.press(k)
            for k in reversed(resolved):
                self._kb.release(k)

    def scroll(self, dx: int, dy: int) -> None:
        self._mouse.scroll(dx, dy)

    def type_text(self, text: str) -> None:
        self._kb.type(text)

    def run(self, command: str) -> None:
        subprocess.Popen(command, shell=True)  # noqa: S602 - user-configured command


class ActionRunner:
    def __init__(self, backend: Optional[Backend] = None):
        self._backend = backend

    @property
    def backend(self) -> Backend:
        if self._backend is None:
            self._backend = PynputBackend()
        return self._backend

    def rotate(self, action: Optional[dict], ticks: int) -> None:
        t = action_type(ROTATE, action)
        if t is None or t.id == "none" or ticks == 0:
            return
        try:
            self._rotate(t, action, ticks)
        except Exception as exc:  # noqa: BLE001 - a bad action must not kill the app
            log.warning("action %s failed: %s", t.id, exc)

    def _rotate(self, t: ActionType, action: dict, ticks: int) -> None:
        if t.id in ("scroll", "hscroll"):
            amount = ticks * int(param(action, t, "lines") or 1)
            if param(action, t, "invert"):
                amount = -amount
            if t.id == "scroll":
                self.backend.scroll(0, -amount)  # clockwise scrolls down, like the crown itself
            else:
                self.backend.scroll(amount, 0)
            return
        cw, ccw = (action.get("cw", ""), action.get("ccw", "")) if t.id == "keys" else (t.cw, t.ccw)
        combo = cw if ticks > 0 else ccw
        if not combo:
            return
        step = int(param(action, t, "step") or 1) if any(p.name == "step" for p in t.params) else 1
        self.backend.tap(parse_combo(combo), abs(ticks) * max(1, step))

    def trigger(self, action: Optional[dict]) -> None:
        t = action_type(TRIGGER, action)
        if t is None or t.id in CONTROLLER_TRIGGERS:
            return
        try:
            if t.id == "keys":
                if action.get("combo"):
                    self.backend.tap(parse_combo(action["combo"]))
            elif t.id == "text":
                if action.get("text"):
                    self.backend.type_text(action["text"])
            elif t.id == "command":
                if action.get("cmd"):
                    self.backend.run(action["cmd"])
            elif t.cw:
                self.backend.tap(parse_combo(t.cw))
        except Exception as exc:  # noqa: BLE001
            log.warning("action %s failed: %s", t.id, exc)
