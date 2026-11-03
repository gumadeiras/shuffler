import tempfile
import unittest
from pathlib import Path

from stagehand import config
from stagehand.config import ConfigError

ROOT = Path(__file__).resolve().parents[1]


def stage(**values: object) -> dict[str, object]:
    return {"name": "X", "driver": "conex", "port": "COM3", **values}


class ConfigTest(unittest.TestCase):
    def parse(self, data: dict[str, object]) -> config.AppConfig:
        return config.parse(data, base=Path("C:/rig"))

    def test_example_file_is_valid(self) -> None:
        app = config.load(ROOT / "stages.toml.example")
        self.assertEqual(app.stage_names, ("CONEX A", "CONEX B", "DDS100"))
        self.assertEqual(app.stages[0].port, "COM3")
        self.assertEqual(app.stages[2].serial, "28253849")
        self.assertEqual(app.stages[2].jog_steps, (0.001, 0.01, 0.1, 1.0, 10.0))
        self.assertEqual(app.presets_path, ROOT / "presets.json")

    def test_defaults(self) -> None:
        app = self.parse({"stage": [stage(port="com5")]})
        only = app.stages[0]
        self.assertEqual(only.port, "COM5")
        self.assertEqual(only.units, "mm")
        self.assertIsNone(only.min)
        self.assertTrue(app.confirm_moves)

    def test_missing_file(self) -> None:
        with self.assertRaisesRegex(ConfigError, "Copy stages.toml.example"):
            config.load(Path(tempfile.gettempdir()) / "no-such-stagehand.toml")

    def test_errors(self) -> None:
        cases = {
            "Add at least one": {},
            "Unknown setting in stage 'X': maximum": {"stage": [stage(maximum=3)]},
            "Unknown setting in the top level": {"stages": []},
            "driver must be one of": {"stage": [stage(driver="pi")]},
            "names must be unique": {"stage": [stage(), stage(port="COM4")]},
            "same port": {"stage": [stage(), stage(name="Y")]},
            "8-digit": {"stage": [{"name": "Z", "driver": "kinesis", "serial": "123"}]},
            "min must be less than max": {"stage": [stage(min=5, max=1)]},
            "velocity must be more than zero": {"stage": [stage(velocity=0)]},
            "jog_steps": {"stage": [stage(jog_steps=[0.1, -1])]},
            "decimals": {"stage": [stage(decimals=9)]},
            "uses port, not serial": {"stage": [stage(serial="28253849")]},
        }
        for expected, data in cases.items():
            with self.subTest(expected=expected), self.assertRaisesRegex(ConfigError, expected):
                self.parse(data)


if __name__ == "__main__":
    unittest.main()
