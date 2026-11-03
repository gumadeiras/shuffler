"""One card per stage: live position, travel bar, and the controls for that stage."""

from PySide6.QtCore import QLocale, QPointF, QRectF, QSettings, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QDoubleValidator,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from stagehand.gui import theme
from stagehand.gui.icons import icon
from stagehand.stage import State
from stagehand.worker import StageStatus, StageWorker


class TargetField(QLineEdit):
    """A typed target. No spin arrows: a stray click must not change a target.

    The validator accepts numbers only. The allowed range is shown in the tooltip but
    not enforced here, so a target outside it reaches the worker, which refuses it
    with a clear message instead of silently changing it.
    """

    def __init__(self, decimals: int, unit: str) -> None:
        super().__init__()
        self._decimals = decimals
        self._unit = unit
        self._range = (-1e9, 1e9)
        validator = QDoubleValidator(-1e9, 1e9, decimals, self)
        validator.setNotation(QDoubleValidator.StandardNotation)
        validator.setLocale(QLocale.c())  # a period as the decimal point on every PC
        self.setValidator(validator)
        self.setPlaceholderText("Target")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setToolTip("Type a target and press Enter or Go.")

    def setRange(self, low: float, high: float) -> None:
        self._range = (low, high)
        self.setToolTip(
            f"Type a target from {low:g} to {high:g} {self._unit} and press Enter or Go."
        )

    def minimum(self) -> float:
        return self._range[0]

    def maximum(self) -> float:
        return self._range[1]

    def setValue(self, value: float) -> None:
        self.setText(f"{value:.{self._decimals}f}")

    def value(self) -> float | None:
        try:
            return float(self.text())
        except ValueError:
            return None


class TitleLabel(QLabel):
    """The stage label. Double-click it to rename the stage."""

    double_clicked = Signal()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self.double_clicked.emit()
        event.accept()


class TravelBar(QWidget):
    """The controller's travel as a track; the allowed range, the stage, and the target on it."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(30)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.travel: tuple[float, float] | None = None
        self.allowed: tuple[float, float] | None = None
        self.position: float | None = None
        self.target: float | None = None
        self.live = False

    def show_status(self, status: StageStatus) -> None:
        info = status.info
        self.travel = (info.lower, info.upper) if info else None
        self.allowed = status.allowed
        self.position = status.position
        self.target = status.target if status.state.busy else None
        self.live = status.state.busy
        self.update()

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        pad = 8.0
        width = self.width() - 2 * pad
        middle = self.height() / 2
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme.TRACK))
        painter.drawRoundedRect(QRectF(pad, middle - 3, width, 6), 3, 3)
        if self.travel is None:
            return
        low, high = self.travel
        span = high - low or 1.0

        def x(value: float) -> float:
            return pad + width * (min(max(value, low), high) - low) / span

        if self.allowed is not None:
            painter.setBrush(QColor(theme.NAVY_WASH if not self.live else theme.PINK_WASH))
            left, right = x(self.allowed[0]), x(self.allowed[1])
            painter.drawRoundedRect(QRectF(left, middle - 3, right - left, 6), 3, 3)
            painter.setPen(QPen(QColor(theme.LINE), 1))
            for edge in (left, right):
                painter.drawLine(QPointF(edge, middle - 7), QPointF(edge, middle + 7))
            painter.setPen(Qt.NoPen)
        if self.target is not None:
            tip = x(self.target)
            path = QPainterPath()
            path.moveTo(tip, middle - 5)
            path.lineTo(tip - 5, middle - 12)
            path.lineTo(tip + 5, middle - 12)
            path.closeSubpath()
            painter.setBrush(QColor(theme.PINK))
            painter.drawPath(path)
        if self.position is not None:
            painter.setBrush(QColor(theme.PINK if self.live else theme.NAVY))
            painter.setPen(QPen(QColor(theme.PANEL), 2))
            painter.drawEllipse(QPointF(x(self.position), middle), 7, 7)
        painter.end()


class StageCard(QFrame):
    rename_requested = Signal()
    move_requested = Signal(int)  # -1 left, +1 right

    def __init__(self, worker: StageWorker, *, confirm_moves: bool, settings: QSettings) -> None:
        super().__init__()
        self.worker = worker
        self.config = worker.config
        self._confirm = confirm_moves
        self._settings = settings
        self._status: StageStatus | None = None
        self._was_connected = False
        self.setObjectName("card")
        self.setFixedWidth(theme.CARD_WIDTH)

        unit = self.config.units
        self.name_label = TitleLabel(worker.label)
        self.name_label.setFont(theme.font(theme.TITLE_PX, bold=True))
        self.name_label.setToolTip("Double-click to rename this stage.")
        self.name_label.setCursor(Qt.PointingHandCursor)
        self.name_label.double_clicked.connect(self.rename_requested)
        self.pill = QLabel()
        self.model_label = QLabel(self.config.address)
        self.model_label.setObjectName("secondary")
        self.model_label.setWordWrap(True)

        self.position_label = QLabel("—")
        self.position_label.setFont(theme.number_font())
        self.position_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        unit_label = QLabel(unit)
        unit_label.setObjectName("secondary")
        unit_label.setFont(theme.font(theme.TITLE_PX))
        self.detail_label = QLabel(" ")
        self.detail_label.setObjectName("secondary")
        self.detail_label.setFont(theme.font(theme.SMALL_PX))
        self.bar = TravelBar()
        self.low_label = QLabel(" ")
        self.high_label = QLabel(" ")
        for label in (self.low_label, self.high_label):
            label.setObjectName("secondary")
            label.setFont(theme.font(theme.SMALL_PX))

        self.target_box = TargetField(self.config.decimals, unit)
        self.target_box.returnPressed.connect(self._go)
        self.go_button = QPushButton(icon("go", theme.PANEL), "Go")
        self.go_button.setObjectName("primary")
        self.go_button.clicked.connect(self._go)

        self.back_button = QPushButton(icon("jog-back"), "")
        self.forward_button = QPushButton(icon("jog-forward"), "")
        self.back_button.setToolTip("Move back by one step")
        self.forward_button.setToolTip("Move forward by one step")
        self.back_button.clicked.connect(lambda: self._jog(-1))
        self.forward_button.clicked.connect(lambda: self._jog(+1))
        self.step_box = QComboBox()
        for step in self.config.jog_steps:
            self.step_box.addItem(f"{step:g} {unit}", step)
        self.step_box.setToolTip("Step size for the - and + buttons")
        saved = settings.value(self._key("step"))
        index = self.step_box.findData(float(saved)) if saved is not None else -1
        self.step_box.setCurrentIndex(index if index >= 0 else min(2, self.step_box.count() - 1))
        self.step_box.currentIndexChanged.connect(
            lambda: settings.setValue(self._key("step"), self.step_box.currentData())
        )

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
        menu = QMenu(self.more_button)
        self.rename_action = menu.addAction(
            icon("label"),
            "Rename…",
            lambda: self.rename_requested.emit(),  # noqa: PLW0108 - a signal is not a slot here
        )
        self.left_action = menu.addAction(
            icon("move-left"), "Move left", lambda: self.move_requested.emit(-1)
        )
        self.right_action = menu.addAction(
            icon("move-right"), "Move right", lambda: self.move_requested.emit(+1)
        )
        menu.addSeparator()
        self.reset_action = menu.addAction(icon("reset"), "Reset controller…", self._reset)
        menu.addSeparator()
        self.connect_action = menu.addAction(icon("connect"), "Connect", worker.connect)
        self.disconnect_action = menu.addAction(icon("disconnect"), "Disconnect", worker.disconnect)
        self.more_button.setMenu(menu)

        self.error_label = QLabel()
        self.error_label.setObjectName("error")
        self.error_label.setWordWrap(True)
        self.error_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.error_label.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.MARGIN, theme.MARGIN, theme.MARGIN, theme.MARGIN)
        layout.setSpacing(theme.GAP)
        header = QHBoxLayout()
        header.addWidget(self.name_label, 1)
        header.addWidget(self.pill, 0, Qt.AlignTop)
        layout.addLayout(header)
        layout.addWidget(self.model_label)
        layout.addSpacing(theme.UNIT)
        readout = QHBoxLayout()
        readout.setSpacing(theme.UNIT)
        readout.addWidget(self.position_label)
        readout.addWidget(unit_label, 0, Qt.AlignBottom)
        readout.addStretch(1)
        layout.addLayout(readout)
        layout.addWidget(self.detail_label)
        layout.addWidget(self.bar)
        limits = QHBoxLayout()
        limits.addWidget(self.low_label)
        limits.addStretch(1)
        limits.addWidget(self.high_label)
        layout.addLayout(limits)
        rule = QFrame()
        rule.setObjectName("rule")
        layout.addWidget(rule)
        go_row = QHBoxLayout()
        go_row.addWidget(self.target_box, 1)
        target_unit = QLabel(unit)
        target_unit.setObjectName("secondary")
        go_row.addWidget(target_unit)
        go_row.addWidget(self.go_button)
        layout.addLayout(go_row)
        jog_row = QHBoxLayout()
        jog_row.addWidget(self.back_button)
        jog_row.addWidget(self.step_box, 1)
        jog_row.addWidget(self.forward_button)
        layout.addLayout(jog_row)
        actions = QHBoxLayout()
        actions.addWidget(self.connect_button)
        actions.addWidget(self.home_button)
        actions.addWidget(self.stop_button)
        actions.addStretch(1)
        actions.addWidget(self.more_button)
        layout.addLayout(actions)
        layout.addWidget(self.error_label)
        layout.addStretch(1)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Minimum)

        self.show_status(worker.status)

    # -- updates ----------------------------------------------------------------------------

    def set_label(self, label: str) -> None:
        self.name_label.setText(label)
        self._show_identity()

    def set_position_in_row(self, index: int, count: int) -> None:
        self.left_action.setEnabled(index > 0)
        self.right_action.setEnabled(index < count - 1)

    def _show_identity(self) -> None:
        """Model and address; the stages.toml name too when the label hides it."""
        address = self.config.address
        if self.worker.label != self.config.name:
            address = f"{address} · {self.config.name}"
        info = self._status.info if self._status else None
        self.model_label.setText(f"{info.model}\n{address}" if info else address)

    def show_status(self, status: StageStatus) -> None:
        if status == self._status:
            return
        self._status = status
        state = status.state
        decimals = self.config.decimals
        self.pill.setText(("! " if state is State.ERROR else "") + state.value)
        self.pill.setStyleSheet(theme.pill_style(state))
        self._show_identity()

        if status.position is None:
            self.position_label.setText("—")
        else:
            self.position_label.setText(f"{status.position:.{decimals}f}")
        detail = status.detail
        if state.busy and status.target is not None:
            detail = f"→ {status.target:.{decimals}f} {self.config.units}"
        self.detail_label.setText(detail or " ")
        self.bar.show_status(status)
        if status.info is not None:
            low, high = status.allowed or (status.info.lower, status.info.upper)
            self.low_label.setText(f"{low:g}")
            self.high_label.setText(f"{high:g} {self.config.units}")
            self.target_box.setRange(low, high)
        else:
            self.low_label.setText(" ")
            self.high_label.setText(" ")

        connected = state.connected
        if connected and not self._was_connected and status.position is not None:
            self.target_box.setValue(status.position)  # start from where the stage is
        self._was_connected = connected and status.position is not None

        can_move = state is State.READY and status.allowed is not None
        for widget in (self.target_box, self.go_button, self.back_button, self.forward_button):
            widget.setEnabled(can_move)
        self.step_box.setEnabled(connected)
        self.home_button.setEnabled(connected and not state.busy)
        self.home_button.setObjectName("primary" if state is State.NOT_HOMED else "")
        self.home_button.setIcon(
            icon("home", theme.PANEL if state is State.NOT_HOMED else theme.NAVY)
        )
        self._restyle(self.home_button)
        self.stop_button.setEnabled(connected)
        self.stop_button.setProperty("live", state.busy)
        self.stop_button.setIcon(icon("stop", theme.PANEL if state.busy else theme.NAVY))
        self._restyle(self.stop_button)
        self.connect_button.setVisible(state is State.OFFLINE)
        self.home_button.setVisible(state is not State.OFFLINE)
        self.stop_button.setVisible(state is not State.OFFLINE)
        self.reset_action.setEnabled(connected)
        self.connect_action.setEnabled(state is State.OFFLINE)
        self.disconnect_action.setEnabled(connected)

        self.error_label.setText(status.error)
        self.error_label.setVisible(bool(status.error))

    # -- actions ----------------------------------------------------------------------------

    def _go(self) -> None:
        if self.go_button.isEnabled():
            target = self.target_box.value()
            if target is None:
                self.error_label.setText("Type a target position first.")
                self.error_label.show()
                return
            self.worker.move_to(target)  # the worker refuses a target outside the range

    def _jog(self, sign: int) -> None:
        self.worker.move_by(sign * float(self.step_box.currentData()))

    def _home(self) -> None:
        if self._confirm:
            extra = (
                " The CONEX-CC controller resets first."
                if self.config.driver == "conex"
                and self._status
                and self._status.state is not State.NOT_HOMED
                else ""
            )
            answer = QMessageBox.question(
                self,
                "Home stage",
                f"Home {self.worker.label}?\n\nThe stage moves to its home position at one end "
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
            f"Reset the {self.worker.label} controller?\n\n{effect}",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer == QMessageBox.Yes:
            self.worker.reset()

    def _key(self, item: str) -> str:
        return f"stage/{self.config.name}/{item}"

    @staticmethod
    def _restyle(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)
