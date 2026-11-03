import json
import tempfile
import unittest
from pathlib import Path

from stagehand.presets import PresetError, PresetStore


class PresetStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "presets.json"

    def tearDown(self) -> None:
        self.folder.cleanup()

    def test_save_and_reload(self) -> None:
        store = PresetStore(self.path)
        store.save("  Odor   port in ", {"X": 1.25, "Z": 50.0})
        again = PresetStore(self.path)
        self.assertEqual(again.names(), ["Odor port in"])
        self.assertEqual(again.get("Odor port in").positions, {"X": 1.25, "Z": 50.0})
        self.assertFalse(self.path.with_name("presets.json.tmp").exists())

    def test_replace_keeps_the_place_in_the_list(self) -> None:
        store = PresetStore(self.path)
        store.save("A", {"X": 1.0})
        store.save("B", {"X": 2.0})
        store.save("A", {"X": 3.0})
        self.assertEqual(store.names(), ["A", "B"])
        self.assertEqual(store.get("A").positions, {"X": 3.0})

    def test_rename_move_delete(self) -> None:
        store = PresetStore(self.path)
        for name in ("A", "B", "C"):
            store.save(name, {"X": 0.0})
        with self.assertRaisesRegex(PresetError, "already exists"):
            store.rename("A", "B")
        store.rename("A", "Start")
        store.move("C", -2)
        self.assertEqual(store.names(), ["C", "Start", "B"])
        store.delete("Start")
        self.assertEqual(PresetStore(self.path).names(), ["C", "B"])

    def test_refuses_empty_or_bad_input(self) -> None:
        store = PresetStore(self.path)
        with self.assertRaisesRegex(PresetError, "name"):
            store.save("   ", {"X": 1.0})
        with self.assertRaisesRegex(PresetError, "at least one stage"):
            store.save("A", {})
        with self.assertRaisesRegex(PresetError, "not a number"):
            store.save("A", {"X": float("nan")})

    def test_damaged_file_is_never_overwritten(self) -> None:
        self.path.write_text("{ not json", encoding="utf-8")
        store = PresetStore(self.path)
        self.assertFalse(store.writable)
        self.assertIn("damaged", store.load_error)
        with self.assertRaises(PresetError):
            store.save("A", {"X": 1.0})
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{ not json")

    def test_file_format(self) -> None:
        PresetStore(self.path).save("A", {"X": 1.5})
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["presets"][0]["name"], "A")
        self.assertEqual(data["presets"][0]["positions"], {"X": 1.5})


if __name__ == "__main__":
    unittest.main()
