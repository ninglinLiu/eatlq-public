"""Shared constants, IO, ClickHouse helper, calendar walk-forward splits."""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/root/base_large_buy_absorption_v1")
OUT = ROOT / "out"
CFG = ROOT / "configs"
WETH = "0x4200000000000000000000000000000000000006"
CHAIN_ID = 8453
LN1P = float(np.log(1.0001))


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_freeze() -> dict:
    return json.loads((CFG / "large_buy_absorption_freeze_v1.json").read_text())


def save_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str) + "\n")


def sha256_file(p: Path | str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def signal_id(pool: str, decision_time: int, actor: str) -> str:
    raw = f"{pool.lower()}|{int(decision_time)}|{actor.lower()}".encode()
    return hashlib.sha256(raw).hexdigest()[:24]


def ch_query(sql: str, timeout: int = 600) -> pd.DataFrame:
    """Run ClickHouse query via docker exec, piping SQL on stdin to avoid ARG_MAX."""
    cmd = [
        "docker", "exec", "-i", "eatlq-clickhouse-1", "clickhouse-client",
        "--format=TSVWithNames",
    ]
    r = subprocess.run(cmd, input=sql, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout)[:1600])
    if not r.stdout.strip():
        return pd.DataFrame()
    return pd.read_csv(StringIO(r.stdout), sep="\t")


def get_rpc_url() -> str:
    p = Path("/tmp/rpc_url.txt")
    if p.exists():
        return p.read_text().strip()
    raise RuntimeError("Missing /tmp/rpc_url.txt for archive RPC")


def calendar_walk_forward(
    decision_times: pd.Series,
    *,
    train_days: int,
    test_days: int,
    embargo_seconds: int,
    sealed_frac: float,
) -> pd.DataFrame:
    """Return a DataFrame with columns: decision_time, split, fold.

    split ∈ {TRAIN, TEST, EMBARGO, SEALED}.
    Folds are rolling calendar windows over the unsealed portion.
    """
    ts = decision_times.astype(int)
    t_min, t_max = int(ts.min()), int(ts.max())
    span = max(t_max - t_min, 1)
    sealed_start = t_max - int(span * sealed_frac)

    train_s = train_days * 86400
    test_s = test_days * 86400
    step = test_s

    rows = []
    fold = 0
    cursor = t_min
    while cursor + train_s + embargo_seconds + test_s <= sealed_start:
        train_lo, train_hi = cursor, cursor + train_s
        emb_lo, emb_hi = train_hi, train_hi + embargo_seconds
        test_lo, test_hi = emb_hi, emb_hi + test_s
        for t in ts.unique():
            t = int(t)
            if t >= sealed_start:
                continue
            if train_lo <= t < train_hi:
                rows.append((t, "TRAIN", fold))
            elif emb_lo <= t < emb_hi:
                rows.append((t, "EMBARGO", fold))
            elif test_lo <= t < test_hi:
                rows.append((t, "TEST", fold))
        fold += 1
        cursor += step

    for t in ts.unique():
        t = int(t)
        if t >= sealed_start:
            rows.append((t, "SEALED", -1))

    if not rows:
        # Fallback: single train/test split if calendar too short
        cut = t_min + int(span * 0.7)
        for t in ts.unique():
            t = int(t)
            if t >= sealed_start:
                rows.append((t, "SEALED", -1))
            elif t < cut:
                rows.append((t, "TRAIN", 0))
            else:
                rows.append((t, "TEST", 0))

    return pd.DataFrame(rows, columns=["decision_time", "split", "fold"]).drop_duplicates(
        subset=["decision_time", "fold"], keep="last"
    )


def parse_i256(x) -> int:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return 0
    if isinstance(x, (int, np.integer)):
        return int(x)
    s = str(x).strip()
    if not s or s == "\\N":
        return 0
    try:
        if s.startswith("0x") or s.startswith("0X"):
            v = int(s, 16)
            if v >= 2**255:
                v -= 2**256
            return v
        return int(float(s)) if "." in s else int(s)
    except Exception:
        return 0
