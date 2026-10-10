"""Per-directory run_metadata.json for evidence register classification."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..errors import ContractError

REQUIRED_KEYS = ("round_id", "artifact_class", "source_commit", "created_at")


def write_run_metadata(directory: Path, payload: dict[str, Any]) -> Path:
    for key in REQUIRED_KEYS:
        if key not in payload or not payload[key]:
            raise ContractError(f"run_metadata missing {key}")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "run_metadata.json"
    if path.exists() or path.is_symlink():
        raise ContractError(f"refuse overwrite run_metadata: {path}")
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def read_run_metadata(directory: Path) -> dict[str, Any] | None:
    path = Path(directory) / "run_metadata.json"
    if not path.is_file() or path.is_symlink():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return None
    for key in REQUIRED_KEYS:
        if key not in data:
            return None
    return data
