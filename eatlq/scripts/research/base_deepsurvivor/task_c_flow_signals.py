#!/usr/bin/env python3
"""Task C: Flow / regime signals — individual tests vs baseline, TRAIN/TEST split."""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from common import (  # noqa: E402
    HERE,
    beat_baseline,
    ch_query,
    load_json,
    pnl_stats,
    rpc_ledger,
    save_json,
    time_split,
)

UNIVERSE = os.path.join(HERE, "_deep_universe.json")
BASELINE = os.path.join(HERE, "_deep_baseline.json")
PRICE_CACHE = os.path.join(HERE, "_baseline_pricing.json")
OUT = os.path.join(HERE, "_flow_signals.json")
CHECKPOINT = os.path.join(HERE, "_flow_checkpoint.json")


def compute_flow_features(rows: list) -> list:
    """Point-in-time flow features from CH pre-entry window."""
    if not rows:
        return rows
    vals = ",".join(
        f"('{r['pool']}',{r['entry_block']},{r['birth_block']})"
        for r in rows[:200]
    )
    sql = f"""
    WITH plist AS (
      SELECT * FROM values('pool String, entry_block UInt64, birth_block UInt64', {vals})
    ),
    sw AS (
      SELECT lower(s.pool_address) AS pool, p.entry_block,
             s.block_number AS bn,
             toFloat64(s.amount0) AS a0, toFloat64(s.amount1) AS a1,
             p.entry_block - p.birth_block AS age_span
      FROM eatlq.uniswap_v3_swaps_decoded s
      INNER JOIN plist p ON lower(s.pool_address)=p.pool
      WHERE s.chain_id=8453 AND s.block_number < p.entry_block
        AND s.block_number >= p.entry_block - 7200
    ),
    agg AS (
      SELECT pool, entry_block,
             countIf(bn >= entry_block - 1800) AS ns_h1,
             countIf(bn < entry_block - 1800) AS ns_h2,
             uniqExactIf(bn, bn >= entry_block - 3600) AS active_h1,
             uniqExactIf(bn, bn < entry_block - 3600 AND bn >= entry_block - 7200) AS active_h2,
             sumIf(abs(a0)+abs(a1), a0>0 OR a1>0) AS buy_vol,
             sumIf(abs(a0)+abs(a1), a0<0 OR a1<0) AS sell_vol,
             argMax(toFloat64(0), bn) AS liq_last
      FROM sw GROUP BY pool, entry_block
    )
    SELECT pool, entry_block, ns_h1, ns_h2, active_h1, active_h2, buy_vol, sell_vol
    FROM agg FORMAT TabSeparated
    """
    feat_map = {}
    for ln in ch_query(sql, timeout=120).splitlines():
        if not ln.strip():
            continue
        p = ln.split("\t")
        pool, eb = p[0], int(p[1])
        ns_h1, ns_h2 = float(p[2]), float(p[3])
        active_h1, active_h2 = float(p[4]), float(p[5])
        buy_vol, sell_vol = float(p[6]), float(p[7])
        net_accel = (ns_h1 - ns_h2) / max(ns_h2, 1)  # inflow acceleration
        holder_slope = (active_h1 - active_h2) / max(active_h2, 1)
        flow_imb = (buy_vol - sell_vol) / max(buy_vol + sell_vol, 1)
        feat_map[(pool, eb)] = {
            "net_inflow_accel": net_accel,
            "holder_growth_slope": holder_slope,
            "flow_imbalance": flow_imb,
            "ns_h1": ns_h1,
        }

    out = []
    for r in rows:
        key = (r["pool"], r["entry_block"])
        f = feat_map.get(key, {})
        out.append({**r, **f})
    return out


def signal_mask(rows: list, signal_name: str, train_rows: list) -> tuple[list, list]:
    """Binary signal: top quartile on TRAIN, frozen threshold applied to all."""
    train_vals = [r.get(signal_name) for r in train_rows if r.get(signal_name) is not None]
    if len(train_vals) < 20:
        return [], []
    thr = sorted(train_vals)[int(len(train_vals) * 0.75)]
    fired = [r for r in rows if (r.get(signal_name) or -999) >= thr]
    return fired, [{"name": signal_name, "threshold": thr, "direction": "top_quartile"}]


def eval_signal(name: str, train_fired: list, test_fired: list, cache: dict, bl: dict) -> dict:
    def pnls(fired):
        nets = []
        for r in fired:
            key = f"{r['pool']}:{r['entry_block']}"
            net = (cache.get(key, {}).get("holds") or {}).get("4h", {}).get("net")
            if net is not None:
                nets.append(net)
        return pnl_stats(nets)

    tr, te = pnls(train_fired), pnls(test_fired)
    bl_te = (bl.get("holds") or {}).get("4h", {}).get("test", {})
    return {
        "signal": name,
        "train": tr,
        "test": te,
        "baseline_test": bl_te,
        "beat_baseline_test": beat_baseline(te, bl_te),
        "train_test_gap": abs(tr.get("mean", 0) - te.get("mean", 0)) if tr.get("n") and te.get("n") else None,
    }


def main():
    print("#### Task C: Flow signals ####", flush=True)
    ckpt = load_json(CHECKPOINT, {})
    if ckpt.get("done") and os.path.exists(OUT):
        print("  checkpoint done")
        return

    uni = load_json(UNIVERSE)
    rows = compute_flow_features(uni.get("rows", []))
    train, test, cut = time_split(rows)
    cache = load_json(PRICE_CACHE, {})
    bl = load_json(BASELINE)

    signals = ["net_inflow_accel", "holder_growth_slope", "flow_imbalance"]
    results = []
    for sig in signals:
        tr_f, _ = signal_mask(rows, sig, train)
        te_f, meta = signal_mask(test, sig, train)  # threshold from train
        # Re-apply train threshold to test
        train_vals = [r.get(sig) for r in train if r.get(sig) is not None]
        if len(train_vals) < 20:
            continue
        thr = sorted(train_vals)[int(len(train_vals) * 0.75)]
        tr_f = [r for r in train if (r.get(sig) or -999) >= thr]
        te_f = [r for r in test if (r.get(sig) or -999) >= thr]
        ev = eval_signal(sig, tr_f, te_f, cache, bl)
        ev["threshold_train_q75"] = thr
        results.append(ev)
        print(f"  {sig}: TEST n={ev['test'].get('n')} mean={ev['test'].get('mean')} beat={ev['beat_baseline_test']}")

    out = {
        "meta": {
            "train_cut_ts": cut,
            "n_signals_tested": len(results),
            "method": "individual top-quartile on TRAIN, frozen threshold; no composite gates",
            "rpc": rpc_ledger(),
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "signals": results,
    }
    save_json(OUT, out)
    save_json(CHECKPOINT, {"done": True})
    print(f"  wrote {OUT}")


if __name__ == "__main__":
    main()
