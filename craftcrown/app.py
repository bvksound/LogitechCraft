"""Application wiring: tray icon, crown service, controller, overlay and settings."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QGridLayout, QMenu, QPushButton, QSystemTrayIcon, QWidget

from . import __version__, config
from .actions import ActionRunner
from .activeapp import AppInfo, get_foreground_app
from .config import Profile
from .controller import Controller
from .device import CrownService
from .hidpp import PRESS_END, PRESS_START, CrownEvent
from .ui.overlay import ACCENT, OverlayUi
from .ui.settings import SettingsWindow

log = logging.getLogger("craftcrown")

APP_POLL_MS = 300
SAVE_DELAY_MS = 400
RECENT_APPS = 15


class _Bridge(QObject):
    """Moves callbacks from the HID thread onto the Qt main thread."""

    event = Signal(object)
    status = Signal(str, bool)
    feel = Signal(object)


class _QtHandle:
    def __init__(self, ms: int, fn, owner: set):
        self._owner = owner
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fire)
        self._fn = fn
        owner.add(self)
        self._timer.start(ms)

    def _fire(self) -> None:
        self._owner.discard(self)
        self._fn()

    def cancel(self) -> None:
        self._timer.stop()
        self._owner.discard(self)


def tray_icon(connected: bool) -> QIcon:
    pix = QPixmap(64, 64)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    color = ACCENT if connected else QColor(140, 140, 150)
    p.setPen(QPen(color, 9))
    p.drawEllipse(QPointF(32, 32), 22, 22)
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawEllipse(QPointF(32, 32), 8, 8)
    p.end()
    return QIcon(pix)


class Simulator(QWidget):
    """Buttons that emulate the crown, for trying things out without a Craft."""

    def __init__(self, send):
        super().__init__()
        self.setWindowTitle("Crown-simulator")
        self.send = send
        grid = QGridLayout(self)
        buttons = [
            ("⟲ Linksom", lambda: send(CrownEvent(ratchet=-1))),
            ("Rechtsom ⟳", lambda: send(CrownEvent(ratchet=1))),
            ("Tik", self._tap),
            ("Lang drukken", self._long),
            ("Ingedrukt ⟲", lambda: self._pressed_turn(-1)),
            ("Ingedrukt ⟳", lambda: self._pressed_turn(1)),
        ]
        for i, (label, fn) in enumerate(buttons):
            b = QPushButton(label)
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(fn)
            grid.addWidget(b, i // 2, i % 2)

    def _tap(self) -> None:
        self.send(CrownEvent(press=PRESS_START))
        self.send(CrownEvent(press=PRESS_END))
        self.send(CrownEvent(tap=True))

    def _long(self) -> None:
        self.send(CrownEvent(press=PRESS_START))
        QTimer.singleShot(900, lambda: self.send(CrownEvent(press=PRESS_END)))

    def _pressed_turn(self, direction: int) -> None:
        self.send(CrownEvent(press=PRESS_START))
        self.send(CrownEvent(ratchet=direction, press=2))
        self.send(CrownEvent(press=PRESS_END))


def _mac_setup() -> None:
    """Run as a menu-bar app (no Dock icon) and check Accessibility permission."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory  # type: ignore
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    except Exception:  # noqa: BLE001
        pass


def _mac_accessibility_ok() -> bool:
    if sys.platform != "darwin":
        return True
    try:
        from ApplicationServices import AXIsProcessTrusted  # type: ignore
        return bool(AXIsProcessTrusted())
    except Exception:  # noqa: BLE001
        return True  # cannot tell; assume fine


class CraftCrownApp:
    def __init__(self, args: argparse.Namespace):
        self.qapp = QApplication.instance() or QApplication(sys.argv)
        self.qapp.setQuitOnLastWindowClosed(False)
        self.qapp.setApplicationName("Craft Crown")
        _mac_setup()

        self.config_path: Path = args.config or config.default_path()
        self.cfg = config.load(self.config_path)
        self.recent: list[AppInfo] = []
        self._timers: set = set()
        self._status = ("Starten…", False)
        self.settings: Optional[SettingsWindow] = None

        self.ui = OverlayUi(self.cfg["general"]["ring_size"])
        self.controller = Controller(self.cfg, ActionRunner(), self.ui, self._schedule,
                                     on_profile=self._profile_changed)

        self.bridge = _Bridge()
        self.bridge.event.connect(self.controller.handle_event)
        self.bridge.status.connect(self._status_changed)
        self.bridge.feel.connect(self.controller.set_scroll_feel)

        self.service: Optional[CrownService] = None
        if args.simulate:
            self.simulator = Simulator(self.controller.handle_event)
            self.simulator.show()
            self._status_changed("Simulatormodus (geen Craft nodig)", True)
        else:
            self.service = CrownService(self.bridge.event.emit, self.bridge.status.emit,
                                        scroll_mode=self.cfg["general"]["scroll_mode"],
                                        on_scroll_feel=self.bridge.feel.emit)

        self._save_timer = QTimer()
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save)

        self._app_timer = QTimer()
        self._app_timer.timeout.connect(self._poll_app)
        self._app_timer.start(APP_POLL_MS)

        self._build_tray()
        self.qapp.aboutToQuit.connect(self._shutdown)
        if args.settings or not QSystemTrayIcon.isSystemTrayAvailable():
            self.open_settings()

    # ------------------------------------------------------------------ plumbing

    def _schedule(self, ms: int, fn) -> _QtHandle:
        return _QtHandle(ms, fn, self._timers)

    def _poll_app(self) -> None:
        app = get_foreground_app()
        if app is None or app.is_self:
            return
        if not self.recent or self.recent[0] != app:
            self.recent = [app] + [a for a in self.recent if a.ident != app.ident][:RECENT_APPS - 1]
        self.controller.set_app(app)

    def _profile_changed(self, profile: Profile, app: Optional[AppInfo]) -> None:
        if hasattr(self, "tray"):
            self.tray.setToolTip(f"Craft Crown — {profile.name}")
        if self.settings is not None:
            self.settings.set_current(profile.name, app)

    def _status_changed(self, text: str, connected: bool) -> None:
        self._status = (text, connected)
        if hasattr(self, "tray"):
            self.tray.setIcon(tray_icon(connected))
            self.status_action.setText(text)
        if self.settings is not None:
            self.settings.set_status(text, connected)

    def _config_edited(self) -> None:
        self.controller.set_config(self.cfg)
        self.ui.ring.set_ring_size(self.cfg["general"]["ring_size"])
        if self.service is not None and self.service.scroll_mode != self.cfg["general"]["scroll_mode"]:
            self.service.set_scroll_mode(self.cfg["general"]["scroll_mode"])
        self._save_timer.start(SAVE_DELAY_MS)

    def _save(self) -> None:
        try:
            config.save(self.cfg, self.config_path)
        except OSError as exc:
            log.error("cannot save config: %s", exc)

    # ------------------------------------------------------------------ tray / windows

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(tray_icon(False))
        menu = QMenu()
        self.status_action = QAction(self._status[0], menu)
        self.status_action.setEnabled(False)
        menu.addAction(self.status_action)
        menu.addSeparator()
        menu.addAction("Instellingen…", self.open_settings)
        menu.addAction("Configuratie herladen", self.reload_config)
        menu.addSeparator()
        menu.addAction("Afsluiten", self.qapp.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.setToolTip("Craft Crown")
        self._menu = menu
        self.tray.show()
        self._status_changed(*self._status)

    def _tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) and sys.platform != "darwin":
            self.open_settings()

    def open_settings(self) -> None:
        if self.settings is None:
            self.settings = SettingsWindow(self.cfg, self._config_edited, lambda: list(self.recent))
            self.settings.set_status(*self._status)
            self.settings.set_current(self.controller.profile.name, self.controller.app)
        self.settings.show()
        self.settings.raise_()
        self.settings.activateWindow()

    def reload_config(self) -> None:
        fresh = config.load(self.config_path)
        self.cfg.clear()
        self.cfg.update(fresh)
        if self.settings is not None:
            self.settings.reload()
        self._config_edited()

    def _shutdown(self) -> None:
        if self._save_timer.isActive():
            self._save()
        if self.service is not None:
            self.service.stop()

    def run(self) -> int:
        if self.service is not None:
            self.service.start()
        if not _mac_accessibility_ok():
            self.tray.showMessage(
                "Craft Crown",
                "Geef toegang via Systeeminstellingen › Privacy en beveiliging › Toegankelijkheid, "
                "anders kunnen sneltoetsen niet verstuurd worden.")
        # Let Ctrl+C in a terminal stop the app.
        signal.signal(signal.SIGINT, lambda *_: self.qapp.quit())
        keepalive = QTimer()
        keepalive.timeout.connect(lambda: None)
        keepalive.start(250)
        return self.qapp.exec()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="craft-crown", description="Bestuur de crown van de Logitech Craft.")
    parser.add_argument("--simulate", action="store_true", help="geen Craft nodig: knoppen simuleren de crown")
    parser.add_argument("--settings", action="store_true", help="open meteen het instellingenvenster")
    parser.add_argument("--config", type=Path, help="pad naar config.json")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return CraftCrownApp(args).run()
