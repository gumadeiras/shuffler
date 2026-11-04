"""One QApplication and a controlled clock shared by the GUI tests."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from shuffler.gui import theme

APP = QApplication.instance() or QApplication([])
theme.apply(APP)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now
