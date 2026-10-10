#!/usr/bin/env python3
"""KS-FWD-R1: frozen KEYSTONE conservative selector near-window decay (research-only)."""
from __future__ import annotations

import hashlib
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd

ROOT_EATLQ = Path("/opt/EATLQ")
HERE = Path("/root/keystone_forward_decay_r1")
sys.path.insert(0, str(ROOT_EATLQ / "scripts/research"))
sys.path.insert(0, str(ROOT_EATLQ / "scripts/lib"))
sys.path.insert(0, str(ROOT_EATLQ / "scripts/research/base_lefttail"))

import keystone_live as K  # noqa
import risk_rebacktest as R  # noqa
import live_config_rebacktest as L  # noqa
import aggressive_live_rebacktest as A  # noqa

PREREG = HERE / "PREREGISTRATION.md"
PREREG_SHA = "d17e5ec1f7c528a22e0bb644af9489473cc6f76be1a372ded10da96d1c2043b8"
GL6_PREREG_SHA = "fa216ded00bd36420f907f529793049fc2dae7e8d286bf0496f6903d34c8c64a"
GL6_ERRATUM_SHA = "5cf1a7da94ceddc341692466e5811bf1adffa54b16d66fc03cf52490573bb9d2"
OUT_DIR = HERE / "out"
WETH = "0x4200000000000000000000000000000000000006"
CH = ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query"]
START = "2026-06-24 11:47:16"
END = "2026-08-14 14:22:59"
F_READY = pd.Timestamp("2026-06-24T11:47:16Z")
W = 600
H = 14400
OFFSETS = (1, 5, 15)
LN1P = math.log(1.0001)
HARD_STOP = math.log(0.5)
TRAIL = 0.35
MAX_HOLD_BLOCKS = 7200
SIZE_USD = 50.09425
PESS = -1.0 - 0.15 / SIZE_USD - 0.02
STRATA = {
    "S0_jun24_30": ("2026-06-24 11:47:16", "2026-06-30 23:59:59"),
    "S1_jul": ("2026-07-01 00:00:00", "2026-07-31 23:59:59"),
    "S2_aug": ("2026-08-01 00:00:00", "2026-08-14 14:22:59"),
}


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def ch(sql: str, timeout=900) -> str:
    import subprocess
    r = subprocess.run(CH + [sql], capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(r.stderr[:4000])
    return r.stdout


def universe_coverage() -> dict:
    sql = f"""
SET max_memory_usage=12000000000,max_execution_time=500,max_threads=4;
WITH cand AS (
 SELECT lower(pool_address) pool_address FROM eatlq.discovered_pools
 WHERE chain_id=8453 AND pool_type='v3' AND has_weth_leg=1
), fs AS (
 SELECT lower(pool_address) pool_address,min(block_time) t0,
        argMin(block_number,tuple(block_time,block_number,tx_hash,log_index)) first_block
 FROM eatlq.uniswap_v3_swaps_decoded WHERE chain_id=8453 AND block_time IS NOT NULL
 GROUP BY pool_address
)
SELECT count(),max(t0),argMax(first_block,tuple(t0,first_block)),min(first_block)
FROM fs INNER JOIN cand USING(pool_address)
WHERE t0>='{START}' AND t0<='{END}' FORMAT TabSeparated
"""
    n, latest, last_b, first_b = ch(sql).strip().split("\t")
    return {
        "raw_universe_n": int(n),
        "latest_first_swap_time": latest + "Z",
        "latest_first_swap_block": int(last_b),
        "earliest_first_swap_block": int(first_b),
    }


def first_swaps() -> pd.DataFrame:
    sql = f"""
SET max_memory_usage=8000000000,max_execution_time=400,max_threads=2;
WITH cand AS (
 SELECT lower(pool_address) pool_address,lower(token0_address) t0a,lower(token1_address) t1a
 FROM eatlq.discovered_pools WHERE chain_id=8453 AND pool_type='v3' AND has_weth_leg=1
), fs AS (
 SELECT lower(pool_address) pool_address,min(block_time) t0
 FROM eatlq.uniswap_v3_swaps_decoded WHERE chain_id=8453 AND block_time IS NOT NULL
 GROUP BY pool_address HAVING t0>='{START}' AND t0<='{END}'
)
SELECT fs.pool_address pool,fs.t0,c.t0a,c.t1a
FROM fs INNER JOIN cand c USING(pool_address)
ORDER BY t0 FORMAT TabSeparatedWithNames
"""
    return pd.read_csv(StringIO(ch(sql)), sep="\t")


def _agg_pre(sw: pd.DataFrame, meta: pd.DataFrame) -> pd.DataFrame:
    sw = sw.merge(meta, on="pool", how="inner")
    sw["sec"] = (pd.to_datetime(sw.bt) - pd.to_datetime(sw.t0)).dt.total_seconds()
    sw = sw[(sw.sec >= 0) & (sw.sec <= W)].copy()
    if sw.empty:
        return pd.DataFrame()
    sw["a0"] = pd.to_numeric(sw.a0, errors="coerce").fillna(0.0)
    sw["a1"] = pd.to_numeric(sw.a1, errors="coerce").fillna(0.0)
    sw["liq"] = pd.to_numeric(sw.liq, errors="coerce").fillna(0.0)
    sw["tick"] = pd.to_numeric(sw.tick, errors="coerce").fillna(0).astype(int)
    sw["bn"] = pd.to_numeric(sw.bn, errors="coerce").fillna(0).astype(int)
    sw["li"] = pd.to_numeric(sw.li, errors="coerce").fillna(0).astype(int)
    sw["weth"] = np.where(sw.t0a.str.lower() == WETH, sw.a0.abs(), sw.a1.abs()) / 1e18
    sw["ord"] = list(zip(sw.bt.astype(str), sw.bn.astype(int), sw.tx.astype(str), sw.li.astype(int)))
    rows = []
    for pool, g in sw.groupby("pool", sort=False):
        g = g.sort_values(["bt", "bn", "tx", "li"])
        first = g.iloc[0]
        last = g.iloc[-1]
        h1 = g[g.sec <= W // 2]
        last_h1 = h1.iloc[-1] if len(h1) else first
        token = first.t1a if first.t0a.lower() == WETH else first.t0a
        orient = -1 if first.t0a.lower() == WETH else 1
        rows.append({
            "pool": pool,
            "orient": orient,
            "token": str(token).lower(),
            "t0": first.t0,
            "ns_pre": int(len(g)),
            "buyers_pre": int(g.rcp.nunique()),
            "senders_pre": int(g.snd.nunique()),
            "first_tick": int(first.tick),
            "last_tick_pre": int(last.tick),
            "min_tick_pre": int(g.tick.min()),
            "max_tick_pre": int(g.tick.max()),
            "liq_first": float(first.liq),
            "liq_last_pre": float(last.liq),
            "liq_min_pre": float(g.liq.min()),
            "ns_h1": int(len(h1)),
            "ns_h2": int(len(g) - len(h1)),
            "last_tick_h1": int(last_h1.tick),
            "entry_block": int(last.bn),
            "max_weth_pre": float(g.weth.max()),
        })
    out = pd.DataFrame(rows)
    return out[out.ns_pre >= 5].reset_index(drop=True)


def candidates() -> pd.DataFrame:
    fs = first_swaps()
    frames = []
    for i in range(0, len(fs), 80):
        chunk = fs.iloc[i:i + 80].copy()
        chunk["pool"] = chunk.pool.str.lower()
        ins = ",".join(f"'{p}'" for p in chunk.pool)
        tmin = str(pd.Timestamp(chunk.t0.min()))
        tmax = str(pd.Timestamp(chunk.t0.max()) + pd.Timedelta(seconds=W + 30))
        sql = f"""
SET max_memory_usage=6000000000,max_execution_time=300,max_threads=2;
SELECT lower(pool_address) pool,tick,block_time bt,block_number bn,tx_hash tx,log_index li,
       lower(recipient) rcp,lower(sender) snd,toFloat64(liquidity) liq,
       toFloat64(amount0) a0,toFloat64(amount1) a1
FROM eatlq.uniswap_v3_swaps_decoded
WHERE chain_id=8453 AND lower(pool_address) IN ({ins})
  AND block_time>='{tmin}' AND block_time<='{tmax}'
FORMAT TabSeparatedWithNames
"""
        raw = pd.read_csv(StringIO(ch(sql)), sep="\t")
        if raw.empty:
            continue
        part = _agg_pre(raw, chunk.rename(columns={"t0a": "t0a", "t1a": "t1a"})[["pool", "t0", "t0a", "t1a"]])
        if not part.empty:
            frames.append(part)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates(subset=["pool"]).sort_values("t0").reset_index(drop=True)


def feature_matrix(df: pd.DataFrame, names: list[str]) -> pd.DataFrame:
    o = df.orient.astype(float)
    lp_first = df.first_tick * LN1P * o
    lp_last = df.last_tick_pre * LN1P * o
    a = df.min_tick_pre * LN1P * o
    b = df.max_tick_pre * LN1P * o
    lp_lo, lp_hi = np.minimum(a, b), np.maximum(a, b)
    lp_h1 = df.last_tick_h1.where(df.ns_h1 > 0, df.first_tick) * LN1P * o
    ns = df.ns_pre.clip(lower=1)
    buyers = df.buyers_pre.clip(lower=1)
    senders = df.senders_pre.clip(lower=1)
    liq0 = df.liq_first.clip(lower=1)
    nsh1 = df.ns_h1.clip(lower=1)
    vals = np.column_stack([
        df.ns_pre, df.buyers_pre, df.senders_pre, ns / buyers, df.buyers_pre / senders,
        lp_last - lp_first, lp_hi - lp_lo, lp_hi - lp_last, lp_last - lp_lo,
        np.log1p(df.liq_last_pre.clip(lower=0)), df.liq_last_pre / liq0, df.liq_min_pre / liq0,
        df.ns_h2 / nsh1, lp_h1 - lp_first, lp_last - lp_h1,
    ])
    return pd.DataFrame(vals, columns=names)


def load_model():
    historical, _ = K.load()
    model, X, _train, _test = R.fit_model(historical)
    return model, list(X.columns)


def step0_anchor():
    old = L.run_lens(L.load_base_df(), "qv2")
    dep, skel, creators, token_skel = L._load_blacklists()
    masks = L.gate_masks(old, dep, skel, creators, token_skel)
    fon = masks["live_full"] & old.gate_fric & old.tradeable
    aggressive = A.aggressive_gate(old, creators)
    anchor = {"labeled_n": len(old), "f_on_pass": int(fon.sum()), "aggressive_gate": int(aggressive.sum())}
    assert anchor == {"labeled_n": 644, "f_on_pass": 120, "aggressive_gate": 339}, anchor
    return anchor


def fetch_paths(t0_by_pool: dict[str, pd.Timestamp]) -> dict[str, list[tuple[int, int, int, float]]]:
    pools = list(t0_by_pool)
    out = {p: [] for p in pools}
    for i in range(0, len(pools), 60):
        chunk = pools[i:i + 60]
        ins = ",".join(f"'{p}'" for p in chunk)
        tmin = str(min(t0_by_pool[p] for p in chunk) + pd.Timedelta(seconds=W))
        tmax = str(max(t0_by_pool[p] for p in chunk) + pd.Timedelta(seconds=W + H))
        sql = f"""
SET max_memory_usage=5000000000,max_execution_time=240,max_threads=2;
SELECT lower(pool_address),tick,block_number,tx_hash,log_index,toFloat64(liquidity),block_time
FROM eatlq.uniswap_v3_swaps_decoded
WHERE chain_id=8453 AND lower(pool_address) IN ({ins})
  AND block_time>='{tmin}' AND block_time<='{tmax}'
ORDER BY 1,block_time,block_number,tx_hash,log_index
FORMAT TabSeparated
"""
        raw = {}
        for line in ch(sql).splitlines():
            p, tick, bn, tx, li, lq, bt = line.split("\t")
            raw.setdefault(p, []).append((pd.Timestamp(bt), int(tick), int(bn), float(lq)))
        for p in chunk:
            t0 = pd.Timestamp(t0_by_pool[p])
            rows = []
            for bt, tick, bn, lq in raw.get(p, []):
                sec = (bt.tz_localize(None) - t0.tz_localize(None)).total_seconds()
                if W <= sec <= W + H:
                    rows.append((int((sec - W) // 600), tick, bn, lq))
            # last tick per bucket
            by_b = {}
            for bkt, tick, bn, lq in rows:
                by_b[bkt] = (bkt, tick, bn, lq)
            out[p] = [by_b[k] for k in sorted(by_b)]
    return out


def fetch_liq_grid(t0_by_pool: dict[str, pd.Timestamp]) -> dict[str, list[tuple[int, float]]]:
    pools = list(t0_by_pool)
    out = {p: [] for p in pools}
    for i in range(0, len(pools), 60):
        chunk = pools[i:i + 60]
        ins = ",".join(f"'{p}'" for p in chunk)
        tmin = str(min(t0_by_pool[p] for p in chunk) + pd.Timedelta(seconds=W))
        tmax = str(max(t0_by_pool[p] for p in chunk) + pd.Timedelta(seconds=W + H + 3600))
        sql = f"""
SET max_memory_usage=5000000000,max_execution_time=240,max_threads=2;
SELECT lower(pool_address),block_number,toFloat64(liquidity),block_time
FROM eatlq.uniswap_v3_swaps_decoded
WHERE chain_id=8453 AND lower(pool_address) IN ({ins})
  AND block_time>='{tmin}' AND block_time<='{tmax}'
ORDER BY 1,block_number,log_index
FORMAT TabSeparated
"""
        raw = {}
        for line in ch(sql).splitlines():
            p, bn, lq, bt = line.split("\t")
            raw.setdefault(p, []).append((pd.Timestamp(bt), int(bn), float(lq)))
        for p in chunk:
            t0 = pd.Timestamp(t0_by_pool[p])
            series = []
            for bt, bn, lq in raw.get(p, []):
                sec = (bt.tz_localize(None) - t0.tz_localize(None)).total_seconds()
                if W <= sec <= W + H + 3600:
                    series.append((bn, lq))
            out[p] = series
    return out


def exit_block(row, path):
    entry_lp = row.last_tick_pre * LN1P * int(row.orient)
    peak = entry_lp
    last_block = int(row.entry_block)
    last_tick = int(row.last_tick_pre)
    for _, tick, block, _liq in path:
        last_block, last_tick = int(block), int(tick)
        lp = tick * LN1P * int(row.orient)
        peak = max(peak, lp)
        if lp - entry_lp <= HARD_STOP:
            return block, "hard_stop", tick
        if lp - peak <= math.log(1 - TRAIL):
            return block, "trail", tick
        if block - int(row.entry_block) >= MAX_HOLD_BLOCKS:
            return block, "max_hold", tick
    if path:
        return min(path[-1][2], int(row.entry_block) + MAX_HOLD_BLOCKS), "path_end", path[-1][1]
    return int(row.entry_block) + MAX_HOLD_BLOCKS, "path_missing", int(row.last_tick_pre)


def f_status(row, creators, token_skel, dep, skels):
    if pd.Timestamp(row.t0, tz="UTC") < F_READY:
        return "F_NOT_AVAILABLE_PIT"
    tok = row.token.lower()
    c = (creators.get(tok) or {}).get("creator", "").lower()
    s = token_skel.get(tok)
    if (c and c in dep) or (s and s in skels):
        return "F_BLOCK"
    if c and s:
        return "F_PASS"
    return "F_IDENTITY_UNRESOLVED"


def pick_liq(series: list[tuple[int, float]], target: int, side: str) -> float | None:
    if not series:
        return None
    if side == "at":
        band = [(b, l) for b, l in series if target - 1 <= b <= target + 15]
        if band:
            return min(band, key=lambda x: (abs(x[0] - target), x[0]))[1]
        before = [(b, l) for b, l in series if b <= target]
        return before[-1][1] if before else None
    if side == "lag":
        after = [(b, l) for b, l in series if b >= target + 15]
        if after:
            return after[0][1]
        band = [(b, l) for b, l in series if target - 1 <= b <= target + 15]
        return band[-1][1] if band else None
    return None


def collapse_branch(entry_l: float, target_l: float | None, lag_l: float | None, thr: float = 0.05) -> str:
    if not entry_l or target_l is None or lag_l is None:
        return "UNRESOLVED"
    tr, lr = target_l / entry_l, lag_l / entry_l
    if tr <= thr and lr <= thr:
        return "CONFIRMED_COLLAPSE"
    if tr > thr:
        return "PROXY_ALIVE"
    return "UNRESOLVED"


def paper_net(row, exit_tick: int) -> float:
    entry_lp = float(row.last_tick_pre) * LN1P * int(row.orient)
    exit_lp = float(exit_tick) * LN1P * int(row.orient)
    raw = math.exp(exit_lp - entry_lp) - 1.0
    return raw - 0.15 / SIZE_USD - 0.02


def metrics(rows: list[dict], off: str) -> dict:
    legs = [r["offsets"][off] for r in rows]
    if not legs:
        return {"n": 0, "mean_interval": [None, None], "unresolved_n": 0, "right_tail_pass": False}
    lo = np.array([x["lower"] for x in legs], dtype=float)
    hi = np.array([x["upper"] for x in legs], dtype=float)
    resolved = [x["ret"] for x in legs if x["resolved"]]
    pos_lo = np.maximum(lo, 0)
    pos_hi = np.maximum(hi, 0)
    top1_lo = float(pos_lo.max() / pos_hi.sum()) if pos_hi.sum() > 0 else None
    top1_hi = float(min(1.0, pos_hi.max() / pos_lo.sum())) if pos_lo.sum() > 0 else None
    n = len(legs)
    n_col = sum(x["branch"] == "CONFIRMED_COLLAPSE" for x in legs)
    n_unr = sum(x["branch"] == "UNRESOLVED" for x in legs)
    return {
        "n": n,
        "resolved_n": len(resolved),
        "unresolved_n": n_unr,
        "mean_interval": [float(lo.mean()), float(hi.mean())],
        "resolved_mean": float(np.mean(resolved)) if resolved else None,
        "median_interval": [float(np.median(lo)), float(np.median(hi))],
        "win_rate_interval": [float(np.mean(lo > 0)), float(np.mean(hi > 0))],
        "branches": dict(Counter(x["branch"] for x in legs)),
        "confirmed_rug_interval": [n_col / n, (n_col + n_unr) / n],
        "top1_positive_contribution_interval": [top1_lo, top1_hi],
        "top1_removed_lower_mean": float(np.sort(lo)[:-1].mean()) if len(lo) > 1 else None,
        "right_tail_pass": False,
        "right_tail_reason": "N<30 and/or point-in-time F selection unresolved; CH_PROXY not adjudicable",
        "pricing": "CH_PROXY_TICK_AND_LIQUIDITY",
    }


def stratum(ts: str) -> str:
    t = pd.Timestamp(ts)
    if t <= pd.Timestamp("2026-06-30 23:59:59+00:00"):
        return "S0_jun24_30"
    if t <= pd.Timestamp("2026-07-31 23:59:59+00:00"):
        return "S1_jul"
    return "S2_aug"


def funnel_for(rows_all_minpre: pd.DataFrame, scored: pd.DataFrame, built: list[dict], mask=None) -> dict:
    if mask is None:
        mp = scored
        built_s = built
        raw_note = "raw counted only on full window"
    else:
        mp = scored[mask]
        built_s = [r for r in built if mask.loc[scored.index[scored.pool == r["pool"]][0]]] if False else [
            r for r in built if True
        ]
        # rebuilt below
        built_s = [r for r in built if _in_mask(r, scored, mask)]
        mp = scored[mask]
    return {
        "min_pre_pass": int(len(mp) if mask is None else mask.sum() and len(scored[mask]) or len(scored[mask])),
        "model_pass": int(len(mp)),
        "f_block": sum(r["f_status"] == "F_BLOCK" for r in built_s),
        "f_pass": sum(r["f_status"] == "F_PASS" for r in built_s),
        "f_identity_unresolved": sum(r["f_status"] == "F_IDENTITY_UNRESOLVED" for r in built_s),
        "f_not_available_pit": sum(r["f_status"] == "F_NOT_AVAILABLE_PIT" for r in built_s),
        "depth_flow_pass": sum(r.get("depth_flow") == "DEPTH_FLOW_PASS" for r in built_s),
        "depth_flow_unresolved": sum(r.get("depth_flow") == "DEPTH_FLOW_UNRESOLVED" for r in built_s),
    }


def _in_mask(rec, scored, mask) -> bool:
    hit = scored.index[scored.pool == rec["pool"]]
    if len(hit) == 0:
        return False
    return bool(mask.loc[hit[0]])


def main():
    assert sha(PREREG) == PREREG_SHA, sha(PREREG)
    assert sha(ROOT_EATLQ / "reports/GL6_preregistration_20260716.md") == GL6_PREREG_SHA
    assert sha(ROOT_EATLQ / "reports/GL6_preregistration_erratum_model_identity_20260716.md") == GL6_ERRATUM_SHA
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    anchor = step0_anchor()
    coverage = universe_coverage()
    df = candidates()
    model, names = load_model()
    df["p_fatal"] = model.predict_proba(feature_matrix(df, names))[:, 1]
    census_path = OUT_DIR / "feature_census.json"
    census_rows = json.loads(df.sort_values(["t0", "pool"]).to_json(orient="records", date_format="iso"))
    census_path.write_text(json.dumps({
        "schema_version": 1,
        "total_order": ["block_time", "block_number", "tx_hash", "log_index"],
        "rows": census_rows,
    }, indent=2, sort_keys=True) + "\n")
    scored = df[(df.ns_pre >= 5) & (df.p_fatal <= 0.27)].copy()

    creators = json.loads((ROOT_EATLQ / "mvp/audits/funding_cluster_poc_20260624/deployer_funding_graph.json").read_text()).get("token_creators", {})
    token_skel = json.loads((ROOT_EATLQ / "scripts/research/base_lefttail/_token_skeleton_644.json").read_text())
    dep = set(json.loads((ROOT_EATLQ / "services/executor/fixtures/safe_rug_deployers.json").read_text())["deployers"])
    dep |= {"0x715c98cedbd0c242e257986535bbf6e1fba764be", "0xc8512b5324402be9ab986d011c9adef8992f076f"}
    skels = set(json.loads((ROOT_EATLQ / "scripts/research/base_lefttail/_rug_seed_validated.json").read_text())["safe_skeletons"])

    scored = scored.copy()
    scored["pool"] = scored.pool.str.lower()
    t0_by_pool = {r.pool: pd.Timestamp(r.t0) for r in scored.itertuples(index=False)}
    paths = fetch_paths(t0_by_pool)
    grids = fetch_liq_grid(t0_by_pool)

    built = []
    for r in scored.itertuples(index=False):
        path = paths.get(r.pool.lower(), [])
        ex, reason, ex_tick = exit_block(r, path)
        d = {k: (v.item() if hasattr(v, "item") else v) for k, v in r._asdict().items()}
        d["t0"] = str(pd.Timestamp(r.t0, tz="UTC"))
        d["stratum"] = stratum(d["t0"])
        d["legacy_exit_block"] = int(ex)
        d["exit_reason"] = reason
        d["exit_tick"] = int(ex_tick)
        d["f_status"] = f_status(r, creators, token_skel, dep, skels)
        d["depth_flow"] = "DEPTH_FLOW_PASS" if float(r.max_weth_pre) >= 3.0 else "DEPTH_FLOW_UNRESOLVED"
        d["gate_status"] = "GATE_UNRESOLVED_NO_ARCHIVE"
        net = paper_net(r, int(ex_tick))
        series = grids.get(r.pool.lower(), [])
        d["offsets"] = {}
        for off in OFFSETS:
            target = int(ex) + off
            tL = pick_liq(series, target, "at")
            lL = pick_liq(series, target, "lag")
            branch = collapse_branch(float(r.liq_last_pre), tL, lL, 0.05)
            resolved = branch in ("PROXY_ALIVE", "CONFIRMED_COLLAPSE")
            ret = PESS if branch == "CONFIRMED_COLLAPSE" else (net if branch == "PROXY_ALIVE" else None)
            d["offsets"][str(off)] = {
                "target_block": target,
                "entry_L": float(r.liq_last_pre),
                "target_L": tL,
                "lag_L": lL,
                "branch": branch,
                "resolved": resolved,
                "ret": ret,
                "paper_net": net,
                "lower": ret if resolved else PESS,
                "upper": ret if resolved else (net if ex_tick is not None else 0.0),
                "sens_0_10": collapse_branch(float(r.liq_last_pre), tL, lL, 0.10),
                "sens_0_01": collapse_branch(float(r.liq_last_pre), tL, lL, 0.01),
            }
        built.append(d)

    built.sort(key=lambda x: (x["t0"], x["pool"]))
    possible = [r for r in built if r["f_status"] != "F_BLOCK"]
    definite = [r for r in possible if r["f_status"] == "F_PASS"]
    baseline = list(built)
    depth_flow = [r for r in possible if r["depth_flow"] == "DEPTH_FLOW_PASS"]
    f_unres = sum(r["f_status"] in ("F_NOT_AVAILABLE_PIT", "F_IDENTITY_UNRESOLVED") for r in possible)
    verdict = "INSUFFICIENT_FORWARD_N" if len(definite) < 30 or (possible and f_unres / len(possible) > 0.2) else "UNADJUDICATED"

    def layer(name, rows):
        return {str(o): metrics(rows, str(o)) for o in OFFSETS}

    strata_funnel = {}
    for key, (a, b) in STRATA.items():
        sub_scored = scored[(scored.t0 >= a) & (scored.t0 <= b)]
        sub_built = [r for r in built if a.replace(" ", "T") in r["t0"] or r["stratum"] == key]
        # use stratum tag
        sub_built = [r for r in built if r["stratum"] == key]
        strata_funnel[key] = {
            "min_pre_pass": int(((df.t0 >= a) & (df.t0 <= b)).sum()),
            "model_pass": int(len(sub_scored)),
            "f_block": sum(r["f_status"] == "F_BLOCK" for r in sub_built),
            "f_pass": sum(r["f_status"] == "F_PASS" for r in sub_built),
            "f_identity_unresolved": sum(r["f_status"] == "F_IDENTITY_UNRESOLVED" for r in sub_built),
            "depth_flow_pass": sum(r.get("depth_flow") == "DEPTH_FLOW_PASS" for r in sub_built),
            "proxy_metrics_model_pass": layer("m", sub_built),
        }

    result = {
        "schema_version": 1,
        "task_id": "KS-FWD-R1",
        "preregistration_sha256": PREREG_SHA,
        "gl6_preregistration_sha256": GL6_PREREG_SHA,
        "gl6_model_identity_erratum_sha256": GL6_ERRATUM_SHA,
        "research_only": True,
        "archive_rpc_available": False,
        "step0_anchor": anchor,
        "frozen_config": {
            "model": "deterministic reconstruction via risk_rebacktest.fit_model on frozen historical _pools.tsv only",
            "forward_rows_in_fit": 0,
            "min_pre": 5,
            "p_cut": 0.27,
            "trail": 0.35,
            "hard_stop": 0.5,
            "max_hold_blocks": 7200,
            "stake_eth": 0.0302,
            "depth_archive": "UNAVAILABLE",
            "friction_archive": "UNAVAILABLE",
        },
        "window": {"start": START + "Z", "end": END + "Z", "f_ready": str(F_READY), "observed_coverage": coverage},
        "feature_census": {"min_pre_pass_n": int(len(df)), "sha256": sha(census_path), "path": str(census_path)},
        "funnel": {
            "raw_universe": coverage["raw_universe_n"],
            "min_pre_block": coverage["raw_universe_n"] - int(len(df)),
            "min_pre_pass": int(len(df)),
            "model_pass": int(len(scored)),
            "f_block": sum(r["f_status"] == "F_BLOCK" for r in built),
            "f_pass": sum(r["f_status"] == "F_PASS" for r in built),
            "f_identity_unresolved": sum(r["f_status"] == "F_IDENTITY_UNRESOLVED" for r in built),
            "f_not_available_pit": sum(r["f_status"] == "F_NOT_AVAILABLE_PIT" for r in built),
            "definite_selected": len(definite),
            "possible_selected": len(possible),
            "f_unresolved_possible": f_unres,
            "depth_flow_pass_possible": len(depth_flow),
            "signals_still_present": int(len(scored)) >= 1,
        },
        "strata_funnel": strata_funnel,
        "definite_selected_metrics": layer("def", definite),
        "possible_selected_ch_proxy_metrics": layer("pos", possible),
        "model_pass_ch_proxy_metrics": layer("base", baseline),
        "depth_flow_possible_ch_proxy_metrics": layer("df", depth_flow),
        "historical_gl5v3": {
            "selected_return_intervals": {
                "1": [1.3672943342260926, 1.4770342541250798],
                "5": [1.3960784104486632, 1.4881408064429673],
                "15": [1.4771676388156305, 1.489564076021217],
            },
            "rug_interval_1": [0, 0.025],
        },
        "historical_gl6": {
            "definite_selected": 0,
            "gate_identified_n": 7,
            "plus1_mean": 0.19927,
            "plus15_mean": -0.24527,
            "plus15_collapse": "4/7",
        },
        "conditional_alpha": {
            "identified": False,
            "reason": "point-in-time F selection is not identified for forward tokens; archive depth/friction unavailable",
        },
        "verdict_candidate": verdict,
        "verdict_reason": "definite F-on forward population is below preregistered N and/or F-unresolved exceeds 20%",
        "go_decision": "NOT_MADE",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
    }
    legs_path = OUT_DIR / "per_leg.json"
    out_path = OUT_DIR / "forward_decay.json"
    legs_path.write_text(json.dumps({"rows": built}, indent=2, sort_keys=True, default=str) + "\n")
    out_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"funnel": result["funnel"], "verdict": verdict, "strata": {k: {kk: vv for kk, vv in v.items() if kk != "proxy_metrics_model_pass"} for k, v in strata_funnel.items()}}, indent=2))


if __name__ == "__main__":
    main()
