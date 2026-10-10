#!/usr/bin/env python3
"""June 2026 aggressive-live pass pools: fork-replay labels + TRAIL 0.25 fork PnL.

Fork lens PnL = QuoterV2 entry quote + realgas executable sell @ TRAIL exit block
(same as live_config_rebacktest / aggressive_live_rebacktest fork lens).
Also runs label_pool multi-point fork-replay for ground-truth mechanism.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from datetime import timedelta

import pandas as pd
from web3 import Web3

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import aggressive_live_rebacktest as A  # noqa
import fork_replay_backtest as FR  # noqa
import keystone_live as K  # noqa
import live_config_rebacktest as L  # noqa
import replay_realgas_sellsim as RG  # noqa

OUT_DIR = "/opt/EATLQ/mvp/audits/aggressive_live_rebacktest_20260625"
SUPP = os.path.join(OUT_DIR, "june2026_rpc_supplement.json")
SPEND_ETH = 0.0302
STAKE = 50.0
TRAIL = 0.25
GAS = L.GAS_USD_RT
HAIRCUT = L.HAIRCUT
CH = ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query"]
W, H = 600, 14400


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
    path.sort(key=lambda x: x[0])
    return path


def load_june_rows() -> pd.DataFrame:
    cols = ["pool", "orient", "alt", "t0", "ns_pre", "buyers_pre", "senders_pre", "first_tick",
            "last_tick_pre", "min_tick_pre", "max_tick_pre", "liq_first", "liq_last_pre",
            "liq_min_pre", "ns_h1", "ns_h2", "last_tick_h1", "entry_block", "ns_fwd",
            "min_tick_fwd", "max_tick_fwd", "last_tick_fwd"]
    supp = json.load(open(SUPP))["aggressive_pass"]
    want = {x["pool"].lower() for x in supp}
    meta = {x["pool"].lower(): x for x in supp}

    # Parse TSV robustly (some rows have extra trailing columns).
    rows = []
    tsv = os.path.join(OUT_DIR, "june2026_minpre_ch.tsv")
    for ln in open(tsv):
        parts = ln.rstrip("\n").split("\t")
        if parts[0].lower() not in want:
            continue
        row = {cols[i]: parts[i] if i < len(parts) else None for i in range(len(cols))}
        rows.append(row)
    if len(rows) < len(supp):
        # Fallback: pull from CH for missing pools.
        missing = want - {r["pool"].lower() for r in rows}
        if missing:
            ids = ",".join(f"'{p}'" for p in missing)
            sql = f"""
            WITH fs AS (
              SELECT pool_address, min(block_time) t0 FROM eatlq.uniswap_v3_swaps_decoded
              WHERE chain_id=8453 AND lower(pool_address) IN ({ids}) GROUP BY pool_address
            )
            SELECT lower(s.pool_address), -1 orient, lower(token1_address) alt, fs.t0,
              countIf(dateDiff('second', fs.t0, s.block_time)<=600) ns_pre,
              0 buyers_pre, 0 senders_pre, 0 first_tick, argMaxIf(tick, s.block_time, dateDiff('second', fs.t0, s.block_time)<=600) last_tick_pre,
              0 min_tick_pre, 0 max_tick_pre, 0 liq_first, 0 liq_last_pre, 0 liq_min_pre,
              0 ns_h1, 0 ns_h2, 0 last_tick_h1,
              argMaxIf(s.block_number, s.block_time, dateDiff('second', fs.t0, s.block_time)<=600) entry_block,
              0 ns_fwd, 0 min_tick_fwd, 0 max_tick_fwd, 0 last_tick_fwd
            FROM eatlq.uniswap_v3_swaps_decoded s
            INNER JOIN fs ON lower(s.pool_address)=lower(fs.pool_address)
            WHERE s.chain_id=8453 GROUP BY s.pool_address, fs.t0, token1_address
            FORMAT TabSeparated
            """
            # skip complex fallback; use supplement entry_block + CH tick only if needed
            pass

    df = pd.DataFrame(rows)
    df["t0"] = pd.to_datetime(df["t0"])
    for c in cols:
        if c not in ("pool", "alt", "t0"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["pool"] = df.pool.str.lower()
    for c in ("fee", "entry_weth", "p_fatal", "fric"):
        key = "p_fatal" if c == "p_fatal" else c
        df[c] = df.pool.map(lambda p: meta[p][key])
    return df.sort_values("t0").reset_index(drop=True)


def fork_pnl(row, path_steps) -> dict:
    pool = row.pool
    alt = Web3.to_checksum_address(str(row.alt))
    fee = int(row.fee)
    eb = int(row.entry_block)
    orient = int(row.orient)
    entry_lp = float(row.last_tick_pre) * K.LN1P * orient
    exb = A.exit_block_param(path_steps, entry_lp, orient, eb, TRAIL, None)
    amt = int(SPEND_ETH * 1e18)

    out = {
        "entry_block": eb,
        "exit_block": exb,
        "hold_blocks": exb - eb,
        "hold_minutes": (exb - eb) * 2 / 60,
    }
    if fee not in (100, 500, 3000, 10000):
        out.update({"why": "bad_fee", "ret": None, "real_net": None})
        return out

    tok = K.quote(K.WETH_BASE, alt, amt, fee, eb)
    if not tok or tok <= 0:
        out.update({"why": "entry_quote_fail", "ret": None, "real_net": -1.0, "fatal": True})
        return out
    back = K.quote(alt, K.WETH_BASE, int(tok), fee, eb)
    fric = (back / amt - 1.0) if back else -1.0
    sell_q = K.quote(alt, K.WETH_BASE, int(tok), fee, exb)
    net = (sell_q / amt - 1.0) if sell_q else -1.0
    ev, _ = RG.realgas_sell_ok(alt, fee, int(tok), eb)
    xv, xout = RG.realgas_sell_ok(alt, fee, int(tok), exb)
    if xv == "ok":
        real_net = xout / amt - 1.0
    elif xv == "revert":
        real_net = -1.0
    else:
        real_net = net
    adj = float(real_net) - GAS / STAKE - HAIRCUT if real_net is not None else None
    out.update({
        "fric": fric,
        "qv2_net": net,
        "real_net": real_net,
        "ret": adj,
        "entry_sell_fork": ev,
        "exit_sell_fork": xv,
        "fatal": bool(adj is not None and adj <= -0.5),
        "why": "ok",
    })
    return out


def temporal_trends():
    """644 aggressive backtest + June supplement: fatal rate & pass count over time."""
    path_all, _ = K.load()
    priced = json.load(open(K.PCACHE))
    df_base = L.load_base_df()
    df_fork = L.run_lens(df_base, "fork")
    _, _, creators, _ = L._load_blacklists()
    gate = A.aggressive_gate(df_fork, creators)
    tr = A.build_trades(df_fork, path_all, priced, gate)
    taken = A.portfolio_sim(tr)
    # rebuild taken list with dates
    open_pos = []
    rows = []
    for t in tr:
        open_pos = [(xt, st) for xt, st in open_pos if xt > t["t0"]]
        if len(open_pos) >= A.MAX_CONC:
            continue
        if sum(st for _, st in open_pos) + A.STAKE > A.MAX_DEPLOYED:
            continue
        rows.append(t)
        hold_s = max((t["exb"] - t["eb"]) * A.BLOCK_S, A.BLOCK_S)
        open_pos.append((t["t0"] + timedelta(seconds=hold_s), A.STAKE))

    hist = pd.DataFrame(rows)
    if not len(hist):
        return pd.DataFrame(), pd.DataFrame()
    hist["month"] = hist.t0.dt.strftime("%Y-%m")
    hist["fatal"] = hist.ret <= -0.5

    monthly = hist.groupby("month").agg(
        n=("pool", "count"),
        fatal_rate=("fatal", "mean"),
        fatal_n=("fatal", "sum"),
        mean_ret=("ret", "mean"),
        total_pnl=("ret", lambda s: (s * STAKE).sum()),
    ).reset_index()

    # gate pass counts by month (not just taken)
    df_fork = df_fork.copy()
    df_fork["month"] = df_fork.t0.dt.strftime("%Y-%m")
    gate_m = df_fork[gate & df_fork.tradeable].groupby("month").size().reset_index(name="gate_pass_tradeable")

    return monthly, gate_m


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    df = load_june_rows()
    print(f"June aggressive-pass pools: {len(df)}")

    results = []
    for i, row in df.iterrows():
        pool = row.pool
        print(f"\n[{i+1}/{len(df)}] {pool[:12]}.. t0={row.t0}", flush=True)
        path_steps = fetch_path(pool)
        print(f"  tick path buckets: {len(path_steps)}", flush=True)

        t0 = time.time()
        lab = FR.label_pool(int(row.entry_block), row.alt, int(row.fee),
                            spend_eth=SPEND_ETH, refine=True, pool=pool,
                            offsets=FR.UNIVERSE_OFFSETS)
        print(f"  label_pool: {lab['label']} mech={lab.get('mechanism')} "
              f"flip={lab.get('flip_block')} ({time.time()-t0:.0f}s)", flush=True)

        pnl = fork_pnl(row, path_steps)
        rec = {
            "pool": pool,
            "token": row.alt,
            "t0": str(row.t0),
            "p_fatal": float(row.p_fatal),
            "entry_weth": float(row.entry_weth),
            "label_fork": lab["label"],
            "mechanism": lab.get("mechanism"),
            "flip_block": lab.get("flip_block"),
            "flip_offset": lab.get("flip_offset"),
            **pnl,
            "pnl_usd": pnl["ret"] * STAKE if pnl.get("ret") is not None else None,
        }
        results.append(rec)
        print(f"  TRAIL0.25 exb={pnl.get('exit_block')} ret={pnl.get('ret'):+.3f} "
              f"fatal={pnl.get('fatal')} fork_exit={pnl.get('exit_sell_fork')}", flush=True)

    june_df = pd.DataFrame(results)
    monthly, gate_m = temporal_trends()

    # June weekly gate pass from supplement
    supp_all = json.load(open(SUPP))["pools"]
    june_all = pd.DataFrame(supp_all)
    june_all["t0"] = pd.to_datetime(june_all["t0"])
    june_all["week"] = june_all.t0.dt.isocalendar().week.astype(int)
    weekly = june_all.groupby("week").agg(
        min_pre=("pool", "count"),
        aggressive_pass=("aggressive_live", "sum"),
    ).reset_index()

    report = {
        "meta": {
            "config": "aggressive live F-off TRAIL=0.25 $50 stake fork lens",
            "spend_eth": SPEND_ETH,
            "pnl_method": "QuoterV2 entry + realgas sell @ TRAIL exit - gas - haircut",
        },
        "june6_trades": results,
        "june6_summary": {
            "n": len(results),
            "fatal_n": int(june_df.fatal.sum()) if "fatal" in june_df else 0,
            "fatal_rate": float(june_df.fatal.mean()) if len(june_df) else 0,
            "total_pnl_usd": float(june_df.pnl_usd.sum()) if june_df.pnl_usd.notna().any() else 0,
            "delayed_rug_n": int((june_df.label_fork == "delayed_rug").sum()),
        },
        "june6_chronological_fatal_cumulative": [
            {
                "t0": r["t0"],
                "pool": r["pool"][:10],
                "ret": r.get("ret"),
                "fatal": r.get("fatal"),
                "label": r["label_fork"],
                "cum_fatal_rate": float(june_df.iloc[:i + 1].fatal.mean()),
            }
            for i, r in enumerate(results)
        ],
        "historical_monthly_portfolio": monthly.to_dict("records"),
        "historical_monthly_gate_pass": gate_m.to_dict("records"),
        "june_weekly_funnel": weekly.to_dict("records"),
    }

    out = os.path.join(OUT_DIR, "june6_fork_trail_backtest.json")
    json.dump(report, open(out, "w"), indent=2, default=str)
    print("\n=== JUNE 6 POOL SUMMARY ===")
    print(json.dumps(report["june6_summary"], indent=2))
    print("\n=== HISTORICAL MONTHLY (644 aggressive sim) ===")
    print(monthly.to_string())
    print(f"\nwrote {out}")
    return report


if __name__ == "__main__":
    main()
