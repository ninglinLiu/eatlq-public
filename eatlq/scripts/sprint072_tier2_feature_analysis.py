"""
sprint072_tier2_feature_analysis.py
Implements A6 (buyer diversity), A7 (same-block toxic flow proxy),
and A8 (smart money 0x6ff5 gate) on the 530-row paper shadow CSV.

FINDINGS PRE-RUN:
- sender_behavior_labels_v1: EMPTY (no chain_id=8453 data)
- sender_pnl_summary_v1: EMPTY / OOM — unusable
- 0x6ff5 is #1 sender in 0xd0b53 (788k swaps) AND present in all paper shadow pools
  A7 PIVOT: same-block sender diversity (batch CH query)
  A8 PIVOT: 0x6ff5 presence within ±300 blocks of entry

RESEARCH ONLY. No production changes.
"""

import csv, json, math, pathlib, statistics, subprocess
from collections import defaultdict

REPO    = pathlib.Path("/opt/EATLQ")
SRC_CSV = REPO / "logs" / "sprint055_paper_shadow.csv"
OUT_DIR = REPO / "mvp" / "audits" / "sprint072_alpha_waterfall"
OUT_DIR.mkdir(parents=True, exist_ok=True)

CHAIN_ID    = 8453
SENDER_6FF5 = "0x6ff5693b99212da76ad316178a184ab56d299b43"
MEM_CAP     = "max_memory_usage = 3000000000"

def log(msg): print(f"[tier2] {msg}", flush=True)

def ch(sql, timeout=45):
    r = subprocess.run(
        ["docker","exec","eatlq-clickhouse-1","clickhouse-client",
         "--database","eatlq","--format","TSV","--query", sql],
        capture_output=True, text=True, timeout=timeout
    )
    return r.stdout.strip(), r.stderr.strip()[:200]

def load_paper_shadow():
    rows = []
    with open(SRC_CSV) as f:
        for r in csv.DictReader(f):
            if r.get("entry_quote_ok") != "True" or not r.get("entry_block"):
                continue
            row = {
                "signal_time": r["signal_time"],
                "pool": r["pool"],
                "entry_block": int(r["entry_block"]),
                "pool_age_sec": float(r["pool_age_sec"]) if r["pool_age_sec"] else None,
                "activity_heat": float(r["activity_heat"]) if r["activity_heat"] else 0,
                "buy_pressure_ratio": float(r["buy_pressure_ratio"]) if r["buy_pressure_ratio"] else 0,
                "onset_lag_sec": float(r["onset_lag_sec"]) if r["onset_lag_sec"] else 0,
            }
            for h in ["30m","1h","2h","4h","6h"]:
                row[f"ok_{h}"]    = r.get(f"exit_{h}_ok") == "True"
                row[f"retag_{h}"] = float(r[f"net_return_{h}_ag"]) if r.get(f"net_return_{h}_ag") else None
            rows.append(row)
    return sorted(rows, key=lambda x: x["signal_time"])

# ─── A6: Buyer Diversity ────────────────────────────────────────────────────

def batch_buyer_diversity(rows):
    """Batch query distinct buyers in [entry_block-100, entry_block+100] per signal."""
    log(f"A6: Querying buyer diversity for {len(rows)} signals (batched by pool)...")
    by_pool = defaultdict(list)
    for i, r in enumerate(rows):
        by_pool[r["pool"]].append((i, r["entry_block"]))

    diversity = {}
    for pool, entries in by_pool.items():
        min_blk = min(e for _, e in entries) - 100
        max_blk = max(e for _, e in entries) + 100
        out, err = ch(f"""
SELECT block_number, count(DISTINCT recipient) as div
FROM eatlq.uniswap_v3_swaps
WHERE chain_id = {CHAIN_ID}
  AND pool_address = '{pool}'
  AND block_number BETWEEN {min_blk} AND {max_blk}
GROUP BY block_number
SETTINGS {MEM_CAP}
""", timeout=45)
        blk_div = {}
        for line in out.splitlines():
            parts = line.split('\t')
            if len(parts) >= 2:
                try:
                    blk_div[int(parts[0])] = int(parts[1])
                except:
                    pass
        # For each entry in this pool, compute diversity in ±100 block window
        for idx, entry_block in entries:
            window_divs = [v for b, v in blk_div.items() if abs(b - entry_block) <= 100]
            if window_divs:
                div_score = sum(window_divs)   # total distinct buyers summed across window
                diversity[idx] = div_score
            else:
                diversity[idx] = None
    return diversity

# ─── A7: Same-block Sender Concentration (Toxic Proxy) ─────────────────────

def batch_toxic_flow(rows):
    """Count distinct senders in entry_block ± 5 blocks per signal.
    LOW diversity (<3 senders) = potentially toxic/bot-dominated block.
    """
    log(f"A7: Querying same-block sender concentration for {len(rows)} signals...")
    by_pool = defaultdict(list)
    for i, r in enumerate(rows):
        by_pool[r["pool"]].append((i, r["entry_block"]))

    sender_div = {}
    for pool, entries in by_pool.items():
        min_blk = min(e for _, e in entries) - 10
        max_blk = max(e for _, e in entries) + 10
        out, err = ch(f"""
SELECT block_number, count(DISTINCT sender) as sender_count, count() as total_txs
FROM eatlq.uniswap_v3_swaps
WHERE chain_id = {CHAIN_ID}
  AND pool_address = '{pool}'
  AND block_number BETWEEN {min_blk} AND {max_blk}
GROUP BY block_number
SETTINGS {MEM_CAP}
""", timeout=45)
        blk_data = {}
        for line in out.splitlines():
            parts = line.split('\t')
            if len(parts) >= 3:
                try:
                    blk_data[int(parts[0])] = {"senders": int(parts[1]), "txs": int(parts[2])}
                except:
                    pass
        for idx, entry_block in entries:
            window = [v for b, v in blk_data.items() if abs(b - entry_block) <= 5]
            if window:
                total_senders = sum(w["senders"] for w in window)
                total_txs     = sum(w["txs"] for w in window)
                sender_div[idx] = {
                    "total_senders_window": total_senders,
                    "total_txs_window": total_txs,
                    "concentration": total_senders / max(total_txs, 1),
                }
            else:
                sender_div[idx] = None
    return sender_div

# ─── A8: Smart Money 0x6ff5 Gate ───────────────────────────────────────────

def batch_smart_money_gate(rows):
    """Check if 0x6ff5 traded in [entry_block-300, entry_block+300] per signal."""
    log(f"A8: Querying 0x6ff5 presence for {len(rows)} signals...")
    by_pool = defaultdict(list)
    for i, r in enumerate(rows):
        by_pool[r["pool"]].append((i, r["entry_block"]))

    sm_present = {}
    for pool, entries in by_pool.items():
        min_blk = min(e for _, e in entries) - 300
        max_blk = max(e for _, e in entries) + 300
        out, err = ch(f"""
SELECT block_number
FROM eatlq.uniswap_v3_swaps
WHERE chain_id = {CHAIN_ID}
  AND pool_address = '{pool}'
  AND sender = '{SENDER_6FF5}'
  AND block_number BETWEEN {min_blk} AND {max_blk}
SETTINGS {MEM_CAP}
""", timeout=45)
        sm_blocks = set()
        for line in out.splitlines():
            try:
                sm_blocks.add(int(line.strip()))
            except:
                pass
        for idx, entry_block in entries:
            nearby = [b for b in sm_blocks if abs(b - entry_block) <= 300]
            sm_present[idx] = len(nearby) > 0
    return sm_present

# ─── Strategy simulation ────────────────────────────────────────────────────

def simulate_with_gate(rows, features, gate_fn, exit_horizon="30m", label="",
                       max_concurrent=3, cooldown_sec=1800):
    from datetime import datetime, timezone
    TRADE_SIZE, INITIAL_EQ = 10.0, 100.0
    cash = INITIAL_EQ
    open_pos, pool_last = {}, {}
    trades, skipped = [], {"gate": 0, "cooldown": 0, "capital": 0, "no_exit": 0}

    for i, row in enumerate(rows):
        # Feature gate
        feat = features.get(i)
        if not gate_fn(feat):
            skipped["gate"] += 1
            continue
        if not row.get(f"ok_{exit_horizon}") or row.get(f"retag_{exit_horizon}") is None:
            skipped["no_exit"] += 1
            continue
        pool = row["pool"]
        st   = row["signal_time"]
        if cash < TRADE_SIZE:
            skipped["capital"] += 1
            continue
        if pool in open_pos or len(open_pos) >= max_concurrent:
            continue
        last = pool_last.get(pool)
        if last:
            t1 = datetime.fromisoformat(last).astimezone(timezone.utc)
            t2 = datetime.fromisoformat(st).astimezone(timezone.utc)
            if (t2 - t1).total_seconds() < cooldown_sec:
                skipped["cooldown"] += 1
                continue
        cash -= TRADE_SIZE
        open_pos[pool] = st
        pool_last[pool] = st
        ret_ag = row[f"retag_{exit_horizon}"]
        pnl = TRADE_SIZE * ret_ag
        cash += TRADE_SIZE + pnl
        open_pos.pop(pool, None)
        trades.append({"pool": pool[:12], "st": st, "ret_ag": round(ret_ag,6),
                        "pnl": round(pnl,4), "win": pnl>0})

    n = len(trades)
    if n == 0:
        return {"label": label, "n": 0, "pf": None, "wr": None, "equity": INITIAL_EQ, "skipped": skipped}
    wins   = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [abs(t["pnl"]) for t in trades if t["pnl"] <= 0]
    pf = sum(wins)/sum(losses) if losses else float("inf")
    wr = sum(1 for t in trades if t["win"]) / n
    equity = INITIAL_EQ + sum(t["pnl"] for t in trades)
    return {
        "label": label, "n": n, "pf": round(pf,4), "wr": round(wr,4),
        "equity": round(equity,4), "return_pct": round((equity-INITIAL_EQ)/INITIAL_EQ*100,2),
        "skipped": skipped
    }

def no_gate(feat): return True  # baseline

# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    log("=" * 65)
    log("Sprint072 TIER 2 — A6/A7/A8 Feature Analysis")
    log("=" * 65)

    rows = load_paper_shadow()
    log(f"Loaded {len(rows)} valid paper shadow rows")

    # ─── A6: Buyer Diversity ─────────────────────────────────────────────
    log("\n--- A6: Buyer Diversity ---")
    diversity = batch_buyer_diversity(rows)
    valid_div = [(rows[i], v) for i, v in diversity.items() if v is not None]
    if valid_div:
        div_vals = [v for _, v in valid_div]
        median_div = statistics.median(div_vals)
        log(f"  Median diversity score: {median_div:.0f}  (min={min(div_vals)}, max={max(div_vals)})")

        # Correlation with 30m return
        hi_div = [(r, v) for r, v in valid_div if v >= median_div]
        lo_div = [(r, v) for r, v in valid_div if v < median_div]

        def pf_wr(subset):
            rets = [r.get("retag_30m") for r, _ in subset if r.get("ok_30m") and r.get("retag_30m") is not None]
            if not rets: return None, None, 0
            wins = [r for r in rets if r > 0]
            losses = [abs(r) for r in rets if r <= 0]
            pf = sum(wins)/sum(losses) if losses else float("inf")
            wr = len(wins)/len(rets)
            return round(pf,3), round(wr,3), len(rets)

        pf_hi, wr_hi, n_hi = pf_wr(hi_div)
        pf_lo, wr_lo, n_lo = pf_wr(lo_div)
        log(f"  High diversity (>= {median_div:.0f}): n={n_hi}  PF={pf_hi}  WR={wr_hi}")
        log(f"  Low  diversity (<  {median_div:.0f}): n={n_lo}  PF={pf_lo}  WR={wr_lo}")

        # Gate: keep high-diversity signals only
        res_a6_hi = simulate_with_gate(rows, diversity,
                                       gate_fn=lambda v: v is not None and v >= median_div,
                                       exit_horizon="30m", label="A6_highDiv_30m")
        res_a6_4h = simulate_with_gate(rows, diversity,
                                       gate_fn=lambda v: v is not None and v >= median_div,
                                       exit_horizon="4h", label="A6_highDiv_4h")
        a6_results = [res_a6_hi, res_a6_4h]
    else:
        a6_results = []
        median_div = 0
        log("  No diversity data available")

    # ─── A7: Toxic Flow ──────────────────────────────────────────────────
    log("\n--- A7: Same-block Sender Concentration ---")
    toxic = batch_toxic_flow(rows)
    valid_tox = {i: v for i, v in toxic.items() if v is not None}

    if valid_tox:
        concs = [v["concentration"] for v in valid_tox.values()]
        median_conc = statistics.median(concs)
        log(f"  Sender concentration (senders/txs): median={median_conc:.3f}  "
            f"min={min(concs):.3f}  max={max(concs):.3f}")

        # Gate: LOW concentration (many senders, more organic) = PASS
        # HIGH concentration (few senders, bot-like) = FAIL (toxic)
        res_a7_organic_30m = simulate_with_gate(
            rows, {i: v["concentration"] if v else None for i, v in toxic.items()},
            gate_fn=lambda v: v is not None and v >= median_conc,  # high diversity = organic
            exit_horizon="30m", label="A7_organic_30m")
        res_a7_organic_4h = simulate_with_gate(
            rows, {i: v["concentration"] if v else None for i, v in toxic.items()},
            gate_fn=lambda v: v is not None and v >= median_conc,
            exit_horizon="4h", label="A7_organic_4h")
        res_a7_toxic_30m = simulate_with_gate(
            rows, {i: v["concentration"] if v else None for i, v in toxic.items()},
            gate_fn=lambda v: v is not None and v < median_conc,   # low diversity = concentrated/toxic
            exit_horizon="30m", label="A7_toxic_30m")
        a7_results = [res_a7_organic_30m, res_a7_organic_4h, res_a7_toxic_30m]
    else:
        a7_results = []
        median_conc = 0
        log("  No toxic flow data")

    # ─── A8: Smart Money Gate ────────────────────────────────────────────
    log("\n--- A8: 0x6ff5 Smart Money Gate ---")
    sm_gate = batch_smart_money_gate(rows)
    n_sm_yes = sum(1 for v in sm_gate.values() if v)
    n_sm_no  = sum(1 for v in sm_gate.values() if not v)
    log(f"  0x6ff5 present: {n_sm_yes}/{len(sm_gate)} signals ({100*n_sm_yes/max(len(sm_gate),1):.1f}%)")

    res_a8_sm_30m = simulate_with_gate(
        rows, sm_gate,
        gate_fn=lambda v: v is True,
        exit_horizon="30m", label="A8_smart_money_30m")
    res_a8_sm_4h = simulate_with_gate(
        rows, sm_gate,
        gate_fn=lambda v: v is True,
        exit_horizon="4h", label="A8_smart_money_4h")
    res_a8_nosm_30m = simulate_with_gate(
        rows, sm_gate,
        gate_fn=lambda v: v is False,
        exit_horizon="30m", label="A8_no_smart_money_30m")
    a8_results = [res_a8_sm_30m, res_a8_sm_4h, res_a8_nosm_30m]

    # ─── Baselines ───────────────────────────────────────────────────────
    baseline = {i: True for i in range(len(rows))}
    res_base_30m = simulate_with_gate(rows, baseline, gate_fn=lambda v: True,
                                      exit_horizon="30m", label="BASELINE_30m")
    res_base_4h  = simulate_with_gate(rows, baseline, gate_fn=lambda v: True,
                                      exit_horizon="4h", label="BASELINE_4h")

    # ─── Print results ───────────────────────────────────────────────────
    all_results = [res_base_30m, res_base_4h] + a6_results + a7_results + a8_results
    print("\n=== FEATURE GATE COMPARISON ===")
    print(f"{'Strategy':<35} {'n':>4} {'PF':>7} {'WR':>6} {'Return':>8}  Skipped(gate+cooldown)")
    for res in all_results:
        if res["pf"] is None:
            print(f"  {res['label']:<33}  {'N/A':>4}  {'N/A':>7}  {'N/A':>6}  {'N/A':>8}")
            continue
        sk = res.get("skipped", {})
        print(f"  {res['label']:<33} {res['n']:>4}  {res['pf']:>7.3f}  {res['wr']:>6.3f}  "
              f"{res['return_pct']:>+7.1f}%   gate={sk.get('gate',0)} cool={sk.get('cooldown',0)}")

    # ─── Save results ────────────────────────────────────────────────────
    output = {
        "generated_at": __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
        "a6_buyer_diversity": {
            "median_diversity_score": median_div,
            "results": a6_results
        },
        "a7_toxic_flow": {
            "median_concentration": round(median_conc, 4) if median_conc else None,
            "interpretation": "concentration = distinct_senders/total_txs; higher=more organic",
            "results": a7_results
        },
        "a8_smart_money_6ff5": {
            "n_signals_with_6ff5": n_sm_yes,
            "n_signals_without_6ff5": n_sm_no,
            "coverage_pct": round(100*n_sm_yes/max(len(sm_gate),1), 1),
            "results": a8_results
        },
        "baselines": [res_base_30m, res_base_4h]
    }
    out_path = OUT_DIR / "sprint072_tier2_feature_results.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    log(f"\nResults written to {out_path}")

    return output

if __name__ == "__main__":
    main()
