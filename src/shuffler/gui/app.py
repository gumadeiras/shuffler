"""The shuffler window: a card per stage, Stop all, and the presets.

uv run shuffler-gui                  uses stages.toml in the current folder
uv run shuffler-gui --config FILE
uv run shuffler-gui --demo           simulated stages; no hardware is touched
"""

import argparse
import logging
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import replace
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QSettings, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QKeyEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from shuffler import __version__
from shuffler import config as config_module
from shuffler.axes import Axis, AxisError, AxisStore
from shuffler.config import AppConfig, ConfigError
from shuffler.gui import theme
from shuffler.gui.axis_dialog import AxisDialog
from shuffler.gui.fly import FlyWidget
from shuffler.gui.icons import icon
from shuffler.gui.presets_panel import PresetsPanel
from shuffler.gui.stage_card import StageCard
from shuffler.gui.widgets import ElidedLabel
from shuffler.presets import PresetStore
from shuffler.rig import driver_factory
from shuffler.stage import State
from shuffler.worker import StageWorker

log = logging.getLogger("shuffler")
REFRESH_MS = 100
GEOMETRY_KEY = "window/geometry-v2"

DEMO_CONFIG = {
    "app": {"connect_on_start": True},
    "stage": [
        {"name": "CONEX A", "driver": "conex", "port": "COM3"},
        {"name": "CONEX B", "driver": "conex", "port": "COM4"},
        {
            "name": "DDS100",
            "driver": "kinesis",
            "serial": "28253849",
            "jog_steps": [0.001, 0.01, 0.1, 1.0, 10.0],
        },
    ],
}


class MainWindow(QMainWindow):
    """Layout, top to bottom: a bar with the status text, Connect all, and Stop all; the
    cards row; and the presets panel.

    All sizes come from the widgets' own size hints; nothing has a hand-set height. The
    window's minimum size is the sum of the minimums, so the cards row and the presets
    panel can never overlap, and the window grows if a card grows after it opens.

    No widget in the window may use height-for-width (a word-wrapped QLabel does). One
    such widget puts the whole window layout into height-for-width mode, and in that
    mode Qt squeezed the cards below their minimum to keep the presets panel at its
    preferred height: the presets panel covered the Home and Stop buttons. Wrapped text
    uses ``WrapLabel`` instead, and a test checks this rule.
    """

    def __init__(
        self,
        app_config: AppConfig,
        workers: Sequence[StageWorker],
        store: PresetStore,
        *,
        axes: AxisStore | None = None,
        demo: bool = False,
        settings: QSettings | None = None,
    ) -> None:
        super().__init__()
        self.app_config = app_config
        self.workers = list(workers)
        self.settings = settings or QSettings("ClarkLab", "shuffler")
        keys = [w.name for w in self.workers]
        self.axes = axes or AxisStore(app_config.axes_path, keys)
        self.setWindowTitle("shuffler" + (" (demo)" if demo else ""))

        self.fly = FlyWidget()  # follows the stages; see gui/fly.py
        demo_badge = QLabel("DEMO · simulated stages")
        demo_badge.setObjectName("demo")
        demo_badge.setVisible(demo)
        # Connect all; once every stage is connected, Disconnect all (red outline). It
        # keeps the width of the longer text, so the top bar does not jump.
        self.connect_all_button = QPushButton()
        self._show_connection_button(all_connected=True)
        widest = self.connect_all_button.sizeHint().width()
        self._show_connection_button(all_connected=False)
        self.connect_all_button.setMinimumWidth(
            max(widest, self.connect_all_button.sizeHint().width())
        )
        self.connect_all_button.clicked.connect(self._connection_button_clicked)
        self.stop_all_button = QPushButton(icon("stop", theme.PANEL), "Stop all (Esc)")
        self.stop_all_button.setObjectName("stopAll")
        self.stop_all_button.setToolTip("Stop every stage now. Esc does the same from anywhere.")
        self.stop_all_button.clicked.connect(self.stop_all)
        # Qt suppresses window shortcuts while a modal dialog is open.
        QApplication.instance().installEventFilter(self)

        self.cards = [
            StageCard(w, confirm_moves=app_config.confirm_moves, settings=self.settings)
            for w in self.workers
        ]
        for card in self.cards:
            card.settings_requested.connect(lambda c=card: self.edit_stage(c))
            card.move_requested.connect(lambda offset, c=card: self.move_stage(c, offset))
        self.cards_box = QWidget()  # the cards row, as one item of the window layout
        self.cards_row = QHBoxLayout(self.cards_box)
        self.cards_row.setContentsMargins(0, 0, 0, 0)
        self.cards_row.setSpacing(theme.SECTION_GAP)

        # Status text; say() shows a message for a while. A long message ends in "…" and
        # never makes the window wider.
        self.status_label = ElidedLabel()
        self.status_label.setObjectName("secondary")
        self._resting_message = ""
        self._message_timer = QTimer(self)
        self._message_timer.setSingleShot(True)
        self._message_timer.timeout.connect(
            lambda: self.status_label.setText(self._resting_message)
        )

        self.presets = PresetsPanel(store, self.workers, confirm_moves=app_config.confirm_moves)
        self.presets.message.connect(self.say)
        for card in self.cards:
            axis = self.axes.get(card.worker.name)
            if axis.speed is not None:  # else the stages.toml velocity, if any
                card.worker.speed = axis.speed  # applied when the stage is READY
            self._apply_axis(card, axis, refresh=False)
        self.presets.set_axes({c.worker.name: c.axis for c in self.cards})
        self._arrange(self.axes.order())

        # The top bar: status text, then the controls for all stages. Stop all stays in
        # view at the top right, turns pink while a stage moves, and Esc works from
        # anywhere. Status text and these buttons share one row, so neither needs its own.
        top_bar = QHBoxLayout()
        top_bar.setSpacing(theme.GAP)
        top_bar.addWidget(self.fly, 0, Qt.AlignVCenter)
        top_bar.addWidget(demo_badge, 0, Qt.AlignVCenter)
        top_bar.addWidget(self.status_label, 1, Qt.AlignVCenter)
        top_bar.addWidget(self.connect_all_button)
        top_bar.addWidget(self.stop_all_button)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(theme.MARGIN, theme.MARGIN, theme.MARGIN, theme.MARGIN)
        layout.setSpacing(theme.SECTION_GAP)
        layout.addLayout(top_bar)
        layout.addWidget(self.cards_box)
        layout.addWidget(self.presets, 1)
        self.setCentralWidget(central)
        self._fit_cards()

        where = app_config.presets_path if not demo else "a temporary file (demo)"
        self._resting_message = f"shuffler {__version__}  ·  presets: {where}"
        self.status_label.setText(self._resting_message)
        if self.axes.load_error:
            self.say(self.axes.load_error, 60000)
        # The first time, open compact: the smallest size that shows everything (the
        # presets table shows three rows). After that, the size the user left it at.
        # The key has a version: sizes saved by older versions opened far too tall.
        geometry = self.settings.value(GEOMETRY_KEY)
        if geometry is None or not self.restoreGeometry(geometry):
            self.resize(self.minimumSizeHint())

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(REFRESH_MS)
        self.refresh()

    def refresh(self) -> None:
        statuses = {w.name: w.status for w in self.workers}
        for card in self.cards:
            status = statuses[card.worker.name]
            info = status.info
            if info is not None and card.axis.travel != (info.lower, info.upper):
                # Keep the travel, so a reversed stage shows correct numbers while offline.
                try:
                    axis = self.axes.remember_travel(card.worker.name, info.lower, info.upper)
                except AxisError as error:
                    log.warning("%s", error)
                    axis = replace(card.axis, travel=(info.lower, info.upper))
                self._apply_axis(card, axis)
            speed = status.accepted_speed
            if speed is not None and speed != card.axis.speed:
                # The controller accepted a new speed: keep it for the next session.
                try:
                    self._apply_axis(card, self.axes.remember_speed(card.worker.name, speed))
                except AxisError as error:
                    log.warning("%s", error)
                    card.axis = replace(card.axis, speed=speed)
                self.say(f"{card.axis.label} speed set to {speed:g} {card.config.units}/s.")
            card.show_status(status)
        self._fit_cards()
        self.presets.show_statuses(statuses)
        self.fly.show_states(s.state for s in statuses.values())
        live = any(s.state.busy for s in statuses.values())
        if self.stop_all_button.property("live") != live:
            self.stop_all_button.setProperty("live", live)
            self.stop_all_button.style().unpolish(self.stop_all_button)
            self.stop_all_button.style().polish(self.stop_all_button)
        all_connected = all(s.state.connected for s in statuses.values())
        self._show_connection_button(all_connected=all_connected)
        self.connect_all_button.setEnabled(
            all_connected or any(s.state is State.OFFLINE for s in statuses.values())
        )

    @property
    def all_connected(self) -> bool:
        return all(w.status.state.connected for w in self.workers)

    def _show_connection_button(self, *, all_connected: bool) -> None:
        button = self.connect_all_button
        name = "disconnectAll" if all_connected else ""
        if button.objectName() == name and button.text():
            return
        button.setObjectName(name)
        if all_connected:
            button.setText("Disconnect all")
            button.setIcon(icon("disconnect", theme.PINK_TEXT))
            button.setToolTip("Disconnect every stage. A moving stage is stopped first.")
        else:
            button.setText("Connect all")
            button.setIcon(icon("connect"))
            button.setToolTip("Connect every stage that is not connected.")
        button.style().unpolish(button)
        button.style().polish(button)

    def _connection_button_clicked(self) -> None:
        if self.all_connected:
            self.disconnect_all()
        else:
            self.connect_all()

    def disconnect_all(self) -> None:
        if any(w.status.state.busy for w in self.workers):
            answer = QMessageBox.question(
                self,
                "Disconnect all",
                "A stage is moving. Stop all stages and disconnect?",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                return
        for worker in self.workers:
            worker.disconnect()  # the worker stops a moving stage before it disconnects
        self.say("Disconnecting every stage.", 5000)
        log.info("disconnect all")

    def _arrange(self, order: Sequence[str]) -> None:
        """Put the cards (and the preset columns) in ``order``."""
        while self.cards_row.count():
            self.cards_row.takeAt(0)
        by_key = {card.worker.name: card for card in self.cards}
        self.cards = [by_key[key] for key in order]
        for index, card in enumerate(self.cards):
            self.cards_row.addWidget(card)
            card.set_position_in_row(index, len(self.cards))
        self.cards_row.addStretch(1)
        self.presets.set_order(order)

    def _fit_cards(self) -> None:
        """One width for every card: the widest that any card's controls need.

        At least CARD_WIDTH, so no control is cut and the cards line up. Heights need no
        help: every widget in a card reports its real height (see gui/widgets.py).
        """
        width = max((card.needed_width() for card in self.cards), default=theme.CARD_WIDTH)
        for card in self.cards:
            card.set_card_width(width)

    def say(self, text: str, milliseconds: int = 8000) -> None:
        """Show a message in the status line, then go back to the resting text."""
        self.status_label.setText(text)
        self._message_timer.start(milliseconds)

    def _apply_axis(self, card: StageCard, axis: Axis, *, refresh: bool = True) -> None:
        """Give a stage's name and direction to its worker (messages), card, and presets."""
        worker = card.worker
        worker.label = axis.label
        worker.display = axis.to_display
        card.set_axis(axis)
        if refresh:
            self.presets.set_axes({c.worker.name: c.axis for c in self.cards})

    def edit_stage(self, card: StageCard) -> None:
        """Name a stage, give it direction words, and reverse it if + moves it the wrong way."""
        worker = card.worker
        dialog = self.make_axis_dialog(card)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            axis = self.axes.update(
                worker.name,
                label=dialog.name(),
                direction=dialog.direction(),
                reversed=dialog.reversed(),
            )
        except AxisError as error:
            QMessageBox.warning(self, "Stage name and direction", str(error))
            return
        old = card.axis
        self._apply_axis(card, axis)
        log.info(
            "stage %s: name %r -> %r, direction %s -> %s, reversed %s -> %s",
            worker.name,
            old.label,
            axis.label,
            old.direction,
            axis.direction,
            old.reversed,
            axis.reversed,
        )
        self.say(f"Saved the name and direction of {axis.label}.")

    def make_axis_dialog(self, card: StageCard) -> AxisDialog:
        """Separate, so the tests can fill the dialog without showing it."""
        return AxisDialog(card.worker.name, card.config.address, card.axis, self)

    def move_stage(self, card: StageCard, offset: int) -> None:
        try:
            order = self.axes.move(card.worker.name, offset)
        except AxisError as error:
            QMessageBox.warning(self, "Move card", str(error))
            return
        self._arrange(order)

    def connect_all(self) -> None:
        for worker in self.workers:
            if not worker.status.state.connected:
                worker.connect()

    def stop_all(self) -> None:
        for worker in self.workers:
            worker.stop()
        self.fly.startle()  # after the stops are queued, never before
        self.say("Stop sent to every stage.", 5000)
        log.info("stop all")

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            event.type() == QEvent.KeyPress
            and isinstance(event, QKeyEvent)
            and event.key() == Qt.Key_Escape
            and isinstance(watched, QWidget)
        ):
            widget = watched
            while widget is not None:
                if widget is self:
                    self.stop_all()
                    return True
                widget = widget.parentWidget()
        return super().eventFilter(watched, event)

    def closeEvent(self, event: QCloseEvent) -> None:
        if any(w.status.state.busy for w in self.workers):
            answer = QMessageBox.question(
                self,
                "Quit",
                "A stage is moving. Stop all stages and quit?",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        self.timer.stop()
        self.settings.setValue(GEOMETRY_KEY, self.saveGeometry())
        self.stop_all()
        for worker in self.workers:
            worker.shutdown()  # stops a moving stage, then disconnects
        event.accept()


def setup_logging(folder: Path | None) -> None:
    root = logging.getLogger("shuffler")
    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)
    if folder is not None:
        try:
            file = RotatingFileHandler(
                folder / "shuffler.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
            )
        except OSError as error:
            log.warning("No log file: %s", error)
        else:
            file.setFormatter(formatter)
            root.addHandler(file)


APP_ID = "ClarkLab.shuffler"  # also on the shortcut that `shuffler shortcut` makes


def set_windows_app_id() -> None:
    """Group the window with its pinned taskbar shortcut, under the shuffler icon.

    The window belongs to a python process, so Windows would group it with Python, not
    with the shortcut. An explicit app ID, the same on the process and the shortcut,
    makes them one taskbar button. Call before the first window.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except (OSError, AttributeError):
        log.warning("Cannot set the Windows app ID; the taskbar may group shuffler with Python")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shuffler-gui", description="Motorized stage control")
    parser.add_argument("--config", type=Path, default=Path("stages.toml"))
    parser.add_argument("--demo", action="store_true", help="simulated stages, no hardware")
    args = parser.parse_args(argv)

    set_windows_app_id()
    application = QApplication.instance() or QApplication(sys.argv[:1])
    application.setApplicationName("shuffler")
    theme.apply(application)

    try:
        if args.demo and not args.config.exists():
            app_config = config_module.parse(DEMO_CONFIG, base=Path.cwd())
        else:
            app_config = config_module.load(args.config)
    except ConfigError as error:
        QMessageBox.critical(None, "shuffler", str(error))
        return 2

    if args.demo:
        presets_path = Path(tempfile.gettempdir()) / "shuffler-demo-presets.json"
        axes_path = Path(tempfile.gettempdir()) / "shuffler-demo-axes.json"
        setup_logging(None)
    else:
        presets_path = app_config.presets_path
        axes_path = app_config.axes_path
        setup_logging(app_config.source.parent if app_config.source else None)
    log.info("start%s with %s", " (demo)" if args.demo else "", ", ".join(app_config.stage_names))

    make = driver_factory(app_config, demo=args.demo)
    workers = [StageWorker(stage, make) for stage in app_config.stages]
    for worker in workers:
        worker.start()
    store = PresetStore(presets_path)
    axes = AxisStore(axes_path, app_config.stage_names)
    window = MainWindow(app_config, workers, store, axes=axes, demo=args.demo)
    window.show()
    if app_config.connect_on_start:
        window.connect_all()
    code = application.exec()
    log.info("quit")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
