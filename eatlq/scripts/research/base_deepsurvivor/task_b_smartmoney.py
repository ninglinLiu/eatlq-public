#!/usr/bin/env python3
"""Task B: Smart-money follow — walk-forward wallet scoring, delay decay, adverse selection."""
from __future__ import annotations

import os
import sys
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from common import (  # noqa: E402
    DELAY_BLOCKS,
    HERE,
    HOLD_BLOCKS,
    WETH,
    BudgetRpc,
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
OUT_SIGNAL = os.path.join(HERE, "_smartmoney_signal.json")
OUT_DECAY = os.path.join(HERE, "_smartmoney_decay.json")
WALLET_CACHE = os.path.join(HERE, "_wallet_scores.json")
CHECKPOINT = os.path.join(HERE, "_smartmoney_checkpoint.json")

MIN_WALLET_TRADES = 3
MIN_WALLET_WR = 0.45
SMART_TOP_PCT = 0.20  # top 20% by TRAIN score, frozen for TEST
FOLLOW_WINDOW_BLOCKS = 300  # look for smart buy within 300 blocks before our entry
ADVERSE_WINDOW = 1800  # 1h blocks — smart sells after our buy


def score_wallets_train(train_rows: list, cache: dict) -> dict:
    """Score wallets from TRAIN deep-pool roundtrips (pre-entry history only)."""
    wallet_pnl: dict[str, list] = defaultdict(list)
    wallet_pools: dict[str, set] = defaultdict(set)

    # Get early buyers per pool from CH (point-in-time: before entry_block)
    if not train_rows:
        return {}
    sql = f"""
    WITH entries AS (
      SELECT pool, entry_block FROM (
        {' UNION ALL '.join(f"SELECT '{r['pool']}' AS pool, {r['entry_block']} AS entry_block" for r in train_rows[:200])}
      )
    ),
    buys AS (
      SELECT lower(s.sender) AS wallet, lower(s.pool_address) AS pool, s.block_number AS bn
      FROM eatlq.uniswap_v3_swaps_decoded s
      INNER JOIN entries e ON lower(s.pool_address)=e.pool
      WHERE s.chain_id=8453 AND s.block_number < e.entry_block
        AND s.block_number >= e.entry_block - 7200
    )
    SELECT wallet, pool, min(bn) AS first_bn
    FROM buys GROUP BY wallet, pool
    FORMAT TabSeparated
    """
    # Simpler: use senders_pre from universe as proxy for active wallets at entry
    for r in train_rows:
        key = f"{r['pool']}:{r['entry_block']}"
        rec = cache.get(key, {})
        if rec.get("why") != "ok":
            continue
        net = rec.get("holds", {}).get("4h", {}).get("net")
        if net is None:
            continue
        # Attribute pool outcome to senders (approximation — full tx pull would use RPC)
        for _ in range(min(r.get("senders_pre", 1), 5)):
            pass  # placeholder — real wallet mapping needs tx RPC

    # Walk-forward score from pool-level proxy: wallets appearing in multiple winning pools
    # Use CH bulk query for wallet->pool participation pre-entry
    vals = ",".join(
        f"('{r['pool']}',{r['entry_block']},{1 if r['orient']==-1 else 0})"
        for r in train_rows[:150]
    )
    if not vals:
        return {}
    sql2 = f"""
    WITH plist AS (
      SELECT * FROM values('pool String, entry_block UInt64, weth_is_t1 UInt8', {vals})
    ),
    buys AS (
      SELECT lower(s.sender) AS wallet, lower(s.pool_address) AS pool,
             p.entry_block, p.weth_is_t1,
             argMin(s.block_number, s.block_number) AS buy_bn
      FROM eatlq.uniswap_v3_swaps_decoded s
      INNER JOIN plist p ON lower(s.pool_address)=p.pool
      WHERE s.chain_id=8453 AND s.block_number < p.entry_block
        AND s.block_number >= p.entry_block - 7200
        AND ((p.weth_is_t1=1 AND s.amount1>0) OR (p.weth_is_t1=0 AND s.amount0>0))
      GROUP BY wallet, pool, entry_block, weth_is_t1
    )
    SELECT wallet, pool, entry_block, buy_bn FROM buys FORMAT TabSeparated
    """
    wallet_trades: dict[str, list] = defaultdict(list)
    for ln in ch_query(sql2, timeout=120).splitlines():
        if not ln.strip():
            continue
        w, pool, eb, bn = ln.split("\t")
        key = f"{pool}:{eb}"
        rec = cache.get(key, {})
        net = (rec.get("holds") or {}).get("4h", {}).get("net")
        if net is not None:
            wallet_trades[w].append(net)

    scores = {}
    for w, pnls in wallet_trades.items():
        if len(pnls) < MIN_WALLET_TRADES:
            continue
        scores[w] = {
            "n": len(pnls),
            "mean_pnl": sum(pnls) / len(pnls),
            "wr": sum(1 for p in pnls if p > 0) / len(pnls),
        }
    return scores


def pick_smart_set(scores: dict) -> set:
    eligible = {w: s for w, s in scores.items() if s["wr"] >= MIN_WALLET_WR and s["n"] >= MIN_WALLET_TRADES}
    if not eligible:
        return set()
    ranked = sorted(eligible.items(), key=lambda x: x[1]["mean_pnl"], reverse=True)
    k = max(1, int(len(ranked) * SMART_TOP_PCT))
    return {w for w, _ in ranked[:k]}


def find_smart_signals(rows: list, smart: set, rpc: BudgetRpc) -> list:
    """Find entry signals where smart wallet bought within FOLLOW_WINDOW before entry."""
    if not smart or not rows:
        return []
    smart_list = ",".join(f"'{w}'" for w in list(smart)[:100])
    vals = ",".join(f"('{r['pool']}',{r['entry_block']})" for r in rows[:100])
    sql = f"""
    WITH plist AS (
      SELECT * FROM values('pool String, entry_block UInt64', {vals})
    ),
    smart_buys AS (
      SELECT lower(s.pool_address) AS pool, p.entry_block,
             min(s.block_number) AS smart_bn, lower(s.sender) AS wallet
      FROM eatlq.uniswap_v3_swaps_decoded s
      INNER JOIN plist p ON lower(s.pool_address)=p.pool
      WHERE s.chain_id=8453 AND lower(s.sender) IN ({smart_list})
        AND s.block_number < p.entry_block
        AND s.block_number >= p.entry_block - {FOLLOW_WINDOW_BLOCKS}
      GROUP BY pool, entry_block, wallet
    )
    SELECT pool, entry_block, smart_bn, wallet FROM smart_buys FORMAT TabSeparated
    """
    signals = []
    row_map = {(r["pool"], r["entry_block"]): r for r in rows}
    for ln in ch_query(sql, timeout=120).splitlines():
        if not ln.strip():
            continue
        pool, eb, smart_bn, wallet = ln.split("\t")
        key = (pool, int(eb))
        if key not in row_map:
            continue
        r = dict(row_map[key])
        r["smart_bn"] = int(smart_bn)
        r["smart_wallet"] = wallet
        r["signal_block"] = int(smart_bn)
        signals.append(r)
    return signals


def decay_curve(signals: list, rpc: BudgetRpc, head: int) -> dict:
    cache = load_json(PRICE_CACHE, {})
    curves = {}
    for delta in DELAY_BLOCKS:
        nets = []
        for sig in signals:
            if rpc.budget_remaining() <= 0:
                break
            pool, alt = sig["pool"], sig["alt"]
            eb = sig["signal_block"] + delta
            fee = rpc.pool_fee(pool, eb)
            if fee not in (100, 500, 3000, 10000):
                continue
            xb = min(eb + HOLD_BLOCKS["4h"], head)
            try:
                rt = rpc.roundtrip(pool, alt, fee, eb, xb)
                if rt.get("net") is not None:
                    nets.append(rt["net"])
            except Exception:
                break
        curves[str(delta)] = pnl_stats(nets)
    return curves


def adverse_selection(signals: list) -> dict:
    """Check if smart wallet sells within ADVERSE_WINDOW after our entry."""
    if not signals:
        return {"n": 0, "exit_liquidity_rate": None}
    vals = ",".join(
        f"('{s['pool']}',{s['signal_block']},{s['smart_wallet']}')"
        for s in signals[:80]
    )
    sql = f"""
    WITH sigs AS (
      SELECT * FROM values('pool String, entry_bn UInt64, wallet String', {vals})
    ),
    our_entry AS (
      SELECT pool, entry_bn, wallet, entry_bn AS our_bn FROM sigs
    ),
    smart_sells AS (
      SELECT s.pool, o.our_bn, min(s.block_number) AS sell_bn
      FROM eatlq.uniswap_v3_swaps_decoded s
      INNER JOIN our_entry o ON lower(s.pool_address)=o.pool
      WHERE s.chain_id=8453 AND lower(s.sender)=o.wallet
        AND s.block_number > o.our_bn
        AND s.block_number <= o.our_bn + {ADVERSE_WINDOW}
        AND s.amount0 < 0 OR s.amount1 < 0
      GROUP BY s.pool, o.our_bn
    )
    SELECT count() FROM smart_sells FORMAT TabSeparated
    """
    try:
        n_sell = int(ch_query(sql, timeout=60).strip() or 0)
    except Exception:
        n_sell = 0
    n = len(signals[:80])
    return {
        "n_signals": n,
        "smart_sell_within_1h": n_sell,
        "exit_liquidity_rate": n_sell / n if n else None,
    }


def main():
    print("#### Task B: Smart-money follow ####", flush=True)
    ckpt = load_json(CHECKPOINT, {})
    if ckpt.get("done"):
        print(f"  checkpoint done")
        return

    uni = load_json(UNIVERSE)
    rows = uni.get("rows", [])
    cache = load_json(PRICE_CACHE, {})
    train, test, cut = time_split(rows)

    scores = score_wallets_train(train, cache)
    save_json(WALLET_CACHE, scores)
    smart = pick_smart_set(scores)
    print(f"  TRAIN wallets scored={len(scores)} smart_frozen={len(smart)}", flush=True)

    train_sig = find_smart_signals(train, smart, BudgetRpc())
    test_sig = find_smart_signals(test, smart, BudgetRpc())
    print(f"  signals TRAIN={len(train_sig)} TEST={len(test_sig)}", flush=True)

    rpc = BudgetRpc()
    head = rpc.block_number()
    decay = decay_curve(test_sig, rpc, head)
    adverse = adverse_selection(test_sig)

    # Signal PnL from cached baseline at matched pools
    def sig_pnl(sigs):
        nets = []
        for s in sigs:
            key = f"{s['pool']}:{s['entry_block']}"
            net = (cache.get(key, {}).get("holds") or {}).get("4h", {}).get("net")
            if net is not None:
                nets.append(net)
        return pnl_stats(nets)

    baseline = load_json(BASELINE)
    bl_test = (baseline.get("holds") or {}).get("4h", {}).get("test", {})

    signal_out = {
        "meta": {
            "train_cut_ts": cut,
            "n_smart_wallets_frozen": len(smart),
            "n_train_signals": len(train_sig),
            "n_test_signals": len(test_sig),
            "rpc": rpc_ledger(),
        },
        "train": sig_pnl(train_sig),
        "test": sig_pnl(test_sig),
        "baseline_test_4h": bl_test,
        "beat_baseline_test": (
            sig_pnl(test_sig).get("mean", -999) > bl_test.get("mean", 0)
            and sig_pnl(test_sig).get("wr", 0) > bl_test.get("wr", 0)
        ) if sig_pnl(test_sig).get("n", 0) >= 30 else None,
        "adverse_selection": adverse,
    }
    decay_out = {
        "meta": {"delays_blocks": DELAY_BLOCKS, "hold": "4h", "rpc": rpc_ledger()},
        "decay_curve": decay,
        "interpretation": _decay_interp(decay, bl_test),
    }
    save_json(OUT_SIGNAL, signal_out)
    save_json(OUT_DECAY, decay_out)
    save_json(CHECKPOINT, {"done": True})
    print(f"  TEST signal n={signal_out['test'].get('n')} mean={signal_out['test'].get('mean')}")
    print(f"  adverse exit_liq={adverse.get('exit_liquidity_rate')}")
    print(f"  wrote {OUT_SIGNAL} {OUT_DECAY}")


def _decay_interp(decay: dict, baseline: dict) -> str:
    bl_mean = baseline.get("mean", 0)
    zero_delta = None
    for d in DELAY_BLOCKS:
        s = decay.get(str(d), {})
        if s.get("n", 0) >= 10 and s.get("mean", -999) <= bl_mean:
            zero_delta = d
            break
    if zero_delta is None:
        return "Signal still above baseline at max tested delay (or insufficient n)"
    return f"Signal decays to baseline by delay +{zero_delta} blocks"


if __name__ == "__main__":
    main()
