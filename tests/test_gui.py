"""The window on the offscreen Qt platform, with simulated stages driven without threads."""

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QSize, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from shuffler import config
from shuffler.axes import AxisStore
from shuffler.fakes import FakeStage
from shuffler.gui import fly as fly_module
from shuffler.gui import theme
from shuffler.gui.app import MainWindow
from shuffler.gui.fly import FlyWidget
from shuffler.gui.presets_panel import GoToPresetDialog, SavePresetDialog, at_preset
from shuffler.presets import PresetStore
from shuffler.stage import DeviceError, State
from shuffler.worker import StageWorker

APP = QApplication.instance() or QApplication([])
theme.apply(APP)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FilledAxisDialog:
    """Stands in for AxisDialog: returns the given answers without showing anything."""

    def __init__(self, name: str, direction: str = "none", reversed: bool = False) -> None:  # noqa: A002
        self._answers = (name, direction, reversed)

    def exec(self) -> int:
        return QDialog.Accepted

    def name(self) -> str:
        return self._answers[0]

    def direction(self) -> str:
        return self._answers[1]

    def reversed(self) -> bool:
        return self._answers[2]


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
            StageWorker(stage, lambda c: self.fakes[c.name], clock=self.clock)
            for stage in self.app_config.stages
        ]
        self.store = PresetStore(folder / "presets.json")
        self.window = self.new_window(self.workers, "ui.ini")

    def new_window(self, workers: list[StageWorker], ini: str) -> MainWindow:
        settings = QSettings(str(Path(self.folder.name) / ini), QSettings.IniFormat)
        return MainWindow(self.app_config, workers, self.store, settings=settings)

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

    def ready(self) -> None:
        self.window.connect_all()
        for worker in self.workers:
            worker.home()
        self.run_all()
        self.settle()

    def card(self, name: str):
        return next(c for c in self.window.cards if c.worker.name == name)

    def edit(self, key: str, name: str, direction: str = "none", reversed: bool = False) -> None:  # noqa: A002
        answers = FilledAxisDialog(name, direction, reversed)
        with mock.patch.object(self.window, "make_axis_dialog", return_value=answers):
            self.card(key).name_label.double_clicked.emit()

    # -- cards ------------------------------------------------------------------------------

    def test_offline_cards_offer_connect_only(self) -> None:
        card = self.card("X")
        self.assertEqual(card.pill.text(), "Offline")
        self.assertEqual(card.detail_label.text(), "Not connected.")
        self.assertFalse(card.go_button.isEnabled())
        self.assertFalse(card.connect_button.isHidden())
        self.assertTrue(card.home_button.isHidden())
        self.assertTrue(card.unit_label.isHidden(), "no unit next to an unknown position")

    def test_not_homed_card_points_to_home(self) -> None:
        self.window.connect_all()
        self.run_all()
        card = self.card("X")
        self.assertEqual(card.pill.text(), "Not homed")
        self.assertEqual(card.detail_label.text(), "Home this stage before you move it.")
        self.assertFalse(card.go_button.isEnabled())
        self.assertTrue(card.home_button.isEnabled())
        self.assertEqual(card.home_button.objectName(), "primary")
        self.assertEqual(card.position_label.text(), "12.5000")

    def test_a_click_in_a_number_field_selects_the_value(self) -> None:
        from PySide6.QtTest import QTest

        self.window.show()
        self.window.activateWindow()
        APP.processEvents()
        self.ready()
        card = self.card("X")
        for box in (card.target_box, card.speed_box):
            with self.subTest(box=box.placeholderText()):
                self.window.setFocus()  # out of the field
                APP.processEvents()
                self.assertTrue(box.text(), "the field shows a value")
                QTest.mouseClick(box, Qt.LeftButton)
                APP.processEvents()
                self.assertTrue(box.hasFocus())
                self.assertEqual(box.selectedText(), box.text(), "typing replaces the value")
                QTest.keyClicks(box, "7")
                self.assertEqual(box.text(), "7")
                QTest.mouseClick(box, Qt.LeftButton)  # a second click places the cursor
                APP.processEvents()
                self.assertFalse(box.hasSelectedText())

    def test_connecting_and_disconnecting_keep_every_size(self) -> None:
        """Disconnecting made every card wider (Connect, Home, and Stop all counted for a
        moment) and the window grew; the subtitle and travel ends changed the height."""
        self.window.show()
        APP.processEvents()

        def sizes() -> list[tuple[int, int]]:
            APP.processEvents()
            return [(c.width(), c.height()) for c in self.window.cards] + [
                (self.window.width(), self.window.height())
            ]

        offline = sizes()
        self.ready()
        steps = {"ready": sizes()}
        self.workers[0].disconnect()
        self.run_all()
        steps["one disconnected"] = sizes()
        self.window.disconnect_all()
        self.run_all()
        steps["all disconnected"] = sizes()
        self.window.connect_all()
        self.run_all()
        steps["reconnected"] = sizes()
        for step, measured in steps.items():
            with self.subTest(step=step):
                self.assertEqual(measured, offline)

    def test_connect_all_becomes_disconnect_all(self) -> None:
        button = self.window.connect_all_button
        self.assertEqual(button.text(), "Connect all")
        self.workers[0].connect()  # one of two: still Connect all, for the other one
        self.run_all()
        self.assertEqual(button.text(), "Connect all")
        self.assertTrue(button.isEnabled())
        width = button.width()
        button.click()
        self.run_all()
        self.assertEqual(button.text(), "Disconnect all")
        self.assertEqual(button.objectName(), "disconnectAll", "the red style")
        self.assertEqual(button.width(), width, "the top bar does not jump")
        button.click()
        self.run_all()
        self.assertEqual([w.status.state for w in self.workers], [State.OFFLINE] * 2)
        self.assertEqual(button.text(), "Connect all")

    def test_disconnect_all_asks_while_a_stage_moves(self) -> None:
        self.ready()
        self.card("X").forward_button.click()
        self.run_all()
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Cancel):
            self.window.connect_all_button.click()
        self.run_all()
        self.assertTrue(all(w.status.state.connected for w in self.workers), "cancelled")
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            self.window.connect_all_button.click()
        self.run_all()
        self.assertIn("stop", self.fakes["X"].commands)
        self.assertEqual([w.status.state for w in self.workers], [State.OFFLINE] * 2)

    def test_a_fault_is_shown_and_blocks_home(self) -> None:
        self.fakes["X"].fault = "DC voltage too low. Check the 24 V power supply"
        self.window.connect_all()
        self.run_all()
        card = self.card("X")
        self.assertFalse(card.home_button.isEnabled())
        self.assertNotEqual(card.home_button.objectName(), "primary")
        self.assertIn("24 V", card.home_button.toolTip())
        self.assertEqual(
            card.error_label.text(), "DC voltage too low. Check the 24 V power supply."
        )
        self.assertIn("hardware error", card.detail_label.text())
        self.assertTrue(self.card("Z").home_button.isEnabled(), "only the faulty stage")
        self.fakes["X"].fault = ""
        self.run_all()
        self.assertTrue(card.home_button.isEnabled())
        self.assertEqual(card.error_label.text().strip(), "")

    def test_home_then_go_from_the_card(self) -> None:
        self.window.connect_all()
        self.run_all()
        card = self.card("X")
        card.home_button.click()
        self.run_all()
        self.assertEqual(card.pill.text(), "Homing")
        self.settle()
        self.assertEqual(card.pill.text(), "Ready")
        self.assertEqual(card.target_box.text(), "0.0000", "the field starts at the position")
        self.assertEqual(card.target_box.maximum(), 20.0, "the configured max narrows the range")
        card.target_box.setValue(7.25)
        card.go_button.click()
        self.run_all()
        self.assertTrue(self.window.stop_all_button.property("live"))
        self.assertEqual(card.detail_label.text(), "Moving to 7.2500 mm")
        self.settle()
        self.assertEqual(card.position_label.text(), "7.2500")
        self.assertFalse(self.window.stop_all_button.property("live"))

    def test_target_field_follows_the_stage_after_a_jog(self) -> None:
        self.ready()
        card = self.card("X")
        card.forward_button.click()  # 0.1 mm, the default step
        self.run_all()
        self.settle()
        self.assertEqual(card.target_box.text(), "0.1000")

    def test_target_field_is_visible_and_out_of_range_targets_are_refused(self) -> None:
        self.window.show()
        APP.processEvents()
        self.ready()
        card = self.card("X")
        self.assertGreater(card.target_box.width(), 100, "the field must not collapse")
        card.target_box.setText("22.5")  # max is 20 in this config
        card.target_box.returnPressed.emit()
        self.run_all()
        self.assertIn("outside the allowed range", self.workers[0].status.error)
        self.assertNotIn("move_to 22.5", self.fakes["X"].commands)

    def test_stop_all_reaches_every_stage(self) -> None:
        self.window.connect_all()
        self.run_all()
        self.window.stop_all()
        self.run_all()
        for fake in self.fakes.values():
            self.assertIn("stop", fake.commands)

    def test_escape_stops_all_stages_with_a_modal_dialog_open(self) -> None:
        self.window.show()
        APP.processEvents()
        self.ready()
        dialog = GoToPresetDialog(
            self.store.save("Rest", {"X": 0.0}),
            {w.name: w.status for w in self.workers},
            self.window,
        )
        dialog.open()
        APP.processEvents()
        QTest.keyClick(dialog.move_button, Qt.Key_Escape)
        self.run_all()
        for fake in self.fakes.values():
            self.assertIn("stop", fake.commands)
        dialog.reject()

    def test_close_queues_every_stop_before_waiting_for_any_worker(self) -> None:
        self.ready()
        first = self.workers[0]
        shutdown = first.shutdown

        def check_stops():
            self.assertTrue(all(w._abort.is_set() for w in self.workers))
            shutdown()

        with mock.patch.object(first, "shutdown", side_effect=check_stops):
            self.window.close()
        for fake in self.fakes.values():
            self.assertEqual(fake.commands[-2:], ["stop", "close"])

    # -- presets ----------------------------------------------------------------------------

    def test_presets_save_match_and_go(self) -> None:
        self.ready()
        statuses = {w.name: w.status for w in self.workers}

        dialog = SavePresetDialog(list(statuses.values()), self.window)
        dialog.name_edit.setText("Rest")
        self.assertEqual(dialog.positions(), {"X": 0.0, "Z": 0.0})
        self.store.save(dialog.name(), dialog.positions())
        self.window.presets.refresh(select="Rest")
        self.assertTrue(at_preset(self.store.get("Rest"), statuses))

        self.store.save("Far", {"X": 30.0, "Z": 60.0})  # X is outside its configured max
        go = GoToPresetDialog(self.store.get("Far"), statuses, self.window)
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
        dialog = SavePresetDialog([w.status for w in self.workers], self.window)
        self.assertEqual(dialog.positions(), {})
        self.assertFalse(dialog.checks["X"].isEnabled())

    def test_poll_failure_blocks_controls_and_presets_until_contact_recovers(self) -> None:
        self.ready()
        worker = self.workers[0]
        preset = self.store.save("Rest", {"X": 0.0})
        self.fakes["X"].fail_read = "No answer."
        worker.tick(0)
        self.window.refresh()
        self.assertFalse(self.card("X").go_button.isEnabled())
        self.assertFalse(self.card("X").forward_button.isEnabled())
        self.assertIn("contact", self.card("X").error_label.text())
        statuses = {w.name: w.status for w in self.workers}
        go = GoToPresetDialog(preset, statuses, self.window)
        self.assertEqual(go.targets(), {})
        self.assertFalse(at_preset(preset, statuses), "a stale position must not mark a preset")
        save = SavePresetDialog(list(statuses.values()), self.window)
        self.assertNotIn("X", save.positions())
        self.fakes["X"].fail_read = None
        worker.tick(0)
        self.window.refresh()
        self.assertTrue(self.card("X").go_button.isEnabled())
        go = GoToPresetDialog(preset, {w.name: w.status for w in self.workers}, self.window)
        self.assertEqual(go.targets(), {"X": 0.0})

    def test_speed_block_is_shared_by_controls_preview_and_worker(self) -> None:
        worker = self.workers[0]
        worker.config = replace(worker.config, max_velocity=4.0)
        with mock.patch.object(
            self.fakes["X"], "set_velocity", side_effect=DeviceError("Refused.")
        ):
            self.ready()
        preset = self.store.save("Both", {"X": 5.0, "Z": 5.0})
        statuses = {w.name: w.status for w in self.workers}
        go = GoToPresetDialog(preset, statuses, self.window)
        self.assertEqual(go.targets(), {"Z": 5.0})
        self.assertFalse(self.card("X").go_button.isEnabled())
        self.assertTrue(self.card("X").speed_box.isEnabled(), "a valid speed can repair the block")
        worker.move_to(5.0)
        self.run_all()
        self.assertNotIn("move_to 5", self.fakes["X"].commands)
        self.window.presets.refresh(select="Both")
        self.window.presets.go_to_selected()
        self.run_all()
        self.assertIn("Not moved: X", self.window.status_label.text())
        self.assertIn("move_to 5", self.fakes["Z"].commands)
        worker.set_velocity(2.0)
        self.run_all()
        self.assertTrue(self.card("X").go_button.isEnabled())

    def test_preset_rechecks_contact_after_the_confirmation_dialog(self) -> None:
        self.ready()
        self.store.save("Both", {"X": 5.0, "Z": 5.0})
        panel = self.window.presets
        panel.refresh(select="Both")
        panel._confirm = True

        def lose_contact(_dialog: GoToPresetDialog) -> int:
            self.fakes["X"].fail_read = "No answer."
            self.workers[0].tick(0)
            return QDialog.Accepted

        with mock.patch.object(GoToPresetDialog, "exec", lose_contact):
            panel.go_to_selected()
        self.assertTrue(self.workers[0]._commands.empty())
        self.run_all()
        self.assertIn("Not moved: X", self.window.status_label.text())
        self.assertIn("move_to 5", self.fakes["Z"].commands)

    def test_preset_boundary_uses_the_same_rounding_as_the_worker(self) -> None:
        self.ready()
        panel = self.window.presets
        preset = self.store.save("Edge", {"X": 20.00004})
        go = GoToPresetDialog(
            preset, {w.name: w.status for w in self.workers}, self.window, panel.views()
        )
        self.assertEqual(go.targets(), {"X": 20.00004})
        panel.refresh(select="Edge")
        panel.go_to_selected()
        self.run_all()
        self.settle()
        self.assertEqual(self.workers[0].status.position, 20.0)
        preset = self.store.save("Outside", {"X": 20.00006})
        go = GoToPresetDialog(
            preset, {w.name: w.status for w in self.workers}, self.window, panel.views()
        )
        self.assertEqual(go.targets(), {})
        self.workers[0].move_to(20.00006)
        self.run_all()
        self.assertIn("outside the allowed range", self.workers[0].status.error)

    # -- names, directions, order -----------------------------------------------------------

    def test_name_a_stage_in_the_window(self) -> None:
        self.store.save("Rest", {"X": 1.0, "Z": 2.0})
        self.window.presets.refresh()
        self.edit("Z", "Odor Z")
        card = self.card("Z")
        self.assertEqual(card.name_label.text(), "Odor Z")
        self.assertIn("SN 28253849  ·  Z", card.model_label.text(), "the toml name stays visible")
        self.assertEqual(self.workers[1].label, "Odor Z")
        self.assertEqual(self.window.presets.table.horizontalHeaderItem(2).text(), "Odor Z")
        self.assertEqual(self.store.get("Rest").positions, {"X": 1.0, "Z": 2.0}, "keys unchanged")

        self.window.timer.stop()
        workers = [StageWorker(s, lambda c: self.fakes[c.name]) for s in self.app_config.stages]
        again = self.new_window(workers, "ui2.ini")
        self.assertEqual(
            next(c for c in again.cards if c.worker.name == "Z").name_label.text(), "Odor Z"
        )
        again.timer.stop()

    def test_worker_messages_use_the_name(self) -> None:
        self.edit("X", "Odor X")
        self.workers[0].move_to(1.0)
        self.run_all()
        self.assertIn("Odor X is", self.workers[0].status.error)

    def test_duplicate_name_is_refused(self) -> None:
        self.edit("X", "Same")
        with mock.patch.object(QMessageBox, "warning") as warning:
            self.edit("Z", "same")
        warning.assert_called_once()
        self.assertEqual(self.card("Z").name_label.text(), "Z")

    def test_direction_words_label_the_controls(self) -> None:
        self.ready()
        self.edit("X", "X", "back-forward")
        card = self.card("X")
        self.assertEqual(card.back_button.text(), "back")
        self.assertEqual(card.forward_button.text(), "forward")
        self.assertIn("back", card.low_label.text())
        self.assertIn("forward", card.high_label.text())

    def test_reversed_stage_makes_plus_forward(self) -> None:
        """X on 0..25 (max 20 in config). Reversed, + moves the controller down."""
        self.ready()
        self.fakes["X"].move_to(5.0)  # put the stage at controller 5
        self.settle()
        self.edit("X", "X", "back-forward", reversed=True)
        card = self.card("X")
        self.assertEqual(card.position_label.text(), "20.0000", "25 - 5")
        self.assertIn("reversed", card.model_label.text())
        self.assertEqual(card.target_box.text(), "20.0000")
        self.assertEqual((card.target_box.minimum(), card.target_box.maximum()), (5.0, 25.0))

        card.forward_button.click()  # + forward: display 20.1, controller 4.9
        self.run_all()
        self.settle()
        self.assertEqual(self.workers[0].status.position, 4.9)
        self.assertEqual(card.position_label.text(), "20.1000")

        card.target_box.setText("24")  # display 24 -> controller 1
        card.go_button.click()
        self.run_all()
        self.settle()
        self.assertEqual(self.workers[0].status.position, 1.0)

        card.target_box.setText("2")  # display 2 -> controller 23, beyond max 20
        card.go_button.click()
        self.run_all()
        error = self.workers[0].status.error
        self.assertIn("2.0000 mm is outside the allowed range 5.0000 to 25.0000", error)

    def test_reversed_stage_shows_mirrored_presets(self) -> None:
        self.ready()
        self.edit("X", "X", reversed=True)
        self.store.save("P", {"X": 5.0})
        self.window.presets.refresh(select="P")
        self.assertEqual(self.window.presets.table.item(0, 1).text(), "20.0000")
        self.assertEqual(self.store.get("P").positions, {"X": 5.0}, "stored in controller units")
        go = GoToPresetDialog(
            self.store.get("P"),
            {w.name: w.status for w in self.workers},
            self.window,
            self.window.presets.views(),
        )
        self.assertEqual(go.targets(), {"X": 5.0})

    def test_reverse_waits_for_the_travel(self) -> None:
        with mock.patch.object(QMessageBox, "warning") as warning:
            self.edit("X", "X", reversed=True)  # never connected: travel unknown
        warning.assert_called_once()

    # -- speed ------------------------------------------------------------------------------

    def test_set_speed_from_the_card_and_keep_it(self) -> None:
        self.ready()
        card = self.card("Z")
        self.assertEqual(card.speed_box.text(), "50.000")
        self.assertFalse(card.speed_button.isEnabled(), "nothing to set yet")
        card.speed_box.setText("20")
        card.speed_box.textEdited.emit("20")
        self.assertTrue(card.speed_button.isEnabled())
        card.speed_button.click()
        self.run_all()
        self.assertEqual(self.workers[1].status.velocity, 20.0)
        self.assertEqual(card.speed_box.text(), "20.000")
        saved = AxisStore(self.app_config.axes_path, self.app_config.stage_names)
        self.assertEqual(saved.get("Z").speed, 20.0)

    def test_startup_speed_is_saved_only_after_the_controller_accepts_it(self) -> None:
        self.window.timer.stop()
        self.window.deleteLater()
        settings = replace(self.app_config.stages[0], velocity=3.0)
        self.fakes["X"] = FakeStage("COM3", conex=True, velocity=2.0, clock=self.clock)
        worker = StageWorker(settings, lambda _c: self.fakes["X"], clock=self.clock)
        self.workers = [worker]
        self.window = self.new_window(self.workers, "startup.ini")

        def saved_speed() -> float | None:
            return AxisStore(self.app_config.axes_path, self.app_config.stage_names).get("X").speed

        self.assertIsNone(saved_speed())
        self.assertNotIn("speed set", self.window.status_label.text())
        self.ready()
        self.assertIn("not applied", worker.status.error)
        self.assertEqual(worker.status.velocity, 2.0)
        self.assertIsNone(saved_speed())
        self.assertNotIn("speed set", self.window.status_label.text())
        changed = StageWorker(replace(settings, velocity=1.0), lambda _c: self.fakes["X"])
        again = self.new_window([changed], "startup-again.ini")
        self.addCleanup(again.deleteLater)
        self.addCleanup(again.timer.stop)
        self.assertEqual(changed.speed, 1.0, "a refused startup value must not override new config")
        worker.set_velocity(1.0)
        self.run_all()
        self.assertEqual(saved_speed(), 1.0)
        self.assertIn("speed set to 1", self.window.status_label.text())

    def test_a_much_faster_speed_asks_first(self) -> None:
        self.ready()
        card = self.card("X")  # 10 mm/s now
        card.speed_box.setText("25")
        card.speed_box.textEdited.emit("25")
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Cancel) as ask:
            card.speed_button.click()
        ask.assert_called_once()
        self.run_all()
        self.assertEqual(self.workers[0].status.velocity, 10.0)

    def test_speed_can_be_set_when_the_previous_speed_is_unknown(self) -> None:
        self.ready()
        worker = self.workers[0]
        worker._set(velocity=None)
        self.window.refresh()
        card = self.card("X")
        self.assertTrue(card.speed_box.isEnabled())
        card.speed_box.setText("2")
        card.speed_box.textEdited.emit("2")
        card.speed_button.click()
        self.run_all()
        self.assertEqual(worker.status.velocity, 2.0)

    def test_speed_read_recovery_enables_controls_without_another_speed_command(self) -> None:
        worker = self.workers[0]
        worker.config = replace(worker.config, max_velocity=4.0)
        self.ready()
        with mock.patch.object(
            self.fakes["X"], "get_velocity", side_effect=DeviceError("No speed reply.")
        ):
            worker.set_velocity(2.0)
            self.run_all()
        self.assertFalse(self.card("X").go_button.isEnabled())
        self.clock.now += 1.0
        self.run_all()
        self.assertTrue(self.card("X").go_button.isEnabled())
        self.assertEqual(self.card("X").speed_box.text(), "2.000")
        self.assertEqual(self.fakes["X"].commands.count("set_velocity 2"), 1)

    def test_travel_bar_is_a_readout(self) -> None:
        from PySide6.QtCore import Qt

        bar = self.card("X").bar
        self.assertTrue(bar.testAttribute(Qt.WA_TransparentForMouseEvents))
        self.assertEqual(bar.focusPolicy(), Qt.NoFocus)

    # -- layout -----------------------------------------------------------------------------

    def test_no_widget_uses_height_for_width(self) -> None:
        """One word-wrapped QLabel puts the window layout into height-for-width mode, and in
        that mode Qt squeezed the cards under the presets panel. Use WrapLabel instead."""
        from PySide6.QtWidgets import QWidget

        offenders = [
            f"{type(w).__name__} {getattr(w, 'text', lambda: '')()!r}"
            for w in self.window.findChildren(QWidget)
            if w.hasHeightForWidth()
        ]
        self.assertEqual(offenders, [])
        self.assertFalse(self.window.centralWidget().layout().hasHeightForWidth())

    def test_cards_that_grow_after_launch_keep_their_height(self) -> None:
        """The launch that hid the Home buttons: a window opened at a remembered size that
        is tight, then the stages connect and the cards grow (speed fields fill in)."""
        from shuffler.gui.fit import find_cropped

        self.window.show()
        APP.processEvents()
        tight = self.window.minimumSizeHint()
        self.window.resize(tight.width(), tight.height() + 20)  # room only before connect
        APP.processEvents()
        self.ready()  # connect and home: every card grows
        self.workers[0].move_to(-99.0)  # and one shows a two-line error
        self.run_all()
        for _ in range(3):
            APP.processEvents()
        problems = find_cropped(self.window)
        self.assertEqual(problems, [])
        card_bottom = max(
            c.mapTo(self.window, c.rect().bottomLeft()).y() for c in self.window.cards
        )
        presets_top = self.window.presets.mapTo(
            self.window, self.window.presets.rect().topLeft()
        ).y()
        self.assertLess(card_bottom, presets_top, "the presets panel must not cover the cards")
        for card in self.window.cards:
            self.assertGreaterEqual(card.home_button.height(), card.home_button.sizeHint().height())

    def test_no_text_is_cut_at_the_smallest_window(self) -> None:
        from shuffler.gui.fit import find_cropped

        self.ready()
        self.edit("X", "A rather long axis name", "back-forward", reversed=True)
        self.store.save("A preset with a fairly long descriptive name", {"X": 5.0, "Z": 7.0})
        self.window.presets.refresh()
        self.workers[1].move_to(1000.0)  # refused: a two-line error on the card
        self.run_all()
        self.window.show()
        for size in (self.window.minimumSizeHint(), self.window.size()):
            self.window.resize(size)
            for _ in range(3):
                APP.processEvents()
            self.window.refresh()
            APP.processEvents()
            with self.subTest(size=f"{size.width()}x{size.height()}"):
                self.assertEqual(find_cropped(self.window), [])

    def test_move_cards_and_preset_columns(self) -> None:
        self.card("Z").move_requested.emit(-1)
        self.assertEqual([c.worker.name for c in self.window.cards], ["Z", "X"])
        self.assertEqual(self.window.presets.table.horizontalHeaderItem(1).text(), "Z")
        self.assertFalse(self.card("Z").left_action.isEnabled())
        self.assertTrue(self.card("Z").right_action.isEnabled())
        axes = AxisStore(self.app_config.axes_path, self.app_config.stage_names)
        self.assertEqual(axes.order(), ["Z", "X"])

    # -- the fly ----------------------------------------------------------------------------

    def test_the_fly_follows_the_stages(self) -> None:
        fly = self.window.fly
        self.assertEqual(fly.mode, "asleep", "no stage connected")
        self.ready()
        self.assertEqual(fly.mode, "groom", "homing ended: the fly tidies up")
        self.card("X").forward_button.click()
        self.run_all()
        self.assertEqual(fly.mode, "shuffle")
        self.window.stop_all()
        self.assertTrue(fly.jumping, "Stop all: the escape jump")


class FlyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()

    def fly(self, *, reduced: bool) -> FlyWidget:
        widget = FlyWidget(reduced=reduced, clock=self.clock)
        widget.resize(widget.sizeHint())
        return widget

    def test_shuffle_then_groom_then_rest(self) -> None:
        fly = self.fly(reduced=False)
        fly.show_states([State.READY, State.MOVING])
        self.assertEqual(fly.mode, "shuffle")
        self.assertTrue(fly.animating)
        self.assertTrue(fly.pose().live)
        fly.show_states([State.READY, State.READY])
        self.assertEqual(fly.mode, "groom", "the move ended: the fly tidies up")
        self.clock.now += fly_module.GROOM_S + 0.01
        fly._frame()
        self.assertEqual(fly.mode, "rest")
        self.assertFalse(fly.animating, "no frames while nothing animates")

    def test_no_groom_when_the_stages_disconnect(self) -> None:
        fly = self.fly(reduced=False)
        fly.show_states([State.MOVING])
        fly.show_states([State.OFFLINE])
        self.assertEqual(fly.mode, "asleep")

    def test_jump_ends(self) -> None:
        fly = self.fly(reduced=False)
        fly.show_states([State.READY])
        fly.startle()
        self.assertTrue(fly.jumping)
        self.assertTrue(fly.animating)
        self.clock.now += fly_module.JUMP_S / 2
        self.assertGreater(fly.pose().lift, 0.99, "the top of the jump")
        self.assertGreater(fly.pose().wings, fly_module.FOLDED_DEG + 20, "wings out")
        self.clock.now += fly_module.JUMP_S / 2 + 0.01
        fly._frame()
        self.assertFalse(fly.jumping)
        self.assertFalse(fly.animating)

    def test_reduced_motion_never_moves(self) -> None:
        fly = self.fly(reduced=True)
        fly.show_states([State.HOMING])
        self.assertFalse(fly.animating)
        self.assertEqual(fly.pose(), fly_module.Pose(live=True), "pink, and still")
        fly.show_states([State.READY])
        self.assertEqual(fly.mode, "rest", "no groom")
        fly.startle()
        self.assertFalse(fly.animating)
        self.assertTrue(fly.pose().live, "Stop all lights the eyes")

    def test_reduce_motion_setting(self) -> None:
        for value, expected in (("1", True), ("yes", True), ("0", False), ("", False)):
            with mock.patch.dict(os.environ, {"SHUFFLER_REDUCE_MOTION": value}):
                self.assertEqual(fly_module.reduce_motion(), expected)

    def test_every_mode_paints(self) -> None:
        fly = self.fly(reduced=False)
        for states in ([State.OFFLINE], [State.READY], [State.MOVING], [State.READY]):
            fly.show_states(states)
            self.clock.now += 0.37
            self.assertFalse(fly.grab().isNull())
        fly.startle()
        self.clock.now += fly_module.JUMP_S / 2
        self.assertFalse(fly.grab().isNull())

    def test_every_pose_stays_inside_the_widget(self) -> None:
        """The widget clips; the jump's spread wings were cut off at the top and bottom.

        Draw the widest poses without clipping, on a canvas with a margin around the
        widget, and check that nothing lands in the margin.
        """
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage, QPainter

        widget = self.fly(reduced=False)
        margin = 20
        poses = [fly_module.Pose(wings=fly_module.JUMP_DEG, lift=1.0), fly_module.Pose(rub=0.25)]
        for sway in (-1.0, 1.0):
            for tilt in (-6.0, 6.0):
                poses.append(
                    fly_module.Pose(sway=sway, tilt=tilt, wings=fly_module.JUMP_DEG, lift=1.0)
                )
                wings = fly_module.FOLDED_DEG + fly_module.BUZZ_DEG
                poses.append(fly_module.Pose(sway=sway, tilt=tilt, wings=wings, buzz=True))
        for pose in poses:
            image = QImage(
                widget.width() + 2 * margin, widget.height() + 2 * margin, QImage.Format_ARGB32
            )
            image.fill(Qt.transparent)
            painter = QPainter(image)
            painter.translate(margin, margin)
            widget.draw_fly(painter, pose)
            painter.end()
            outside = [
                (x - margin, y - margin)
                for y in range(image.height())
                for x in range(image.width())
                if not (
                    margin <= x < margin + widget.width() and margin <= y < margin + widget.height()
                )
                and image.pixelColor(x, y).alpha() > 8
            ]
            with self.subTest(pose=pose):
                self.assertEqual(outside[:3], [], "the fly reaches outside its widget")

    def test_ico_file(self) -> None:
        """A valid .ico: every size, 256 px as PNG, the rest as 32-bit bitmaps whose
        corners are transparent (the rounded tile) and whose centre is opaque."""
        import struct

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "shuffler.ico"
            fly_module.write_ico(path)
            data = path.read_bytes()
        reserved, kind, count = struct.unpack_from("<HHH", data)
        self.assertEqual((reserved, kind, count), (0, 1, len(fly_module.ICON_SIZES)))
        for index, size in enumerate(fly_module.ICON_SIZES):
            width, height, _, _, planes, bits, length, offset = struct.unpack_from(
                "<BBBBHHII", data, 6 + 16 * index
            )
            with self.subTest(size=size):
                self.assertEqual((width or 256, height or 256, planes, bits), (size, size, 1, 32))
                self.assertLessEqual(offset + length, len(data))
                blob = data[offset : offset + length]
                if size == 256:
                    self.assertTrue(blob.startswith(b"\x89PNG"))
                    continue
                self.assertEqual(struct.unpack_from("<Iii", blob), (40, size, size * 2))

                def alpha(x: int, y: int, blob: bytes = blob, size: int = size) -> int:
                    row = size - 1 - y  # bottom-up
                    return blob[40 + (row * size + x) * 4 + 3]

                self.assertEqual(alpha(0, 0), 0, "rounded corner")
                self.assertEqual(alpha(size // 2, size // 2), 255)

    def test_window_icon(self) -> None:
        sizes = fly_module.window_icon().availableSizes()
        self.assertIn(QSize(16, 16), sizes)
        self.assertIn(QSize(256, 256), sizes)


class ThemeTest(unittest.TestCase):
    def test_text_contrast(self) -> None:
        for label, (foreground, background) in theme.TEXT_PAIRS.items():
            with self.subTest(label):
                self.assertGreaterEqual(theme.contrast_ratio(foreground, background), 4.5)

    def test_every_state_has_a_pill(self) -> None:
        self.assertEqual(set(theme.PILL), set(State))

    def test_numbers_use_a_monospace_font(self) -> None:
        """A named monospace font where one is installed; the system fixed font otherwise.

        The offscreen test platform has no Windows fonts, so check what is asked for.
        """
        from PySide6.QtGui import QFont, QFontDatabase

        number = theme.number_font()
        if any(QFontDatabase.hasFamily(f) for f in theme.MONOSPACE):
            self.assertIn(number.family(), theme.MONOSPACE)
            self.assertEqual(number.styleHint(), QFont.Monospace)
        else:
            fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
            self.assertEqual(number.family(), fixed.family())


if __name__ == "__main__":
    unittest.main()
