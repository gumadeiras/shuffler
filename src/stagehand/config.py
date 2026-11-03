"""``stages.toml``: which stages exist, how to reach them, and their safe ranges.

The file is machine-specific and git-ignored. Copy ``stages.toml.example`` to start.
Unknown keys are errors, so a typo cannot silently drop a safety limit.
"""

import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DRIVERS = ("conex", "kinesis")
DEFAULT_JOG_STEPS = (0.001, 0.01, 0.1, 1.0)
DEFAULT_KINESIS_DIR = Path(r"C:\Program Files\Thorlabs\Kinesis")


class ConfigError(Exception):
    """The configuration file is missing or not valid. The message is for the user."""


@dataclass(frozen=True)
class StageConfig:
    name: str
    driver: str
    port: str | None = None  # conex
    serial: str | None = None  # kinesis
    units: str = "mm"
    min: float | None = None  # optional limits, inside the controller's own limits
    max: float | None = None
    velocity: float | None = None  # applied when the stage connects; None keeps the device value
    jog_steps: tuple[float, ...] = DEFAULT_JOG_STEPS
    decimals: int = 4

    @property
    def address(self) -> str:
        return self.port if self.driver == "conex" else f"SN {self.serial}"


@dataclass(frozen=True)
class AppConfig:
    stages: tuple[StageConfig, ...]
    presets_path: Path
    kinesis_dir: Path = DEFAULT_KINESIS_DIR
    connect_on_start: bool = True
    confirm_moves: bool = True
    source: Path | None = None
    stage_names: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage_names", tuple(s.name for s in self.stages))


APP_KEYS = {"presets", "kinesis_dir", "connect_on_start", "confirm_moves"}
STAGE_KEYS = {
    "name",
    "driver",
    "port",
    "serial",
    "units",
    "min",
    "max",
    "velocity",
    "jog_steps",
    "decimals",
}


def load(path: Path) -> AppConfig:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(
            f"The configuration file {path} does not exist. "
            "Copy stages.toml.example to stages.toml and edit it."
        ) from None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path.name} is not valid TOML: {error}") from None
    return parse(data, base=path.parent, source=path)


def parse(data: dict[str, Any], *, base: Path, source: Path | None = None) -> AppConfig:
    _no_unknown(data, {"app", "stage"}, "the top level")
    app = data.get("app", {})
    if not isinstance(app, dict):
        raise ConfigError("[app] must be a table.")
    _no_unknown(app, APP_KEYS, "[app]")

    raw_stages = data.get("stage", [])
    if not isinstance(raw_stages, list) or not raw_stages:
        raise ConfigError("Add at least one [[stage]] table.")
    stages = tuple(_stage(raw, index) for index, raw in enumerate(raw_stages, start=1))

    names = [s.name for s in stages]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ConfigError(f"Stage names must be unique: {', '.join(duplicates)}.")
    addresses = [s.address.upper() for s in stages]
    clashes = sorted({a for a in addresses if addresses.count(a) > 1})
    if clashes:
        raise ConfigError(f"Two stages use the same port or serial: {', '.join(clashes)}.")

    presets = Path(_string(app, "presets", "[app]", default="presets.json"))
    kinesis_dir = Path(_string(app, "kinesis_dir", "[app]", default=str(DEFAULT_KINESIS_DIR)))
    return AppConfig(
        stages=stages,
        presets_path=presets if presets.is_absolute() else base / presets,
        kinesis_dir=kinesis_dir,
        connect_on_start=_bool(app, "connect_on_start", "[app]", default=True),
        confirm_moves=_bool(app, "confirm_moves", "[app]", default=True),
        source=source,
    )


def _stage(raw: Any, index: int) -> StageConfig:
    where = f"stage {index}"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a table.")
    name = _string(raw, "name", where)
    where = f"stage {name!r}"
    _no_unknown(raw, STAGE_KEYS, where)
    driver = _string(raw, "driver", where)
    if driver not in DRIVERS:
        raise ConfigError(f"{where}: driver must be one of {', '.join(DRIVERS)}, not {driver!r}.")

    port = serial = None
    if driver == "conex":
        port = _string(raw, "port", where).upper()
        if "serial" in raw:
            raise ConfigError(f"{where}: a conex stage uses port, not serial.")
    else:
        serial = str(raw.get("serial", "")).strip()
        if not serial.isdigit() or len(serial) != 8:
            raise ConfigError(f"{where}: serial must be the 8-digit Thorlabs serial number.")
        if "port" in raw:
            raise ConfigError(f"{where}: a kinesis stage uses serial, not port.")

    low = _number(raw, "min", where)
    high = _number(raw, "max", where)
    if low is not None and high is not None and low >= high:
        raise ConfigError(f"{where}: min must be less than max.")
    velocity = _number(raw, "velocity", where)
    if velocity is not None and velocity <= 0:
        raise ConfigError(f"{where}: velocity must be more than zero.")

    steps = raw.get("jog_steps", DEFAULT_JOG_STEPS)
    if (
        not isinstance(steps, list | tuple)
        or not steps
        or not all(isinstance(s, int | float) and math.isfinite(s) and s > 0 for s in steps)
    ):
        raise ConfigError(f"{where}: jog_steps must be a list of positive numbers.")

    decimals = raw.get("decimals", 4)
    if not isinstance(decimals, int) or isinstance(decimals, bool) or not 0 <= decimals <= 6:
        raise ConfigError(f"{where}: decimals must be a whole number from 0 to 6.")

    return StageConfig(
        name=name,
        driver=driver,
        port=port,
        serial=serial,
        units=_string(raw, "units", where, default="mm"),
        min=low,
        max=high,
        velocity=velocity,
        jog_steps=tuple(sorted(float(s) for s in steps)),
        decimals=decimals,
    )


def _no_unknown(table: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"Unknown setting in {where}: {', '.join(unknown)}.")


def _string(table: dict[str, Any], key: str, where: str, *, default: str | None = None) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: {key} must be text.")
    return value.strip()


def _number(table: dict[str, Any], key: str, where: str) -> float | None:
    value = table.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ConfigError(f"{where}: {key} must be a number.")
    return float(value)


def _bool(table: dict[str, Any], key: str, where: str, *, default: bool) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: {key} must be true or false.")
    return value
