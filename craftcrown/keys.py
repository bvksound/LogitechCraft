"""Parse shortcut strings such as ``"mod+shift+z"`` into key names.

``mod`` means Cmd on macOS and Ctrl elsewhere, so most shortcuts can be
written once.  When an app really differs per platform, give both variants:

    "win=ctrl+y; mac=cmd+shift+z"
"""

from __future__ import annotations

import sys

ALIASES = {
    "control": "ctrl", "ctl": "ctrl", "option": "alt", "opt": "alt",
    "command": "cmd", "super": "cmd", "meta": "cmd", "win": "cmd", "windows": "cmd",
    "return": "enter", "escape": "esc", "del": "delete", "bksp": "backspace",
    "pgup": "page_up", "pageup": "page_up", "pgdn": "page_down", "pagedown": "page_down",
    "plus": "+", "minus": "-", "equal": "=", "equals": "=", "comma": ",", "period": ".",
    "left_bracket": "[", "right_bracket": "]", "spacebar": "space",
}

MODIFIERS = ("ctrl", "cmd", "alt", "shift")

SPECIAL_KEYS = {
    "ctrl", "alt", "shift", "cmd", "tab", "enter", "esc", "space", "backspace", "delete",
    "up", "down", "left", "right", "home", "end", "page_up", "page_down", "insert",
    "caps_lock", "menu",
    "media_volume_up", "media_volume_down", "media_volume_mute",
    "media_play_pause", "media_next", "media_previous",
} | {f"f{i}" for i in range(1, 21)}


def platform_key() -> str:
    if sys.platform == "darwin":
        return "mac"
    if sys.platform.startswith("win"):
        return "win"
    return "linux"


PLATFORMS = ("win", "mac", "linux")


def select_variant(spec: str, platform: str | None = None) -> str:
    """Pick the right variant from ``"win=...; mac=..."`` (or return spec as-is)."""
    platform = platform or platform_key()
    spec = spec.strip()
    variants: dict[str, str] = {}
    for part in spec.split(";"):
        name, sep, combo = part.partition("=")
        if sep and name.strip().lower() in PLATFORMS:
            variants[name.strip().lower()] = combo.strip()
        elif part.strip():
            return spec  # a plain shortcut such as "mod+="
    if not variants:
        return spec
    if platform in variants:
        return variants[platform]
    if platform == "linux" and "win" in variants:
        return variants["win"]
    return next(iter(variants.values()))


def parse_combo(spec: str, platform: str | None = None) -> list[str]:
    """Return normalized key names, modifiers first.

    >>> parse_combo("mod+shift+Z", "mac")
    ['cmd', 'shift', 'z']
    """
    platform = platform or platform_key()
    combo = select_variant(spec, platform)
    if not combo:
        return []
    # Allow "+" itself as a key: "mod++" or "ctrl+plus".
    parts: list[str] = []
    token = ""
    for i, ch in enumerate(combo):
        if ch == "+" and token:
            parts.append(token)
            token = ""
        elif ch == "+" and not token and (i == len(combo) - 1 or combo[i + 1] == "+"):
            token = "+"
        elif ch != "+":
            token += ch
    if token:
        parts.append(token)

    keys: list[str] = []
    for raw in parts:
        name = raw.strip()
        if not name:
            continue
        lower = name.lower()
        lower = ALIASES.get(lower, lower)
        if lower == "mod":
            lower = "cmd" if platform == "mac" else "ctrl"
        if len(lower) != 1 and lower not in SPECIAL_KEYS:
            raise ValueError(f"onbekende toets: {raw!r}")
        keys.append(lower)
    mods = [k for k in MODIFIERS if k in keys]
    rest = [k for k in keys if k not in MODIFIERS]
    return mods + rest
