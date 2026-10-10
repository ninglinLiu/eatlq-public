#!/usr/bin/env python3
"""Counterfactual: pools missed during 2026-06-25..27 archiver outage.

The signal bridge was blind while ClickHouse swap data stalled (~38h).
This script finds pools whose entry window overlapped the outage, scores them
with the live bridge model, applies live executor gates, and backtests
QuoterV2 round-trip PnL with TRAIL=0.25 (current aggressive live config).
"""
from __future__ import annotations

import json
import math
import os
import pickle
import subprocess
import sys
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from eth_abi import encode
from web3 import Web3

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE_DIR = "/opt/EATLQ/services/signal_bridge"
sys.path.insert(0, HERE)
import keystone_live as K  # noqa
import live_config_rebacktest as L  # noqa

OUT_DIR = "/opt/EATLQ/mvp/audits/outage_missed_20260627"
OUTAGE_START = datetime(2026, 6, 25, 15, 47, tzinfo=timezone.utc)
OUTAGE_END = datetime(2026, 6, 27, 6, 3, tzinfo=timezone.utc)
W, H = 600, 14400
P_CUT = 0.27
MIN_PRE = 5
MIN_DEPTH = 3.0
FRIC_THR = -0.05
TRAIL = 0.25
SIZE_ETH = 0.0302
ETH_USD = 1658.0
NOTIONAL = int(SIZE_ETH * ETH_USD / ETH_USD * 1e18)  # 0.0302 ETH in wei
AMT_IN = int(SIZE_ETH * 1e18)

ARCHIVE = os.environ.get("FORK_ARCHIVE_RPC", "")
if not ARCHIVE:
    raise SystemExit("FORK_ARCHIVE_RPC is not set — export an archive RPC endpoint (the token must never be committed)")
w3 = Web3(Web3.HTTPProvider(ARCHIVE, request_kwargs={"timeout": 30}))
WETH = Web3.to_checksum_address("0x4200000000000000000000000000000000000006")
QUOTER = Web3.to_checksum_address("0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a")
_QSIG = Web3.keccak(text="quoteExactInputSingle((address,address,uint256,uint24,uint160))")[:4]
CH = ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query"]
LN1P = math.log(1.0001)
HARD_STOP = math.log(0.5)

# Pools already seen by live bridge (emit or reject) — not "missed"
LIVE_SEEN = {
    "0x0f8c6c6994c159486b2261abc291765f57520039",  # NES emit 6/25
    "0xebb459fac2f2a33aeef7a65d05d28db13c1f596f",  # LOCKV3 emit 6/25
    "0x31eca2f53a4529e0f06ca6f99098f905e8b1c2a7",  # V3TEST reject 6/25
}


def load_bridge_model():
    with open(os.path.join(BRIDGE_DIR, "_model.pkl"), "rb") as f:
        clf = pickle.load(f)
    params = json.load(open(os.path.join(BRIDGE_DIR, "_params.json")))
    return clf, params


def bridge_features(row: pd.Series) -> np.ndarray:
    o = int(row.orient)
    lp = lambda t: t * LN1P * o
    lp_first = lp(row.first_tick)
    lp_last = lp(row.last_tick_pre)
    lp_lo = min(lp(row.min_tick_pre), lp(row.max_tick_pre))
    lp_hi = max(lp(row.min_tick_pre), lp(row.max_tick_pre))
    lp_h1 = lp(row.last_tick_h1) if row.last_tick_h1 else lp_first
    ns_pre = max(row.ns_pre, 1)
    buyers = max(row.buyers_pre, 1)
    senders = max(row.senders_pre, 1)
    liq_first = max(row.liq_first, 1)
    ns_h1 = max(row.ns_h1, 1)
    return np.array([[
        row.ns_pre, row.buyers_pre, row.senders_pre,
        ns_pre / buyers, row.buyers_pre / senders,
        lp_last - lp_first, lp_hi - lp_lo, lp_hi - lp_last, lp_last - lp_lo,
        np.log1p(max(row.liq_last_pre, 0)), row.liq_last_pre / liq_first,
        row.liq_min_pre / liq_first, row.ns_h2 / ns_h1,
        lp_h1 - lp_first, lp_last - lp_h1,
    ]], dtype=np.float64)


def entry_window_overlaps_outage(t0: pd.Timestamp) -> bool:
    if t0.tzinfo is None:
        t0 = t0.tz_localize("UTC")
    win_lo = t0 + pd.Timedelta(seconds=W)
    win_hi = t0 + pd.Timedelta(seconds=W + 300)
    o_start = pd.Timestamp(OUTAGE_START)
    o_end = pd.Timestamp(OUTAGE_END)
    return win_lo <= o_end and win_hi >= o_start


def exit_block_trail(path_steps, entry_lp, orient, entry_block, head, trail=TRAIL):
    if not path_steps:
        return min(entry_block + 7200, head), "silent", entry_lp
    peak = entry_lp
    for _bkt, ltick, lblock in path_steps:
        lp = ltick * LN1P * orient
        peak = max(peak, lp)
        if (lp - entry_lp) <= HARD_STOP:
            return lblock, "stop", lp
        if (lp - peak) <= math.log(1 - trail):
            return lblock, "trail", lp
    return path_steps[-1][2], "hold_end", path_steps[-1][1] * LN1P * orient


def eth_call(to: str, data: str, block: int):
    for _ in range(3):
        try:
            r = w3.provider.make_request(
                "eth_call", [{"to": Web3.to_checksum_address(to), "data": data}, hex(int(block))]
            )
            return r.get("result")
        except Exception:
            time.sleep(0.25)
    return None


def pool_fee(pool: str, block: int) -> int | None:
    res = eth_call(pool, "0xddca3f43", block)
    if res and res != "0x":
        return int(res, 16)
    return None


def weth_depth(pool: str, block: int) -> float | None:
    data = "0x70a08231" + "0" * 24 + pool[2:].lower()
    res = eth_call(WETH, data, block)
    return None if res is None else int(res, 16) / 1e18


def quote(tin, tout, amt, fee, block):
    params = encode(
        ["(address,address,uint256,uint24,uint160)"],
        [(Web3.to_checksum_address(tin), Web3.to_checksum_address(tout), int(amt), int(fee), 0)],
    )
    res = eth_call(QUOTER, "0x" + (_QSIG + params).hex(), block)
    if not res or res == "0x":
        return None
    return int(res[:66], 16)


def fetch_path(pool: str) -> list[tuple[int, int, int]]:
    sql = f"""
SET max_memory_usage = 1073741824, max_execution_time = 90, max_threads = 1;
WITH fs AS (
  SELECT min(block_time) t0 FROM eatlq.uniswap_v3_swaps_decoded
  WHERE chain_id=8453 AND pool_address='{pool}' AND block_time IS NOT NULL
),
sw AS (
  SELECT tick, block_number bn, dateDiff('second', fs.t0, block_time) sec
  FROM eatlq.uniswap_v3_swaps_decoded, fs
  WHERE chain_id=8453 AND pool_address='{pool}' AND block_time IS NOT NULL
    AND dateDiff('second', fs.t0, block_time) BETWEEN {W} AND {W + H}
)
SELECT intDiv(sec - {W}, 600) AS bkt, argMax(tick, bn) AS ltick, max(bn) AS lblock
FROM sw GROUP BY bkt ORDER BY bkt FORMAT TabSeparated
"""
    r = subprocess.run(CH + [sql], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return []
    path = []
    for ln in r.stdout.strip().split("\n"):
        if not ln:
            continue
        bkt, lt, lb = ln.split("\t")
        path.append((int(bkt), int(lt), int(lb)))
    return sorted(path, key=lambda x: x[0])


def pull_candidates() -> pd.DataFrame:
    sql = f"""
WITH cand AS (
  SELECT pool_address, lower(token0_address) t0a, lower(token1_address) t1a,
         token0_symbol, token1_symbol
  FROM eatlq.discovered_pools
  WHERE chain_id=8453 AND pool_type='v3' AND has_weth_leg=1
),
fs AS (
  SELECT pool_address, min(block_time) t0 FROM eatlq.uniswap_v3_swaps_decoded
  WHERE chain_id=8453 AND block_time IS NOT NULL GROUP BY pool_address
  HAVING t0 >= '2026-06-25 14:00:00' AND t0 <= '2026-06-27 05:30:00'
),
sw AS (
  SELECT s.pool_address pool, s.tick tick, s.block_time bt, s.block_number bn,
         s.recipient rcp, s.sender snd, toFloat64(s.liquidity) liq,
         dateDiff('second', fs.t0, s.block_time) sec, fs.t0 t0
  FROM eatlq.uniswap_v3_swaps_decoded s INNER JOIN fs ON s.pool_address=fs.pool_address
  WHERE s.chain_id=8453 AND s.block_time IS NOT NULL
    AND dateDiff('second', fs.t0, s.block_time) BETWEEN 0 AND {W + H}
)
SELECT lower(sw.pool) pool,
  if(c.t0a='0x4200000000000000000000000000000000000006', -1, 1) orient,
  if(c.t0a='0x4200000000000000000000000000000000000006', c.t1a, c.t0a) alt,
  any(c.token0_symbol) t0sym, any(c.token1_symbol) t1sym,
  any(t0) t0,
  countIf(sec<= {W}) ns_pre, uniqExactIf(rcp, sec<= {W}) buyers_pre,
  uniqExactIf(snd, sec<= {W}) senders_pre,
  argMinIf(tick, bt, sec<= {W}) first_tick, argMaxIf(tick, bt, sec<= {W}) last_tick_pre,
  minIf(tick, sec<= {W}) min_tick_pre, maxIf(tick, sec<= {W}) max_tick_pre,
  argMinIf(liq, bt, sec<= {W}) liq_first, argMaxIf(liq, bt, sec<= {W}) liq_last_pre,
  minIf(liq, sec<= {W}) liq_min_pre,
  countIf(sec<= {W//2}) ns_h1, countIf(sec> {W//2} AND sec<= {W}) ns_h2,
  argMaxIf(tick, bt, sec<= {W//2}) last_tick_h1,
  argMaxIf(bn, bt, sec<= {W}) entry_block
FROM sw INNER JOIN cand c ON sw.pool=c.pool_address
GROUP BY pool, orient, alt HAVING ns_pre >= {MIN_PRE}
FORMAT TabSeparated
"""
    cols = ["pool", "orient", "alt", "t0sym", "t1sym", "t0", "ns_pre", "buyers_pre",
            "senders_pre", "first_tick", "last_tick_pre", "min_tick_pre", "max_tick_pre",
            "liq_first", "liq_last_pre", "liq_min_pre", "ns_h1", "ns_h2", "last_tick_h1",
            "entry_block"]
    r = subprocess.run(CH + [sql], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:2000])
    lines = [ln for ln in r.stdout.strip().split("\n") if ln]
    df = pd.read_csv(pd.io.common.StringIO("\n".join(lines)), sep="\t", names=cols)
    df["t0"] = pd.to_datetime(df["t0"], utc=True)
    for c in cols:
        if c not in ("pool", "alt", "t0sym", "t1sym", "t0"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    clf, params = load_bridge_model()
    feat_names = params["features"]

    df = pull_candidates()
    df["missed_window"] = df["t0"].apply(entry_window_overlaps_outage)
    missed = df[df["missed_window"]].copy()
    missed = missed[~missed["pool"].str.lower().isin(LIVE_SEEN)]

    print(f"Total MIN_PRE>={MIN_PRE} pools in window: {len(df)}")
    print(f"Entry window overlaps outage: {len(missed)}")

    rows = []
    for i, row in missed.iterrows():
        fv = bridge_features(row)
        p_fatal = float(clf.predict_proba(pd.DataFrame(fv, columns=feat_names))[0, 1])
        eb = int(row.entry_block)
        alt = Web3.to_checksum_address(row.alt)
        fee = pool_fee(row.pool, eb)
        depth = weth_depth(row.pool, eb)
        fric = None
        net = None
        exit_reason = None
        gate = []

        if p_fatal > P_CUT:
            gate.append("p_fatal")
        if depth is None or depth < MIN_DEPTH:
            gate.append("shallow")
        if fee not in (100, 500, 3000, 10000):
            gate.append("bad_fee")
        else:
            tok = quote(WETH, alt, AMT_IN, fee, eb)
            if not tok or tok <= 0:
                gate.append("entry_quote_fail")
                fric = -1.0
            else:
                back = quote(alt, WETH, tok, fee, eb)
                fric = (back / AMT_IN - 1.0) if back else -1.0
                if fric < FRIC_THR:
                    gate.append("friction")

        would_emit = len(gate) == 0 and p_fatal <= P_CUT

        if would_emit or (p_fatal <= P_CUT and "p_fatal" not in gate):
            path = fetch_path(row.pool)
            head = w3.eth.block_number
            entry_lp = row.last_tick_pre * LN1P * int(row.orient)
            if fee in (100, 500, 3000, 10000) and fric is not None and fric >= FRIC_THR:
                tok = quote(WETH, alt, AMT_IN, fee, eb)
                if tok and tok > 0:
                    exb, exit_reason, _ = exit_block_trail(path, entry_lp, int(row.orient), eb, head)
                    sell = quote(alt, WETH, tok, fee, exb)
                    net = (sell / AMT_IN - 1.0) if sell else -1.0

        sym = f"{row.t0sym}/{row.t1sym}" if row.orient == -1 else f"{row.t1sym}/{row.t0sym}"
        rec = {
            "pool": row.pool,
            "token": row.alt,
            "symbol": sym,
            "t0": str(row.t0),
            "signal_window": f"{row.t0 + pd.Timedelta(seconds=W)} .. {row.t0 + pd.Timedelta(seconds=W+300)}",
            "ns_pre": int(row.ns_pre),
            "p_fatal": round(p_fatal, 4),
            "entry_block": eb,
            "depth_eth": round(depth, 4) if depth is not None else None,
            "friction": round(fric, 4) if fric is not None else None,
            "gates_blocked": gate,
            "would_have_bought": would_emit,
            "exit_reason": exit_reason,
            "net_pnl_pct": round(net * 100, 2) if net is not None else None,
            "net_pnl_usd": round(net * SIZE_ETH * ETH_USD, 2) if net is not None else None,
        }
        rows.append(rec)
        if (len(rows)) % 5 == 0:
            print(f"  processed {len(rows)}/{len(missed)}", flush=True)

    bought = [r for r in rows if r["would_have_bought"]]
    model_pass = [r for r in rows if r["p_fatal"] <= P_CUT]
    blocked_after_model = [r for r in model_pass if not r["would_have_bought"]]

    summary = {
        "meta": {
            "outage": f"{OUTAGE_START.isoformat()} .. {OUTAGE_END.isoformat()}",
            "live_config": {"P_CUT": P_CUT, "MIN_DEPTH": MIN_DEPTH, "FRIC_THR": FRIC_THR,
                            "TRAIL": TRAIL, "SIZE_ETH": SIZE_ETH},
            "method": "bridge pickle + archive QuoterV2 + TRAIL 0.25 tick path",
            "caveat": "QuoterV2 backtest; does not model honeypot/real sell failure/MEV/latency",
        },
        "funnel": {
            "min_pre_pools_in_outage_window": len(missed),
            "p_fatal_le_027": len(model_pass),
            "would_have_bought_live_gates": len(bought),
            "blocked_after_model": len(blocked_after_model),
        },
        "counterfactual_trades": bought,
        "blocked_after_model": blocked_after_model,
        "all_scored": rows,
    }

    if bought:
        pnls = [r["net_pnl_pct"] for r in bought if r["net_pnl_pct"] is not None]
        summary["pnl_if_bought"] = {
            "n": len(pnls),
            "total_usd": sum(r["net_pnl_usd"] or 0 for r in bought),
            "mean_pct": round(float(np.mean(pnls)), 2) if pnls else None,
            "median_pct": round(float(np.median(pnls)), 2) if pnls else None,
            "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 2) if pnls else None,
            "fatal_rate": round(sum(1 for p in pnls if p <= -50) / len(pnls), 2) if pnls else None,
        }

    out = os.path.join(OUT_DIR, "outage_missed_counterfactual.json")
    json.dump(summary, open(out, "w"), indent=2, default=str)
    print(json.dumps(summary["funnel"], indent=2))
    if "pnl_if_bought" in summary:
        print("PnL if bought:", json.dumps(summary["pnl_if_bought"], indent=2))
    if bought:
        print("\nWould-have-bought:")
        for r in bought:
            print(f"  {r['symbol']} p={r['p_fatal']} net={r['net_pnl_pct']}% (${r['net_pnl_usd']}) exit={r['exit_reason']} gates={r['gates_blocked']}")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
