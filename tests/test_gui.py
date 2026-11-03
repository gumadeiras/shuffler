"""The window on the offscreen Qt platform, with simulated stages driven without threads."""

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from stagehand import config
from stagehand.fakes import FakeStage
from stagehand.gui import theme
from stagehand.gui.app import MainWindow
from stagehand.gui.presets_panel import GoToPresetDialog, SavePresetDialog, at_preset
from stagehand.presets import PresetStore
from stagehand.stage import State
from stagehand.worker import StageWorker

APP = QApplication.instance() or QApplication([])
theme.apply(APP)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class WindowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        folder = Path(self.folder.name)
        self.clock = Clock()
        self.app_config = config.parse(
            {
                "app": {"confirm_moves": False},
                "stage": [
                    {"name": "X", "driver": "conex", "port": "COM3", "max": 20.0},
                    {"name": "Z", "driver": "kinesis", "serial": "28253849"},
                ],
            },
            base=folder,
        )
        self.fakes = {
            "X": FakeStage("COM3", upper=25.0, start=12.5, velocity=10.0, clock=self.clock),
            "Z": FakeStage("SN 28253849", upper=100.0, start=50.0, velocity=50.0, clock=self.clock),
        }
        self.workers = [
            StageWorker(stage, lambda c: self.fakes[c.name]) for stage in self.app_config.stages
        ]
        self.store = PresetStore(folder / "presets.json")
        settings = QSettings(str(folder / "ui.ini"), QSettings.IniFormat)
        self.window = MainWindow(self.app_config, self.workers, self.store, settings=settings)

    def tearDown(self) -> None:
        self.window.timer.stop()
        self.window.deleteLater()
        self.folder.cleanup()

    def run_all(self) -> None:
        for worker in self.workers:
            while not worker._commands.empty():
                worker.tick(0)
            worker.tick(0)
        self.window.refresh()

    def settle(self) -> None:
        self.clock.now += 10.0
        self.run_all()

    def card(self, name: str):
        return next(c for c in self.window.cards if c.worker.name == name)

    def test_offline_cards_offer_connect_only(self) -> None:
        card = self.card("X")
        self.assertEqual(card.pill.text(), "Offline")
        self.assertFalse(card.go_button.isEnabled())
        self.assertFalse(card.connect_button.isHidden())
        self.assertTrue(card.home_button.isHidden())

    def test_not_homed_card_points_to_home(self) -> None:
        self.window.connect_all()
        self.run_all()
        card = self.card("X")
        self.assertEqual(card.pill.text(), "Not homed")
        self.assertFalse(card.go_button.isEnabled())
        self.assertTrue(card.home_button.isEnabled())
        self.assertEqual(card.home_button.objectName(), "primary")
        self.assertEqual(card.position_label.text(), "12.5000")
        self.assertEqual(card.target_box.value(), 12.5)

    def test_home_then_go_from_the_card(self) -> None:
        self.window.connect_all()
        self.run_all()
        card = self.card("X")
        card.home_button.click()
        self.run_all()
        self.assertEqual(card.pill.text(), "Homing")
        self.settle()
        self.assertEqual(card.pill.text(), "Ready")
        self.assertEqual(card.target_box.maximum(), 20.0, "the configured max narrows the box")
        card.target_box.setValue(7.25)
        card.go_button.click()
        self.run_all()
        self.assertTrue(self.window.stop_all_button.property("live"))
        self.settle()
        self.assertEqual(card.position_label.text(), "7.2500")
        self.assertFalse(self.window.stop_all_button.property("live"))

    def test_stop_all_reaches_every_stage(self) -> None:
        self.window.connect_all()
        self.run_all()
        self.window.stop_all()
        self.run_all()
        for fake in self.fakes.values():
            self.assertIn("stop", fake.commands)

    def test_presets_save_match_and_go(self) -> None:
        self.window.connect_all()
        for card in self.window.cards:
            card.worker.home()
        self.run_all()
        self.settle()
        statuses = {w.name: w.status for w in self.workers}

        dialog = SavePresetDialog(list(statuses.values()), {"X": 4, "Z": 4}, self.window)
        dialog.name_edit.setText("Rest")
        self.assertEqual(dialog.positions(), {"X": 0.0, "Z": 0.0})
        self.store.save(dialog.name(), dialog.positions())
        self.window.presets.refresh(select="Rest")
        self.assertTrue(at_preset(self.store.get("Rest"), statuses))

        self.store.save("Far", {"X": 30.0, "Z": 60.0})  # X is outside its configured max
        go = GoToPresetDialog(self.store.get("Far"), statuses, {"X": 4, "Z": 4}, self.window)
        self.assertEqual(go.targets(), {"Z": 60.0})
        self.assertFalse(go.checks["X"].isEnabled())

        self.window.presets.refresh(select="Far")
        self.window.presets.go_to_selected()  # confirm_moves is off: moves at once
        self.run_all()
        self.settle()
        self.assertEqual(self.workers[1].status.position, 60.0)
        self.assertEqual(self.workers[0].status.position, 0.0)

    def test_saving_skips_stages_that_are_not_homed(self) -> None:
        self.window.connect_all()
        self.run_all()
        statuses = [w.status for w in self.workers]
        dialog = SavePresetDialog(statuses, {"X": 4, "Z": 4}, self.window)
        self.assertEqual(dialog.positions(), {})
        self.assertFalse(dialog.checks["X"].isEnabled())


class ThemeTest(unittest.TestCase):
    def test_text_contrast(self) -> None:
        for label, (foreground, background) in theme.TEXT_PAIRS.items():
            with self.subTest(label):
                self.assertGreaterEqual(theme.contrast_ratio(foreground, background), 4.5)

    def test_every_state_has_a_pill(self) -> None:
        self.assertEqual(set(theme.PILL), set(State))


if __name__ == "__main__":
    unittest.main()
