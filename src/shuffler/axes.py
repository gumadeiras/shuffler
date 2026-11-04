"""How the window presents each stage: its name, its direction words, and its sense.

Each stage has a fixed key: its ``name`` in stages.toml. Presets and settings use
the key, so these choices never break them. They are made in the window and kept
in ``axes.json`` next to stages.toml.

Direction: a stage can say what + means ("forward", "up", "right", "in"). If the
stage is mounted so that the controller's + moves it the other way, mark it
*reversed*. The window then shows positions counted from the other end of the
travel (``lower + upper - x``), so a larger number and the + button always move
the stage forward, up, right, or in. Only the display changes: the worker, the
controllers, and the presets keep the controller's own coordinates.

The mirror needs the travel limits. They are saved when the stage connects, so a
reversed stage shows correct numbers in the preset table while it is offline.
"""

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from shuffler.jsonfile import write_atomic

MAX_LABEL = 24

# Direction key -> (word for -, word for +). + is always forward, up, right, or in.
DIRECTIONS: dict[str, tuple[str, str]] = {
    "none": ("", ""),
    "left-right": ("left", "right"),
    "down-up": ("down", "up"),
    "back-forward": ("back", "forward"),
    "out-in": ("out", "in"),
}


class AxisError(Exception):
    """The axes file could not be read or written, or a setting is not valid."""


@dataclass(frozen=True)
class Axis:
    label: str
    direction: str = "none"
    reversed: bool = False
    travel: tuple[float, float] | None = None  # the controller's limits, saved on connect
    speed: float | None = None  # units/s set in the window; applied on each connect

    @property
    def words(self) -> tuple[str, str]:
        return DIRECTIONS.get(self.direction, ("", ""))

    @property
    def mirrored(self) -> bool:
        return self.reversed and self.travel is not None

    def to_display(self, value: float) -> float:
        """Controller coordinate to the number the window shows."""
        if not self.mirrored:
            return value
        assert self.travel is not None
        return self.travel[0] + self.travel[1] - value

    def to_device(self, value: float) -> float:
        """Number the window shows to the controller coordinate (the mirror is its own inverse)."""
        return self.to_display(value)

    @property
    def sign(self) -> int:
        """Multiply a display step by this to get the controller step."""
        return -1 if self.mirrored else 1

    def display_range(self, low: float, high: float) -> tuple[float, float]:
        a, b = self.to_display(low), self.to_display(high)
        return (a, b) if a <= b else (b, a)


class AxisStore:
    def __init__(self, path: Path, keys: Sequence[str]) -> None:
        self.path = path
        self.keys = tuple(keys)
        self._axes: dict[str, Axis] = {}
        self._order: list[str] = []
        self.load_error = ""
        try:
            self._read()
        except AxisError as error:
            self.load_error = str(error)

    @property
    def writable(self) -> bool:
        return not self.load_error

    def get(self, key: str) -> Axis:
        return self._axes.get(key) or Axis(label=key)

    def order(self) -> list[str]:
        """The saved order; stages not in it follow in stages.toml order."""
        known = [k for k in self._order if k in self.keys]
        return known + [k for k in self.keys if k not in known]

    def update(
        self,
        key: str,
        *,
        label: str | None = None,
        direction: str | None = None,
        reversed: bool | None = None,  # noqa: A002 - the user-facing word
    ) -> Axis:
        """Change the name, direction, or sense. An empty name goes back to the key."""
        if key not in self.keys:
            raise AxisError(f"There is no stage {key!r}.")
        axis = self.get(key)
        if label is not None:
            label = " ".join(label.split()) or key
            if len(label) > MAX_LABEL:
                raise AxisError(f"Use a name of {MAX_LABEL} characters or fewer.")
            others = {self.get(k).label.casefold() for k in self.keys if k != key}
            if label.casefold() in others:
                raise AxisError(f"Another stage is already called {label!r}.")
            axis = replace(axis, label=label)
        if direction is not None:
            if direction not in DIRECTIONS:
                raise AxisError(f"Unknown direction {direction!r}.")
            axis = replace(axis, direction=direction)
        if reversed is not None:
            if reversed and axis.travel is None:
                raise AxisError("Connect the stage once before you reverse it.")
            axis = replace(axis, reversed=reversed)
        self._write({**self._axes, key: axis}, self._order)
        return axis

    def remember_speed(self, key: str, speed: float) -> Axis:
        """Save the speed that the controller accepted."""
        if not (math.isfinite(speed) and speed > 0):
            raise AxisError("The speed must be more than zero.")
        axis = replace(self.get(key), speed=speed)
        self._write({**self._axes, key: axis}, self._order)
        return axis

    def remember_travel(self, key: str, lower: float, upper: float) -> Axis:
        """Save the controller's limits when they change. Returns the axis now in use."""
        axis = self.get(key)
        if axis.travel == (lower, upper) or not self.writable:
            return replace(axis, travel=(lower, upper))
        axis = replace(axis, travel=(lower, upper))
        self._write({**self._axes, key: axis}, self._order)
        return axis

    def move(self, key: str, offset: int) -> list[str]:
        """Move a stage left (negative) or right in the window. Returns the new order."""
        order = self.order()
        index = order.index(key)
        target = max(0, min(len(order) - 1, index + offset))
        order.insert(target, order.pop(index))
        self._write(self._axes, order)
        return order

    def _read(self) -> None:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as error:
            raise AxisError(f"Cannot read {self.path}: {error}") from None
        try:
            data = json.loads(text)
            axes = {}
            for key, item in data.get("axes", {}).items():
                travel = item.get("travel")
                if travel is not None:
                    low, high = (float(v) for v in travel)
                    if not (math.isfinite(low) and math.isfinite(high) and low < high):
                        raise ValueError(f"bad travel for {key}")
                    travel = (low, high)
                direction = str(item.get("direction", "none"))
                speed = item.get("speed")
                if speed is not None:
                    speed = float(speed)
                    if not (math.isfinite(speed) and speed > 0):
                        raise ValueError(f"bad speed for {key}")
                axes[str(key)] = Axis(
                    label=str(item.get("label", key)),
                    direction=direction if direction in DIRECTIONS else "none",
                    reversed=bool(item.get("reversed", False)) and travel is not None,
                    travel=travel,
                    speed=speed,
                )
            order = [str(k) for k in data.get("order", [])]
        except (ValueError, TypeError, AttributeError) as error:
            raise AxisError(
                f"{self.path.name} is damaged ({error}). Stage names and directions cannot "
                "be changed until the file is repaired or moved away."
            ) from None
        self._axes, self._order = axes, order

    def _write(self, axes: dict[str, Axis], order: list[str]) -> None:
        if not self.writable:
            raise AxisError(self.load_error)
        data = {
            "version": 1,
            "axes": {
                key: {
                    "label": axis.label,
                    "direction": axis.direction,
                    "reversed": axis.reversed,
                    "travel": list(axis.travel) if axis.travel else None,
                    "speed": axis.speed,
                }
                for key, axis in axes.items()
            },
            "order": order,
        }
        try:
            write_atomic(self.path, data)
        except OSError as error:
            raise AxisError(f"Cannot save stage settings to {self.path}: {error}") from None
        self._axes, self._order = axes, order
