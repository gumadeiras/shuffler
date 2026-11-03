"""One card per stage: live position, travel bar, and the controls for that stage.

The card shows positions as the axis presents them (``Axis.to_display``): for a
reversed stage, counted from the other end of the travel, so + and larger numbers
always mean forward, up, right, or in. It converts back (``Axis.to_device``) before
it sends a target to the worker.

Sizes: the window gives every card the same fixed width (``set_card_width``), at
least ``theme.CARD_WIDTH`` and wider when a card's controls need it. Wrapped text
(``WrapLabel``) wraps at the inner width and reports its real height, so the card's
height is always right and the window layout keeps it clear of the presets panel.
"""

from html import escape

from PySide6.QtCore import QPointF, QRectF, QSettings, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from stagehand.axes import Axis
from stagehand.gui import theme
from stagehand.gui.icons import icon
from stagehand.gui.widgets import NumberField, TitleLabel, WrapLabel
from stagehand.stage import State
from stagehand.worker import StageStatus, StageWorker

BORDER = 1  # the card frame


def pill_text(state: State) -> str:
    return ("! " if state is State.ERROR else "") + state.value


_pill_width: int | None = None


def widest_pill() -> int:
    """The width of the widest state pill, measured once on a label that is never shown."""
    global _pill_width  # noqa: PLW0603 - a cache for the process
    if _pill_width is None:
        probe = QLabel()
        widths = []
        for state in State:
            probe.setStyleSheet(theme.pill_style(state))
            probe.setText(pill_text(state))
            probe.ensurePolished()
            widths.append(probe.sizeHint().width())
        probe.deleteLater()
        _pill_width = max(widths)
    return _pill_width


class TravelBar(QWidget):
    """Where the stage is in its travel, drawn like a progress bar. A readout only.

    The bar fills from the low end to the position. There is no handle, the widget
    ignores the mouse, and it has no hover effect, so nothing suggests that it can be
    dragged. While the stage moves, a small pink mark above the bar shows the target.
    Thin ticks mark the configured min and max when they are inside the travel. All
    values are display values, so the + end is always on the right.
    """

    BAR = 8.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedHeight(26)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.travel: tuple[float, float] | None = None
        self.allowed: tuple[float, float] | None = None
        self.position: float | None = None
        self.target: float | None = None
        self.live = False

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        left_edge, width = 1.0, self.width() - 2.0
        top = self.height() - self.BAR - 4  # room above for the target mark
        radius = self.BAR / 2
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme.TRACK))
        painter.drawRoundedRect(QRectF(left_edge, top, width, self.BAR), radius, radius)
        if self.travel is None:
            painter.end()
            return
        low, high = self.travel
        span = high - low or 1.0

        def x(value: float) -> float:
            return left_edge + width * (min(max(value, low), high) - low) / span

        if self.position is not None:
            fill = QRectF(left_edge, top, max(x(self.position) - left_edge, self.BAR), self.BAR)
            painter.setBrush(QColor(theme.PINK_FILL if self.live else theme.NAVY))
            painter.drawRoundedRect(fill, radius, radius)
        if self.allowed is not None and self.allowed != self.travel:
            painter.setPen(QPen(QColor(theme.INK_SOFT), 1.5))
            for edge in (x(self.allowed[0]), x(self.allowed[1])):
                painter.drawLine(QPointF(edge, top - 3), QPointF(edge, top + self.BAR + 3))
            painter.setPen(Qt.NoPen)
        if self.target is not None:
            tip = x(self.target)
            path = QPainterPath()
            path.moveTo(tip, top - 2)
            path.lineTo(tip - 4, top - 8)
            path.lineTo(tip + 4, top - 8)
            path.closeSubpath()
            painter.setBrush(QColor(theme.PINK))
            painter.drawPath(path)
        painter.end()


class StageCard(QFrame):
    settings_requested = Signal()  # name and direction
    move_requested = Signal(int)  # -1 left, +1 right

    def __init__(self, worker: StageWorker, *, confirm_moves: bool, settings: QSettings) -> None:
        super().__init__()
        self.worker = worker
        self.config = worker.config
        self.axis = Axis(label=worker.label)
        self._confirm = confirm_moves
        self._status: StageStatus | None = None
        self._was_ready = False
        self.setObjectName("card")
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Minimum)
        unit = self.config.units

        # Header: name and state.
        self.name_label = TitleLabel(worker.label)
        self.name_label.setFont(theme.font(theme.TITLE_PX, bold=True))
        self.name_label.setToolTip("Double-click to change the name and direction.")
        self.name_label.setCursor(Qt.PointingHandCursor)
        self.name_label.double_clicked.connect(self.settings_requested)
        self.pill = QLabel()
        self.pill.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.model_label = WrapLabel(self.config.address)
        self.model_label.setObjectName("secondary")

        # Readout.
        self.position_label = QLabel("—")
        self.position_label.setFont(theme.number_font())
        self.position_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.unit_label = QLabel(unit)
        self.unit_label.setObjectName("secondary")
        self.unit_label.setFont(theme.font(theme.TITLE_PX))
        self.detail_label = WrapLabel()
        self.detail_label.setObjectName("secondary")
        self.detail_label.setFont(theme.font(theme.SMALL_PX))
        self.bar = TravelBar()
        self.low_label = QLabel(" ")
        self.high_label = QLabel(" ")
        self.high_label.setAlignment(Qt.AlignRight | Qt.AlignTop)
        for label in (self.low_label, self.high_label):
            label.setObjectName("secondary")
            label.setFont(theme.font(theme.SMALL_PX))
            label.setTextFormat(Qt.RichText)

        # Go to a target.
        self.target_box = NumberField(self.config.decimals, unit, "Target")
        self.target_box.returnPressed.connect(self._go)
        target_unit = QLabel(unit)
        target_unit.setObjectName("secondary")
        self.go_button = QPushButton(icon("go", theme.PANEL), "Go")
        self.go_button.setObjectName("primary")
        self.go_button.clicked.connect(self._go)

        # Jog.
        self.back_button = QPushButton(icon("jog-back"), "")
        self.forward_button = QPushButton(icon("jog-forward"), "")
        self.forward_button.setLayoutDirection(Qt.RightToLeft)  # the icon after the word
        self.back_button.clicked.connect(lambda: self._jog(-1))
        self.forward_button.clicked.connect(lambda: self._jog(+1))
        self.step_box = QComboBox()
        self.step_box.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        for step in self.config.jog_steps:
            self.step_box.addItem(f"{step:g} {unit}", step)
        self.step_box.setToolTip("Step size for − and +")
        saved = settings.value(self._key("step"))
        index = self.step_box.findData(float(saved)) if saved is not None else -1
        self.step_box.setCurrentIndex(index if index >= 0 else min(2, self.step_box.count() - 1))
        self.step_box.currentIndexChanged.connect(
            lambda: settings.setValue(self._key("step"), self.step_box.currentData())
        )

        # Speed.
        speed_label = QLabel("Speed")
        speed_label.setObjectName("secondary")
        self.speed_box = NumberField(3, f"{unit}/s", "Speed")
        self.speed_box.returnPressed.connect(self._set_speed)
        self.speed_box.textEdited.connect(self._update_speed_button)
        speed_unit = QLabel(f"{unit}/s")
        speed_unit.setObjectName("secondary")
        self.speed_button = QPushButton("Set")
        self.speed_button.setToolTip("Use this speed for the next moves.")
        self.speed_button.clicked.connect(self._set_speed)

        # Actions.
        self.home_button = QPushButton(icon("home"), "Home")
        self.home_button.setToolTip("Find the home position (reference the stage).")
        self.home_button.clicked.connect(self._home)
        self.stop_button = QPushButton(icon("stop"), "Stop")
        self.stop_button.setObjectName("stop")
        self.stop_button.clicked.connect(worker.stop)
        self.connect_button = QPushButton(icon("connect", theme.PANEL), "Connect")
        self.connect_button.setObjectName("primary")
        self.connect_button.clicked.connect(worker.connect)
        self.more_button = QToolButton()
        self.more_button.setIcon(icon("more"))
        self.more_button.setPopupMode(QToolButton.InstantPopup)
        self.more_button.setToolTip("More")
        self.more_button.setMenu(self._menu(worker))

        self.error_label = WrapLabel(collapse=True)
        self.error_label.setObjectName("error")
        self.error_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.MARGIN, theme.MARGIN, theme.MARGIN, theme.MARGIN)
        layout.setSpacing(theme.GAP)
        layout.addLayout(_row(self.name_label, (self.pill, 0, Qt.AlignTop), stretch_first=True))
        layout.addWidget(self.model_label)
        readout = QHBoxLayout()
        readout.setSpacing(theme.UNIT)
        readout.addWidget(self.position_label, 0, Qt.AlignBaseline)
        readout.addWidget(self.unit_label, 0, Qt.AlignBaseline)
        readout.addStretch(1)
        layout.addLayout(readout)
        layout.addWidget(self.detail_label)
        layout.addWidget(self.bar)
        limits = QHBoxLayout()
        limits.addWidget(self.low_label, 0, Qt.AlignTop)
        limits.addStretch(1)
        limits.addWidget(self.high_label, 0, Qt.AlignTop)
        layout.addLayout(limits)
        rule = QFrame()
        rule.setObjectName("rule")
        layout.addWidget(rule)
        layout.addLayout(_row((self.target_box, 1), target_unit, self.go_button))
        layout.addLayout(_row(self.back_button, (self.step_box, 1), self.forward_button))
        layout.addLayout(_row(speed_label, (self.speed_box, 1), speed_unit, self.speed_button))
        actions = _row(self.connect_button, self.home_button, self.stop_button)
        actions.addStretch(1)
        actions.addWidget(self.more_button)
        layout.addLayout(actions)
        layout.addWidget(self.error_label)
        layout.addStretch(1)

        self.set_card_width(theme.CARD_WIDTH)
        self.set_axis(self.axis)

    def _menu(self, worker: StageWorker) -> QMenu:
        menu = QMenu(self)
        self.settings_action = menu.addAction(
            icon("label"),
            "Name and direction…",
            lambda: self.settings_requested.emit(),  # noqa: PLW0108 - a signal is not a slot
        )
        self.left_action = menu.addAction(
            icon("move-left"), "Move card left", lambda: self.move_requested.emit(-1)
        )
        self.right_action = menu.addAction(
            icon("move-right"), "Move card right", lambda: self.move_requested.emit(+1)
        )
        menu.addSeparator()
        self.reset_action = menu.addAction(icon("reset"), "Reset controller…", self._reset)
        menu.addSeparator()
        self.connect_action = menu.addAction(icon("connect"), "Connect", worker.connect)
        self.disconnect_action = menu.addAction(icon("disconnect"), "Disconnect", worker.disconnect)
        return menu

    # -- size -------------------------------------------------------------------------------

    def needed_width(self) -> int:
        """The card width that the controls need. Wrapped text never adds to it."""
        controls = (
            self.back_button,
            self.forward_button,
            self.step_box,
            self.go_button,
            self.speed_button,
            self.home_button,
            self.stop_button,
            self.connect_button,
            self.more_button,
        )
        for widget in controls:  # a row never squeezes a control below its text
            widget.setMinimumWidth(widget.sizeHint().width())
        layout = self.layout()
        return max(theme.CARD_WIDTH, layout.minimumSize().width())

    def set_card_width(self, width: int) -> None:
        if self.minimumWidth() == width == self.maximumWidth():
            return
        self.setFixedWidth(width)
        inner = width - 2 * (theme.MARGIN + BORDER)
        for label in (self.model_label, self.detail_label, self.error_label):
            label.set_wrap_width(inner)
        self.name_label.set_wrap_width(inner - widest_pill() - theme.GAP)

    # -- updates ----------------------------------------------------------------------------

    def set_axis(self, axis: Axis) -> None:
        """Show a new name, direction, or sense."""
        changed_sense = axis.mirrored != self.axis.mirrored
        self.axis = axis
        self.name_label.setText(axis.label)
        negative, positive = axis.words
        self.back_button.setText(negative)
        self.forward_button.setText(positive)
        self.back_button.setToolTip(f"Move {negative or 'back'} by one step (−)")
        self.forward_button.setToolTip(f"Move {positive or 'forward'} by one step (+)")
        status, self._status = self._status, None  # redraw everything
        self.show_status(status or self.worker.status)
        if changed_sense and self._status and self._status.position is not None:
            self.target_box.setValue(axis.to_display(self._status.position))

    def set_position_in_row(self, index: int, count: int) -> None:
        self.left_action.setEnabled(index > 0)
        self.right_action.setEnabled(index < count - 1)

    def show_status(self, status: StageStatus) -> None:
        if status == self._status:
            return
        self._status = status
        state = status.state
        axis = self.axis
        units = self.config.units
        info = status.info
        self.pill.setText(pill_text(state))
        self.pill.setStyleSheet(theme.pill_style(state))
        self.model_label.setText(self._identity(status))

        position = None if status.position is None else axis.to_display(status.position)
        self.position_label.setText("—" if position is None else self._fmt(position))
        self.unit_label.setVisible(position is not None)
        self.detail_label.setText(self._detail(status))

        if info is not None:
            travel = axis.display_range(info.lower, info.upper)
            allowed = axis.display_range(*status.allowed) if status.allowed else None
            low, high = allowed or travel
            negative, positive = axis.words
            self.low_label.setText(self._end(f"{low:g}", negative, left=True))
            self.high_label.setText(self._end(f"{high:g} {units}", positive, left=False))
            self.target_box.setRange(low, high)
            self.bar.travel, self.bar.allowed = travel, allowed
        else:
            self.low_label.setText(" ")
            self.high_label.setText(" ")
            self.bar.travel = self.bar.allowed = None
        self.bar.position = position
        moving_to = status.target if state.busy else None
        self.bar.target = None if moving_to is None else axis.to_display(moving_to)
        self.bar.live = state.busy
        self.bar.update()

        # Start from where the stage is, and follow it after each move, unless the user
        # is typing a target.
        ready = state is State.READY and position is not None
        if ready and not self._was_ready and not self.target_box.hasFocus():
            self.target_box.setValue(position)
        self._was_ready = ready

        self._show_speed(status)
        self._enable(status)
        self.error_label.setText(status.error)

    def _show_speed(self, status: StageStatus) -> None:
        """The controller's speed, unless the user is typing one."""
        if not self.speed_box.hasFocus():
            if status.velocity is None:
                self.speed_box.clear()
            else:
                self.speed_box.setValue(status.velocity)
        units = self.config.units
        limit = self.worker.speed_limit()
        if limit is not None:
            ceiling = f" Maximum {limit:g} {units}/s."
        elif self.config.driver == "conex":
            ceiling = " Maximum: the speed stored in the controller."
        else:
            ceiling = ""
        self.speed_box.setToolTip(
            f"Speed for moves and jogs, in {units}/s.{ceiling} Set it when the stage is "
            "homed and still. Homing uses the controller's own homing speed."
        )

    def _enable(self, status: StageStatus) -> None:
        state = status.state
        connected = state.connected
        can_move = state is State.READY and status.allowed is not None
        for widget in (self.target_box, self.go_button, self.back_button, self.forward_button):
            widget.setEnabled(can_move)
        self.step_box.setEnabled(connected)
        # Only in READY: a CONEX-CC refuses VA before homing and during motion.
        self.speed_box.setEnabled(state is State.READY and status.velocity is not None)
        self._update_speed_button()
        not_homed = state is State.NOT_HOMED
        self.home_button.setEnabled(connected and not state.busy)
        self.home_button.setObjectName("primary" if not_homed else "")
        self.home_button.setIcon(icon("home", theme.PANEL if not_homed else theme.NAVY))
        _restyle(self.home_button)
        self.stop_button.setEnabled(connected)
        self.stop_button.setProperty("live", state.busy)
        self.stop_button.setIcon(icon("stop", theme.PANEL if state.busy else theme.NAVY))
        _restyle(self.stop_button)
        offline = state is State.OFFLINE
        self.connect_button.setVisible(offline)
        self.home_button.setVisible(not offline)
        self.stop_button.setVisible(not offline)
        self.reset_action.setEnabled(connected)
        self.connect_action.setEnabled(offline)
        self.disconnect_action.setEnabled(connected)

    def _identity(self, status: StageStatus) -> str:
        """Model, address, the stages.toml name when the label hides it, and the sense."""
        parts = [self.config.address]
        if self.axis.label != self.config.name:
            parts.append(self.config.name)
        if self.axis.mirrored:
            parts.append("reversed")
        line = "  ·  ".join(parts)
        return f"{status.info.model}\n{line}" if status.info else line

    def _detail(self, status: StageStatus) -> str:
        """One short line that says what the stage is doing or needs."""
        state = status.state
        if state is State.OFFLINE:
            return "Not connected."
        if state is State.CONNECTING:
            return "Connecting…"
        if state is State.HOMING:
            return "Finding the home position…"
        if state is State.MOVING and status.target is not None:
            target = self._fmt(self.axis.to_display(status.target))
            return f"Moving to {target} {self.config.units}"
        if state is State.NOT_HOMED:
            return "Home this stage before you move it."
        return status.detail

    @staticmethod
    def _end(number: str, word: str, *, left: bool) -> str:
        """A travel bar end: the number, and under it the direction word in gold."""
        if not word:
            return escape(number)
        text = f"◂ {word}" if left else f"{word} ▸"
        return (
            f"{escape(number)}<br><span style='color:{theme.GOLD}; font-weight:600'>"
            f"{escape(text)}</span>"
        )

    def _fmt(self, value: float) -> str:
        return f"{value:.{self.config.decimals}f}"

    # -- actions ----------------------------------------------------------------------------

    def _go(self) -> None:
        if not self.go_button.isEnabled():
            return
        target = self.target_box.value()
        if target is None:
            self.error_label.setText("Type a target position first.")
            return
        self.worker.move_to(self.axis.to_device(target))  # the worker checks the range

    def _jog(self, sign: int) -> None:
        self.worker.move_by(sign * self.axis.sign * float(self.step_box.currentData()))

    def _update_speed_button(self) -> None:
        typed = self.speed_box.value()
        current = self._status.velocity if self._status else None
        self.speed_button.setEnabled(
            self.speed_box.isEnabled()
            and typed is not None
            and (current is None or abs(typed - current) > 1e-9)
        )

    def _set_speed(self) -> None:
        if not self.speed_button.isEnabled():
            return
        speed = self.speed_box.value()
        assert speed is not None
        current = self._status.velocity if self._status else None
        units = self.config.units
        if current and speed > 2 * current:
            answer = QMessageBox.question(
                self,
                "Set speed",
                f"Set {self.axis.label} to {speed:g} {units}/s?\n\nThat is "
                f"{speed / current:.1f} times faster than now ({current:g} {units}/s).",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                return
        self.speed_box.clearFocus()
        self.worker.set_velocity(speed)

    def _home(self) -> None:
        if self._confirm:
            resets = (
                self.config.driver == "conex"
                and self._status is not None
                and self._status.state is not State.NOT_HOMED
            )
            extra = " The CONEX-CC controller resets first." if resets else ""
            answer = QMessageBox.question(
                self,
                "Home stage",
                f"Home {self.axis.label}?\n\nThe stage moves to its home position at one end "
                f"of its travel. Make sure nothing is in its path.{extra}",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                return
        self.worker.home()

    def _reset(self) -> None:
        if self.config.driver == "conex":
            effect = "The controller restarts. The stage must be homed again before it can move."
        else:
            effect = "Faults are cleared and the motor is disabled and enabled again."
        answer = QMessageBox.question(
            self,
            "Reset controller",
            f"Reset the {self.axis.label} controller?\n\n{effect}",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer == QMessageBox.Yes:
            self.worker.reset()

    def _key(self, item: str) -> str:
        return f"stage/{self.config.name}/{item}"


def _row(*items: object, stretch_first: bool = False) -> QHBoxLayout:
    """A row of widgets. An item is a widget, or (widget, stretch[, alignment])."""
    row = QHBoxLayout()
    row.setSpacing(theme.GAP)
    for index, item in enumerate(items):
        widget, stretch, *align = item if isinstance(item, tuple) else (item, 0)
        if stretch_first and index == 0:
            stretch = 1
        row.addWidget(widget, stretch, *align)
    return row


def _restyle(widget: QWidget) -> None:
    """Apply a changed object name or property to the stylesheet."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
