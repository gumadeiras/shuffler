"""What the window calls each stage, and the order of the cards. Set in the window.

Each stage has a fixed key: its ``name`` in stages.toml. Presets and settings use
the key, so a new label never breaks them. The label ("X", "Odor Z", ...) is only
what the window shows. Labels and the order live in ``labels.json`` next to
stages.toml. If that file cannot be read, the window uses the keys and refuses to
write, so a damaged file is never replaced.
"""

import json
from collections.abc import Sequence
from pathlib import Path

from stagehand.jsonfile import write_atomic

MAX_LABEL = 24


class LabelError(Exception):
    """The labels file could not be read or written, or a label is not valid."""


class LabelStore:
    def __init__(self, path: Path, keys: Sequence[str]) -> None:
        self.path = path
        self.keys = tuple(keys)
        self._labels: dict[str, str] = {}
        self._order: list[str] = []
        self.load_error = ""
        try:
            self._read()
        except LabelError as error:
            self.load_error = str(error)

    @property
    def writable(self) -> bool:
        return not self.load_error

    def label(self, key: str) -> str:
        return self._labels.get(key, key)

    def labels(self) -> dict[str, str]:
        return {key: self.label(key) for key in self.keys}

    def order(self) -> list[str]:
        """The saved order; stages not in it follow in stages.toml order."""
        known = [k for k in self._order if k in self.keys]
        return known + [k for k in self.keys if k not in known]

    def set_label(self, key: str, text: str) -> str:
        """Set a label. Empty text goes back to the key. Returns the label now shown."""
        if key not in self.keys:
            raise LabelError(f"There is no stage {key!r}.")
        text = " ".join(text.split())
        if len(text) > MAX_LABEL:
            raise LabelError(f"Use a label of {MAX_LABEL} characters or fewer.")
        others = {self.label(k).casefold() for k in self.keys if k != key}
        if text and text.casefold() in others:
            raise LabelError(f"Another stage is already called {text!r}.")
        labels = dict(self._labels)
        if text and text != key:
            labels[key] = text
        else:
            labels.pop(key, None)
        self._write(labels, self._order)
        return self.label(key)

    def move(self, key: str, offset: int) -> list[str]:
        """Move a stage left (negative) or right in the window. Returns the new order."""
        order = self.order()
        index = order.index(key)
        target = max(0, min(len(order) - 1, index + offset))
        order.insert(target, order.pop(index))
        self._write(self._labels, order)
        return order

    def _read(self) -> None:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as error:
            raise LabelError(f"Cannot read {self.path}: {error}") from None
        try:
            data = json.loads(text)
            labels = {str(k): str(v) for k, v in data.get("labels", {}).items()}
            order = [str(k) for k in data.get("order", [])]
        except (ValueError, TypeError, AttributeError) as error:
            raise LabelError(
                f"{self.path.name} is damaged ({error}). Stage labels cannot be changed until "
                "the file is repaired or moved away."
            ) from None
        self._labels, self._order = labels, order

    def _write(self, labels: dict[str, str], order: list[str]) -> None:
        if not self.writable:
            raise LabelError(self.load_error)
        try:
            write_atomic(self.path, {"version": 1, "labels": labels, "order": order})
        except OSError as error:
            raise LabelError(f"Cannot save stage labels to {self.path}: {error}") from None
        self._labels, self._order = labels, order
