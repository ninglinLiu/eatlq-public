"""
Sprint066b: Canonical Gate Expansion Labeler

Applies Sprint037G canonical gate to May04+ weeks, excluding the 13 pools
already labeled in Sprint062.  Labels qualifying rows with:
  - entry   (QuoterV2 WETH → token at entry_block)
  - exit_5m (QuoterV2 token → WETH at entry_block + 150)   [proper 5 min @ 2s/block]
  - exit_30m(QuoterV2 token → WETH at entry_block + 900)   [30 min]
Then computes P3/P4/P5 from CH and combines with the existing 141-row dataset.

Canonical gate (Sprint037G, FROZEN — do not tune):
  pool_max_age_sec = 86400   pool must be < 24h old at entry decision
  onset_max_sec    = 3600    entry bucket within first 60 min of pool creation
  onset_thresh     = 10      entry bucket ≥ 10 swaps
  bucket_size_sec  = 300     5-minute canonical buckets

Budget guard: aborts if estimated RPC calls > 500.
No token blacklist / whitelist / post-hoc removal.
Decision-time observable features only.
"""

import csv, datetime, json, math, os, pathlib, subprocess, sys, time
from collections import defaultdict
from typing import Optional

REPO       = pathlib.Path(__file__).resolve().parent.parent
DIAG_DIR   = REPO / "mvp/diagnostics/sprint066"
AUDIT_DIR  = REPO / "mvp/audits/sprint066"
DIAG_DIR.mkdir(parents=True, exist_ok=True)
AUDIT_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(REPO / "scripts"))
try:
    from lib.v3_quoterv2_struct import quote_exact_input_single_struct, JsonRpcClient
    RPC_URL         = os.environ.get("EVM_HTTP_URL", "")
    QUOTER_AVAILABLE = bool(RPC_URL)
except Exception:
    QUOTER_AVAILABLE = False
    RPC_URL = ""

PROBE_WEI = 5e13        # 0.00005 WETH — consistent with sprint062 entry probe
WETH      = "0x4200000000000000000000000000000000000006"
FEE_TIER  = 100
CHAIN_ID  = 8453

# Sprint037G canonical gate — FROZEN
GATE = dict(pool_max_age_sec=86400, onset_max_sec=3600, onset_thresh=10, bucket_sec=300)

# Budget guard
RPC_CALL_BUDGET = 500

# Sprint062 pools already labeled — skip these
SPRINT062_POOLS = {
    "0x0f4c24d00267d58ec9fad43a9df5a8ffeed3d542",
    "0x29fd6a7c3afb5047b0e2703ac99871849e7ecdad",
    "0x3e118916cd67cc9bfeb0fc44ae3dde8bc33c2d4a",
    "0x46ad711536d02de716a263263c394669c696b7a3",
    "0x73e0a6ce4aec1da0ee87bb44246ac695f4489488",
    "0x790cf0e891badd11424dce95b475e8eef7f52527",
    "0x7a9713b17843dddc3325a37ec2c139d5ed2afe29",
    "0x991cf2b5eb33211bd9ba75fcd0d9456a3248c42a",
    "0xa3eb19bbde120ea2caf148171cc70ffad16d4c5a",
    "0xc1169323b9e7885c12b04b23d45cb046cfe43087",
    "0xcc5eeaabd1f7421a558524cbb9036f47130a9678",
    "0xd6bd28f8c493dbfa7a9eac890d4e3121dab29a84",
    "0xf7df90730870571529c6f6743443e1ca40e0a457",
}


# ─── CH helper ────────────────────────────────────────────────────────────────
def ch_query(sql: str, timeout_s: int = 25) -> list[dict]:
    cmd = ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
           "--format", "JSONEachRow", "--query", sql]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return [json.loads(l) for l in r.stdout.strip().splitlines() if l.strip()]


# ─── Discover new qualifying pools in a week window ───────────────────────────
def discover_new_pools(week_start: str, week_end: str) -> list[dict]:
    """Return discovered_pools metadata for new fee_100bps WETH-leg pools in the window."""
    rows = ch_query(f"""
        SELECT
            dp.pool_address,
            dp.token0_address,
            dp.token1_address,
            dp.token0_symbol,
            dp.token1_symbol,
            dp.fee_bps,
            dp.has_weth_leg,
            s.first_swap,
            s.total_swaps
        FROM eatlq.discovered_pools dp
        INNER JOIN (
            SELECT pool_address, min(block_time) AS first_swap, count() AS total_swaps
            FROM eatlq.uniswap_v3_swaps_decoded
            WHERE chain_id = {CHAIN_ID}
              AND block_time >= '{week_start}'
              AND block_time  < '{week_end}'
            GROUP BY pool_address
        ) s ON lower(dp.pool_address) = lower(s.pool_address)
        WHERE dp.chain_id = {CHAIN_ID}
          AND dp.fee_bps = {FEE_TIER}
          AND dp.has_weth_leg = 1
          AND s.first_swap >= '{week_start}'
          AND s.first_swap  < '{week_end}'
        ORDER BY s.first_swap
    """, timeout_s=30)
    # Exclude already-labeled pools
    return [r for r in rows if r["pool_address"].lower() not in SPRINT062_POOLS]


# ─── Apply canonical gate to one pool ─────────────────────────────────────────
def canonical_gate_rows(pool: str, first_swap_str: str,
                        week_start: str, week_end: str) -> list[dict]:
    """
    Build 5-min canonical buckets for this pool within pool's first hour.
    Returns rows that pass gate_thresh (≥10 swaps in bucket within onset_max_sec).
    """
    first_swap_dt = datetime.datetime.strptime(first_swap_str[:19], "%Y-%m-%d %H:%M:%S")
    onset_end     = first_swap_dt + datetime.timedelta(seconds=GATE["onset_max_sec"])
    we_str        = week_end[:10] + " 00:00:00" if len(week_end) <= 10 else week_end[:19]
    scan_end      = min(onset_end, datetime.datetime.strptime(we_str, "%Y-%m-%d %H:%M:%S"))

    # Fetch all swaps in first hour
    t0  = first_swap_str[:19]
    t1  = scan_end.strftime("%Y-%m-%d %H:%M:%S")

    try:
        raw = ch_query(f"""
            SELECT block_time, block_number, sender, recipient,
                   toUInt256(sqrt_price_x96) AS spx
            FROM eatlq.uniswap_v3_swaps_decoded
            WHERE chain_id = {CHAIN_ID}
              AND lower(pool_address) = '{pool}'
              AND block_time >= '{t0}'
              AND block_time  < '{t1}'
            ORDER BY block_time
        """, timeout_s=20)
    except Exception as e:
        print(f"    WARNING {pool[:10]} CH error: {e}", flush=True)
        return []

    if not raw:
        return []

    # Bucket swaps into 5-min bins by pool_age_sec
    bucket_sec = GATE["bucket_sec"]
    buckets: dict[int, list] = defaultdict(list)
    for s in raw:
        bt = datetime.datetime.strptime(s["block_time"][:19], "%Y-%m-%d %H:%M:%S")
        age_sec = int((bt - first_swap_dt).total_seconds())
        b_key   = (age_sec // bucket_sec) * bucket_sec  # bucket start (onset_sec)
        buckets[b_key].append(s)

    # Apply gate: bucket within onset_max_sec AND ≥ onset_thresh swaps
    result = []
    for b_onset, swaps in sorted(buckets.items()):
        if b_onset > GATE["onset_max_sec"]:
            continue
        if len(swaps) < GATE["onset_thresh"]:
            continue
        # Entry block = first block in bucket
        entry_swap  = swaps[0]
        entry_block = int(entry_swap["block_number"])
        bt_str      = entry_swap["block_time"][:19]

        # Determine token direction (WETH → other token)
        # token_in = WETH, token_out = non-WETH token
        # This will be set from discovered_pools metadata in the caller
        result.append({
            "pool":              pool,
            "bucket_onset_sec":  b_onset,
            "block_time":        bt_str,
            "entry_block":       entry_block,
            "bucket_swaps":      len(swaps),
        })

    return result


# ─── QuoterV2 label one row ────────────────────────────────────────────────────
def label_row(rpc: JsonRpcClient, token_in: str, token_out: str,
              entry_block: int) -> dict:
    """Call QuoterV2 for entry, 5m exit, 30m exit. Returns dict of results."""
    result = {"entry_ok": False, "label_5m_ok": False, "label_30m_ok": False,
              "entry_amount_out": 0, "exit_weth_5m": 0, "exit_weth_30m": 0,
              "net_return_5m": None, "net_return_30m": None}

    # Entry: WETH → token
    r_entry = quote_exact_input_single_struct(
        rpc=rpc, token_in=WETH, token_out=token_out,
        fee=FEE_TIER, amount_in=int(PROBE_WEI),
        sqrt_price_limit_x96=0, block_identifier=entry_block,
    )
    if not r_entry.get("ok"):
        return result
    entry_ao = int(r_entry.get("amount_out", 0) or 0)
    if entry_ao <= 0:
        return result
    result["entry_ok"]         = True
    result["entry_amount_out"] = entry_ao

    # 5m exit: token → WETH at entry_block + 150 (~5 min at 2s/block)
    r_5m = quote_exact_input_single_struct(
        rpc=rpc, token_in=token_out, token_out=WETH,
        fee=FEE_TIER, amount_in=entry_ao,
        sqrt_price_limit_x96=0, block_identifier=entry_block + 150,
    )
    if r_5m.get("ok"):
        weth_5m = int(r_5m.get("amount_out", 0) or 0)
        if weth_5m > 0:
            result["label_5m_ok"]  = True
            result["exit_weth_5m"] = weth_5m
            result["net_return_5m"] = (weth_5m / PROBE_WEI) - 1.0

    # 30m exit: token → WETH at entry_block + 900 (30 min)
    r_30m = quote_exact_input_single_struct(
        rpc=rpc, token_in=token_out, token_out=WETH,
        fee=FEE_TIER, amount_in=entry_ao,
        sqrt_price_limit_x96=0, block_identifier=entry_block + 900,
    )
    if r_30m.get("ok"):
        weth_30m = int(r_30m.get("amount_out", 0) or 0)
        if weth_30m > 0:
            result["label_30m_ok"]  = True
            result["exit_weth_30m"] = weth_30m
            result["net_return_30m"] = (weth_30m / PROBE_WEI) - 1.0

    return result


# ─── Per-bucket P3/P4/P5 from CH ──────────────────────────────────────────────
def compute_features(pool: str, bt: str) -> dict:
    """Same P3/P4/P5 queries as sprint066 but for a single bucket."""
    bucket_end_dt = (datetime.datetime.strptime(bt, "%Y-%m-%d %H:%M:%S")
                     + datetime.timedelta(minutes=5))
    bucket_end = bucket_end_dt.strftime("%Y-%m-%d %H:%M:%S")

    defaults = {"p3_distinct_rcpt": None, "p3_top_rcpt_share": None,
                "p4_same_block_frac": None, "p5_early_conc_w": None,
                "p2b_price_move_pct": None}
    try:
        q_a = ch_query(f"""
            SELECT count() AS distinct_rcpt, sum(rcnt) AS total_swaps, max(rcnt) AS top_rcpt_cnt
            FROM (
                SELECT recipient, count() AS rcnt
                FROM eatlq.uniswap_v3_swaps_decoded
                WHERE chain_id = {CHAIN_ID}
                  AND lower(pool_address) = '{pool}'
                  AND block_time >= '{bt}'
                  AND block_time  < '{bucket_end}'
                GROUP BY recipient
            )
        """, timeout_s=12)
        q_b = ch_query(f"""
            SELECT max(blk_cnt) AS max_block_swaps, sum(blk_cnt) AS total_swaps_b,
                   min(price_first) AS price_min,
                   maxIf(price_last, is_last=1) AS price_last
            FROM (
                SELECT block_number, count() AS blk_cnt,
                       min(toUInt256(sqrt_price_x96)) AS price_first,
                       anyLast(toUInt256(sqrt_price_x96)) AS price_last,
                       if(block_number = max(block_number) OVER (), 1, 0) AS is_last
                FROM eatlq.uniswap_v3_swaps_decoded
                WHERE chain_id = {CHAIN_ID}
                  AND lower(pool_address) = '{pool}'
                  AND block_time >= '{bt}'
                  AND block_time  < '{bucket_end}'
                GROUP BY block_number
            )
        """, timeout_s=12)
    except Exception:
        return defaults

    if q_a and q_a[0].get("total_swaps"):
        a      = q_a[0]
        total  = max(int(a["total_swaps"]), 1)
        dr     = int(a["distinct_rcpt"])
        top    = int(a.get("top_rcpt_cnt", total))
        blk_max = 0
        price_move_pct = None
        if q_b and q_b[0].get("max_block_swaps"):
            b = q_b[0]
            blk_max = int(b["max_block_swaps"])
            try:
                p_min  = float(b["price_min"])
                p_last = float(b["price_last"])
                price_move_pct = abs(p_last - p_min) / max(p_min, 1e-9) * 100.0
            except Exception:
                price_move_pct = None
        return {
            "p3_distinct_rcpt":   dr,
            "p3_top_rcpt_share":  round(top / total, 4),
            "p4_same_block_frac": round(blk_max / total, 4),
            "p5_early_conc_w":    round(top / total, 4),
            "p2b_price_move_pct": round(price_move_pct, 4) if price_move_pct is not None else None,
        }
    return defaults


# ─── Main expansion loop ───────────────────────────────────────────────────────
def run_expansion(weeks: list[tuple[str, str]]) -> list[dict]:
    """
    For each (week_start, week_end), discover new pools, apply canonical gate,
    label qualifying rows, compute features. Returns new_rows list.
    """
    if not QUOTER_AVAILABLE:
        print("  QuoterV2 not available — set EVM_HTTP_URL", flush=True)
        return []

    rpc = JsonRpcClient(RPC_URL, delay_s=0.08)
    total_rpc = 0
    new_rows: list[dict] = []

    for week_start, week_end in weeks:
        print(f"\n[Expansion] Week {week_start} → {week_end}", flush=True)
        pools_meta = discover_new_pools(week_start, week_end)
        print(f"  New pools (excl S062): {len(pools_meta)}", flush=True)

        for pm in pools_meta:
            pool      = pm["pool_address"].lower()
            first_str = pm["first_swap"][:19]
            t0_sym    = pm.get("token0_symbol", "?")
            t1_sym    = pm.get("token1_symbol", "?")
            t0_addr   = pm.get("token0_address", "").lower()
            t1_addr   = pm.get("token1_address", "").lower()

            # Determine token direction: WETH is token_in, other is token_out
            if t0_addr == WETH.lower():
                token_out = t1_addr
                token_sym = t1_sym
            elif t1_addr == WETH.lower():
                token_out = t0_addr
                token_sym = t0_sym
            else:
                # has_weth_leg=1 but neither token is standard WETH → skip
                print(f"    SKIP {pool[:10]}: WETH address mismatch", flush=True)
                continue

            gate_rows = canonical_gate_rows(pool, first_str, week_start, week_end)
            if not gate_rows:
                print(f"    {pool[:10]} {token_sym}: 0 canonical rows", flush=True)
                continue

            # Budget guard
            rpc_needed = len(gate_rows) * 3  # entry + 5m + 30m per row
            if total_rpc + rpc_needed > RPC_CALL_BUDGET:
                print(f"    BUDGET EXCEEDED — stopping ({total_rpc}+{rpc_needed}>{RPC_CALL_BUDGET})", flush=True)
                break

            print(f"    {pool[:10]} {token_sym}: {len(gate_rows)} gate rows → {rpc_needed} RPC calls", flush=True)

            for gr in gate_rows:
                entry_block = gr["entry_block"]
                bt          = gr["block_time"]

                lbl = label_row(rpc, WETH, token_out, entry_block)
                total_rpc += 3

                if not lbl["entry_ok"]:
                    continue  # skip non-quoteable entries (P1 fail)

                feats = compute_features(pool, bt)

                # Compute pool_age_sec at entry time
                first_dt  = datetime.datetime.strptime(first_str, "%Y-%m-%d %H:%M:%S")
                bt_dt     = datetime.datetime.strptime(bt, "%Y-%m-%d %H:%M:%S")
                pool_age  = max(0, int((bt_dt - first_dt).total_seconds()))

                new_rows.append({
                    "pool":              pool,
                    "week":              week_start[:10],
                    "pool_age_sec":      pool_age,
                    "bucket_onset_sec":  gr["bucket_onset_sec"],
                    "block_time":        bt,
                    "entry_block":       entry_block,
                    "exit_block_5m":     entry_block + 150,
                    "token_in":          WETH,
                    "token_out":         token_out,
                    "token":             token_sym,
                    "entry_amount_out":  lbl["entry_amount_out"],
                    "exit_weth_5m":      lbl["exit_weth_5m"],
                    "exit_weth_30m":     lbl["exit_weth_30m"],
                    "net_return_5m":     lbl["net_return_5m"],
                    "net_return_30m":    lbl["net_return_30m"],
                    "label_5m_ok":       lbl["label_5m_ok"],
                    "label_30m_ok":      lbl["label_30m_ok"],
                    "entry_quote_ok":    True,
                    "exit_quote_ok":     lbl["label_5m_ok"],
                    "risk_state":        "UNKNOWN",
                    "s063_status":       "NEW",
                    "avg_heat":          0.0,
                    "early_conc":        0.0,
                    # P-features
                    "p1_entry_ok":       True,
                    "p1_exit_ok":        lbl["label_5m_ok"],
                    "p2_amount_nonzero": lbl["entry_amount_out"] > 0 and lbl["exit_weth_5m"] > 0,
                    "p2_slippage_pct":   -lbl["net_return_5m"] * 100 if lbl["net_return_5m"] is not None else None,
                    **feats,
                })

        print(f"  Expansion so far: {len(new_rows)} new rows, {total_rpc} RPC calls", flush=True)

    return new_rows


def main():
    print(f"\nSprint066b Expansion Labeler — {datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')}", flush=True)
    print(f"  QUOTER_AVAILABLE={QUOTER_AVAILABLE}", flush=True)

    # Define expansion weeks (week_start inclusive, week_end exclusive)
    weeks = [
        ("2026-05-04", "2026-05-11"),  # May04 week (not yet labeled beyond 6 S062 pools)
        ("2026-05-11", "2026-05-12"),  # May11 partial (today)
    ]

    print(f"\n[Preflight] Checking new pool counts...", flush=True)
    for w_start, w_end in weeks:
        try:
            pools = discover_new_pools(w_start, w_end)
            print(f"  {w_start}: {len(pools)} new pools (excl S062)", flush=True)
            for p in pools[:5]:
                print(f"    {p['pool_address'][:12]} {p.get('token1_symbol','?')} swaps={p['total_swaps']}", flush=True)
        except Exception as e:
            print(f"  {w_start}: ERROR {e}", flush=True)

    # Count estimated RPC calls
    total_est = 0
    for w_start, w_end in weeks:
        try:
            pools = discover_new_pools(w_start, w_end)
            for pm in pools:
                gate_rows = canonical_gate_rows(
                    pm["pool_address"].lower(),
                    pm["first_swap"][:19],
                    w_start, w_end,
                )
                total_est += len(gate_rows) * 3
        except Exception:
            pass

    print(f"\n[Budget] Estimated RPC calls: {total_est} (budget={RPC_CALL_BUDGET})", flush=True)
    if total_est > RPC_CALL_BUDGET:
        print(f"  NOTE: Will stop at {RPC_CALL_BUDGET} calls — partial expansion only", flush=True)

    # Run expansion
    new_rows = run_expansion(weeks)

    if not new_rows:
        print("\nNo new rows from expansion.", flush=True)
        print("Summary: 0 new pools, 0 new rows", flush=True)
        return

    # Combine with existing S062 labeled rows
    old_enriched = DIAG_DIR / "sprint066_enriched_universe.csv"
    old_rows = []
    if old_enriched.exists():
        old_rows = list(csv.DictReader(open(old_enriched)))
        for r in old_rows:
            for k in ("net_return_5m", "net_return_30m", "p3_distinct_rcpt",
                      "p3_top_rcpt_share", "p4_same_block_frac", "p5_early_conc_w",
                      "p2b_price_move_pct"):
                v = r.get(k)
                if v is not None and v != "":
                    try:
                        r[k] = float(v)
                    except (ValueError, TypeError):
                        pass
            # Normalize bool-like strings
            for k in ("label_30m_ok", "label_5m_ok", "entry_quote_ok", "exit_quote_ok",
                      "p1_entry_ok", "p1_exit_ok", "p2_amount_nonzero"):
                if r.get(k) in ("True", "true", "1", 1):
                    r[k] = True
                elif r.get(k) in ("False", "false", "0", 0):
                    r[k] = False

    combined = old_rows + new_rows
    pools_combined  = len(set(r["pool"] for r in combined))
    weeks_combined  = len(set(r.get("week","?")[:10] for r in combined))
    n30m_combined   = sum(1 for r in combined if r.get("label_30m_ok"))
    print(f"\n[Combined] rows={len(combined)} pools={pools_combined} weeks={weeks_combined} 30m_ok={n30m_combined}", flush=True)

    # Write expanded CSV
    out_path = DIAG_DIR / "sprint066_expanded_universe.csv"
    all_keys = sorted({k for r in combined for k in r.keys()})
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(combined)
    print(f"  Wrote {len(combined)} rows → {out_path}", flush=True)

    # Write expansion manifest
    manifest = {
        "generated_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "new_rows": len(new_rows),
        "new_pools": len(set(r["pool"] for r in new_rows)),
        "combined_rows": len(combined),
        "combined_pools": pools_combined,
        "combined_weeks": weeks_combined,
        "n30m_ok": n30m_combined,
        "weeks_expanded": [w[0] for w in weeks],
    }
    mf_path = AUDIT_DIR / "sprint066b_expansion_manifest.json"
    with open(mf_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"  Manifest → {mf_path}", flush=True)
    print(f"\nExpansion complete: +{len(new_rows)} rows, {pools_combined} total pools, {weeks_combined} weeks", flush=True)


if __name__ == "__main__":
    main()
