"""The stagehand window: a card per stage, Stop all, and the presets.

uv run stagehand-gui                  uses stages.toml in the current folder
uv run stagehand-gui --config FILE
uv run stagehand-gui --demo           simulated stages; no hardware is touched
"""

import argparse
import logging
import sys
import tempfile
from collections.abc import Sequence
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from stagehand import __version__
from stagehand import config as config_module
from stagehand.config import AppConfig, ConfigError
from stagehand.gui import theme
from stagehand.gui.icons import icon
from stagehand.gui.presets_panel import PresetsPanel
from stagehand.gui.stage_card import StageCard
from stagehand.presets import PresetStore
from stagehand.rig import driver_factory
from stagehand.worker import StageWorker

log = logging.getLogger("stagehand")
REFRESH_MS = 100

DEMO_CONFIG = {
    "app": {"connect_on_start": True},
    "stage": [
        {"name": "CONEX A", "driver": "conex", "port": "COM3"},
        {"name": "CONEX B", "driver": "conex", "port": "COM4"},
        {
            "name": "DDS100",
            "driver": "kinesis",
            "serial": "28253849",
            "jog_steps": [0.01, 0.1, 1.0, 10.0],
        },
    ],
}


class MainWindow(QMainWindow):
    def __init__(
        self,
        app_config: AppConfig,
        workers: Sequence[StageWorker],
        store: PresetStore,
        *,
        demo: bool = False,
        settings: QSettings | None = None,
    ) -> None:
        super().__init__()
        self.app_config = app_config
        self.workers = list(workers)
        self.settings = settings or QSettings("ClarkLab", "stagehand")
        self.setWindowTitle("stagehand" + (" (demo)" if demo else ""))

        title = QLabel("stagehand")
        title.setFont(theme.font(theme.TITLE_PX, bold=True))
        subtitle = QLabel("Motorized stages")
        subtitle.setObjectName("secondary")
        demo_badge = QLabel("DEMO: simulated stages")
        demo_badge.setObjectName("demo")
        demo_badge.setVisible(demo)
        self.connect_all_button = QPushButton(icon("connect"), "Connect all")
        self.connect_all_button.clicked.connect(self.connect_all)
        self.stop_all_button = QPushButton(icon("stop", theme.PANEL), "Stop all   Esc")
        self.stop_all_button.setObjectName("stopAll")
        self.stop_all_button.setToolTip("Stop every stage now (Esc).")
        self.stop_all_button.clicked.connect(self.stop_all)
        QShortcut(
            QKeySequence(Qt.Key_Escape),
            self,
            activated=self.stop_all,
            context=Qt.ApplicationShortcut,
        )

        header = QHBoxLayout()
        header.setSpacing(theme.GAP)
        header.addWidget(title)
        header.addWidget(subtitle, 0, Qt.AlignBottom)
        header.addSpacing(theme.UNIT)
        header.addWidget(demo_badge)
        header.addStretch(1)
        header.addWidget(self.connect_all_button)
        header.addWidget(self.stop_all_button)

        self.cards = [
            StageCard(w, confirm_moves=app_config.confirm_moves, settings=self.settings)
            for w in self.workers
        ]
        cards_row = QHBoxLayout()
        cards_row.setSpacing(theme.SECTION_GAP)
        for card in self.cards:
            cards_row.addWidget(card)
        cards_row.addStretch(1)

        self.presets = PresetsPanel(store, self.workers, confirm_moves=app_config.confirm_moves)
        self.presets.message.connect(lambda text: self.statusBar().showMessage(text, 8000))

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(theme.MARGIN, theme.MARGIN, theme.MARGIN, theme.UNIT)
        layout.setSpacing(theme.SECTION_GAP)
        layout.addLayout(header)
        layout.addLayout(cards_row)
        layout.addWidget(self.presets, 1)
        self.setCentralWidget(central)

        where = app_config.presets_path if not demo else "a temporary file (demo)"
        self.statusBar().showMessage(f"stagehand {__version__}. Presets: {where}")
        geometry = self.settings.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        else:
            width = len(self.cards) * (theme.CARD_WIDTH + theme.SECTION_GAP) + 2 * theme.MARGIN
            self.resize(max(width, 760), 820)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(REFRESH_MS)
        self.refresh()

    def refresh(self) -> None:
        statuses = {w.name: w.status for w in self.workers}
        for card in self.cards:
            card.show_status(statuses[card.worker.name])
        self.presets.show_statuses(statuses)
        live = any(s.state.busy for s in statuses.values())
        if self.stop_all_button.property("live") != live:
            self.stop_all_button.setProperty("live", live)
            self.stop_all_button.style().unpolish(self.stop_all_button)
            self.stop_all_button.style().polish(self.stop_all_button)
        self.connect_all_button.setEnabled(any(not s.state.connected for s in statuses.values()))

    def connect_all(self) -> None:
        for worker in self.workers:
            if not worker.status.state.connected:
                worker.connect()

    def stop_all(self) -> None:
        for worker in self.workers:
            worker.stop()
        self.statusBar().showMessage("Stop sent to every stage.", 5000)
        log.info("stop all")

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
        self.settings.setValue("window/geometry", self.saveGeometry())
        for worker in self.workers:
            worker.shutdown()  # stops a moving stage, then disconnects
        event.accept()


def setup_logging(folder: Path | None) -> None:
    root = logging.getLogger("stagehand")
    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)
    if folder is not None:
        try:
            file = RotatingFileHandler(
                folder / "stagehand.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
            )
        except OSError as error:
            log.warning("No log file: %s", error)
        else:
            file.setFormatter(formatter)
            root.addHandler(file)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stagehand-gui", description="Motorized stage control")
    parser.add_argument("--config", type=Path, default=Path("stages.toml"))
    parser.add_argument("--demo", action="store_true", help="simulated stages, no hardware")
    args = parser.parse_args(argv)

    application = QApplication.instance() or QApplication(sys.argv[:1])
    application.setApplicationName("stagehand")
    theme.apply(application)

    try:
        if args.demo and not args.config.exists():
            app_config = config_module.parse(DEMO_CONFIG, base=Path.cwd())
        else:
            app_config = config_module.load(args.config)
    except ConfigError as error:
        QMessageBox.critical(None, "stagehand", str(error))
        return 2

    if args.demo:
        presets_path = Path(tempfile.gettempdir()) / "stagehand-demo-presets.json"
        setup_logging(None)
    else:
        presets_path = app_config.presets_path
        setup_logging(app_config.source.parent if app_config.source else None)
    log.info("start%s with %s", " (demo)" if args.demo else "", ", ".join(app_config.stage_names))

    make = driver_factory(app_config, demo=args.demo)
    workers = [StageWorker(stage, make) for stage in app_config.stages]
    for worker in workers:
        worker.start()
    store = PresetStore(presets_path)
    window = MainWindow(app_config, workers, store, demo=args.demo)
    window.show()
    if app_config.connect_on_start:
        window.connect_all()
    code = application.exec()
    log.info("quit")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
