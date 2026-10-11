"""On-screen ring and HUD.

Both are click-through, never take focus (otherwise synthesized shortcuts
would go to us instead of the app) and float above everything.
"""

from __future__ import annotations

import math
import sys
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, QPropertyAnimation
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from ..controller import RingView

ACCENT = QColor(0, 184, 252)
DISC = QColor(24, 24, 28, 232)
SEGMENT = QColor(48, 48, 56, 255)
TEXT = QColor(235, 235, 240)
MUTED = QColor(160, 160, 170)


def paint_ring(p: QPainter, rect: QRectF, view: RingView) -> None:
    """Draw the ring into ``rect``; shared by the overlay and the settings preview."""
    p.setRenderHint(QPainter.Antialiasing)
    size = min(rect.width(), rect.height())
    c = rect.center()
    outer = size / 2 - 4
    inner = outer * 0.52
    p.setPen(Qt.NoPen)
    p.setBrush(DISC)
    p.drawEllipse(c, outer + 2, outer + 2)

    n = max(1, len(view.labels))
    gap = 2.0 if n > 1 else 0.0
    span = 360.0 / n
    font = QFont()
    font.setPixelSize(max(10, int(size / 30)))
    font.setBold(True)
    fm = QFontMetrics(font)
    p.setFont(font)
    for i, label in enumerate(view.labels):
        # Slot 0 at the top, going clockwise (Qt angles are counter-clockwise from 3 o'clock).
        start = 90 - i * span - span / 2
        path = _sector(c, inner, outer - 3, start + gap / 2, span - gap)
        selected = i == view.selected
        p.setBrush(ACCENT if selected else SEGMENT)
        p.drawPath(path)
        mid = math.radians(start + span / 2)
        r = (inner + outer) / 2
        pos = QPointF(c.x() + r * math.cos(mid), c.y() - r * math.sin(mid))
        if i == view.active:
            p.setBrush(TEXT if selected else ACCENT)
            dot_r = (outer - 3 - r) * 0.18 + 2
            p.drawEllipse(QPointF(c.x() + (outer - 12) * math.cos(mid),
                                  c.y() - (outer - 12) * math.sin(mid)), dot_r, dot_r)
        max_w = min(2 * math.pi * r / n - 8, (outer - inner) * 1.6)
        text = fm.elidedText(label, Qt.ElideRight, int(max_w))
        p.setPen(QColor(10, 10, 12) if selected else TEXT)
        p.drawText(QRectF(pos.x() - max_w / 2, pos.y() - fm.height(), max_w, fm.height() * 2),
                   Qt.AlignCenter, text)
        p.setPen(Qt.NoPen)

    # Center: selected label + profile name
    big = QFont(font)
    big.setPixelSize(max(12, int(size / 17)))
    p.setFont(big)
    p.setPen(TEXT)
    label = view.labels[view.selected] if view.labels else ""
    p.drawText(QRectF(c.x() - inner, c.y() - inner * 0.45, inner * 2, inner * 0.6),
               Qt.AlignCenter, QFontMetrics(big).elidedText(label, Qt.ElideRight, int(inner * 1.7)))
    small = QFont(font)
    small.setBold(False)
    small.setPixelSize(max(9, int(size / 34)))
    p.setFont(small)
    p.setPen(MUTED)
    p.drawText(QRectF(c.x() - inner, c.y() + inner * 0.1, inner * 2, inner * 0.4),
               Qt.AlignCenter, QFontMetrics(small).elidedText(view.title, Qt.ElideRight, int(inner * 1.6)))


def _sector(c: QPointF, r_in: float, r_out: float, start_deg: float, span_deg: float) -> QPainterPath:
    path = QPainterPath()
    outer = QRectF(c.x() - r_out, c.y() - r_out, 2 * r_out, 2 * r_out)
    inner = QRectF(c.x() - r_in, c.y() - r_in, 2 * r_in, 2 * r_in)
    path.arcMoveTo(outer, start_deg)
    path.arcTo(outer, start_deg, span_deg)
    path.arcTo(inner, start_deg + span_deg, -span_deg)
    path.closeSubpath()
    return path


def _make_overlay(widget: QWidget) -> None:
    widget.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                          | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
    widget.setAttribute(Qt.WA_TranslucentBackground)
    widget.setAttribute(Qt.WA_ShowWithoutActivating)
    if sys.platform == "darwin":
        widget.setAttribute(Qt.WA_MacAlwaysShowToolWindow)


def _float_everywhere(widget: QWidget) -> None:
    """macOS: also show above full-screen apps and on every Space."""
    if sys.platform != "darwin":
        return
    try:
        import objc  # type: ignore
        from AppKit import (NSStatusWindowLevel,  # type: ignore
                            NSWindowCollectionBehaviorCanJoinAllSpaces,
                            NSWindowCollectionBehaviorFullScreenAuxiliary)
        view = objc.objc_object(c_void_p=int(widget.winId()))
        window = view.window()
        window.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces
                                      | NSWindowCollectionBehaviorFullScreenAuxiliary)
        window.setLevel_(NSStatusWindowLevel)
    except Exception:  # noqa: BLE001 - cosmetic only
        pass


def _center_on_cursor_screen(widget: QWidget) -> None:
    screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
    geo = screen.availableGeometry()
    widget.move(geo.center().x() - widget.width() // 2, geo.center().y() - widget.height() // 2)


class RingOverlay(QWidget):
    def __init__(self, size: int = 340):
        super().__init__()
        _make_overlay(self)
        self.view: Optional[RingView] = None
        self.set_ring_size(size)
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(140)
        self._fade.finished.connect(self._fade_done)
        self._prepared = False

    def set_ring_size(self, size: int) -> None:
        self.setFixedSize(size, size)

    def show_view(self, view: RingView) -> None:
        self.view = view
        if not self.isVisible() or self._fade.endValue() == 0.0:
            _center_on_cursor_screen(self)
            self._fade.stop()
            self.setWindowOpacity(0.0)
            self.show()
            if not self._prepared:
                _float_everywhere(self)
                self._prepared = True
            self._fade.setStartValue(0.0)
            self._fade.setEndValue(1.0)
            self._fade.start()
        self.update()

    @property
    def is_open(self) -> bool:
        return self.isVisible() and self._fade.endValue() != 0.0

    def hide_animated(self) -> None:
        if not self.isVisible():
            return
        self._fade.stop()
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.start()

    def _fade_done(self) -> None:
        if self._fade.endValue() == 0.0:
            self.hide()

    def paintEvent(self, _event) -> None:
        if self.view is None:
            return
        p = QPainter(self)
        paint_ring(p, QRectF(self.rect()), self.view)


class Hud(QWidget):
    """Small transient indicator: which tool is active / what just happened."""

    def __init__(self):
        super().__init__()
        _make_overlay(self)
        self.setFixedSize(220, 220)
        self.title = ""
        self.detail = ""
        self.direction = 0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._start_fade)
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(250)
        self._fade.setStartValue(1.0)
        self._fade.setEndValue(0.0)
        self._fade.finished.connect(self.hide)
        self._prepared = False

    def show_message(self, title: str, detail: str, direction: int, ms: int = 900) -> None:
        self.title, self.detail, self.direction = title, detail, direction
        self._fade.stop()
        self.setWindowOpacity(1.0)
        if not self.isVisible():
            _center_on_cursor_screen(self)
            self.show()
            if not self._prepared:
                _float_everywhere(self)
                self._prepared = True
        self.update()
        self._timer.start(ms)

    def _start_fade(self) -> None:
        self._fade.start()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(10, 10, -10, -10)
        c = r.center()
        radius = r.width() / 2
        p.setPen(Qt.NoPen)
        p.setBrush(DISC)
        p.drawEllipse(c, radius, radius)
        p.setPen(QPen(ACCENT, 6, Qt.SolidLine, Qt.RoundCap))
        if self.direction:
            # Arc that points in the turning direction.
            span = -70 if self.direction > 0 else 70
            p.drawArc(r.adjusted(8, 8, -8, -8), 90 * 16, span * 16)
        else:
            p.drawEllipse(c, radius - 8, radius - 8)
        f = QFont()
        f.setBold(True)
        f.setPixelSize(22)
        p.setFont(f)
        p.setPen(TEXT)
        p.drawText(QRectF(r.left() + 16, c.y() - 30, r.width() - 32, 34), Qt.AlignCenter,
                   QFontMetrics(f).elidedText(self.title, Qt.ElideRight, int(r.width() - 36)))
        f.setBold(False)
        f.setPixelSize(13)
        p.setFont(f)
        p.setPen(MUTED)
        p.drawText(QRectF(r.left() + 20, c.y() + 6, r.width() - 40, 36), Qt.AlignCenter | Qt.TextWordWrap,
                   self.detail)


class OverlayUi:
    """Implements the controller's ``Ui`` protocol with the widgets above."""

    def __init__(self, ring_size: int = 340):
        self.ring = RingOverlay(ring_size)
        self.hud = Hud()

    def show_ring(self, view: RingView) -> None:
        self.hud.hide()
        self.ring.show_view(view)

    def hide_ring(self) -> None:
        self.ring.hide_animated()

    def show_hud(self, title: str, detail: str, direction: int) -> None:
        if self.ring.is_open:
            return
        self.hud.show_message(title, detail, direction)
