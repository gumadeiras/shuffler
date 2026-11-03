import json
import tempfile
import unittest
from pathlib import Path

from stagehand.axes import Axis, AxisError, AxisStore

KEYS = ("CONEX A", "CONEX B", "DDS100")


class AxisTest(unittest.TestCase):
    def test_plain_axis_shows_controller_numbers(self) -> None:
        axis = Axis("X", travel=(0.0, 25.0))
        self.assertEqual(axis.to_display(3.0), 3.0)
        self.assertEqual(axis.sign, 1)

    def test_reversed_axis_counts_from_the_other_end(self) -> None:
        axis = Axis("X", direction="back-forward", reversed=True, travel=(0.0, 25.0))
        self.assertEqual(axis.to_display(3.0), 22.0)
        self.assertEqual(axis.to_device(22.0), 3.0)
        self.assertEqual(axis.sign, -1)
        self.assertEqual(axis.display_range(2.0, 20.0), (5.0, 23.0))
        self.assertEqual(axis.words, ("back", "forward"))

    def test_reversed_without_travel_is_not_mirrored(self) -> None:
        axis = Axis("X", reversed=True)
        self.assertFalse(axis.mirrored)
        self.assertEqual(axis.to_display(3.0), 3.0)


class AxisStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "axes.json"

    def tearDown(self) -> None:
        self.folder.cleanup()

    def test_defaults(self) -> None:
        store = AxisStore(self.path, KEYS)
        self.assertEqual(store.get("CONEX A"), Axis("CONEX A"))
        self.assertEqual(store.order(), list(KEYS))
        self.assertFalse(self.path.exists(), "nothing is written until something changes")

    def test_settings_persist(self) -> None:
        store = AxisStore(self.path, KEYS)
        store.remember_travel("CONEX B", 0.0, 25.0)
        store.update("CONEX B", label="  X ", direction="left-right", reversed=True)
        store.move("CONEX B", -1)
        again = AxisStore(self.path, KEYS)
        axis = again.get("CONEX B")
        self.assertEqual(axis, Axis("X", "left-right", True, (0.0, 25.0)))
        self.assertEqual(again.order(), ["CONEX B", "CONEX A", "DDS100"])

    def test_reverse_needs_the_travel(self) -> None:
        store = AxisStore(self.path, KEYS)
        with self.assertRaisesRegex(AxisError, "Connect the stage once"):
            store.update("DDS100", reversed=True)

    def test_empty_name_returns_to_the_key(self) -> None:
        store = AxisStore(self.path, KEYS)
        store.update("DDS100", label="Z")
        self.assertEqual(store.update("DDS100", label="  ").label, "DDS100")

    def test_names_must_differ_and_be_short(self) -> None:
        store = AxisStore(self.path, KEYS)
        store.update("CONEX A", label="X")
        with self.assertRaisesRegex(AxisError, "already called"):
            store.update("CONEX B", label="x")
        with self.assertRaisesRegex(AxisError, "already called"):
            store.update("CONEX B", label="DDS100")
        with self.assertRaisesRegex(AxisError, "characters or fewer"):
            store.update("CONEX B", label="y" * 30)
        with self.assertRaisesRegex(AxisError, "Unknown direction"):
            store.update("CONEX B", direction="sideways")

    def test_travel_is_written_only_when_it_changes(self) -> None:
        store = AxisStore(self.path, KEYS)
        store.remember_travel("DDS100", 0.0, 100.0)
        written = self.path.stat().st_mtime_ns
        self.path.write_text(self.path.read_text(encoding="utf-8"), encoding="utf-8")
        before = self.path.stat().st_mtime_ns
        store.remember_travel("DDS100", 0.0, 100.0)
        self.assertEqual(self.path.stat().st_mtime_ns, before)
        self.assertGreaterEqual(before, written)

    def test_new_and_removed_stages(self) -> None:
        AxisStore(self.path, KEYS).move("DDS100", -2)
        store = AxisStore(self.path, ("DDS100", "CONEX A", "New"))
        self.assertEqual(store.order(), ["DDS100", "CONEX A", "New"])

    def test_damaged_file_is_never_overwritten(self) -> None:
        self.path.write_text("[not an object", encoding="utf-8")
        store = AxisStore(self.path, KEYS)
        self.assertFalse(store.writable)
        self.assertEqual(store.get("CONEX A").label, "CONEX A")
        with self.assertRaises(AxisError):
            store.update("CONEX A", label="X")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "[not an object")

    def test_file_format(self) -> None:
        store = AxisStore(self.path, KEYS)
        store.update("CONEX A", label="Y", direction="down-up")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(
            data["axes"]["CONEX A"],
            {
                "label": "Y",
                "direction": "down-up",
                "reversed": False,
                "travel": None,
                "speed": None,
            },
        )

    def test_speed_persists(self) -> None:
        store = AxisStore(self.path, KEYS)
        store.remember_speed("DDS100", 12.5)
        self.assertEqual(AxisStore(self.path, KEYS).get("DDS100").speed, 12.5)
        with self.assertRaisesRegex(AxisError, "more than zero"):
            store.remember_speed("DDS100", 0)


if __name__ == "__main__":
    unittest.main()
