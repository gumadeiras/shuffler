"""Atomic JSON files for the state the window keeps (presets, stage labels)."""

import json
from pathlib import Path
from typing import Any


def write_atomic(path: Path, data: Any) -> None:
    """Write to a temporary file, then replace: a crash never leaves a half-written file.

    Raises OSError.
    """
    temporary = path.with_name(path.name + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)  # atomic on the same volume
