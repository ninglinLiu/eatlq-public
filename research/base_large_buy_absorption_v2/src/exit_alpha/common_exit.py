"""Shared paths / config for Exit Alpha v0."""
from __future__ import annotations

import json
from pathlib import Path

from ..common import OUT, ROOT, save_json, utcnow

EXIT_OUT = OUT / "exit_alpha"
EXIT_CFG = ROOT / "configs" / "freeze_exit_alpha_v0.json"


def load_exit_cfg() -> dict:
    return json.loads(EXIT_CFG.read_text())


def ensure_exit_out() -> Path:
    EXIT_OUT.mkdir(parents=True, exist_ok=True)
    return EXIT_OUT


def stamp_status(status: str, **extra) -> None:
    cfg = load_exit_cfg()
    cfg["status"] = status
    cfg.update(extra)
    EXIT_CFG.write_text(json.dumps(cfg, indent=2, default=str) + "\n")


__all__ = [
    "EXIT_OUT",
    "EXIT_CFG",
    "load_exit_cfg",
    "ensure_exit_out",
    "stamp_status",
    "save_json",
    "utcnow",
    "OUT",
    "ROOT",
]
