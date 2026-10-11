import pytest

from craftcrown import config as cfgmod
from craftcrown.actions import ActionRunner
from craftcrown.activeapp import AppInfo
from craftcrown.controller import Controller
from craftcrown.hidpp import PRESS_END, PRESS_START, CrownEvent
from craftcrown.keys import parse_combo

from .fakes import FakeBackend, FakeClock, FakeTimers, FakeUi

CHROME_WIN = AppInfo("chrome.exe", "chrome", 1234)
CHROME_MAC = AppInfo("com.google.Chrome", "Google Chrome", 1234)
NOTEPAD = AppInfo("notepad.exe", "notepad", 99)


# ---------------------------------------------------------------- keys

@pytest.mark.parametrize("spec,platform,expected", [
    ("mod+shift+Z", "mac", ["cmd", "shift", "z"]),
    ("mod+shift+Z", "win", ["ctrl", "shift", "z"]),
    ("mod+=", "win", ["ctrl", "="]),
    ("mod++", "win", ["ctrl", "+"]),
    ("win=ctrl+y; mac=cmd+shift+z", "mac", ["cmd", "shift", "z"]),
    ("win=ctrl+y; mac=cmd+shift+z", "win", ["ctrl", "y"]),
    ("win=alt+left; mac=cmd+[", "mac", ["cmd", "["]),
    ("Page Down".replace(" ", ""), "win", ["page_down"]),
])
def test_parse_combo(spec, platform, expected):
    assert parse_combo(spec, platform) == expected


def test_parse_combo_rejects_unknown_key():
    with pytest.raises(ValueError):
        parse_combo("ctrl+bogus", "win")


# ---------------------------------------------------------------- config

def test_profile_matching_works_on_both_platforms():
    cfg = cfgmod.default_config()
    assert cfgmod.resolve_profile(cfg, CHROME_WIN).name == "Browsers"
    assert cfgmod.resolve_profile(cfg, CHROME_MAC).name == "Browsers"
    assert cfgmod.resolve_profile(cfg, NOTEPAD).key == "default"


def test_profile_inherits_unset_fields():
    cfg = cfgmod.default_config()
    excel = cfgmod.resolve_profile(cfg, AppInfo("EXCEL.EXE", "EXCEL"))
    assert excel.rotate == cfg["default"]["rotate"]  # rotate is None in the Excel profile
    assert excel.ring[0]["label"] == "Kolommen"


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "c.json"
    cfg = cfgmod.default_config()
    cfg["general"]["ring_timeout_ms"] = 1234
    cfgmod.save(cfg, path)
    assert cfgmod.load(path)["general"]["ring_timeout_ms"] == 1234


# ---------------------------------------------------------------- controller

@pytest.fixture
def rig():
    clock = FakeClock()
    timers = FakeTimers(clock)
    ui = FakeUi()
    backend = FakeBackend()
    cfg = cfgmod.default_config()
    ctl = Controller(cfg, ActionRunner(backend), ui, timers, clock)
    ctl.set_app(NOTEPAD)
    return ctl, ui, backend, timers


def turn(ctl, n):
    ctl.handle_event(CrownEvent(ratchet=n))


def tap(ctl, timers):
    ctl.handle_event(CrownEvent(press=PRESS_START))
    ctl.handle_event(CrownEvent(press=PRESS_END))
    ctl.handle_event(CrownEvent(tap=True))
    timers.advance(200)


def test_default_rotation_is_volume(rig):
    ctl, ui, backend, _ = rig
    turn(ctl, 2)
    turn(ctl, -1)
    assert backend.calls == [("tap", ("media_volume_up",), 2), ("tap", ("media_volume_down",), 1)]


def test_tap_opens_ring_turn_selects_tap_activates(rig):
    ctl, ui, backend, timers = rig
    tap(ctl, timers)
    assert ctl.ring_open and ui.ring.labels[0] == "Standaard" and ui.ring.selected == 0
    turn(ctl, 2)  # -> "Scrollen"
    assert ui.ring.selected == 2 and backend.calls == []
    tap(ctl, timers)
    assert not ctl.ring_open
    assert ctl.active_tool["label"] == "Scrollen"
    turn(ctl, 1)
    assert backend.calls == [("scroll", 0, -2)]


def test_tap_counted_once_with_or_without_tap_event(rig):
    ctl, ui, _, timers = rig
    # Device that only reports press start/end (no separate tap flag)
    ctl.handle_event(CrownEvent(press=PRESS_START))
    ctl.handle_event(CrownEvent(press=PRESS_END))
    timers.advance(200)
    assert ctl.ring_open
    timers.advance(10_000)  # ring timeout
    assert not ctl.ring_open
    # Device that reports both
    tap(ctl, timers)
    assert ui.events.count(("hide",)) == 1 and ctl.ring_open


def test_ring_wraps_and_times_out(rig):
    ctl, ui, _, timers = rig
    tap(ctl, timers)
    turn(ctl, -1)
    assert ui.ring.selected == len(ui.ring.labels) - 1
    timers.advance(3000)
    turn(ctl, 1)  # input resets the timeout
    timers.advance(3000)
    assert ctl.ring_open
    timers.advance(2000)
    assert not ctl.ring_open


def test_long_press_runs_action_and_does_not_tap(rig):
    ctl, ui, backend, timers = rig
    ctl.handle_event(CrownEvent(press=PRESS_START))
    timers.advance(600)
    ctl.handle_event(CrownEvent(press=PRESS_END))
    timers.advance(300)
    assert backend.calls == [("tap", ("media_play_pause",), 1)]
    assert not ctl.ring_open


def test_press_and_turn_uses_rotate_pressed(rig):
    ctl, ui, backend, timers = rig
    ctl.handle_event(CrownEvent(press=PRESS_START))
    ctl.handle_event(CrownEvent(ratchet=1, press=2))
    ctl.handle_event(CrownEvent(press=PRESS_END))
    timers.advance(1000)
    assert backend.calls == [("scroll", 2, 0)]  # default rotate_pressed = hscroll
    assert not ctl.ring_open


def test_active_tool_is_remembered_per_app(rig):
    ctl, ui, backend, timers = rig
    tap(ctl, timers)
    turn(ctl, 4)  # -> "Zoom"
    tap(ctl, timers)
    assert ctl.active_tool["label"] == "Zoom"
    ctl.set_app(CHROME_WIN)
    assert ctl.profile.name == "Browsers" and ctl.active_tool is None
    turn(ctl, 1)
    assert backend.calls[-1] == ("scroll", 0, -3)
    ctl.set_app(NOTEPAD)
    assert ctl.active_tool["label"] == "Zoom"


def test_button_tool_runs_once_and_keeps_mode(rig):
    ctl, ui, backend, timers = rig
    tap(ctl, timers)
    turn(ctl, len(ui.ring.labels) - 1)  # last slot: "Play / pauze"
    assert ui.ring.labels[ui.ring.selected] == "Play / pauze"
    tap(ctl, timers)
    assert backend.calls == [("tap", ("media_play_pause",), 1)]
    assert ctl.active_tool is None


def test_own_window_does_not_switch_profile(rig):
    import os
    ctl, *_ = rig
    ctl.set_app(CHROME_WIN)
    ctl.set_app(AppInfo("python.exe", "python", os.getpid()))
    assert ctl.profile.name == "Browsers"


def test_smooth_rotation_accumulates(rig):
    ctl, ui, backend, timers = rig
    ctl.set_scroll_feel("smooth")
    for _ in range(5):
        ctl.handle_event(CrownEvent(rotation=5))  # 25 units, 12 per tick -> 2 ticks
    assert sum(c[2] for c in backend.calls) == 2


def test_volume_uses_direct_backend_when_available():
    class VolumeBackend(FakeBackend):
        def volume(self, steps):
            self.calls.append(("volume", steps))
            return "42%"

    seen = []
    backend = VolumeBackend()
    runner = ActionRunner(backend, listener=seen.append)
    runner.rotate({"type": "volume"}, -2)
    assert backend.calls == [("volume", -2)] and seen == ["Volume 42%"]


def test_volume_falls_back_to_media_keys():
    class NoVolume(FakeBackend):
        def volume(self, steps):
            return None

    backend = NoVolume()
    ActionRunner(backend).rotate({"type": "volume"}, 1)
    assert backend.calls == [("tap", ("media_volume_up",), 1)]
