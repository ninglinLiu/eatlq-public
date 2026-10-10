"""Shared paths / config for Alpha Hunt v1 (does not mutate v2/v3 outputs)."""
from __future__ import annotations

import json
from pathlib import Path

from ..common import OUT, ROOT, save_json, utcnow

ALPHA_OUT = OUT / "alpha"
ALPHA_CFG = ROOT / "configs" / "freeze_alpha_v1.json"


def load_alpha_cfg() -> dict:
    return json.loads(ALPHA_CFG.read_text())


def ensure_alpha_out() -> Path:
    ALPHA_OUT.mkdir(parents=True, exist_ok=True)
    return ALPHA_OUT


def stamp_status(status: str, **extra) -> None:
    cfg = load_alpha_cfg()
    cfg["status"] = status
    cfg.update(extra)
    ALPHA_CFG.write_text(json.dumps(cfg, indent=2, default=str) + "\n")


__all__ = [
    "ALPHA_OUT",
    "ALPHA_CFG",
    "load_alpha_cfg",
    "ensure_alpha_out",
    "stamp_status",
    "save_json",
    "utcnow",
    "OUT",
    "ROOT",
]
