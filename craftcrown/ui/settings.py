"""Settings window: profiles per app, actions and the ring tools."""

from __future__ import annotations

import copy
from typing import Callable, Optional

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QPushButton, QSpinBox, QSplitter,
                               QTabWidget, QVBoxLayout, QWidget)

from ..actions import ACTION_TYPES, ROTATE, TRIGGER
from ..activeapp import AppInfo, list_running_apps
from ..controller import DEFAULT_SLOT_LABEL, RingView
from .overlay import paint_ring

INHERIT = "__inherit__"


class ActionEditor(QWidget):
    """Choose an action type and edit its parameters."""

    changed = Signal()

    def __init__(self, kind: str, inherit_label: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.kind = kind
        self._loading = False
        self._params: dict[str, QWidget] = {}
        self.type_box = QComboBox()
        if inherit_label:
            self.type_box.addItem(inherit_label, INHERIT)
        for t in ACTION_TYPES[kind].values():
            self.type_box.addItem(t.label, t.id)
        self.form = QFormLayout()
        self.form.setContentsMargins(16, 0, 0, 0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.type_box)
        layout.addLayout(self.form)
        self.type_box.currentIndexChanged.connect(self._type_changed)
        self._build_params({})

    def set_value(self, action: Optional[dict]) -> None:
        self._loading = True
        type_id = INHERIT if action is None and self.type_box.findData(INHERIT) >= 0 else \
            (action or {}).get("type", "none")
        idx = self.type_box.findData(type_id)
        self.type_box.setCurrentIndex(max(0, idx))
        self._build_params(action or {})
        self._loading = False

    def value(self) -> Optional[dict]:
        type_id = self.type_box.currentData()
        if type_id == INHERIT:
            return None
        out: dict = {"type": type_id}
        for name, w in self._params.items():
            if isinstance(w, QSpinBox):
                out[name] = w.value()
            elif isinstance(w, QCheckBox):
                out[name] = w.isChecked()
            else:
                out[name] = w.text().strip()
        return out

    def _type_changed(self) -> None:
        if self._loading:
            return
        self._build_params({})
        self.changed.emit()

    def _build_params(self, action: dict) -> None:
        while self.form.rowCount():
            self.form.removeRow(0)
        self._params.clear()
        t = ACTION_TYPES[self.kind].get(self.type_box.currentData())
        if t is None:
            return
        for p in t.params:
            value = action.get(p.name, p.default)
            if p.kind == "int":
                w = QSpinBox()
                w.setRange(1, 50)
                w.setValue(int(value or 1))
                w.valueChanged.connect(self._emit)
            elif p.kind == "bool":
                w = QCheckBox()
                w.setChecked(bool(value))
                w.toggled.connect(self._emit)
            else:
                w = QLineEdit(str(value or ""))
                if p.kind == "combo":
                    w.setPlaceholderText("bv. mod+shift+z  of  win=ctrl+y; mac=cmd+shift+z")
                w.textEdited.connect(self._emit)
            self._params[p.name] = w
            self.form.addRow(p.label, w)

    def _emit(self, *_args) -> None:
        if not self._loading:
            self.changed.emit()


class RingPreview(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.view = RingView("", [], 0, -1)
        self.setMinimumSize(220, 220)

    def set_view(self, view: RingView) -> None:
        self.view = view
        self.update()

    def paintEvent(self, _event) -> None:
        if not self.view.labels:
            return
        p = QPainter(self)
        size = min(self.width(), self.height())
        rect = QRectF((self.width() - size) / 2, (self.height() - size) / 2, size, size)
        paint_ring(p, rect, self.view)


class RingEditor(QWidget):
    """List of ring tools plus an editor for the selected tool."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tools: list[dict] = []
        self.title = ""
        self.default_slot = True
        self._loading = False

        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.NoDragDrop)
        self.list.currentRowChanged.connect(self._select)
        add, remove = QPushButton("+ Tool"), QPushButton("Verwijderen")
        up, down = QPushButton("▲"), QPushButton("▼")
        add.clicked.connect(self._add)
        remove.clicked.connect(self._remove)
        up.clicked.connect(lambda: self._move(-1))
        down.clicked.connect(lambda: self._move(1))
        buttons = QHBoxLayout()
        for b in (add, remove, up, down):
            buttons.addWidget(b)

        self.label_edit = QLineEdit()
        self.label_edit.textEdited.connect(self._edited)
        self.rotate = ActionEditor(ROTATE, "Geen (knop-tool)")
        self.rotate_pressed = ActionEditor(ROTATE, "Zelfde als draaien")
        self.select = ActionEditor(TRIGGER, "Niets")
        self.long_press = ActionEditor(TRIGGER, "Zoals profiel")
        for ed in (self.rotate, self.rotate_pressed, self.select, self.long_press):
            ed.changed.connect(self._edited)
        self.tool_box = QGroupBox("Geselecteerde tool")
        form = QFormLayout(self.tool_box)
        form.addRow("Naam", self.label_edit)
        form.addRow("Draaien", self.rotate)
        form.addRow("Ingedrukt draaien", self.rotate_pressed)
        form.addRow("Bij kiezen (knop-tool)", self.select)
        form.addRow("Lang drukken", self.long_press)

        self.preview = RingPreview()
        left = QVBoxLayout()
        left.addWidget(self.list, 1)
        left.addLayout(buttons)
        left.addWidget(self.preview, 1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(left, 2)
        layout.addWidget(self.tool_box, 3)

    def load(self, tools: list[dict], title: str, default_slot: bool) -> None:
        self.tools = tools
        self.title = title
        self.default_slot = default_slot
        self._refresh_list(0 if tools else -1)

    def _refresh_list(self, row: int) -> None:
        self._loading = True
        self.list.clear()
        for tool in self.tools:
            self.list.addItem(tool.get("label") or "Tool")
        self._loading = False
        self.list.setCurrentRow(row)
        self._select(row)
        self._update_preview()

    def _select(self, row: int) -> None:
        if self._loading:
            return
        valid = 0 <= row < len(self.tools)
        self.tool_box.setEnabled(valid)
        tool = self.tools[row] if valid else {}
        self._loading = True
        self.label_edit.setText(tool.get("label", ""))
        self.rotate.set_value(tool.get("rotate"))
        self.rotate_pressed.set_value(tool.get("rotate_pressed"))
        self.select.set_value(tool.get("select"))
        self.long_press.set_value(tool.get("long_press"))
        self._loading = False
        self._update_preview()

    def _edited(self, *_args) -> None:
        if self._loading:
            return
        row = self.list.currentRow()
        if not 0 <= row < len(self.tools):
            return
        tool = self.tools[row]
        tool["label"] = self.label_edit.text()
        tool["rotate"] = self.rotate.value()
        tool["rotate_pressed"] = self.rotate_pressed.value()
        tool["select"] = self.select.value()
        if tool["select"] and tool["select"].get("type") == "none":
            tool["select"] = None
        tool["long_press"] = self.long_press.value()
        self.list.item(row).setText(tool["label"] or "Tool")
        self._update_preview()
        self.changed.emit()

    def _add(self) -> None:
        self.tools.append({"label": "Nieuwe tool", "rotate": {"type": "volume"},
                           "rotate_pressed": None, "select": None, "long_press": None})
        self._refresh_list(len(self.tools) - 1)
        self.changed.emit()

    def _remove(self) -> None:
        row = self.list.currentRow()
        if 0 <= row < len(self.tools):
            del self.tools[row]
            self._refresh_list(min(row, len(self.tools) - 1))
            self.changed.emit()

    def _move(self, delta: int) -> None:
        row = self.list.currentRow()
        new = row + delta
        if 0 <= row < len(self.tools) and 0 <= new < len(self.tools):
            self.tools[row], self.tools[new] = self.tools[new], self.tools[row]
            self._refresh_list(new)
            self.changed.emit()

    def _update_preview(self) -> None:
        labels = ([DEFAULT_SLOT_LABEL] if self.default_slot else []) + \
            [t.get("label") or "Tool" for t in self.tools]
        offset = 1 if self.default_slot else 0
        row = self.list.currentRow()
        selected = row + offset if row >= 0 else 0
        self.preview.set_view(RingView(self.title, labels, selected, -1))


class ProfileEditor(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.profile: dict = {}
        self.is_default = True
        self.default_slot = True
        self._loading = False

        self.name_edit = QLineEdit()
        self.match_edit = QLineEdit()
        self.match_edit.setPlaceholderText("chrome.exe, com.google.Chrome")
        self.name_edit.textEdited.connect(self._edited)
        self.match_edit.textEdited.connect(self._edited)
        self.app_box = QGroupBox("App")
        app_form = QFormLayout(self.app_box)
        app_form.addRow("Naam", self.name_edit)
        app_form.addRow("Herkennen aan", self.match_edit)
        hint = QLabel("Windows: naam van het .exe-bestand · macOS: bundle-id. "
                      "Meerdere waarden scheiden met komma's.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray")
        app_form.addRow("", hint)

        self.editors: dict[str, ActionEditor] = {}
        crown_box = QGroupBox("Draaiknop")
        crown_form = QFormLayout(crown_box)
        for key, kind, label in (("rotate", ROTATE, "Draaien"),
                                 ("rotate_pressed", ROTATE, "Ingedrukt draaien"),
                                 ("tap", TRIGGER, "Tikken"),
                                 ("long_press", TRIGGER, "Lang drukken")):
            ed = ActionEditor(kind, "Zoals standaardprofiel")
            ed.changed.connect(self._edited)
            self.editors[key] = ed
            crown_form.addRow(label, ed)

        self.own_ring = QCheckBox("Eigen ring voor deze app (anders de ring van het standaardprofiel)")
        self.own_ring.toggled.connect(self._own_ring_toggled)
        self.ring = RingEditor()
        self.ring.changed.connect(self.changed)
        ring_box = QGroupBox("Ring (tik op de crown om te openen)")
        ring_layout = QVBoxLayout(ring_box)
        ring_layout.addWidget(self.own_ring)
        ring_layout.addWidget(self.ring)

        layout = QVBoxLayout(self)
        layout.addWidget(self.app_box)
        layout.addWidget(crown_box)
        layout.addWidget(ring_box, 1)

    def load(self, profile: dict, is_default: bool, default_ring: list[dict], default_slot: bool) -> None:
        self._loading = True
        self.profile = profile
        self.is_default = is_default
        self.default_ring = default_ring
        self.default_slot = default_slot
        self.app_box.setVisible(not is_default)
        self.own_ring.setVisible(not is_default)
        self.name_edit.setText(profile.get("name", ""))
        self.match_edit.setText(", ".join(profile.get("match", [])))
        for key, ed in self.editors.items():
            # The inherit option only makes sense for app profiles.
            inherit_idx = ed.type_box.findData(INHERIT)
            if inherit_idx >= 0:
                ed.type_box.model().item(inherit_idx).setEnabled(not is_default)
            ed.set_value(profile.get(key) if (profile.get(key) is not None or not is_default)
                         else {"type": "none"})
        has_ring = is_default or profile.get("ring") is not None
        self.own_ring.setChecked(has_ring)
        self._load_ring()
        self._loading = False

    def _load_ring(self) -> None:
        if self.is_default or self.profile.get("ring") is not None:
            self.ring.setEnabled(True)
            self.ring.load(self.profile["ring"], self._title(), self.default_slot)
        else:
            self.ring.setEnabled(False)
            self.ring.load(copy.deepcopy(self.default_ring), self._title(), self.default_slot)

    def _title(self) -> str:
        return "Alle apps" if self.is_default else self.profile.get("name", "")

    def _own_ring_toggled(self, on: bool) -> None:
        if self._loading or self.is_default:
            return
        self.profile["ring"] = copy.deepcopy(self.default_ring) if on else None
        self._load_ring()
        self.changed.emit()

    def _edited(self, *_args) -> None:
        if self._loading:
            return
        if not self.is_default:
            self.profile["name"] = self.name_edit.text().strip() or "App"
            self.profile["match"] = [m.strip() for m in self.match_edit.text().split(",") if m.strip()]
        for key, ed in self.editors.items():
            self.profile[key] = ed.value()
        self.changed.emit()


class AddAppDialog(QDialog):
    def __init__(self, recent: list[AppInfo], parent=None):
        super().__init__(parent)
        self.setWindowTitle("App toevoegen")
        self.list = QListWidget()
        seen = set()
        for app in recent + list_running_apps():
            if app.ident.lower() in seen:
                continue
            seen.add(app.ident.lower())
            item = QListWidgetItem(f"{app.name}   ({app.ident})")
            item.setData(Qt.UserRole, app)
            self.list.addItem(item)
        self.list.currentItemChanged.connect(self._picked)
        self.name = QLineEdit()
        self.match = QLineEdit()
        self.match.setPlaceholderText("bv. chrome.exe, com.google.Chrome")
        form = QFormLayout()
        form.addRow("Naam", self.name)
        form.addRow("Herkennen aan", self.match)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Kies een app die (recent) open stond, of vul ze zelf in:"))
        layout.addWidget(self.list, 1)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.resize(460, 420)

    def _picked(self, item) -> None:
        if item is None:
            return
        app: AppInfo = item.data(Qt.UserRole)
        self.name.setText(app.name)
        self.match.setText(app.ident)

    def result_profile(self) -> Optional[dict]:
        match = [m.strip() for m in self.match.text().split(",") if m.strip()]
        if not match:
            return None
        return {"name": self.name.text().strip() or match[0], "match": match,
                "rotate": None, "rotate_pressed": None, "tap": None, "long_press": None, "ring": None}


class GeneralTab(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.general: dict = {}
        self._loading = False
        self.timeout = QSpinBox()
        self.timeout.setRange(1000, 30000)
        self.timeout.setSingleStep(500)
        self.timeout.setSuffix(" ms")
        self.long_press = QSpinBox()
        self.long_press.setRange(200, 2000)
        self.long_press.setSingleStep(50)
        self.long_press.setSuffix(" ms")
        self.per_tick = QSpinBox()
        self.per_tick.setRange(1, 100)
        self.ring_size = QSpinBox()
        self.ring_size.setRange(200, 700)
        self.ring_size.setSuffix(" px")
        self.hud = QCheckBox("Toon een indicator bij draaien met een ring-tool")
        self.default_slot = QCheckBox("Eerste plek in de ring = \"Standaard\" (terug naar normaal draaien)")
        self.mode = QComboBox()
        for value, label in (("auto", "Niet wijzigen"), ("ratchet", "Ratel (klikjes)"),
                             ("smooth", "Vloeiend")):
            self.mode.addItem(label, value)
        form = QFormLayout(self)
        form.addRow("Gevoel van de crown", self.mode)
        form.addRow("Ring sluit na", self.timeout)
        form.addRow("Lang drukken na", self.long_press)
        form.addRow("Vloeiend: eenheden per stap", self.per_tick)
        form.addRow("Grootte van de ring", self.ring_size)
        form.addRow("", self.hud)
        form.addRow("", self.default_slot)
        for w in (self.timeout, self.long_press, self.per_tick, self.ring_size):
            w.valueChanged.connect(self._edited)
        self.hud.toggled.connect(self._edited)
        self.default_slot.toggled.connect(self._edited)
        self.mode.currentIndexChanged.connect(self._edited)

    def load(self, general: dict) -> None:
        self._loading = True
        self.general = general
        self.timeout.setValue(general["ring_timeout_ms"])
        self.long_press.setValue(general["long_press_ms"])
        self.per_tick.setValue(general["rotation_per_tick"])
        self.ring_size.setValue(general["ring_size"])
        self.hud.setChecked(general["show_hud"])
        self.default_slot.setChecked(general["ring_default_slot"])
        self.mode.setCurrentIndex(max(0, self.mode.findData(general["scroll_mode"])))
        self._loading = False

    def _edited(self, *_args) -> None:
        if self._loading:
            return
        self.general.update({
            "ring_timeout_ms": self.timeout.value(),
            "long_press_ms": self.long_press.value(),
            "rotation_per_tick": self.per_tick.value(),
            "ring_size": self.ring_size.value(),
            "show_hud": self.hud.isChecked(),
            "ring_default_slot": self.default_slot.isChecked(),
            "scroll_mode": self.mode.currentData(),
        })
        self.changed.emit()


class SettingsWindow(QWidget):
    """Edits ``cfg`` in place and calls ``on_change`` after every edit."""

    def __init__(self, cfg: dict, on_change: Callable[[], None],
                 recent_apps: Callable[[], list[AppInfo]]):
        super().__init__()
        self.setWindowTitle("Craft Crown — instellingen")
        self.cfg = cfg
        self.on_change = on_change
        self.recent_apps = recent_apps

        self.status = QLabel("…")
        self.status.setWordWrap(True)
        self.current = QLabel("")
        self.current.setStyleSheet("color: gray")
        self.activity = QLabel("Crown-test: nog geen signalen van de crown ontvangen — draai of tik eens.")
        self.activity.setWordWrap(True)
        self.permission = QLabel("")
        self.permission.setWordWrap(True)
        self.permission.setStyleSheet("color: #c0392b; font-weight: bold")
        self.permission.hide()

        self.profiles = QListWidget()
        self.profiles.currentRowChanged.connect(self._profile_selected)
        add, remove = QPushButton("+ App"), QPushButton("Verwijderen")
        add.clicked.connect(self._add_app)
        remove.clicked.connect(self._remove_app)
        self.remove_btn = remove
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.profiles, 1)
        row = QHBoxLayout()
        row.addWidget(add)
        row.addWidget(remove)
        left_layout.addLayout(row)

        self.editor = ProfileEditor()
        self.editor.changed.connect(self._profile_edited)
        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(self.editor)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 760])

        self.general = GeneralTab()
        self.general.changed.connect(self._general_edited)
        tabs = QTabWidget()
        tabs.addTab(splitter, "Profielen")
        tabs.addTab(self.general, "Algemeen")

        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(self.current)
        layout.addWidget(self.permission)
        layout.addWidget(self.activity)
        layout.addWidget(tabs, 1)
        self.resize(1000, 760)
        self.reload()

    def reload(self) -> None:
        self.general.load(self.cfg["general"])
        self._fill_profiles(0)

    def set_status(self, text: str, connected: bool) -> None:
        dot = "🟢" if connected else "🔴"
        self.status.setText(f"{dot}  {text}")

    def set_activity(self, text: str) -> None:
        self.activity.setText(f"Crown-test: {text}")

    def set_permission_warning(self, text: str) -> None:
        self.permission.setText(text)
        self.permission.setVisible(bool(text))

    def set_current(self, profile_name: str, app: Optional[AppInfo]) -> None:
        where = f"{app.name} ({app.ident})" if app else "—"
        self.current.setText(f"Actieve app: {where}   ·   profiel: {profile_name}")

    def _fill_profiles(self, row: int) -> None:
        self.profiles.blockSignals(True)
        self.profiles.clear()
        self.profiles.addItem("★  Alle apps (standaard)")
        for app in self.cfg["apps"]:
            self.profiles.addItem(app["name"])
        self.profiles.blockSignals(False)
        self.profiles.setCurrentRow(min(row, self.profiles.count() - 1))
        self._profile_selected(self.profiles.currentRow())

    def _profile_selected(self, row: int) -> None:
        if row < 0:
            return
        is_default = row == 0
        profile = self.cfg["default"] if is_default else self.cfg["apps"][row - 1]
        self.remove_btn.setEnabled(not is_default)
        self.editor.load(profile, is_default, self.cfg["default"]["ring"],
                         self.cfg["general"]["ring_default_slot"])

    def _profile_edited(self) -> None:
        row = self.profiles.currentRow()
        if row > 0:
            self.profiles.item(row).setText(self.cfg["apps"][row - 1]["name"])
        self.on_change()

    def _general_edited(self) -> None:
        self._profile_selected(self.profiles.currentRow())
        self.on_change()

    def _add_app(self) -> None:
        dlg = AddAppDialog(self.recent_apps(), self)
        if dlg.exec() != QDialog.Accepted:
            return
        profile = dlg.result_profile()
        if profile is None:
            QMessageBox.warning(self, "App toevoegen", "Vul minstens één herkenningswaarde in.")
            return
        self.cfg["apps"].append(profile)
        self._fill_profiles(len(self.cfg["apps"]))
        self.on_change()

    def _remove_app(self) -> None:
        row = self.profiles.currentRow()
        if row <= 0:
            return
        name = self.cfg["apps"][row - 1]["name"]
        if QMessageBox.question(self, "Verwijderen", f"Profiel '{name}' verwijderen?") != QMessageBox.Yes:
            return
        del self.cfg["apps"][row - 1]
        self._fill_profiles(row - 1)
        self.on_change()

