"""Saved positions: a table of presets, and the dialogs to save one and to go to one."""

from collections.abc import Sequence

from PySide6.QtCore import Qt, Signal
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

from stagehand.gui import theme
from stagehand.gui.icons import blank, icon
from stagehand.presets import Preset, PresetError, PresetStore
from stagehand.stage import State
from stagehand.worker import StageStatus, StageWorker

MATCH_TOLERANCE = 0.001  # a stage is "at" a preset position within this distance


def at_preset(preset: Preset, statuses: dict[str, StageStatus]) -> bool:
    """Every stage in the preset is connected and at its saved position."""
    for name, value in preset.positions.items():
        status = statuses.get(name)
        if status is None or status.position is None or not status.state.connected:
            return False
        if abs(status.position - value) > MATCH_TOLERANCE:
            return False
    return True


def blocked_reason(status: StageStatus, target: float) -> str:
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
        return f"outside {low:g} to {high:g}"
    return ""


class SavePresetDialog(QDialog):
    """Name the preset and pick the stages it stores. Stages that cannot be saved are shown."""

    def __init__(
        self,
        statuses: Sequence[StageStatus],
        decimals: dict[str, int],
        parent: QWidget,
        labels: dict[str, str] | None = None,
    ):
        super().__init__(parent)
        labels = labels or {}
        self.setWindowTitle("Save preset")
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("For example: Odor port in")
        self.checks: dict[str, QCheckBox] = {}
        self._positions: dict[str, float] = {}

        grid = QGridLayout()
        grid.setHorizontalSpacing(theme.SECTION_GAP)
        for row, status in enumerate(statuses):
            check = QCheckBox(labels.get(status.name, status.name))
            usable = (
                status.position is not None
                and status.state.connected
                and status.state not in (State.NOT_HOMED, State.ERROR)
                and not status.state.busy
            )
            if usable:
                assert status.position is not None
                self._positions[status.name] = status.position
                value = f"{status.position:.{decimals[status.name]}f}"
                check.setChecked(True)
            else:
                value = "not saved: " + (
                    "moving" if status.state.busy else status.state.value.lower()
                )
                check.setEnabled(False)
            note = QLabel(value)
            note.setObjectName("secondary" if usable else "error")
            note.setFont(theme.number_font(theme.BODY_PX) if usable else theme.font())
            check.toggled.connect(self._validate)
            self.checks[status.name] = check
            grid.addWidget(check, row, 0)
            grid.addWidget(note, row, 1)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.name_edit.textChanged.connect(self._validate)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.MARGIN, theme.MARGIN, theme.MARGIN, theme.MARGIN)
        layout.setSpacing(theme.GAP)
        layout.addWidget(QLabel("Name"))
        layout.addWidget(self.name_edit)
        layout.addSpacing(theme.UNIT)
        layout.addWidget(QLabel("Stages to store"))
        layout.addLayout(grid)
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
    """Show each move (from, to, distance) and let the user leave stages out."""

    def __init__(
        self,
        preset: Preset,
        statuses: dict[str, StageStatus],
        decimals: dict[str, int],
        parent: QWidget,
        labels: dict[str, str] | None = None,
    ):
        super().__init__(parent)
        labels = labels or {}
        self.setWindowTitle("Go to preset")
        self.checks: dict[str, QCheckBox] = {}
        self._targets: dict[str, float] = {}

        title = QLabel(f"Go to “{preset.name}”?")
        title.setFont(theme.font(theme.TITLE_PX, bold=True))
        grid = QGridLayout()
        grid.setHorizontalSpacing(theme.SECTION_GAP)
        for column, heading in enumerate(("Stage", "Now", "Target", "Change")):
            label = QLabel(heading)
            label.setObjectName("secondary")
            grid.addWidget(label, 0, column)
        for row, (name, target) in enumerate(preset.positions.items(), start=1):
            check = QCheckBox(labels.get(name, name))
            status = statuses.get(name)
            places = decimals.get(name, 4)
            reason = "not in this rig" if status is None else blocked_reason(status, target)
            now = (
                "—"
                if status is None or status.position is None
                else f"{status.position:.{places}f}"
            )
            change = ""
            if status is not None and status.position is not None:
                change = f"{target - status.position:+.{places}f}"
            if reason:
                check.setEnabled(False)
                change = reason
            else:
                check.setChecked(True)
                self._targets[name] = target
            check.toggled.connect(self._validate)
            self.checks[name] = check
            cells = (QLabel(now), QLabel(f"{target:.{places}f}"), QLabel(change))
            for column, cell in enumerate(cells, start=1):
                cell.setFont(
                    theme.number_font(theme.BODY_PX) if not reason or column < 3 else theme.font()
                )
                if reason and column == 3:
                    cell.setObjectName("error")
            grid.addWidget(check, row, 0)
            for column, cell in enumerate(cells, start=1):
                grid.addWidget(cell, row, column)

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
        layout.setSpacing(theme.GAP)
        layout.addWidget(title)
        layout.addLayout(grid)
        layout.addWidget(note)
        layout.addWidget(self.buttons)
        self._validate()

    def _validate(self) -> None:
        self.move_button.setEnabled(bool(self.targets()))

    def targets(self) -> dict[str, float]:
        return {n: t for n, t in self._targets.items() if self.checks[n].isChecked()}


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
        self.decimals = {w.name: w.config.decimals for w in workers}
        self._confirm = confirm_moves
        self._statuses: dict[str, StageStatus] = {}
        self._current: set[str] = set()

        title = QLabel("Presets")
        title.setFont(theme.font(theme.TITLE_PX, bold=True))
        self.save_button = QPushButton(icon("save", theme.PANEL), "Save current…")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self.save_current)
        self.go_button = QPushButton(icon("go"), "Go to")
        self.go_button.clicked.connect(self.go_to_selected)
        self.rename_button = QPushButton(icon("rename"), "Rename")
        self.rename_button.clicked.connect(self.rename_selected)
        self.up_button = QPushButton(icon("move-up"), "")
        self.up_button.setToolTip("Move up the list")
        self.up_button.clicked.connect(lambda: self._move_selected(-1))
        self.down_button = QPushButton(icon("move-down"), "")
        self.down_button.setToolTip("Move down the list")
        self.down_button.clicked.connect(lambda: self._move_selected(+1))
        self.delete_button = QPushButton(icon("delete"), "Delete")
        self.delete_button.clicked.connect(self.delete_selected)

        names = self.order
        self.table = QTableWidget(0, len(names) + 2)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, len(names) + 2):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.cellDoubleClicked.connect(lambda _r, _c: self.go_to_selected())
        self.table.setToolTip("Double-click a preset to go to it.")

        self.error_label = QLabel(store.load_error)
        self.error_label.setObjectName("error")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(bool(store.load_error))
        self.empty_label = QLabel(
            "No presets yet. Move the stages where you want them, then press Save current."
        )
        self.empty_label.setObjectName("secondary")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.MARGIN, theme.MARGIN, theme.MARGIN, theme.MARGIN)
        layout.setSpacing(theme.GAP)
        top = QHBoxLayout()
        top.addWidget(title)
        top.addStretch(1)
        for button in (
            self.go_button,
            self.rename_button,
            self.up_button,
            self.down_button,
            self.delete_button,
            self.save_button,
        ):
            top.addWidget(button)
        layout.addLayout(top)
        layout.addWidget(self.error_label)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.empty_label)
        self.refresh()

    # -- display ----------------------------------------------------------------------------

    def labels(self) -> dict[str, str]:
        return {name: worker.label for name, worker in self.workers.items()}

    def set_order(self, order: Sequence[str]) -> None:
        """Show the stage columns in the order of the cards."""
        self.order = list(order)
        self.refresh()

    def refresh(self, select: str | None = None) -> None:
        select = select or self.selected_name()
        names = self.order
        labels = self.labels()
        self.table.setHorizontalHeaderLabels(["Preset", *(labels[n] for n in names), "Saved"])
        self.table.setRowCount(len(self.store.presets))
        for row, preset in enumerate(self.store.presets):
            item = QTableWidgetItem(preset.name)
            item.setData(Qt.UserRole, preset.name)
            self.table.setItem(row, 0, item)
            for column, stage in enumerate(names, start=1):
                value = preset.positions.get(stage)
                text = "—" if value is None else f"{value:.{self.decimals[stage]}f}"
                cell = QTableWidgetItem(text)
                cell.setFont(theme.number_font(theme.BODY_PX))
                cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(row, column, cell)
            saved = QTableWidgetItem(preset.saved.replace("T", " ")[:16])
            saved.setForeground(Qt.gray)
            self.table.setItem(row, len(names) + 1, saved)
            if preset.name == select:
                self.table.selectRow(row)
        self.empty_label.setVisible(not self.store.presets)
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
            item.setToolTip("The stages are at this preset." if here else "")
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
        any_position = any(
            s.position is not None and s.state.connected and not s.state.busy
            for s in self._statuses.values()
        )
        self.save_button.setEnabled(writable and any_position)
        self.go_button.setEnabled(selected)
        for button in (self.rename_button, self.up_button, self.down_button, self.delete_button):
            button.setEnabled(selected and writable)

    # -- actions ----------------------------------------------------------------------------

    def save_current(self) -> None:
        statuses = [self.workers[name].status for name in self.order]
        dialog = SavePresetDialog(statuses, self.decimals, self, self.labels())
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
        labels = self.labels()
        dialog = GoToPresetDialog(preset, statuses, self.decimals, self, labels)
        if self._confirm and dialog.exec() != QDialog.Accepted:
            return
        targets = dialog.targets()
        if not targets:
            QMessageBox.information(self, "Go to preset", "No stage in this preset can move now.")
            return
        for stage, target in targets.items():
            self.workers[stage].move_to(target)
        skipped = sorted(labels.get(s, s) for s in set(preset.positions) - set(targets))
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
