"""Named stage positions, kept in one JSON file next to stages.toml.

A preset maps stage keys (the ``name`` in stages.toml, which never changes when a
stage is relabeled in the window) to positions. It can hold a subset of the stages. The file
is rewritten atomically after each change. If it cannot be read, the store refuses
to write, so a damaged file is never replaced by an empty one.
"""

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from shuffler.jsonfile import write_atomic

VERSION = 1


class PresetError(Exception):
    """The preset file could not be read or written. The message is for the user."""


@dataclass(frozen=True)
class Preset:
    name: str
    positions: dict[str, float]
    saved: str = ""  # local time, ISO 8601


class PresetStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.presets: list[Preset] = []
        self.load_error = ""
        try:
            self.presets = self._read()
        except PresetError as error:
            self.load_error = str(error)

    @property
    def writable(self) -> bool:
        return not self.load_error

    def names(self) -> list[str]:
        return [p.name for p in self.presets]

    def get(self, name: str) -> Preset | None:
        return next((p for p in self.presets if p.name == name), None)

    def save(self, name: str, positions: dict[str, float]) -> Preset:
        """Add a preset, or replace the one with the same name in place."""
        name = _clean_name(name)
        if not positions:
            raise PresetError("A preset needs the position of at least one stage.")
        for stage, value in positions.items():
            if not math.isfinite(value):
                raise PresetError(f"The position of {stage} is not a number.")
        preset = Preset(name, dict(positions), datetime.now().isoformat(timespec="seconds"))
        updated = list(self.presets)
        index = next((i for i, p in enumerate(updated) if p.name == name), None)
        if index is None:
            updated.append(preset)
        else:
            updated[index] = preset
        self._write(updated)
        return preset

    def rename(self, old: str, new: str) -> None:
        new = _clean_name(new)
        preset = self.get(old)
        if preset is None:
            raise PresetError(f"There is no preset named {old!r}.")
        if new != old and self.get(new) is not None:
            raise PresetError(f"A preset named {new!r} already exists.")
        self._write(
            [Preset(new, p.positions, p.saved) if p.name == old else p for p in self.presets]
        )

    def delete(self, name: str) -> None:
        self._write([p for p in self.presets if p.name != name])

    def move(self, name: str, offset: int) -> None:
        """Move a preset up (negative offset) or down the list."""
        names = self.names()
        index = names.index(name)
        target = max(0, min(len(names) - 1, index + offset))
        if target == index:
            return
        updated = list(self.presets)
        updated.insert(target, updated.pop(index))
        self._write(updated)

    def _read(self) -> list[Preset]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as error:
            raise PresetError(f"Cannot read {self.path}: {error}") from None
        try:
            data = json.loads(text)
            presets = [
                Preset(
                    str(item["name"]),
                    {str(k): float(v) for k, v in item["positions"].items()},
                    str(item.get("saved", "")),
                )
                for item in data["presets"]
            ]
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise PresetError(
                f"{self.path.name} is damaged ({error}). Presets are read-only until the "
                "file is repaired or moved away."
            ) from None
        return presets

    def _write(self, presets: list[Preset]) -> None:
        if not self.writable:
            raise PresetError(self.load_error)
        data = {
            "version": VERSION,
            "presets": [
                {"name": p.name, "positions": p.positions, "saved": p.saved} for p in presets
            ],
        }
        try:
            write_atomic(self.path, data)
        except OSError as error:
            raise PresetError(f"Cannot save presets to {self.path}: {error}") from None
        self.presets = presets


def _clean_name(name: str) -> str:
    name = " ".join(name.split())
    if not name:
        raise PresetError("Give the preset a name.")
    if len(name) > 60:
        raise PresetError("Use a preset name of 60 characters or fewer.")
    return name
