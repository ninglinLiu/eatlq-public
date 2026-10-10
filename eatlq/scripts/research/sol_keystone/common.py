#!/usr/bin/env python3
"""Shared loaders for KEYSTONE → Solana offline validation (zero RPC)."""
from __future__ import annotations

import glob
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path("/opt/EATLQ")
DIAG = ROOT / "mvp/diagnostics/sol"
OUT = Path(__file__).resolve().parent

# Pre-entry observation ends at T+W; entry decision at T+ENTRY_S (seconds after create).
W = 180
ENTRY_S = 180
HOLD_S = 7200  # ~2h from create; best grid coverage in cached data
COST_RT = 0.03  # pump 1%/side + slippage/priority buffer (sprint099 standard)
TRAIN_DAYS = {"06-11", "06-12", "06-13"}
TEST_DAYS = {"06-14", "06-15"}
FATAL_STOP = -0.50
GRID_ORDER = ["0", "30", "45", "60", "90", "120", "180", "240", "300", "420",
              "600", "900", "1800", "3600", "5400", "7200"]
EXIT_OFFSETS = {"instant": 0, "1m": 60, "5m": 300, "30m": 1800, "4h": 14400}


def day_of(ms: int | float) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%m-%d")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in open(path):
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def load_labeled() -> dict[str, dict]:
    return {r["mint"]: r for r in load_jsonl(DIAG / "creates_labeled.jsonl")}


def load_earlypath() -> dict[str, dict]:
    return {r["mint"]: r for r in load_jsonl(DIAG / "creates_earlypath.jsonl")}


def load_price_paths() -> dict[str, dict]:
    return {r["mint"]: r for r in load_jsonl(DIAG / "price_paths.jsonl")}


def load_grids() -> dict[str, dict]:
    """Union smart_buyers* grid + price_paths path."""
    grids: dict[str, dict] = {}
    for f in sorted(glob.glob(str(DIAG / "smart_buyers*.jsonl"))):
        for r in load_jsonl(Path(f)):
            m = r.get("mint")
            if m and r.get("grid") and m not in grids:
                grids[m] = {"grid": r["grid"], "source": Path(f).name}
    for m, r in load_price_paths().items():
        if m not in grids and r.get("path"):
            grids[m] = {"grid": {str(k): v for k, v in r["path"].items()}, "source": "price_paths.jsonl"}
    return grids


def load_deployer_history() -> dict[str, dict]:
    hist: dict[str, dict] = {}
    for f in sorted(glob.glob(str(DIAG / "deployer_history*.jsonl"))):
        for r in load_jsonl(Path(f)):
            c = r.get("creator")
            if not c:
                continue
            if c not in hist:
                hist[c] = {"coins": []}
            hist[c]["coins"].extend(r.get("coins") or [])
    return hist


def grid_price(grid: dict, horizon_s: float) -> float | None:
    """Price at seconds-after-create; fall back to last earlier non-null."""
    if not grid:
        return None
    keys = sorted((float(k), k) for k in grid.keys())
    target = float(horizon_s)
    best = None
    for ts, k in keys:
        v = grid.get(k)
        if v and ts <= target:
            best = v
        elif ts > target:
            break
    if best is not None:
        return best
    for ts, k in keys:
        v = grid.get(k)
        if v:
            return v
    return None


def roundtrip_ret(grid: dict, entry_s: float, exit_s: float, cost: float = COST_RT) -> float | None:
    pe = grid_price(grid, entry_s)
    px = grid_price(grid, exit_s)
    if not pe or not px or pe <= 0:
        return None
    return (px / pe) * (1.0 - cost) - 1.0


def deployer_feats(creator: str | None, created_ms: int, hist: dict[str, dict]) -> dict[str, float]:
    if not creator or creator not in hist:
        return {"n_prior": 0, "prior_grad_rate": 0.0, "prior_log_ath": 0.0}
    prior = [c for c in hist[creator]["coins"] if (c.get("ct") or 0) < created_ms]
    if not prior:
        return {"n_prior": 0, "prior_grad_rate": 0.0, "prior_log_ath": 0.0}
    grads = sum(1 for c in prior if c.get("complete"))
    aths = [float(c.get("ath") or 0) for c in prior]
    return {
        "n_prior": len(prior),
        "prior_grad_rate": grads / len(prior),
        "prior_log_ath": math.log1p(max(aths) if aths else 0),
    }


def build_universe_rows() -> list[dict]:
    labeled = load_labeled()
    early = load_earlypath()
    grids = load_grids()
    deployers = load_deployer_history()
    rows = []
    for mint, lab in labeled.items():
        ep = early.get(mint, {})
        g = grids.get(mint, {}).get("grid")
        if not lab.get("created_ms") or not lab.get("bonding_curve"):
            continue
        created_ms = int(lab["created_ms"])
        d = day_of(created_ms)
        split = "TRAIN" if d in TRAIN_DAYS else ("TEST" if d in TEST_DAYS else "OTHER")
        dep = deployer_feats(lab.get("creator"), created_ms, deployers)
        rows.append({
            "mint": mint,
            "created_ms": created_ms,
            "day": d,
            "split": split,
            "entry_slot_s": ENTRY_S,
            "venue": "pump_bonding_curve",
            "graduated": bool(lab.get("graduated")),
            "ath_mc": lab.get("ath_mc"),
            "time_to_ath_s": lab.get("time_to_ath_s"),
            "has_earlypath": mint in early,
            "has_grid": g is not None,
            "n_trades_60s": ep.get("n_trades_60s"),
            "n_trades_180s": ep.get("n_trades_180s"),
            "n_trades_600s": ep.get("n_trades_600s"),
            "time_to_5th_s": ep.get("time_to_5th_s"),
            "alive_at_180s": ep.get("alive_at_180s"),
            "lifespan_s": ep.get("lifespan_s"),
            "trade_err_rate": ep.get("trade_err_rate"),
            "has_desc": lab.get("has_desc"),
            "desc_len": lab.get("desc_len"),
            "has_twitter": lab.get("has_twitter"),
            "has_website": lab.get("has_website"),
            "has_telegram": lab.get("has_telegram"),
            "name_len": lab.get("name_len"),
            **dep,
        })
    return rows


def feature_matrix(row: dict) -> dict[str, float]:
    """Point-in-time features using only data < entry_slot (T+180s)."""
    n180 = float(row.get("n_trades_180s") or 0)
    n60 = float(row.get("n_trades_60s") or 0)
    n600 = float(row.get("n_trades_600s") or 0)
    t5 = float(row.get("time_to_5th_s") or 999)
    life = float(row.get("lifespan_s") or 0)
    return {
        "n_trades_60s": n60,
        "n_trades_180s": n180,
        "n_trades_600s": n600,
        "trades_per_min_60": n60 / max(n60 and 1 or 1, 1),
        "heat_accel": (n180 - n60) / max(n60, 1),
        "time_to_5th_s": min(t5, 600),
        "alive_at_180s": 1.0 if row.get("alive_at_180s") else 0.0,
        "lifespan_log": math.log1p(life),
        "trade_err_rate": float(row.get("trade_err_rate") or 0),
        "has_desc": 1.0 if row.get("has_desc") else 0.0,
        "desc_len": float(row.get("desc_len") or 0),
        "has_twitter": 1.0 if row.get("has_twitter") else 0.0,
        "has_website": 1.0 if row.get("has_website") else 0.0,
        "has_telegram": 1.0 if row.get("has_telegram") else 0.0,
        "name_len": float(row.get("name_len") or 0),
        "n_prior": float(row.get("n_prior") or 0),
        "prior_grad_rate": float(row.get("prior_grad_rate") or 0),
        "prior_log_ath": float(row.get("prior_log_ath") or 0),
    }


def summarize_rets(rets: list[float]) -> dict[str, Any]:
    if not rets:
        return {"n": 0}
    import statistics as st
    srt = sorted(rets)
    n = len(rets)
    wins = [x for x in rets if x > 0]
    gains = sum(x for x in rets if x > 0)
    losses = -sum(x for x in rets if x < 0)
    pf = gains / losses if losses else float("inf")
    g2 = sum(x for x in srt[1:] if x > 0)
    pf_top1 = g2 / losses if losses else float("inf")
    return {
        "n": n,
        "mean": sum(rets) / n,
        "median": st.median(rets),
        "win_rate": len(wins) / n,
        "pct_ge_100": sum(1 for x in rets if x >= 1.0) / n,
        "pct_ge_300": sum(1 for x in rets if x >= 3.0) / n,
        "pct_le_m50": sum(1 for x in rets if x <= -0.5) / n,
        "pct_le_m90": sum(1 for x in rets if x <= -0.9) / n,
        "pf": pf,
        "pf_top1_removed": pf_top1,
        "best": max(rets),
        "worst": min(rets),
    }


def write_json(name: str, obj: Any) -> Path:
    p = OUT / name
    p.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
    return p
