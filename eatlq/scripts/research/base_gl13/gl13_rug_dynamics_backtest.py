#!/usr/bin/env python3
"""GL13 — full-universe rug dynamics backtest (644 labeled pools, CH-only, zero RPC).

Replays CURRENT live gate tags on the frozen 644 candidate universe and measures:
  1. Rug speed (t0 → first burn / labeled flip)
  2. WETH-in-pool trajectory (cliff vs gradual)
  3. Buy/sell asymmetry in pre-window W=600s
  4. Deployer == burn tx.from
  5. Post-buy E-window depth-collapse (1800s, 0.5 ETH) on rugs NOT F-blocked
  6. Deployer funding source (frozen GL12 artifact)
  7. Top-1 holder % at entry (CH erc20 transfers, PIT)

Does NOT touch live executor / Redis / RPC. Read-only ClickHouse + frozen JSON.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/opt/EATLQ")
LT = ROOT / "scripts/research/base_lefttail"
GL12 = ROOT / "scripts/research/base_gl12"
OUT = ROOT / "scripts/research/base_gl13"
OUT.mkdir(parents=True, exist_ok=True)

WETH = "0x4200000000000000000000000000000000000006".lower()
W_PRE = 600  # seconds — MIN_PRE / feature window
ENTRY_LO = W_PRE + 300  # 900s — bridge entry window upper bound (t0+600..900)
ENTRY_HI = W_PRE  # 600s — lower bound
FAST_RUG_BLOCKS = 300  # ~10 min on Base
E_WINDOW_SEC = 1800
E_COLLAPSE_ETH = 0.5
BASE_BLOCK_SEC = 2.0

# Live config mirrors live_config_rebacktest.py + current .env
MIN_PRE = 5
P_CUT = 0.27
MIN_DEPTH_ETH = 3.0
PARADAENGI_DEPLOYERS = {
    "0x715c98cedbd0c242e257986535bbf6e1fba764be",
    "0xc8512b5324402be9ab986d011c9adef8992f076f",
}


def ch_query(sql: str) -> list[tuple]:
    cmd = ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--format=JSONEachRow", "-q", sql]
    out = subprocess.check_output(cmd, text=True)
    rows = []
    for line in out.strip().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def ch_scalar(sql: str):
    rows = ch_query(sql)
    if not rows:
        return None
    return list(rows[0].values())[0]


def ch_query_batched(sql_template: str, pools: list[str], chunk: int = 80) -> list[dict]:
    rows: list[dict] = []
    for i in range(0, len(pools), chunk):
        batch = pools[i : i + chunk]
        in_list = ",".join(f"'{p}'" for p in batch)
        rows.extend(ch_query(sql_template.format(in_list=in_list)))
    return rows


def load_universe() -> pd.DataFrame:
    sys.path.insert(0, str(LT))
    import keystone_live as K  # noqa
    import live_config_rebacktest as L  # noqa

    df, _ = K.load()
    labels = L._load_labels()
    ewd = json.load(open(LT / "_entry_weth_644.json"))
    pools_gl12 = {p["pool"].lower(): p for p in json.load(open(GL12 / "_gl12_pools.json"))}
    token_skel = json.load(open(LT / "_token_skeleton_644.json"))
    deployers, skeletons, creators, _ = L._load_blacklists()

    df = df[df.pool.isin(labels)].copy()
    df["label"] = df.pool.map(lambda p: labels[p]["label"])
    df["mechanism"] = df.pool.map(lambda p: labels[p].get("mechanism"))
    df["flip_block"] = df.pool.map(lambda p: labels[p].get("flip_block"))
    df["flip_offset"] = df.pool.map(lambda p: labels[p].get("flip_offset"))
    df["token"] = df.pool.map(lambda p: labels[p]["token"].lower())
    df["entry_weth"] = df.pool.map(lambda p: (ewd.get(p) or 0) / 1e18)
    df["entry_block"] = df.pool.str.lower().map(lambda p: pools_gl12.get(p, {}).get("entry_block"))
    df["deployer"] = df.pool.str.lower().map(lambda p: (pools_gl12.get(p, {}) or {}).get("deployer") or "").str.lower()
    df["skeleton"] = df.token.map(lambda t: token_skel.get(t))
    df["f_deployer"] = df.deployer.isin(deployers)
    df["f_skeleton"] = df.skeleton.isin(skeletons)
    df["f_any"] = df["f_deployer"] | df["f_skeleton"]
    import risk_rebacktest as R  # noqa

    clf, X, _, _ = R.fit_model(df)
    df["p_fatal"] = clf.predict_proba(X)[:, 1]
    priced = json.load(open(LT / "_priced_live.json"))
    df["fric"] = df.pool.map(lambda p: (priced.get(p) or {}).get("fric"))
    df["tradeable"] = df.pool.map(lambda p: (priced.get(p) or {}).get("why") == "ok")
    df["gate_fric"] = df.fric.notna() & (df.fric >= -0.05)
    df["model_ok"] = (df.ns_pre >= MIN_PRE) & (df.p_fatal <= P_CUT)
    df["depth_ok"] = df.entry_weth >= MIN_DEPTH_ETH
    df["live_traded"] = df.model_ok & df.depth_ok & df.tradeable & df.gate_fric & ~df.f_any
    df["counterfactual_traded"] = df.model_ok & df.depth_ok & df.tradeable & df.gate_fric  # F gate off
    df["is_rug"] = df.label == "delayed_rug"
    return df


def fetch_pool_orient(pools: list[str]) -> dict[str, int]:
    in_list = ",".join(f"'{p}'" for p in pools)
    sql = f"""
    SELECT lower(pool_address) pool,
      if(lower(token0_address)='{WETH}', -1, 1) orient
    FROM eatlq.discovered_pools
    WHERE chain_id=8453 AND lower(pool_address) IN ({in_list})
    """
    rows = ch_query(sql)
    out = {}
    for r in rows:
        out[r["pool"]] = int(r["orient"])
    # fallback: infer from swaps — if missing, assume token0=WETH when amount0 dominant
    missing = [p for p in pools if p not in out]
    if missing:
        in2 = ",".join(f"'{p}'" for p in missing)
        sql2 = f"""
        SELECT lower(pool_address) pool,
          if(sum(abs(amount0)) > sum(abs(amount1)), -1, 1) orient
        FROM eatlq.uniswap_v3_swaps_decoded
        WHERE chain_id=8453 AND lower(pool_address) IN ({in2})
        GROUP BY pool
        """
        for r in ch_query(sql2):
            out[r["pool"]] = int(r["orient"])
    return out


def fetch_timeline(pools: list[str]) -> dict[str, dict]:
    sql_tpl = """
    WITH fs AS (
      SELECT lower(pool_address) AS pool,
        min(block_number) AS t0_block,
        toUnixTimestamp(min(block_time)) AS t0_unix
      FROM eatlq.uniswap_v3_swaps_decoded
      WHERE chain_id=8453 AND lower(pool_address) IN ({in_list}) AND block_time IS NOT NULL
      GROUP BY pool
    ),
    mints AS (
      SELECT lower(pool_address) AS pool,
        min(block_number) AS mint_block,
        argMin(toFloat64(amount0)/1e18, block_number) AS mint_a0,
        argMin(toFloat64(amount1)/1e18, block_number) AS mint_a1
      FROM eatlq.uniswap_v3_mints_decoded
      WHERE chain_id=8453 AND lower(pool_address) IN ({in_list})
      GROUP BY pool
    ),
    burns AS (
      SELECT lower(b.pool_address) AS pool,
        min(b.block_number) AS first_burn_block,
        any(t.tx_from) AS burn_tx_from
      FROM eatlq.uniswap_v3_burns_decoded b
      LEFT JOIN eatlq.tx_from_by_hash t ON lower(t.tx_hash)=lower(b.tx_hash)
      WHERE b.chain_id=8453 AND lower(b.pool_address) IN ({in_list})
      GROUP BY pool
    )
    SELECT fs.pool AS pool, fs.t0_block, fs.t0_unix,
      m.mint_block, m.mint_a0, m.mint_a1,
      b.first_burn_block, lower(b.burn_tx_from) AS burn_tx_from
    FROM fs
    LEFT JOIN mints m ON m.pool=fs.pool
    LEFT JOIN burns b ON b.pool=fs.pool
    """
    return {r["pool"]: r for r in ch_query_batched(sql_tpl, pools) if r.get("pool")}


def fetch_pre_buy_sell(pools: list[str], orient: dict[str, int]) -> dict[str, dict]:
    sql_tpl = """
    WITH fs AS (
      SELECT lower(pool_address) AS pool, toUnixTimestamp(min(block_time)) AS t0_unix
      FROM eatlq.uniswap_v3_swaps_decoded
      WHERE chain_id=8453 AND lower(pool_address) IN ({in_list}) AND block_time IS NOT NULL
      GROUP BY pool
    )
    SELECT lower(s.pool_address) AS pool,
      countIf(toUnixTimestamp(s.block_time)-fs.t0_unix BETWEEN 0 AND {w} AND s.amount0 > 0) AS a0_in,
      countIf(toUnixTimestamp(s.block_time)-fs.t0_unix BETWEEN 0 AND {w} AND s.amount0 < 0) AS a0_out,
      countIf(toUnixTimestamp(s.block_time)-fs.t0_unix BETWEEN 0 AND {w} AND s.amount1 > 0) AS a1_in,
      countIf(toUnixTimestamp(s.block_time)-fs.t0_unix BETWEEN 0 AND {w} AND s.amount1 < 0) AS a1_out
    FROM eatlq.uniswap_v3_swaps_decoded s
    INNER JOIN fs ON fs.pool=lower(s.pool_address)
    WHERE s.chain_id=8453 AND lower(s.pool_address) IN ({in_list})
    GROUP BY pool
    """.replace("{w}", str(W_PRE))
    out = {}
    for r in ch_query_batched(sql_tpl, pools):
        pool = r["pool"]
        o = orient.get(pool, -1)
        if o == -1:  # token0=WETH: amount0>0 = WETH into pool = buy token
            buys = int(r["a0_in"])
            sells = int(r["a0_out"])
        else:
            buys = int(r["a1_in"])
            sells = int(r["a1_out"])
        out[pool] = {"weth_buys": buys, "weth_sells": sells, "ratio_sell_buy": sells / max(buys, 1)}
    return out


def fetch_weth_snapshots(pools: list[str], orient: dict[str, int]) -> dict[str, list]:
    """Cumulative WETH-in-pool proxy at mint + swap/burn events (for cliff detection)."""
    sql_tpl = """
    SELECT pool, block_number, typ, d0, d1 FROM (
      SELECT lower(pool_address) AS pool, block_number, 'm' AS typ,
        toFloat64(amount0)/1e18 AS d0, toFloat64(amount1)/1e18 AS d1
      FROM eatlq.uniswap_v3_mints_decoded WHERE chain_id=8453 AND lower(pool_address) IN ({in_list})
      UNION ALL
      SELECT lower(pool_address), block_number, 'b',
        -toFloat64(amount0)/1e18, -toFloat64(amount1)/1e18
      FROM eatlq.uniswap_v3_burns_decoded WHERE chain_id=8453 AND lower(pool_address) IN ({in_list})
      UNION ALL
      SELECT lower(pool_address), block_number, 's',
        toFloat64(amount0)/1e18, toFloat64(amount1)/1e18
      FROM eatlq.uniswap_v3_swaps_decoded WHERE chain_id=8453 AND lower(pool_address) IN ({in_list})
    ) ORDER BY pool, block_number
    """
    rows = ch_query_batched(sql_tpl, pools, chunk=40)
    by_pool: dict[str, list] = defaultdict(list)
    for r in rows:
        by_pool[r["pool"]].append(r)

    out = {}
    for pool, evs in by_pool.items():
        o = orient.get(pool, -1)
        weth = 0.0
        series = []
        for e in evs:
            dw = e["d0"] if o == -1 else e["d1"]
            weth += dw
            series.append((int(e["block_number"]), weth))
        out[pool] = series
    return out


def weth_at_block(series: list, block: int) -> float:
    v = 0.0
    for b, w in series:
        if b <= block:
            v = w
        else:
            break
    return max(v, 0.0)


def fetch_top1_holder(tokens: list[str], entry_blocks: dict[str, str]) -> dict[str, float]:
    """Top-1 non-pool holder % of supply at entry block (batched, rugs + F-blocked only)."""
    if not tokens:
        return {}
    in_list = ",".join(f"'{t}'" for t in tokens)
    # aggregate transfers up to max entry — filter in Python per token
    max_entry = max(int(v) for v in entry_blocks.values() if v)
    sql = f"""
    SELECT lower(token_address) token, lower(sender) from_a, lower(recipient) to_a,
      toFloat64(amount)/1e18 val, block_number
    FROM eatlq.erc20_transfers_decoded
    WHERE chain_id=8453 AND lower(token_address) IN ({in_list})
      AND block_number <= {max_entry}
    """
    try:
        rows = ch_query(sql)
    except Exception:
        return {}
    balances: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        tok = r["token"]
        eb = int(entry_blocks.get(tok, max_entry))
        if int(r["block_number"]) > eb:
            continue
        fa, ta, v = r["from_a"], r["to_a"], float(r["val"])
        if fa != "0x0000000000000000000000000000000000000000":
            balances[tok][fa] -= v
        if ta != "0x0000000000000000000000000000000000000000":
            balances[tok][ta] += v
    out = {}
    for tok, bal in balances.items():
        total = sum(x for x in bal.values() if x > 0)
        if total <= 0:
            continue
        top1 = max(bal.values())
        out[tok] = top1 / total if top1 > 0 else 0.0
    return out


def load_funding() -> dict[str, dict]:
    p = GL12 / "_gl12_funding_raw.json"
    if not p.exists():
        return {}
    raw = json.load(open(p))
    if isinstance(raw, list):
        return {r["deployer"].lower(): r for r in raw if r.get("deployer")}
    return {k.lower(): v for k, v in raw.items()}


def _median_int(vals):
    v = [x for x in vals if x is not None]
    return int(np.median(v)) if v else None


def _median_float(vals, scale=1.0):
    v = [x for x in vals if x is not None]
    return round(float(np.median(v)) * scale, 1) if v else None
    if not skel:
        return "unknown"
    return skel[:18]


def main():
    print("GL13 rug dynamics backtest — CH-only, no RPC")
    t0 = datetime.now(timezone.utc)
    df = load_universe()
    pools = df.pool.str.lower().tolist()
    print(f"universe n={len(df)} rugs={df.is_rug.sum()} live_traded={df.live_traded.sum()} f_blocked={df.f_any.sum()}")

    orient = fetch_pool_orient(pools)
    timeline = fetch_timeline(pools)
    buy_sell = fetch_pre_buy_sell(pools, orient)
    entry_blocks = {r.pool: int(r.entry_block) for r in df.itertuples() if r.entry_block}
    weth_series = fetch_weth_snapshots(pools, orient)
    funding = load_funding()
    conc = {}
    for fn in ("_gl12_concentration.json", "_gl12_concentration2.json"):
        p = GL12 / fn
        if p.exists():
            for row in json.load(open(p)):
                conc[row.get("token", "").lower()] = row

    # holder concentration: rugs + f_blocked tokens only (save CH work)
    holder_tokens = df[df.is_rug | df.f_any].token.unique().tolist()
    tok_entry = {r.token: str(int(r.entry_block)) for r in df.itertuples() if r.token in holder_tokens and r.entry_block}
    top1 = fetch_top1_holder(list(tok_entry.keys()), tok_entry)

    per_pool = []
    for row in df.itertuples():
        pool = row.pool.lower()
        tl = timeline.get(pool, {})
        bs = buy_sell.get(pool, {})
        o = orient.get(pool, -1)
        mint_weth = float(tl.get("mint_a0" if o == -1 else "mint_a1") or 0)
        t0_block = int(tl.get("t0_block") or 0)
        t0_unix = int(tl.get("t0_unix") or 0)
        burn_block = int(tl.get("first_burn_block") or 0)
        flip_raw = row.flip_block
        flip = int(flip_raw) if flip_raw is not None and not (isinstance(flip_raw, float) and np.isnan(flip_raw)) else burn_block
        rug_anchor = burn_block or flip  # labeled flip when CH burn sparse
        entry_blk_raw = row.entry_block
        entry_blk = int(entry_blk_raw) if entry_blk_raw is not None and not (isinstance(entry_blk_raw, float) and np.isnan(entry_blk_raw)) else int(getattr(row, "entry_block", 0) or 0)
        if not entry_blk and t0_block:
            # fallback: bridge entry ≈ t0 + 600s
            entry_blk = t0_block + int(W_PRE / BASE_BLOCK_SEC)
        series = weth_series.get(pool, [])

        rug_speed_blocks = (rug_anchor - t0_block) if rug_anchor and t0_block else None
        if rug_speed_blocks is None and row.flip_offset is not None and not (isinstance(row.flip_offset, float) and np.isnan(row.flip_offset)):
            rug_speed_blocks = int(row.flip_offset)
        rug_speed_sec = rug_speed_blocks * BASE_BLOCK_SEC if rug_speed_blocks is not None else None
        signal_to_rug_blocks = (flip - entry_blk) if flip and entry_blk else None
        signal_to_rug_sec = signal_to_rug_blocks * BASE_BLOCK_SEC if signal_to_rug_blocks else None

        weth_entry = weth_at_block(series, entry_blk) if entry_blk else row.entry_weth
        weth_flip = weth_at_block(series, flip) if flip else 0.0
        weth_pre50 = weth_at_block(series, max(flip - 50, 0)) if flip else 0.0
        cliff = (weth_pre50 > 0.5 and weth_flip < 0.05) if flip else None

        # E-window: within entry+900 blocks (~1800s), did WETH drop below 0.5?
        e_escape = None
        e_trigger_block = None
        if entry_blk and row.is_rug and (row.live_traded or row.counterfactual_traded):
            end_blk = entry_blk + int(E_WINDOW_SEC / BASE_BLOCK_SEC)
            for b, w in series:
                if entry_blk < b <= end_blk and w < E_COLLAPSE_ETH:
                    e_escape = True
                    e_trigger_block = b
                    break
            if e_escape is None:
                e_escape = False

        dep = (row.deployer or "").lower()
        burn_from = (tl.get("burn_tx_from") or "").lower()
        fund = funding.get(dep, {})

        rec = {
            "pool": pool,
            "token": row.token,
            "label": row.label,
            "is_rug": bool(row.is_rug),
            "mechanism": row.mechanism,
            "skeleton": row.skeleton,
            "deployer": dep,
            "p_fatal": round(float(row.p_fatal), 4),
            "ns_pre": int(row.ns_pre),
            "entry_weth": round(float(row.entry_weth), 4),
            "f_skeleton": bool(row.f_skeleton),
            "f_deployer": bool(row.f_deployer),
            "f_any": bool(row.f_any),
            "live_traded": bool(row.live_traded),
            "counterfactual_traded": bool(row.counterfactual_traded),
            "t0_block": t0_block,
            "entry_block": entry_blk,
            "flip_block": flip,
            "burn_block": burn_block,
            "rug_speed_blocks": rug_speed_blocks,
            "rug_speed_sec": rug_speed_sec,
            "rug_speed_class": (
                "fast" if rug_speed_blocks is not None and rug_speed_blocks <= FAST_RUG_BLOCKS
                else ("slow" if rug_speed_blocks is not None else "unknown")
            ),
            "signal_to_rug_blocks": signal_to_rug_blocks,
            "signal_to_rug_sec": signal_to_rug_sec,
            "mint_weth_eth": round(mint_weth, 4),
            "weth_buys_pre": bs.get("weth_buys"),
            "weth_sells_pre": bs.get("weth_sells"),
            "sell_buy_ratio_pre": round(bs.get("ratio_sell_buy", 0), 3),
            "deployer_eq_burn_from": dep == burn_from if dep and burn_from else None,
            "burn_tx_from": burn_from,
            "weth_at_entry_proxy": round(weth_entry, 4),
            "weth_at_flip_proxy": round(weth_flip, 4),
            "liquidity_cliff": cliff,
            "e_window_would_trigger": e_escape,
            "e_trigger_block": e_trigger_block,
            "funder_first": fund.get("first_funder"),
            "funder_inbound_n": fund.get("n_inbound"),
            "top1_holder_pct": round(top1.get(row.token, 0), 4) if row.token in top1 else None,
            "top1_circ_pct": conc.get(row.token, {}).get("top1_circ"),
            "pool_share_at_entry": conc.get(row.token, {}).get("pool_share"),
        }
        per_pool.append(rec)

    # skeleton summaries
    skel_groups = defaultdict(list)
    for r in per_pool:
        if r["is_rug"]:
            skel_groups[r["skeleton"] or "unknown"].append(r)

    skel_summary = []
    for skel, items in sorted(skel_groups.items(), key=lambda x: -len(x[1])):
        speeds = [i["rug_speed_blocks"] for i in items if i["rug_speed_blocks"] is not None]
        skel_summary.append({
            "skeleton": skel,
            "n_rug": len(items),
            "n_f_blocked": sum(1 for i in items if i["f_any"]),
            "fast_pct": sum(1 for i in items if i["rug_speed_class"] == "fast") / len(items),
            "slow_pct": sum(1 for i in items if i["rug_speed_class"] == "slow") / len(items),
            "median_rug_speed_blocks": int(np.median(speeds)) if speeds else None,
            "median_rug_speed_min": round(float(np.median(speeds)) * BASE_BLOCK_SEC / 60, 1) if speeds else None,
            "deployer_eq_burn_pct": sum(1 for i in items if i["deployer_eq_burn_from"]) / max(
                sum(1 for i in items if i["deployer_eq_burn_from"] is not None), 1
            ),
            "cliff_pct": sum(1 for i in items if i["liquidity_cliff"]) / len(items),
            "median_sell_buy_ratio": round(float(np.median([i["sell_buy_ratio_pre"] for i in items])), 2),
        })

    # gate-level summaries
    rugs = [r for r in per_pool if r["is_rug"]]
    live_rugs = [r for r in rugs if r["live_traded"]]
    cf_rugs = [r for r in rugs if r["counterfactual_traded"] and not r["live_traded"]]
    f_blocked_rugs = [r for r in rugs if r["f_any"]]

    summary = {
        "generated_at": t0.isoformat(),
        "method": "ClickHouse-only, frozen 644 universe, live gate tags offline",
        "live_config": {
            "MIN_PRE": MIN_PRE, "P_CUT": P_CUT, "MIN_DEPTH_ETH": MIN_DEPTH_ETH,
            "F_skeleton_n": 33, "F_deployer_n": 245,
            "E_WINDOW_SEC": E_WINDOW_SEC, "E_COLLAPSE_ETH": E_COLLAPSE_ETH,
            "FAST_RUG_BLOCKS": FAST_RUG_BLOCKS,
        },
        "universe": {
            "n": len(per_pool),
            "n_rug": len(rugs),
            "n_live_traded": int(df.live_traded.sum()),
            "n_f_blocked": int(df.f_any.sum()),
            "n_f_blocked_rug": len(f_blocked_rugs),
            "n_live_traded_rug": len(live_rugs),
            "n_counterfactual_rug_only_f_blocked": len(cf_rugs),
        },
        "rug_speed_all_rugs": {
            "fast_n": sum(1 for r in rugs if r["rug_speed_class"] == "fast"),
            "slow_n": sum(1 for r in rugs if r["rug_speed_class"] == "slow"),
            "unknown_n": sum(1 for r in rugs if r["rug_speed_class"] == "unknown"),
            "median_blocks": _median_int([r["rug_speed_blocks"] for r in rugs]),
            "median_minutes": _median_float([r["rug_speed_blocks"] for r in rugs], BASE_BLOCK_SEC / 60),
        },
        "rug_speed_f_blocked_rugs": {
            "fast_n": sum(1 for r in f_blocked_rugs if r["rug_speed_class"] == "fast"),
            "slow_n": sum(1 for r in f_blocked_rugs if r["rug_speed_class"] == "slow"),
            "median_minutes": _median_float([r["rug_speed_blocks"] for r in f_blocked_rugs], BASE_BLOCK_SEC / 60),
        },
        "signal_to_rug_live_traded_rugs": {
            "n": len(live_rugs),
            "median_sec": round(float(np.median([r["signal_to_rug_sec"] for r in live_rugs if r["signal_to_rug_sec"]])), 0)
            if live_rugs else None,
        },
        "deployer_eq_burn_all_rugs_pct": sum(1 for r in rugs if r["deployer_eq_burn_from"]) / max(
            sum(1 for r in rugs if r["deployer_eq_burn_from"] is not None), 1
        ),
        "liquidity_cliff_all_rugs_pct": sum(1 for r in rugs if r["liquidity_cliff"]) / max(len(rugs), 1),
        "e_window_on_counterfactual_rugs": {
            "would_trigger_n": sum(1 for r in cf_rugs if r["e_window_would_trigger"]),
            "total": len(cf_rugs),
            "pct": sum(1 for r in cf_rugs if r["e_window_would_trigger"]) / max(len(cf_rugs), 1),
        },
        "sell_buy_ratio_pre_rugs_median": round(float(np.median([r["sell_buy_ratio_pre"] for r in rugs])), 2),
        "sell_buy_ratio_pre_clean_median": round(float(np.median([r["sell_buy_ratio_pre"] for r in per_pool if not r["is_rug"]])), 2),
        "rpc_calls": 0,
    }

    # CUBEMAN skeleton reference (live blocked case — not in 644, document separately)
    cubeman_skel = "0xf725c23838e9d38e0e9377d691d1b5258643d04e7ff1e51a2ec5deb22e31f3c5"
    summary["cubeman_skeleton_in_644"] = {
        "skeleton": cubeman_skel,
        "n_rug_in_universe": sum(1 for r in per_pool if r["skeleton"] == cubeman_skel and r["is_rug"]),
        "note": "Live CUBEMAN blocked 2026-07-17; 3 historical rugs share this skeleton in 644",
    }

    json.dump(per_pool, open(OUT / "_gl13_per_pool.json", "w"), indent=1)
    json.dump(skel_summary, open(OUT / "_gl13_skeleton_summary.json", "w"), indent=1)
    json.dump(summary, open(OUT / "_gl13_summary.json", "w"), indent=1)

    # markdown report
    lines = [
        "# GL13 Rug Dynamics Backtest (644 universe, CH-only)",
        "",
        f"Generated: {summary['generated_at']}",
        "",
        "## 方法",
        "- 冻结 644 labeled universe + 当前实盘 gate 标签（离线回放，**零 RPC，不影响实盘**）",
        "- 链上事件来自 ClickHouse：`swaps_decoded` / `mints_decoded` / `burns_decoded` / `tx_from_by_hash`",
        "",
        "## 实盘 gate 覆盖",
        f"- 会通过 live_full（模型+深度+非F）: **{summary['universe']['n_live_traded']}** 池",
        f"- F闸拦截（骨架或部署者）: **{summary['universe']['n_f_blocked']}** 池",
        f"- 其中 rug 被 F 拦: **{summary['universe']['n_f_blocked_rug']}** / {summary['universe']['n_rug']} rugs",
        f"- 若无 F 闸会买到的 rug（反事实）: **{summary['universe']['n_counterfactual_rug_only_f_blocked']}**",
        "",
        "## 1. Rug 速度（首 Swap → 首次 Burn）",
        f"- 全部 rug: fast(≤{FAST_RUG_BLOCKS}blk) **{summary['rug_speed_all_rugs']['fast_n']}**, slow **{summary['rug_speed_all_rugs']['slow_n']}**",
        f"- 中位速度: **{summary['rug_speed_all_rugs']['median_minutes']} 分钟**",
        f"- 被 F 拦的 rug: fast **{summary['rug_speed_f_blocked_rugs']['fast_n']}**, slow **{summary['rug_speed_f_blocked_rugs']['slow_n']}**, 中位 **{summary['rug_speed_f_blocked_rugs']['median_minutes']} min**",
        "",
        "## 2. 信号 → Rug（entry_block → flip_block，仅 live_traded rugs）",
        f"- n={summary['signal_to_rug_live_traded_rugs']['n']}, 中位 **{summary['signal_to_rug_live_traded_rugs']['median_sec']}s**",
        "",
        "## 3. 买卖不对称（W=600s 预窗口，WETH 进/出池笔数比）",
        f"- rug 中位 sell/buy: **{summary['sell_buy_ratio_pre_rugs_median']}**",
        f"- clean 中位 sell/buy: **{summary['sell_buy_ratio_pre_clean_median']}**",
        "",
        "## 4. 部署者 = Burn 发起者",
        f"- 全部 rug: **{100*summary['deployer_eq_burn_all_rugs_pct']:.1f}%**",
        "",
        "## 5. 流动性 cliff（flip 前 50blk 有深度 → flip 时 ≈0）",
        f"- 全部 rug: **{100*summary['liquidity_cliff_all_rugs_pct']:.1f}%**",
        "",
        "## 6. E 窗深度坍塌（1800s / 0.5 ETH，反事实：F 闸关闭时会被买到的 rug）",
        f"- 会触发: **{summary['e_window_on_counterfactual_rugs']['would_trigger_n']}** / {summary['e_window_on_counterfactual_rugs']['total']} ({100*summary['e_window_on_counterfactual_rugs']['pct']:.1f}%)",
        f"- 当前实盘 live_traded rug 数: **{summary['universe']['n_live_traded_rug']}**（F 闸全开时 rug 进不去）",
        "",
        "## 7. 同骨架 Top clusters（rug 数 ≥2）",
        "",
        "| skeleton | n_rug | F拦 | fast% | 中位(min) | deployer=burn | cliff% | sell/buy |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for s in sorted(skel_summary, key=lambda x: -x["n_rug"])[:15]:
        if s["n_rug"] < 2:
            continue
        lines.append(
            f"| `{s['skeleton'][:14]}…` | {s['n_rug']} | {s['n_f_blocked']} | "
            f"{100*s['fast_pct']:.0f}% | {s['median_rug_speed_min']} | "
            f"{100*s['deployer_eq_burn_pct']:.0f}% | {100*s['cliff_pct']:.0f}% | {s['median_sell_buy_ratio']} |"
        )
    lines += [
        "",
        "## CUBEMAN（实盘案例，不在 644 内）",
        f"- 同骨架在 644 中 rug 数: {summary['cubeman_skeleton_in_644']['n_rug_in_universe']}（链上 live 案例为慢 rug ~26min）",
        "",
        "Artifacts: `scripts/research/base_gl13/_gl13_*.json`",
    ]
    (OUT / "GL13_rug_dynamics_report.md").write_text("\n".join(lines) + "\n")

    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    summary["elapsed_sec"] = elapsed
    json.dump(summary, open(OUT / "_gl13_summary.json", "w"), indent=1)
    print(json.dumps(summary, indent=2))
    print(f"Done in {elapsed:.1f}s — artifacts in {OUT}")


if __name__ == "__main__":
    main()
