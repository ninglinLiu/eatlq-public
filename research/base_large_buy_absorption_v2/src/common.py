"""Shared constants, IO, ClickHouse helper, calendar walk-forward splits (v2)."""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/root/base_large_buy_absorption_v2")
OUT = ROOT / "out"
CFG = ROOT / "configs"
WETH = "0x4200000000000000000000000000000000000006"
CHAIN_ID = 8453
LN1P = float(np.log(1.0001))


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_freeze() -> dict:
    return json.loads((CFG / "large_buy_absorption_freeze_v2.json").read_text())


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
    times = np.sort(decision_times.astype(np.int64).unique())
    if len(times) == 0:
        return pd.DataFrame(columns=["decision_time", "split", "fold"])

    n_seal = max(1, int(len(times) * sealed_frac))
    sealed_cut = times[-n_seal]
    unsealed = times[times < sealed_cut]
    if len(unsealed) == 0:
        unsealed = times

    day = 86400
    t0 = int(unsealed[0])
    t1 = int(unsealed[-1])
    rows = []
    fold = 0
    cursor = t0
    while cursor + (train_days + test_days) * day <= t1 + day:
        train_end = cursor + train_days * day
        embargo_end = train_end + embargo_seconds
        test_end = embargo_end + test_days * day
        for t in unsealed:
            t = int(t)
            if cursor <= t < train_end:
                rows.append({"decision_time": t, "split": "TRAIN", "fold": fold})
            elif train_end <= t < embargo_end:
                rows.append({"decision_time": t, "split": "EMBARGO", "fold": fold})
            elif embargo_end <= t < test_end:
                rows.append({"decision_time": t, "split": "TEST", "fold": fold})
        fold += 1
        cursor += test_days * day
        if fold > 40:
            break

    for t in times:
        t = int(t)
        if t >= sealed_cut:
            rows.append({"decision_time": t, "split": "SEALED", "fold": -1})

    if not rows:
        # fallback single fold
        cut = times[int(len(times) * 0.7)]
        for t in times:
            t = int(t)
            if t >= sealed_cut:
                rows.append({"decision_time": t, "split": "SEALED", "fold": -1})
            elif t <= cut:
                rows.append({"decision_time": t, "split": "TRAIN", "fold": 0})
            else:
                rows.append({"decision_time": t, "split": "TEST", "fold": 0})
    return pd.DataFrame(rows).drop_duplicates(subset=["decision_time", "fold"], keep="last")


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
