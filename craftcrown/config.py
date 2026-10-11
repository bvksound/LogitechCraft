"""Configuration: a default profile plus optional per-app profiles.

Every field of an app profile may be ``None``, meaning "use the default
profile's value".  A profile's ring is a list of tools; each tool says what
turning the crown does while that tool is active.

    {
      "general": {...},
      "default": {"rotate": ..., "rotate_pressed": ..., "tap": ..., "long_press": ...,
                  "ring": [{"label": "Volume", "rotate": {"type": "volume"}}, ...]},
      "apps": [{"name": "Chrome", "match": ["chrome.exe", "com.google.Chrome"], ...}]
    }
"""

from __future__ import annotations

import copy
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .activeapp import AppInfo

log = logging.getLogger(__name__)

PROFILE_ACTIONS = ("rotate", "rotate_pressed", "tap", "long_press")
TOOL_ACTIONS = ("rotate", "rotate_pressed", "select", "long_press")

DEFAULT_GENERAL = {
    "ring_timeout_ms": 4000,  # ring closes after this long without input
    "long_press_ms": 500,
    "show_hud": True,  # small indicator when a ring tool is used
    "ring_default_slot": True,  # first ring slot = "Standaard" (back to default rotation)
    "scroll_mode": "auto",  # crown feel: "auto" (leave as is), "ratchet" or "smooth"
    "rotation_per_tick": 12,  # smooth mode: raw rotation units per step
    "ring_size": 340,
}


def _tool(label: str, rotate: Optional[dict] = None, **extra) -> dict:
    tool = {"label": label, "rotate": rotate}
    tool.update(extra)
    return tool


DEFAULT_PROFILE = {
    "rotate": {"type": "volume"},
    "rotate_pressed": {"type": "hscroll", "lines": 2},
    "tap": {"type": "ring"},
    "long_press": {"type": "play_pause"},
    "ring": [
        _tool("Volume", {"type": "volume"}),
        _tool("Scrollen", {"type": "scroll", "lines": 2}),
        _tool("Horizontaal", {"type": "hscroll", "lines": 2}),
        _tool("Zoom", {"type": "zoom"}),
        _tool("Tabbladen", {"type": "tabs"}),
        _tool("Ongedaan maken", {"type": "undo_redo"}),
        _tool("Nummer", {"type": "media_track"}),
        # A tool without rotate action is a button: selecting it runs "select".
        _tool("Play / pauze", None, select={"type": "play_pause"}),
    ],
}

EXAMPLE_APPS = [
    {
        "name": "Browsers",
        "match": ["chrome.exe", "msedge.exe", "firefox.exe", "brave.exe",
                  "com.google.Chrome", "com.apple.Safari", "org.mozilla.firefox",
                  "com.microsoft.edgemac", "com.brave.Browser"],
        "rotate": {"type": "scroll", "lines": 3},
        "rotate_pressed": {"type": "tabs"},
        "tap": None,
        "long_press": None,
        "ring": [
            _tool("Tabbladen", {"type": "tabs"}),
            _tool("Zoom", {"type": "zoom"}),
            _tool("Geschiedenis", {"type": "keys", "cw": "win=alt+right; mac=cmd+]",
                                   "ccw": "win=alt+left; mac=cmd+["}),
            _tool("Volume", {"type": "volume"}),
        ],
    },
    {
        "name": "Photoshop",
        "match": ["photoshop.exe", "com.adobe.Photoshop"],
        "rotate": {"type": "keys", "cw": "]", "ccw": "["},
        "rotate_pressed": {"type": "zoom"},
        "tap": None,
        "long_press": None,
        "ring": [
            _tool("Penseelgrootte", {"type": "keys", "cw": "]", "ccw": "["}),
            _tool("Hardheid", {"type": "keys", "cw": "shift+]", "ccw": "shift+["}),
            _tool("Zoom", {"type": "zoom"}),
            _tool("Geschiedenis", {"type": "keys", "cw": "mod+shift+z", "ccw": "mod+z"}),
            _tool("Lagen", {"type": "keys", "cw": "alt+]", "ccw": "alt+["}),
        ],
    },
    {
        "name": "Excel",
        "match": ["excel.exe", "com.microsoft.Excel"],
        "rotate": None,
        "rotate_pressed": {"type": "hscroll", "lines": 3},
        "tap": None,
        "long_press": None,
        "ring": [
            _tool("Kolommen", {"type": "hscroll", "lines": 3}),
            _tool("Werkbladen", {"type": "keys", "cw": "win=ctrl+page_down; mac=alt+right",
                                 "ccw": "win=ctrl+page_up; mac=alt+left"}),
            _tool("Zoom", {"type": "zoom"}),
            _tool("Volume", {"type": "volume"}),
        ],
    },
]


def default_config() -> dict:
    return {
        "version": 1,
        "general": dict(DEFAULT_GENERAL),
        "default": copy.deepcopy(DEFAULT_PROFILE),
        "apps": copy.deepcopy(EXAMPLE_APPS),
    }


def config_dir() -> Path:
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "CraftCrown"


def default_path() -> Path:
    return config_dir() / "config.json"


def normalize(cfg: dict) -> dict:
    """Fill in missing keys so that the rest of the code can rely on them."""
    out = default_config()
    out["apps"] = []
    out["general"].update(cfg.get("general") or {})
    default = cfg.get("default") or {}
    for key in PROFILE_ACTIONS:
        if key in default:
            out["default"][key] = default[key]
    if isinstance(default.get("ring"), list):
        out["default"]["ring"] = [_normalize_tool(t) for t in default["ring"]]
    for app in cfg.get("apps") or []:
        prof = {"name": str(app.get("name") or "App"),
                "match": [str(m) for m in app.get("match") or []]}
        for key in PROFILE_ACTIONS:
            prof[key] = app.get(key)
        ring = app.get("ring")
        prof["ring"] = [_normalize_tool(t) for t in ring] if isinstance(ring, list) else None
        out["apps"].append(prof)
    return out


def _normalize_tool(tool: dict) -> dict:
    out = {"label": str(tool.get("label") or "Tool")}
    for key in TOOL_ACTIONS:
        out[key] = tool.get(key)
    return out


def load(path: Optional[Path] = None) -> dict:
    path = path or default_path()
    if not path.exists():
        cfg = default_config()
        try:
            save(cfg, path)
        except OSError as exc:
            log.warning("cannot write default config: %s", exc)
        return cfg
    try:
        with open(path, encoding="utf-8") as fh:
            return normalize(json.load(fh))
    except (OSError, ValueError) as exc:
        log.error("config %s unreadable (%s); using defaults", path, exc)
        return default_config()


def save(cfg: dict, path: Optional[Path] = None) -> None:
    path = path or default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


@dataclass
class Profile:
    """A profile with inheritance resolved."""

    key: str  # "default" or "app:<name>"
    name: str
    rotate: Optional[dict]
    rotate_pressed: Optional[dict]
    tap: Optional[dict]
    long_press: Optional[dict]
    ring: list[dict]


def find_app_profile(cfg: dict, app: Optional[AppInfo]) -> Optional[dict]:
    if app is None:
        return None
    keys = app.keys()
    for prof in cfg.get("apps", []):
        for m in prof.get("match", []):
            m = m.strip().lower()
            if m and (m in keys or (m.endswith(".exe") and m[:-4] in keys)):
                return prof
    return None


def resolve_profile(cfg: dict, app: Optional[AppInfo]) -> Profile:
    default = cfg["default"]
    prof = find_app_profile(cfg, app)
    if prof is None:
        return Profile("default", "Alle apps", *(default.get(k) for k in PROFILE_ACTIONS),
                       ring=list(default.get("ring") or []))

    def pick(key):
        value = prof.get(key)
        return default.get(key) if value is None else value

    ring = prof.get("ring")
    return Profile(f"app:{prof['name']}", prof["name"], *(pick(k) for k in PROFILE_ACTIONS),
                   ring=list(default.get("ring") or []) if ring is None else list(ring))
