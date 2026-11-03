"""Saved positions: a table of presets, and the dialogs to save one and to go to one.

Presets store controller coordinates under the stage keys. Everything shown here goes
through each stage's ``View``, so a reversed stage shows the same numbers in the
table, the dialogs, and its card.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from stagehand.axes import Axis
from stagehand.gui import theme
from stagehand.gui.icons import blank, icon
from stagehand.presets import Preset, PresetError, PresetStore
from stagehand.stage import State
from stagehand.worker import StageStatus, StageWorker

MATCH_TOLERANCE = 0.001  # a stage is "at" a preset position within this distance


@dataclass(frozen=True)
class View:
    """How one stage is shown: its name, digits, units, and direction."""

    label: str
    decimals: int = 4
    units: str = "mm"
    axis: Axis | None = None

    def value(self, device: float) -> float:
        return self.axis.to_display(device) if self.axis else device

    def text(self, device: float) -> str:
        return f"{self.value(device):.{self.decimals}f}"

    def change(self, now: float, target: float) -> str:
        return f"{self.value(target) - self.value(now):+.{self.decimals}f}"

    def range_text(self, low: float, high: float) -> str:
        a, b = sorted((self.value(low), self.value(high)))
        return f"{a:g} to {b:g} {self.units}"


def at_preset(preset: Preset, statuses: dict[str, StageStatus]) -> bool:
    """Every stage in the preset is connected and at its saved position."""
    for name, value in preset.positions.items():
        status = statuses.get(name)
        if status is None or status.position is None or not status.state.connected:
            return False
        if abs(status.position - value) > MATCH_TOLERANCE:
            return False
    return True


def blocked_reason(status: StageStatus, target: float, view: View | None = None) -> str:
    """Why a stage cannot go to ``target`` now, or "" if it can."""
    if not status.state.connected:
        return "not connected"
    if status.state is State.NOT_HOMED:
        return "not homed"
    if status.state.busy:
        return "moving"
    if status.state is not State.READY:
        return status.state.value.lower()
    if status.allowed is None:
        return "no allowed range"
    low, high = status.allowed
    if not low - 1e-6 <= target <= high + 1e-6:
        shown = (view or View("")).range_text(low, high)
        return f"outside {shown}"
    return ""


def _views(views: dict[str, View] | None, keys: Sequence[str]) -> dict[str, View]:
    views = dict(views or {})
    for key in keys:
        views.setdefault(key, View(label=key))
    return views


class SavePresetDialog(QDialog):
    """Name the preset and pick the stages it stores. Stages that cannot be saved are shown."""

    def __init__(
        self,
        statuses: Sequence[StageStatus],
        parent: QWidget,
        views: dict[str, View] | None = None,
    ):
        super().__init__(parent)
        views = _views(views, [s.name for s in statuses])
        self.setWindowTitle("Save preset")
        self.setMinimumWidth(360)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("For example: Odor port in")
        self.checks: dict[str, QCheckBox] = {}
        self._positions: dict[str, float] = {}

        grid = QGridLayout()
        grid.setHorizontalSpacing(theme.SECTION_GAP)
        grid.setVerticalSpacing(theme.GAP)
        for row, status in enumerate(statuses):
            view = views[status.name]
            check = QCheckBox(view.label)
            usable = (
                status.position is not None
                and status.state.connected
                and status.state not in (State.NOT_HOMED, State.ERROR)
                and not status.state.busy
            )
            if usable:
                assert status.position is not None
                self._positions[status.name] = status.position
                note = QLabel(f"{view.text(status.position)} {view.units}")
                note.setFont(theme.number_font(theme.BODY_PX))
                note.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                check.setChecked(True)
            else:
                reason = "moving" if status.state.busy else status.state.value.lower()
                note = QLabel(f"not saved: {reason}")
                note.setObjectName("secondary")
                check.setEnabled(False)
            check.toggled.connect(self._validate)
            self.checks[status.name] = check
            grid.addWidget(check, row, 0)
            grid.addWidget(note, row, 1)
        grid.setColumnStretch(0, 1)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Save).setObjectName("primary")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.name_edit.textChanged.connect(self._validate)

        heading = QLabel("Stages to store")
        heading.setObjectName("secondary")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.MARGIN, theme.MARGIN, theme.MARGIN, theme.MARGIN)
        layout.setSpacing(theme.GAP)
        layout.addWidget(QLabel("Preset name"))
        layout.addWidget(self.name_edit)
        layout.addSpacing(theme.UNIT)
        layout.addWidget(heading)
        layout.addLayout(grid)
        layout.addSpacing(theme.UNIT)
        layout.addWidget(self.buttons)
        self._validate()

    def _validate(self) -> None:
        ok = bool(self.name_edit.text().strip()) and bool(self.positions())
        self.buttons.button(QDialogButtonBox.Save).setEnabled(ok)

    def name(self) -> str:
        return self.name_edit.text()

    def positions(self) -> dict[str, float]:
        return {n: p for n, p in self._positions.items() if self.checks[n].isChecked()}


class GoToPresetDialog(QDialog):
    """Show each move (now, target, change) and let the user leave stages out."""

    def __init__(
        self,
        preset: Preset,
        statuses: dict[str, StageStatus],
        parent: QWidget,
        views: dict[str, View] | None = None,
    ):
        super().__init__(parent)
        views = _views(views, list(preset.positions))
        self.setWindowTitle("Go to preset")
        self.checks: dict[str, QCheckBox] = {}
        self._targets: dict[str, float] = {}

        title = QLabel(f"Go to “{preset.name}”?")
        title.setFont(theme.font(theme.TITLE_PX, bold=True))
        grid = QGridLayout()
        grid.setHorizontalSpacing(theme.SECTION_GAP)
        grid.setVerticalSpacing(theme.GAP)
        for column, heading in enumerate(("Stage", "Now", "Target", "Change")):
            label = QLabel(heading)
            label.setObjectName("secondary")
            if column:
                label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid.addWidget(label, 0, column)
        for row, (name, target) in enumerate(preset.positions.items(), start=1):
            view = views[name]
            check = QCheckBox(view.label)
            status = statuses.get(name)
            reason = "not in this rig" if status is None else blocked_reason(status, target, view)
            known = status is not None and status.position is not None
            now = view.text(status.position) if known else "—"
            change = view.change(status.position, target) if known else ""
            if reason:
                check.setEnabled(False)
            else:
                check.setChecked(True)
                self._targets[name] = target
            check.toggled.connect(self._validate)
            self.checks[name] = check
            grid.addWidget(check, row, 0)
            for column, text in enumerate((now, view.text(target)), start=1):
                cell = QLabel(text)
                cell.setFont(theme.number_font(theme.BODY_PX))
                cell.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                grid.addWidget(cell, row, column)
            last = QLabel(reason or change)
            last.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if reason:
                last.setObjectName("error")
            else:
                last.setFont(theme.number_font(theme.BODY_PX))
            grid.addWidget(last, row, 3)

        note = QLabel("The checked stages move at the same time. Make sure their paths are clear.")
        note.setObjectName("secondary")
        note.setWordWrap(True)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.move_button = self.buttons.addButton("Move", QDialogButtonBox.AcceptRole)
        self.move_button.setObjectName("primary")
        self.move_button.setIcon(icon("go", theme.PANEL))
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.MARGIN, theme.MARGIN, theme.MARGIN, theme.MARGIN)
        layout.setSpacing(theme.SECTION_GAP)
        layout.addWidget(title)
        layout.addLayout(grid)
        layout.addWidget(note)
        layout.addWidget(self.buttons)
        self._validate()

    def _validate(self) -> None:
        self.move_button.setEnabled(bool(self.targets()))

    def targets(self) -> dict[str, float]:
        return {n: t for n, t in self._targets.items() if self.checks[n].isChecked()}


def saved_text(iso: str) -> str:
    """'2026-11-03T15:33:10' -> '3 Nov 15:33' (the year only if it is not this year)."""
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    year = "" if moment.year == datetime.now().year else f" {moment.year}"
    return f"{moment.day} {moment:%b}{year} {moment:%H:%M}"


class PresetsPanel(QFrame):
    message = Signal(str)

    def __init__(
        self, store: PresetStore, workers: Sequence[StageWorker], *, confirm_moves: bool
    ) -> None:
        super().__init__()
        self.setObjectName("card")
        self.store = store
        self.workers = {w.name: w for w in workers}
        self.order = [w.name for w in workers]  # column order: follows the cards
        self.axes: dict[str, Axis] = {}
        self._confirm = confirm_moves
        self._statuses: dict[str, StageStatus] = {}
        self._current: set[str] = set()

        title = QLabel("Presets")
        title.setFont(theme.font(theme.TITLE_PX, bold=True))
        self.save_button = QPushButton(icon("save", theme.PANEL), "Save current…")
        self.save_button.setObjectName("primary")
        self.save_button.setToolTip("Save the positions of the stages under a name.")
        self.save_button.clicked.connect(self.save_current)
        self.go_button = QPushButton(icon("go"), "Go to")
        self.go_button.setToolTip("Move the stages to the selected preset (double-click a row).")
        self.go_button.clicked.connect(self.go_to_selected)
        self.rename_button = QPushButton(icon("rename"), "Rename")
        self.rename_button.clicked.connect(self.rename_selected)
        self.up_button = QPushButton(icon("move-up"), "")
        self.up_button.setToolTip("Move the preset up the list")
        self.up_button.clicked.connect(lambda: self._move_selected(-1))
        self.down_button = QPushButton(icon("move-down"), "")
        self.down_button.setToolTip("Move the preset down the list")
        self.down_button.clicked.connect(lambda: self._move_selected(+1))
        self.delete_button = QPushButton(icon("delete"), "Delete")
        self.delete_button.clicked.connect(self.delete_selected)

        self.table = QTableWidget(0, len(self.order) + 2)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setFocusPolicy(Qt.StrongFocus)
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.setMinimumHeight(36 + 3 * 32)  # the header and three presets
        self.table.horizontalHeader().setHighlightSections(False)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.cellDoubleClicked.connect(lambda _r, _c: self.go_to_selected())

        self.error_label = QLabel(store.load_error)
        self.error_label.setObjectName("error")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(bool(store.load_error))
        self.empty_label = QLabel(
            "No presets yet. Move the stages where you want them, then press Save current."
        )
        self.empty_label.setObjectName("secondary")
        self.empty_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.MARGIN, theme.MARGIN, theme.MARGIN, theme.MARGIN)
        layout.setSpacing(theme.GAP)
        top = QHBoxLayout()
        top.setSpacing(theme.GAP)
        top.addWidget(title)
        top.addStretch(1)
        for button in (
            self.go_button,
            self.rename_button,
            self.up_button,
            self.down_button,
            self.delete_button,
        ):
            top.addWidget(button)
        top.addSpacing(theme.UNIT)
        top.addWidget(self.save_button)
        layout.addLayout(top)
        layout.addWidget(self.error_label)
        layout.addWidget(self.empty_label)
        layout.addWidget(self.table, 1)
        self.refresh()

    # -- display ----------------------------------------------------------------------------

    def views(self) -> dict[str, View]:
        return {
            key: View(
                label=worker.label,
                decimals=worker.config.decimals,
                units=worker.config.units,
                axis=self.axes.get(key),
            )
            for key, worker in self.workers.items()
        }

    def set_order(self, order: Sequence[str]) -> None:
        """Show the stage columns in the order of the cards."""
        self.order = list(order)
        self.refresh()

    def set_axes(self, axes: dict[str, Axis]) -> None:
        self.axes = dict(axes)
        self.refresh()

    def refresh(self, select: str | None = None) -> None:
        select = select or self.selected_name()
        views = self.views()
        names = self.order
        last = len(names) + 1
        self.table.setColumnCount(last + 1)
        headers = [("Preset", Qt.AlignLeft)]
        headers += [(views[n].label, Qt.AlignRight) for n in names]
        headers += [("Saved", Qt.AlignLeft)]
        for column, (text, side) in enumerate(headers):
            item = QTableWidgetItem(text)
            item.setTextAlignment(side | Qt.AlignVCenter)
            self.table.setHorizontalHeaderItem(column, item)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, last + 1):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)

        soft = QBrush(QColor(theme.INK_SOFT))
        self.table.setRowCount(len(self.store.presets))
        for row, preset in enumerate(self.store.presets):
            item = QTableWidgetItem(preset.name)
            item.setData(Qt.UserRole, preset.name)
            self.table.setItem(row, 0, item)
            for column, stage in enumerate(names, start=1):
                value = preset.positions.get(stage)
                cell = QTableWidgetItem("—" if value is None else views[stage].text(value))
                cell.setFont(theme.number_font(theme.BODY_PX))
                cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if value is None:
                    cell.setForeground(soft)
                    cell.setToolTip(f"This preset does not move {views[stage].label}.")
                self.table.setItem(row, column, cell)
            saved = QTableWidgetItem(saved_text(preset.saved))
            saved.setForeground(soft)
            saved.setToolTip(preset.saved.replace("T", " "))
            self.table.setItem(row, last, saved)
            if preset.name == select:
                self.table.selectRow(row)
        # The name column stretches, but never below its longest name: the table asks for
        # enough width, so Qt never shortens a name with "…".
        needed = max(self.table.sizeHintForColumn(0), header.sectionSizeHint(0))
        needed += sum(
            max(self.table.sizeHintForColumn(c), header.sectionSizeHint(c))
            for c in range(1, last + 1)
        )
        self.table.setMinimumWidth(needed + 2 * self.table.frameWidth() + 4)
        empty = not self.store.presets
        self.empty_label.setVisible(empty)
        self.table.setToolTip("" if empty else "Double-click a preset to go to it.")
        self._current = set()
        self.show_statuses(self._statuses, force=True)
        self._update_buttons()

    def show_statuses(self, statuses: dict[str, StageStatus], *, force: bool = False) -> None:
        """Mark the presets that the stages are at now."""
        self._statuses = statuses
        current = {p.name for p in self.store.presets if at_preset(p, statuses)}
        if current == self._current and not force:
            self._update_buttons()
            return
        self._current = current
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            here = item.data(Qt.UserRole) in current
            item.setIcon(icon("here", theme.PINK) if here else blank())
            item.setToolTip("The stages are at this preset now." if here else "")
            font = item.font()
            font.setBold(here)
            item.setFont(font)
        self._update_buttons()

    def selected_name(self) -> str | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.table.item(rows[0].row(), 0).data(Qt.UserRole)

    def _update_buttons(self) -> None:
        selected = self.selected_name() is not None
        writable = self.store.writable
        any_position = any(  # the same rule as SavePresetDialog
            s.position is not None
            and s.state.connected
            and s.state not in (State.NOT_HOMED, State.ERROR)
            and not s.state.busy
            for s in self._statuses.values()
        )
        self.save_button.setEnabled(writable and any_position)
        self.go_button.setEnabled(selected)
        for button in (self.rename_button, self.up_button, self.down_button, self.delete_button):
            button.setEnabled(selected and writable)

    # -- actions ----------------------------------------------------------------------------

    def save_current(self) -> None:
        statuses = [self.workers[name].status for name in self.order]
        dialog = SavePresetDialog(statuses, self, self.views())
        selected = self.selected_name()
        if selected:
            dialog.name_edit.setText(selected)
            dialog.name_edit.selectAll()
        if dialog.exec() != QDialog.Accepted:
            return
        name = " ".join(dialog.name().split())
        if self.store.get(name) is not None:
            answer = QMessageBox.question(
                self,
                "Replace preset",
                f"Replace the positions saved in “{name}”?",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                return
        try:
            preset = self.store.save(name, dialog.positions())
        except PresetError as error:
            QMessageBox.warning(self, "Save preset", str(error))
            return
        self.refresh(select=preset.name)
        self.message.emit(f"Saved preset “{preset.name}”.")

    def go_to_selected(self) -> None:
        name = self.selected_name()
        preset = self.store.get(name) if name else None
        if preset is None:
            return
        statuses = {n: w.status for n, w in self.workers.items()}
        views = self.views()
        dialog = GoToPresetDialog(preset, statuses, self, views)
        if self._confirm and dialog.exec() != QDialog.Accepted:
            return
        targets = dialog.targets()
        if not targets:
            QMessageBox.information(self, "Go to preset", "No stage in this preset can move now.")
            return
        for stage, target in targets.items():
            self.workers[stage].move_to(target)  # controller coordinates, as stored
        skipped = sorted(
            views[s].label if s in views else s for s in set(preset.positions) - set(targets)
        )
        note = f" Not moved: {', '.join(skipped)}." if skipped else ""
        self.message.emit(f"Going to preset “{preset.name}”.{note}")

    def rename_selected(self) -> None:
        old = self.selected_name()
        if old is None:
            return
        new, ok = QInputDialog.getText(self, "Rename preset", "New name", text=old)
        if not ok or not new.strip() or new.strip() == old:
            return
        try:
            self.store.rename(old, new)
        except PresetError as error:
            QMessageBox.warning(self, "Rename preset", str(error))
            return
        self.refresh(select=" ".join(new.split()))

    def delete_selected(self) -> None:
        name = self.selected_name()
        if name is None:
            return
        answer = QMessageBox.question(
            self,
            "Delete preset",
            f"Delete the preset “{name}”?",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.store.delete(name)
        except PresetError as error:
            QMessageBox.warning(self, "Delete preset", str(error))
            return
        self.refresh()
        self.message.emit(f"Deleted preset “{name}”.")

    def _move_selected(self, offset: int) -> None:
        name = self.selected_name()
        if name is None:
            return
        try:
            self.store.move(name, offset)
        except PresetError as error:
            QMessageBox.warning(self, "Move preset", str(error))
            return
        self.refresh(select=name)
