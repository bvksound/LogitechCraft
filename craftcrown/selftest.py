"""``--selftest``: check that a (frozen) build actually works on this machine.

Loads every native dependency, enumerates HID devices, creates the overlay
and settings window, drives the ring through the controller with simulated
crown events and saves screenshots.  No input is sent to other apps.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Callable, Optional


class _NullBackend:
    def tap(self, keys, times=1): pass

    def scroll(self, dx, dy): pass

    def type_text(self, text): pass

    def run(self, command): pass


def run_selftest(out_dir: Optional[Path]) -> int:
    lines: list[str] = []
    failures = 0

    def log(text: str) -> None:
        lines.append(text)
        print(text, flush=True)

    def check(name: str, fn: Callable[[], object]) -> object:
        nonlocal failures
        try:
            result = fn()
            log(f"OK    {name}" + (f": {result}" if result not in (None, "") else ""))
            return result
        except Exception:  # noqa: BLE001
            failures += 1
            log(f"FAIL  {name}\n{traceback.format_exc()}")
            return None

    import platform

    from . import __version__
    log(f"Craft Crown {__version__} selftest on {platform.platform()} ({platform.machine()}), "
        f"Python {platform.python_version()}, frozen={getattr(sys, 'frozen', False)}")

    def hid_devices():
        import hid
        from . import device
        cands = device.enumerate_candidates()
        return f"{len(cands)} Logitech HID++ interface(s)"

    check("hidapi", hid_devices)
    check("pynput", lambda: __import__("pynput.keyboard", fromlist=["Controller"]).Controller and "loaded")
    if sys.platform == "darwin":
        check("AppKit", lambda: __import__("AppKit").NSWorkspace and "loaded")
        check("ApplicationServices (Toegankelijkheid)",
              lambda: f"trusted={__import__('ApplicationServices').AXIsProcessTrusted()}")

        def input_monitoring():
            from .device import request_input_monitoring
            status = request_input_monitoring(request=False)
            return {0: "toegestaan", 1: "geweigerd", 2: "nog niet gevraagd"}.get(status, status)

        check("Invoermonitoring", input_monitoring)

    from .activeapp import get_foreground_app, list_running_apps
    check("foreground app", lambda: get_foreground_app())
    check("running apps", lambda: f"{len(list_running_apps())} apps")

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    check("Qt platform", lambda: app.platformName())

    from . import config
    from .actions import ActionRunner
    from .controller import Controller
    from .hidpp import PRESS_END, PRESS_START, CrownEvent
    from .ui.overlay import OverlayUi
    from .ui.settings import SettingsWindow

    cfg = config.default_config()
    ui = OverlayUi(cfg["general"]["ring_size"])
    timers: list = []

    class Handle:
        def __init__(self, ms, fn):
            self.t = QTimer()
            self.t.setSingleShot(True)
            self.t.timeout.connect(fn)
            self.t.start(ms)
            timers.append(self)

        def cancel(self):
            self.t.stop()

    ctl = Controller(cfg, ActionRunner(_NullBackend()), ui, Handle)

    def drive_ring():
        for ev in (CrownEvent(press=PRESS_START), CrownEvent(press=PRESS_END), CrownEvent(tap=True)):
            ctl.handle_event(ev)
        ctl.handle_event(CrownEvent(ratchet=4))
        app.processEvents()
        assert ctl.ring_open, "ring did not open"
        if out_dir:
            ui.ring.grab().save(str(out_dir / "selftest-ring.png"))
        label = ui.ring.view.labels[ui.ring.view.selected]
        ctl.handle_event(CrownEvent(press=PRESS_START))
        ctl.handle_event(CrownEvent(press=PRESS_END))
        ctl.handle_event(CrownEvent(tap=True))
        assert ctl.active_tool and ctl.active_tool["label"] == label, "tool not activated"
        return f"ring opened, selected and activated '{label}'"

    check("ring + controller", drive_ring)

    def settings_window():
        w = SettingsWindow(cfg, lambda: None, lambda: [])
        w.set_status("Selftest", True)
        w.show()
        app.processEvents()
        if out_dir:
            w.grab().save(str(out_dir / "selftest-settings.png"))
        w.close()
        return "rendered"

    check("settings window", settings_window)

    def full_app():
        """Start the real app (tray, window, crown service) for a moment."""
        import argparse
        import tempfile

        from .app import CraftCrownApp

        cfg_path = Path(tempfile.mkdtemp()) / "config.json"
        crown_app = CraftCrownApp(argparse.Namespace(simulate=False, settings=False, background=False,
                                                     config=cfg_path))
        if crown_app.service is not None:
            crown_app.service.start()
        seen = {}

        def inspect():
            seen["window"] = crown_app.settings is not None and crown_app.settings.isVisible()
            seen["tray"] = crown_app.tray.isVisible()
            if seen["window"] and out_dir:
                crown_app.settings.grab().save(str(out_dir / "selftest-startup.png"))
            app.quit()

        QTimer.singleShot(2500, inspect)
        app.exec()
        assert seen.get("window"), "window not shown"
        status = crown_app._status[0]
        crown_app._shutdown()
        return f"window shown, tray={seen['tray']}, status: {status}"

    check("app startup", full_app)

    log("RESULT: " + ("PASS" if failures == 0 else f"FAIL ({failures})"))
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "selftest.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if failures == 0 else 1
