#!/usr/bin/env python3
"""June 2026 live-config supplement: CH MIN_PRE pools + archive RPC depth/friction.

Extends backtest window through 2026-06-25 (old _pools.tsv stops at 2026-06-01).
RPC: WETH depth @ entry_block, QuoterV2 round-trip friction @ entry_block.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pandas as pd
from eth_abi import encode
from web3 import Web3

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import keystone_live as K  # noqa
import live_config_rebacktest as L  # noqa
import risk_rebacktest as R  # noqa

OUT_DIR = "/opt/EATLQ/mvp/audits/aggressive_live_rebacktest_20260625"
DATE_LO, DATE_HI = "2026-06-01", "2026-06-25 23:59:59"
W, H = 600, 14400
WETH = Web3.to_checksum_address("0x4200000000000000000000000000000000000006")
QUOTER = Web3.to_checksum_address("0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a")
NOTIONAL_USD, ETH_USD = 100.0, 3000.0
AMT_IN = int(NOTIONAL_USD / ETH_USD * 1e18)
CH = ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query"]

ARCHIVE = os.environ.get("FORK_ARCHIVE_RPC", "")
if not ARCHIVE:
    raise SystemExit("FORK_ARCHIVE_RPC is not set — export an archive RPC endpoint (the token must never be committed)")
w3 = Web3(Web3.HTTPProvider(ARCHIVE, request_kwargs={"timeout": 30}))
_QSIG = Web3.keccak(text="quoteExactInputSingle((address,address,uint256,uint24,uint160))")[:4]

SQL = f"""
WITH cand AS (
  SELECT pool_address, lower(token0_address) t0a, lower(token1_address) t1a
  FROM eatlq.discovered_pools
  WHERE chain_id=8453 AND pool_type='v3' AND has_weth_leg=1
),
fs AS (
  SELECT pool_address, min(block_time) t0 FROM eatlq.uniswap_v3_swaps_decoded
  WHERE chain_id=8453 AND block_time IS NOT NULL GROUP BY pool_address
  HAVING t0 >= '{DATE_LO}' AND t0 <= '{DATE_HI}'
),
sw AS (
  SELECT s.pool_address pool, s.tick tick, s.block_time bt, s.block_number bn,
         s.recipient rcp, s.sender snd, toFloat64(s.liquidity) liq,
         dateDiff('second', fs.t0, s.block_time) sec, fs.t0 t0
  FROM eatlq.uniswap_v3_swaps_decoded s INNER JOIN fs ON s.pool_address=fs.pool_address
  WHERE s.chain_id=8453 AND s.block_time IS NOT NULL
    AND dateDiff('second', fs.t0, s.block_time) BETWEEN 0 AND {W + H}
)
SELECT pool, if(c.t0a='0x4200000000000000000000000000000000000006', -1, 1) orient,
  if(c.t0a='0x4200000000000000000000000000000000000006', c.t1a, c.t0a) alt, any(t0) t0,
  countIf(sec<= {W}) ns_pre, uniqExactIf(rcp, sec<= {W}) buyers_pre,
  uniqExactIf(snd, sec<= {W}) senders_pre,
  argMinIf(tick, bt, sec<= {W}) first_tick, argMaxIf(tick, bt, sec<= {W}) last_tick_pre,
  minIf(tick, sec<= {W}) min_tick_pre, maxIf(tick, sec<= {W}) max_tick_pre,
  argMinIf(liq, bt, sec<= {W}) liq_first, argMaxIf(liq, bt, sec<= {W}) liq_last_pre,
  minIf(liq, sec<= {W}) liq_min_pre,
  countIf(sec<= {W//2}) ns_h1, countIf(sec> {W//2} AND sec<= {W}) ns_h2,
  argMaxIf(tick, bt, sec<= {W//2}) last_tick_h1,
  argMaxIf(bn, bt, sec<= {W}) entry_block,
  countIf(sec> {W} AND sec<= {W+H}) ns_fwd,
  minIf(tick, sec> {W} AND sec<= {W+H}) min_tick_fwd,
  maxIf(tick, sec> {W} AND sec<= {W+H}) max_tick_fwd,
  argMaxIf(tick, bt, sec> {W} AND sec<= {W+H}) last_tick_fwd
FROM sw INNER JOIN cand c ON sw.pool=c.pool_address
GROUP BY pool, orient, alt HAVING ns_pre >= 5
FORMAT TabSeparated
"""
COLS = ["pool", "orient", "alt", "t0", "ns_pre", "buyers_pre", "senders_pre", "first_tick",
        "last_tick_pre", "min_tick_pre", "max_tick_pre", "liq_first", "liq_last_pre",
        "liq_min_pre", "ns_h1", "ns_h2", "last_tick_h1", "entry_block", "ns_fwd",
        "min_tick_fwd", "max_tick_fwd", "last_tick_fwd"]


def eth_call(to: str, data: str, block: int):
    for _ in range(3):
        try:
            r = w3.provider.make_request(
                "eth_call", [{"to": Web3.to_checksum_address(to), "data": data}, hex(int(block))]
            )
            return r.get("result")
        except Exception:
            time.sleep(0.3)
    return None


def pool_fee(pool: str, block: int) -> int | None:
    res = eth_call(pool, "0xddca3f43", block)
    if res and res != "0x":
        return int(res, 16)
    return None


def weth_depth(pool: str, block: int) -> float | None:
    data = "0x70a08231" + "0" * 24 + pool[2:].lower()
    res = eth_call(WETH, data, block)
    if res is None:
        return None
    return int(res, 16) / 1e18


def quote(tin, tout, amt, fee, block):
    params = encode(
        ["(address,address,uint256,uint24,uint160)"],
        [(Web3.to_checksum_address(tin), Web3.to_checksum_address(tout), int(amt), int(fee), 0)],
    )
    res = eth_call(QUOTER, "0x" + (_QSIG + params).hex(), block)
    if not res or res == "0x":
        return None
    return int(res[:66], 16)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    r = subprocess.run(CH + [SQL], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        print(r.stderr[:2000])
        sys.exit(1)

    lines = [ln for ln in r.stdout.strip().split("\n") if ln]
    cols = COLS
    df = pd.read_csv(pd.io.common.StringIO("\n".join(lines)), sep="\t", names=cols)
    df["t0"] = pd.to_datetime(df["t0"])
    for c in cols:
        if c not in ("pool", "alt", "t0"):
            df[c] = pd.to_numeric(df[c], errors="coerce")

    raw = K.load()[0]
    clf, X_hist, _, _ = R.fit_model(raw)
    X_j, _ = K.build(df)
    df["p"] = clf.predict_proba(X_j)[:, 1]

    _, _, creators, _ = L._load_blacklists()
    rows = []
    for i, row in df.iterrows():
        pool = row.pool
        eb = int(row.entry_block)
        alt = Web3.to_checksum_address(row.alt)
        fee = pool_fee(pool, eb)
        depth = weth_depth(pool, eb)
        fric = None
        why = "no_fee"
        if fee in (100, 500, 3000, 10000):
            tok = quote(WETH, alt, AMT_IN, fee, eb)
            if tok and tok > 0:
                back = quote(alt, WETH, tok, fee, eb)
                fric = (back / AMT_IN - 1.0) if back else -1.0
                why = "ok" if fric >= L.FRIC_THR else "fric_fail"
            else:
                fric = -1.0
                why = "entry_fail"
        rec = {
            "pool": pool,
            "token": row.alt,
            "t0": str(row.t0),
            "ns_pre": int(row.ns_pre),
            "buyers_pre": int(row.buyers_pre),
            "p_fatal": float(row.p),
            "entry_block": eb,
            "fee": fee,
            "entry_weth": depth,
            "fric": fric,
            "why": why,
            "model_ok": bool(row.ns_pre >= 5 and row.p <= 0.27),
            "depth_ok": bool(depth is not None and depth >= 3.0),
            "fric_ok": bool(fric is not None and fric >= L.FRIC_THR),
            "para_blocked": (creators.get(row.alt.lower()) or {}).get("creator", "").lower()
            in L.PARADAENGI_DEPLOYERS,
        }
        rec["aggressive_live"] = (
            rec["model_ok"] and rec["depth_ok"] and rec["fric_ok"] and not rec["para_blocked"]
        )
        rows.append(rec)
        if (i + 1) % 10 == 0:
            print(f"  RPC {i + 1}/{len(df)}", flush=True)

    passed = [x for x in rows if x["aggressive_live"]]
    report = {
        "meta": {
            "window": f"{DATE_LO} .. {DATE_HI}",
            "rpc": "archive eth_call (depth + QuoterV2 friction)",
            "live_config": {
                "MIN_PRE": 5, "P_CUT": 0.27, "MIN_DEPTH_ETH": 3.0,
                "FRIC_THR": L.FRIC_THR, "F_blacklist": "OFF",
            },
            "note": "No fork-replay labels/PnL for June — gate funnel only.",
        },
        "funnel": {
            "min_pre5": len(rows),
            "p_fatal_le027": sum(1 for x in rows if x["model_ok"]),
            "depth_ge3": sum(1 for x in rows if x["depth_ok"]),
            "fric_ok": sum(1 for x in rows if x["fric_ok"]),
            "aggressive_live_pass": len(passed),
        },
        "pools": rows,
        "aggressive_pass": passed,
    }
    out = os.path.join(OUT_DIR, "june2026_rpc_supplement.json")
    json.dump(report, open(out, "w"), indent=2, default=str)
    print(json.dumps(report["funnel"], indent=2))
    print(f"wrote {out}")
    if passed:
        print("PASS:", [p["pool"] for p in passed])


if __name__ == "__main__":
    main()
