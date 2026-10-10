"""Shared paths / config for Alpha Hunt v2 (does not mutate v1 seal)."""
from __future__ import annotations

import json
from pathlib import Path

from ..common import OUT, ROOT, save_json, utcnow

ALPHA_V2_OUT = OUT / "alpha_v2"
ALPHA_V2_CFG = ROOT / "configs" / "freeze_alpha_v2.json"


def load_alpha_v2_cfg() -> dict:
    return json.loads(ALPHA_V2_CFG.read_text())


def ensure_alpha_v2_out() -> Path:
    ALPHA_V2_OUT.mkdir(parents=True, exist_ok=True)
    return ALPHA_V2_OUT


def stamp_status(status: str, **extra) -> None:
    cfg = load_alpha_v2_cfg()
    cfg["status"] = status
    cfg.update(extra)
    ALPHA_V2_CFG.write_text(json.dumps(cfg, indent=2, default=str) + "\n")


__all__ = [
    "ALPHA_V2_OUT",
    "ALPHA_V2_CFG",
    "load_alpha_v2_cfg",
    "ensure_alpha_v2_out",
    "stamp_status",
    "save_json",
    "utcnow",
    "OUT",
    "ROOT",
]
