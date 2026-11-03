"""The window on the offscreen Qt platform, with simulated stages driven without threads."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

from stagehand import config
from stagehand.fakes import FakeStage
from stagehand.gui import theme
from stagehand.gui.app import MainWindow
from stagehand.gui.presets_panel import GoToPresetDialog, SavePresetDialog, at_preset
from stagehand.labels import LabelStore
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

    def test_target_field_is_visible_and_out_of_range_targets_are_refused(self) -> None:
        self.window.show()
        APP.processEvents()
        self.window.connect_all()
        for worker in self.workers:
            worker.home()
        self.run_all()
        self.settle()
        card = self.card("X")
        self.assertGreater(card.target_box.width(), 100, "the field must not collapse")
        self.assertEqual(card.target_box.text(), "12.5000")
        card.target_box.setText("22.5")  # max is 20 in this config
        card.target_box.returnPressed.emit()
        self.run_all()
        self.assertIn("outside the allowed range", self.workers[0].status.error)
        self.assertNotIn("move_to 22.5", self.fakes["X"].commands)

    def rename(self, key: str, text: str) -> None:
        card = self.card(key)
        with mock.patch.object(QInputDialog, "getText", return_value=(text, True)):
            card.name_label.double_clicked.emit()

    def test_rename_a_stage_in_the_window(self) -> None:
        self.store.save("Rest", {"X": 1.0, "Z": 2.0})
        self.window.presets.refresh()
        self.rename("Z", "Odor Z")
        card = self.card("Z")
        self.assertEqual(card.name_label.text(), "Odor Z")
        self.assertIn("SN 28253849 · Z", card.model_label.text(), "the toml name stays visible")
        self.assertEqual(self.workers[1].label, "Odor Z")
        header = self.window.presets.table.horizontalHeaderItem(2).text()
        self.assertEqual(header, "Odor Z")
        self.assertEqual(self.store.get("Rest").positions, {"X": 1.0, "Z": 2.0}, "keys unchanged")

        # Persisted: a new window over the same files shows the new label.
        self.window.timer.stop()
        again = MainWindow(
            self.app_config,
            [StageWorker(s, lambda c: self.fakes[c.name]) for s in self.app_config.stages],
            self.store,
            settings=QSettings(str(Path(self.folder.name) / "ui2.ini"), QSettings.IniFormat),
        )
        self.assertEqual(
            next(c for c in again.cards if c.worker.name == "Z").name_label.text(), "Odor Z"
        )
        again.timer.stop()

    def test_worker_messages_use_the_label(self) -> None:
        self.rename("X", "Odor X")
        self.workers[0].move_to(1.0)
        self.run_all()
        self.assertIn("Odor X is", self.workers[0].status.error)

    def test_duplicate_label_is_refused(self) -> None:
        self.rename("X", "Same")
        with mock.patch.object(QMessageBox, "warning") as warning:
            self.rename("Z", "same")
        warning.assert_called_once()
        self.assertEqual(self.card("Z").name_label.text(), "Z")

    def test_move_cards_and_preset_columns(self) -> None:
        self.card("Z").move_requested.emit(-1)
        self.assertEqual([c.worker.name for c in self.window.cards], ["Z", "X"])
        self.assertEqual(self.window.presets.table.horizontalHeaderItem(1).text(), "Z")
        self.assertFalse(self.card("Z").left_action.isEnabled())
        self.assertTrue(self.card("Z").right_action.isEnabled())
        labels = LabelStore(self.app_config.labels_path, self.app_config.stage_names)
        self.assertEqual(labels.order(), ["Z", "X"])


class ThemeTest(unittest.TestCase):
    def test_text_contrast(self) -> None:
        for label, (foreground, background) in theme.TEXT_PAIRS.items():
            with self.subTest(label):
                self.assertGreaterEqual(theme.contrast_ratio(foreground, background), 4.5)

    def test_every_state_has_a_pill(self) -> None:
        self.assertEqual(set(theme.PILL), set(State))


if __name__ == "__main__":
    unittest.main()
