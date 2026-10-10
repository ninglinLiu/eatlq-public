"""Load and validate the frozen V3 hypothesis registry."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path("/root/base_greenfield_directional_v0")
REG_PATH = ROOT / "configs" / "v3_hypothesis_registry.json"

REQUIRED = [
    "id",
    "mechanism",
    "pit_fields",
    "predicts",
    "lifecycle_buckets",
    "failure_condition",
    "max_searches",
    "multiplicity",
]


def load_registry(path: Path | None = None) -> dict[str, Any]:
    p = path or REG_PATH
    obj = json.loads(p.read_text())
    validate_registry(obj)
    return obj


def validate_registry(obj: dict[str, Any]) -> None:
    hyps = obj.get("hypotheses")
    if not isinstance(hyps, list) or not hyps:
        raise ValueError("hypothesis registry must contain a non-empty hypotheses list")
    ids = []
    for h in hyps:
        for k in REQUIRED:
            if k not in h:
                raise ValueError(f"hypothesis missing {k}: {h.get('id')}")
        if not isinstance(h["pit_fields"], list) or not h["pit_fields"]:
            raise ValueError(f"{h['id']}: pit_fields must be non-empty list")
        if int(h["max_searches"]) < 1:
            raise ValueError(f"{h['id']}: max_searches must be >= 1")
        ids.append(h["id"])
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate hypothesis ids")


def hypothesis_ids(reg: dict[str, Any] | None = None) -> list[str]:
    reg = reg or load_registry()
    return [h["id"] for h in reg["hypotheses"]]
