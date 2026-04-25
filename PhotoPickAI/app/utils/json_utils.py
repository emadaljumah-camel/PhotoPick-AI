from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_json_file(path: Path, default: Any = None) -> Any:
    """Read JSON from disk with a fallback default."""

    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def write_json_file(path: Path, payload: Any) -> None:
    """Write JSON with pretty formatting."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
