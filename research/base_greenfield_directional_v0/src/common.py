"""Shared constants, IO, CH helper, split helpers."""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/root/base_greenfield_directional_v0")
OUT = ROOT / "out"
CFG = ROOT / "configs"
WETH = "0x4200000000000000000000000000000000000006"
LN1P = float(np.log(1.0001))

AGE_BUCKETS = {
    "5-15m": (5 * 60, 15 * 60),
    "15-60m": (15 * 60, 60 * 60),
    "1-6h": (60 * 60, 6 * 3600),
    "6-24h": (6 * 3600, 24 * 3600),
    "1-7d": (24 * 3600, 7 * 24 * 3600),
}

PRIMARY_BUCKET = "15-60m"
WAVE1_ANCHORS_S = (20 * 60, 30 * 60, 40 * 60, 50 * 60)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_freeze() -> dict:
    return json.loads((CFG / "design_freeze.json").read_text())


def save_freeze(obj: dict) -> None:
    (CFG / "design_freeze.json").write_text(json.dumps(obj, indent=2) + "\n")


def load_bans() -> dict:
    return json.loads((CFG / "hard_bans.json").read_text())


def sha256_file(p: Path | str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def signal_id(pool: str, decision_time: int, age_bucket: str) -> str:
    raw = f"{pool.lower()}|{int(decision_time)}|{age_bucket}".encode()
    return hashlib.sha256(raw).hexdigest()[:24]


def ch_query(sql: str, timeout: int = 600) -> pd.DataFrame:
    cmd = [
        "docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
        "--format=TSVWithNames", f"--query={sql}",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout)[:1200])
    if not r.stdout.strip():
        return pd.DataFrame()
    return pd.read_csv(StringIO(r.stdout), sep="\t")


def assign_splits(decision_times: pd.Series, freeze: dict | None = None) -> pd.Series:
    freeze = freeze or load_freeze()
    q = decision_times.rank(method="first") / max(len(decision_times), 1)
    ld = freeze["splits"]["label_design_frac"]
    dev = freeze["splits"]["dev_frac"]
    out = np.where(q <= ld, "LABEL_DESIGN", np.where(q <= ld + dev, "DEV", "SEALED_HOLDOUT"))
    return pd.Series(out, index=decision_times.index)


def age_bucket_of(age_s: float) -> str | None:
    for name, (lo, hi) in AGE_BUCKETS.items():
        if lo <= age_s < hi:
            return name
    return None


def assert_no_banned_features(columns: list[str]) -> None:
    bans = load_bans()
    bad = []
    for c in columns:
        cl = c.lower()
        if c in bans["banned_feature_exact"] or cl in {x.lower() for x in bans["banned_feature_exact"]}:
            bad.append(c)
            continue
        for p in bans["banned_feature_column_prefixes"]:
            if cl.startswith(p.lower()):
                bad.append(c)
    if bad:
        raise ValueError(f"Banned feature columns present: {bad}")
