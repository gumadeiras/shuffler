import json
import tempfile
import unittest
from pathlib import Path

from stagehand.labels import LabelError, LabelStore

KEYS = ("CONEX A", "CONEX B", "DDS100")


class LabelStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "labels.json"

    def tearDown(self) -> None:
        self.folder.cleanup()

    def test_defaults_are_the_keys_in_config_order(self) -> None:
        store = LabelStore(self.path, KEYS)
        self.assertEqual(store.labels(), {k: k for k in KEYS})
        self.assertEqual(store.order(), list(KEYS))
        self.assertFalse(self.path.exists(), "nothing is written until something changes")

    def test_label_and_order_persist(self) -> None:
        store = LabelStore(self.path, KEYS)
        store.set_label("CONEX B", "  X ")
        store.set_label("DDS100", "Z")
        store.move("CONEX B", -1)
        again = LabelStore(self.path, KEYS)
        self.assertEqual(again.label("CONEX B"), "X")
        self.assertEqual(again.label("CONEX A"), "CONEX A")
        self.assertEqual(again.order(), ["CONEX B", "CONEX A", "DDS100"])

    def test_empty_label_returns_to_the_key(self) -> None:
        store = LabelStore(self.path, KEYS)
        store.set_label("DDS100", "Z")
        self.assertEqual(store.set_label("DDS100", "   "), "DDS100")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["labels"], {})

    def test_labels_must_differ(self) -> None:
        store = LabelStore(self.path, KEYS)
        store.set_label("CONEX A", "X")
        with self.assertRaisesRegex(LabelError, "already called"):
            store.set_label("CONEX B", "x")
        with self.assertRaisesRegex(LabelError, "already called"):
            store.set_label("CONEX B", "DDS100")
        with self.assertRaisesRegex(LabelError, "characters or fewer"):
            store.set_label("CONEX B", "y" * 30)

    def test_new_and_removed_stages(self) -> None:
        LabelStore(self.path, KEYS).move("DDS100", -2)
        store = LabelStore(self.path, ("DDS100", "CONEX A", "New"))
        self.assertEqual(store.order(), ["DDS100", "CONEX A", "New"])

    def test_damaged_file_is_never_overwritten(self) -> None:
        self.path.write_text("[not an object", encoding="utf-8")
        store = LabelStore(self.path, KEYS)
        self.assertFalse(store.writable)
        self.assertEqual(store.label("CONEX A"), "CONEX A")
        with self.assertRaises(LabelError):
            store.set_label("CONEX A", "X")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "[not an object")


if __name__ == "__main__":
    unittest.main()
