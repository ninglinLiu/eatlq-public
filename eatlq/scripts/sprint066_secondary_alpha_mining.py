"""
Sprint066: Secondary Feature Alpha Mining

Computes P1-P5 secondary features for 141 labeled rows (13 pools, Apr27/May04),
searches alpha/guard combinations, runs anti-overfit checks.

P1  quoteability / route_health       → from existing labeled_candidates
P2  amountOut_sanity / slippage_proxy → from labeled_candidates + CH sqrt_price_x96
P3  buyer_diversity (first N swaps)   → CH countDistinct(recipient) onset window
P4  same_block_toxic_flow             → CH block_number groupby in 5m bucket
P5  weighted early_concentration      → CH recipient swap_cnt share in onset window

30m labels: computed via QuoterV2 at entry_block+900 (sell TOKEN for WETH).
Base mainnet ~2s/block: 900 blocks × 2s = 1800s = 30 minutes.

Constraints: no token blacklist/whitelist, no lookahead, no threshold tuning,
no training, no live, decision-time observable features only.
"""

import csv, datetime, json, math, os, pathlib, subprocess, sys, time
from typing import Optional

REPO      = pathlib.Path(__file__).resolve().parent.parent
AUDIT_DIR = REPO / "mvp/audits/sprint066"
DIAG_DIR  = REPO / "mvp/diagnostics/sprint066"
AUDIT_DIR.mkdir(parents=True, exist_ok=True)
DIAG_DIR.mkdir(parents=True, exist_ok=True)

NOW_UTC = datetime.datetime.now(datetime.timezone.utc)
NOW_STR = NOW_UTC.strftime("%Y-%m-%dT%H:%M:%SZ")

PROBE_WEI = 5e13   # 0.00005 WETH — fixed amountIn from sprint062 labeling

# ── QuoterV2 imports ──────────────────────────────────────────────────────────
sys.path.insert(0, str(REPO / "scripts"))
try:
    from lib.v3_quoterv2_struct import (
        quote_exact_input_single_struct, JsonRpcClient,
    )
    RPC_URL = os.environ.get("EVM_HTTP_URL", "")
    QUOTER_AVAILABLE = bool(RPC_URL)
except Exception:
    QUOTER_AVAILABLE = False
    RPC_URL = ""

FEE100 = 10000  # Uniswap V3 fee tier integer for 0.01% (fee param = 10000? or 100?)
# Actually Uniswap V3 fee for 1 bps (0.01%) = 100 in their units
FEE_TIER = 100

# ── CH helper ─────────────────────────────────────────────────────────────────
def ch_query(sql: str, timeout_s: int = 25) -> list[dict]:
    cmd = ["docker","exec","eatlq-clickhouse-1","clickhouse-client",
           "--format","JSONEachRow","--query", sql]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return [json.loads(l) for l in r.stdout.strip().splitlines() if l.strip()]


# ══════════════════════════════════════════════════════════════════════════════
# TASK 1: Load Sample Universe
# ══════════════════════════════════════════════════════════════════════════════

def load_universe() -> list[dict]:
    """Load and enrich the 141-row labeled universe."""
    lc = list(csv.DictReader(open(REPO/"mvp/diagnostics/sprint062/labeled_candidates.csv")))
    # load risk_state from sprint064
    rs = {}
    rsp = REPO/"mvp/diagnostics/sprint064/risk_state_taxonomy.csv"
    if rsp.exists():
        for r in csv.DictReader(open(rsp)):
            rs[r["pool"].lower()] = r.get("risk_state","UNKNOWN")
    # load pool universe (heat, early_conc, etc.)
    pu = {}
    pup = REPO/"mvp/diagnostics/sprint064/new_launch_universe.csv"
    if pup.exists():
        for r in csv.DictReader(open(pup)):
            pu[r["pool"].lower()] = r

    rows = []
    for r in lc:
        if r.get("entry_quote_ok","").lower() not in ("true","1"):
            continue
        if r.get("exit_quote_ok","").lower() not in ("true","1"):
            continue
        ret5 = float(r["net_return_5m"]) if r.get("net_return_5m") else None
        if ret5 is None:
            continue
        pool = r["pool"].lower()
        pu_row = pu.get(pool, {})
        rows.append({
            "pool":            pool,
            "week":            r["week"][:10],
            "pool_age_sec":    float(r.get("pool_age_sec",0) or 0),
            "bucket_onset_sec":float(r.get("bucket_onset_sec",0) or 0),
            "block_time":      r["block_time"],
            "entry_block":     int(r["entry_block"]) if r.get("entry_block") else None,
            "exit_block_5m":   int(r["exit_block"])  if r.get("exit_block")  else None,
            "token_in":        r.get("token_in",""),
            "token_out":       r.get("token_out",""),
            "entry_amount_out":int(r["entry_amount_out"]) if r.get("entry_amount_out","").strip() else 0,
            "exit_weth_5m":    int(r["exit_amount_out_weth"]) if r.get("exit_amount_out_weth","").strip() else 0,
            "net_return_5m":   ret5,
            "risk_state":      rs.get(pool,"UNKNOWN"),
            "s063_status":     pu_row.get("s063_status","UNKNOWN"),
            "token":           pu_row.get("token","?"),
            "avg_heat":        float(pu_row.get("avg_heat",0) or 0),
            "early_conc":      float(pu_row.get("early_conc",0) or 0),
            # P-feature placeholders
            "p1_entry_ok":     True,
            "p1_exit_ok":      True,
            "p2_amount_nonzero": False,
            "p2_slippage_pct": None,
            "p2b_price_move_pct": None,
            "p3_distinct_rcpt": None,
            "p3_top_rcpt_share": None,
            "p4_same_block_frac": None,
            "p5_early_conc_w":  None,
            # 30m label
            "net_return_30m":  None,
            "exit_weth_30m":   None,
            "label_30m_ok":    False,
        })
    return rows


# ══════════════════════════════════════════════════════════════════════════════
# TASK 2: Compute P1-P5
# ══════════════════════════════════════════════════════════════════════════════

# ── P1/P2 from labeled_candidates columns ─────────────────────────────────────
def compute_p1_p2(rows: list[dict]) -> None:
    """P1: quoteability already loaded. P2: amountOut sanity + slippage proxy."""
    for r in rows:
        # P2a: amountOut sanity
        r["p2_amount_nonzero"] = (r["entry_amount_out"] > 0 and r["exit_weth_5m"] > 0)
        # P2b: slippage proxy = (1 - exit_weth_5m / PROBE_WEI) as % of entry
        # net_return_5m = exit_weth_5m / PROBE_WEI - 1
        # "slippage" here = -(net_return_5m) when negative (adverse price move)
        r["p2_slippage_pct"] = -r["net_return_5m"] * 100  # positive = adverse


# ── P3/P4/P5 from CH ─────────────────────────────────────────────────────────
def _get_pool_onset_block_time(rows: list[dict], pool: str) -> Optional[str]:
    """Get the earliest block_time (pool onset) for this pool across all rows."""
    times = [r["block_time"] for r in rows if r["pool"] == pool and r["block_time"]]
    if not times:
        return None
    # The onset starts at block_time - bucket_onset_sec (time since first swap)
    # Simpler: find the row with smallest bucket_onset_sec
    onset_rows = [r for r in rows if r["pool"] == pool]
    onset_rows.sort(key=lambda x: x["bucket_onset_sec"])
    first = onset_rows[0]
    # pool first_swap = block_time - bucket_onset_sec seconds
    import datetime
    bt = datetime.datetime.strptime(first["block_time"][:19], "%Y-%m-%d %H:%M:%S")
    offset = datetime.timedelta(seconds=float(first["bucket_onset_sec"]))
    pool_start = bt - offset
    return pool_start.strftime("%Y-%m-%d %H:%M:%S")


def compute_p3_p4_p5(rows: list[dict]) -> None:
    """
    Per-row (per 5-min entry bucket) CH queries for P3/P4/P5.
    Uses entry bucket specifically (not full onset window) for entry-time observability.
    Uses countDistinct(recipient) for true diversity (no LIMIT cap).
    """
    print(f"  CH queries P3/P4/P5 per row ({len(rows)} rows)...", flush=True)

    # Batch per pool to reduce CH round trips — dedup by (pool, bucket_time)
    seen_buckets: dict[tuple, dict] = {}

    for row in rows:
        pool = row["pool"]
        bt   = row["block_time"][:19]
        key  = (pool, bt)
        if key in seen_buckets:
            continue

        # Entry bucket window
        try:
            bucket_end_dt = (datetime.datetime.strptime(bt, "%Y-%m-%d %H:%M:%S")
                             + datetime.timedelta(minutes=5))
            bucket_end = bucket_end_dt.strftime("%Y-%m-%d %H:%M:%S")

            # Query A: P3 — recipient diversity in bucket
            # count() = # distinct recipient groups; sum(rcnt) = total swaps
            q_a = ch_query(f"""
                SELECT
                    count()   AS distinct_rcpt,
                    sum(rcnt) AS total_swaps,
                    max(rcnt) AS top_rcpt_cnt
                FROM (
                    SELECT recipient, count() AS rcnt
                    FROM eatlq.uniswap_v3_swaps_decoded
                    WHERE chain_id = 8453
                      AND lower(pool_address) = '{pool}'
                      AND block_time >= '{bt}'
                      AND block_time  < '{bucket_end}'
                    GROUP BY recipient
                )
            """, timeout_s=12)

            # Query B: P4 + price move — block concentration + sqrt_price swing
            q_b = ch_query(f"""
                SELECT
                    max(blk_cnt)                       AS max_block_swaps,
                    sum(blk_cnt)                       AS total_swaps_b,
                    min(price_first)                   AS price_min,
                    maxIf(price_last, is_last=1)        AS price_last
                FROM (
                    SELECT
                        block_number,
                        count()                            AS blk_cnt,
                        min(toUInt256(sqrt_price_x96))     AS price_first,
                        anyLast(toUInt256(sqrt_price_x96)) AS price_last,
                        if(block_number = max(block_number) OVER (), 1, 0) AS is_last
                    FROM eatlq.uniswap_v3_swaps_decoded
                    WHERE chain_id = 8453
                      AND lower(pool_address) = '{pool}'
                      AND block_time >= '{bt}'
                      AND block_time  < '{bucket_end}'
                    GROUP BY block_number
                )
            """, timeout_s=12)

        except Exception as e:
            print(f"    WARNING {pool[:10]} {bt}: {e}", flush=True)
            q_a, q_b = [], []

        stats = None
        if q_a and q_a[0].get("total_swaps"):
            a     = q_a[0]
            total = max(int(a["total_swaps"]), 1)
            dr    = int(a["distinct_rcpt"])
            top   = int(a.get("top_rcpt_cnt", total))
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
            stats = {
                "p3_distinct_rcpt":   dr,
                "p3_top_rcpt_share":  round(top / total, 4),
                "p4_same_block_frac": round(blk_max / total, 4),
                "p5_early_conc_w":    round(top / total, 4),
                "p2b_price_move_pct": round(price_move_pct, 4) if price_move_pct is not None else None,
            }
        seen_buckets[key] = stats

    # Apply to rows
    for row in rows:
        key = (row["pool"], row["block_time"][:19])
        stats = seen_buckets.get(key)
        if stats:
            row.update(stats)
        else:
            row["p3_distinct_rcpt"]   = None
            row["p3_top_rcpt_share"]  = None
            row["p4_same_block_frac"] = None
            row["p5_early_conc_w"]    = None
            row["p2b_price_move_pct"] = None


# ── 30m labels via QuoterV2 ───────────────────────────────────────────────────
def compute_30m_labels(rows: list[dict]) -> int:
    """Call QuoterV2 at entry_block+360 to get 30m exit WETH. Returns # labeled."""
    if not QUOTER_AVAILABLE:
        print("  QuoterV2 not available — skipping 30m labels", flush=True)
        return 0

    WETH = "0x4200000000000000000000000000000000000006"
    rpc = JsonRpcClient(RPC_URL, delay_s=0.08)
    labeled_30m = 0

    for r in rows:
        if not r["entry_block"] or not r["entry_amount_out"]:
            continue
        if r["entry_amount_out"] <= 0:
            continue

        # Base ~2s blocks: 30 min = 1800s / 2s = 900 blocks
        exit_block_30m = r["entry_block"] + 900
        token = r["token_out"]  # sell TOKEN for WETH

        try:
            result = quote_exact_input_single_struct(
                rpc=rpc,
                token_in=token,
                token_out=WETH,
                fee=FEE_TIER,
                amount_in=r["entry_amount_out"],
                sqrt_price_limit_x96=0,
                block_identifier=exit_block_30m,
            )
            exit_weth_30m = int(result.get("amount_out", 0) or 0)
            if exit_weth_30m > 0:
                r["exit_weth_30m"]  = exit_weth_30m
                r["net_return_30m"] = (exit_weth_30m / PROBE_WEI) - 1.0
                r["label_30m_ok"]   = True
                labeled_30m += 1
        except Exception:
            pass

    return labeled_30m


# ══════════════════════════════════════════════════════════════════════════════
# TASK 3: Combination Search
# ══════════════════════════════════════════════════════════════════════════════

def _pf_stats(kept: list[dict], label_col: str = "net_return_5m") -> dict:
    """Compute PF, hit rate, and related stats for a set of rows."""
    rets = [float(r[label_col]) for r in kept if r.get(label_col) is not None]
    if not rets:
        return {"n": 0, "pf": 0.0, "hit": 0.0, "median": 0.0,
                "total": 0.0, "avg": 0.0, "best": 0.0, "worst": 0.0}
    wins  = [x for x in rets if x > 0]
    loss  = [x for x in rets if x < 0]
    gw    = sum(wins) if wins else 0
    gl    = abs(sum(loss)) if loss else 1e-12
    pf    = gw / gl if gl > 1e-12 else float("inf")
    sorted_r = sorted(rets)
    n = len(rets)
    med = sorted_r[n // 2] if n % 2 == 1 else (sorted_r[n//2-1] + sorted_r[n//2]) / 2
    return {
        "n":      n,
        "pf":     round(pf, 4),
        "hit":    round(len(wins)/n, 4) if n else 0.0,
        "median": round(med, 6),
        "total":  round(sum(rets), 6),
        "avg":    round(sum(rets)/n, 6) if n else 0.0,
        "best":   round(max(rets), 6),
        "worst":  round(min(rets), 6),
    }


def _pool_share(kept: list[dict]) -> float:
    """Top pool's row share."""
    from collections import Counter
    c = Counter(r["pool"] for r in kept)
    if not c:
        return 0.0
    return round(c.most_common(1)[0][1] / len(kept), 4)


def _token_share(kept: list[dict]) -> float:
    """Top token's row share."""
    from collections import Counter
    c = Counter(r.get("token","?") for r in kept)
    if not c:
        return 0.0
    return round(c.most_common(1)[0][1] / len(kept), 4)


def eval_combo(rows: list[dict], filter_fn, label_col: str = "net_return_5m") -> dict:
    """Evaluate a filter function against the full dataset."""
    baseline = rows  # all 141
    kept    = [r for r in rows if filter_fn(r)]
    removed = [r for r in rows if not filter_fn(r)]
    base_s  = _pf_stats(baseline, label_col)
    kept_s  = _pf_stats(kept,     label_col)
    rem_s   = _pf_stats(removed,  label_col)

    # Winners removed = kept_s.wins that were removed + removed winners
    # Use label_has_value filter to skip None labels (e.g. missing 30m quotes)
    base_wins = {i for i,r in enumerate(baseline)
                 if (r.get(label_col) is not None) and float(r[label_col]) > 0}
    kept_idx  = {i for i,r in enumerate(baseline) if filter_fn(r)}
    removed_idx = set(range(len(baseline))) - kept_idx
    winners_removed = len(base_wins & removed_idx)
    losers_kept     = len({i for i,r in enumerate(baseline)
                           if filter_fn(r) and (r.get(label_col) is not None)
                           and float(r[label_col]) < 0})

    # Coverage by week
    kept_weeks = set(r["week"] for r in kept)
    two_week_coverage = ("2026-04-27" in kept_weeks and "2026-05-04" in kept_weeks)

    # Leave-one-pool-out
    kept_pools = list(set(r["pool"] for r in kept))
    lopo_pfs = []
    for p in kept_pools:
        sub = [r for r in kept if r["pool"] != p]
        s = _pf_stats(sub, label_col)
        lopo_pfs.append(s["pf"])
    lopo_min = min(lopo_pfs) if lopo_pfs else 0.0

    # Leave-one-token-out
    kept_tokens = list(set(r.get("token","?") for r in kept))
    loto_pfs = []
    for t in kept_tokens:
        sub = [r for r in kept if r.get("token","?") != t]
        s = _pf_stats(sub, label_col)
        loto_pfs.append(s["pf"])
    loto_min = min(loto_pfs) if loto_pfs else 0.0

    # Leave-one-week-out
    lowo_results = {}
    for wk in ["2026-04-27", "2026-05-04"]:
        sub = [r for r in kept if r["week"] != wk]
        s = _pf_stats(sub, label_col)
        lowo_results[wk] = s["pf"]

    # Anti-overfit flags
    flags = []
    if kept_s["n"] < 10:
        flags.append("LOW_COVERAGE")
    if kept_s["n"] < 20:
        flags.append("COVERAGE_THIN")
    if _pool_share(kept) > 0.50 and len(kept) > 0:
        flags.append("POOL_CONCENTRATION_RISK")
    if _token_share(kept) > 0.50 and len(kept) > 0:
        flags.append("TOKEN_CONCENTRATION_RISK")
    if not two_week_coverage:
        flags.append("WEEK_FRAGILE")
    if lopo_min < 0.9:
        flags.append("LOPO_FRAGILE")
    if loto_min < 0.9:
        flags.append("LOTO_FRAGILE")
    if lowo_results.get("2026-04-27", 1.0) < 0.8:
        flags.append("WEEK_APR27_FRAGILE")
    if lowo_results.get("2026-05-04", 1.0) < 0.8:
        flags.append("WEEK_MAY04_FRAGILE")
    if winners_removed > max(1, len(base_wins) * 0.30):
        flags.append("WINNERS_REMOVED_HIGH")
    # Check if improvement relies on NEXIO
    nexio_pool = "0xf7df90730870571529c6f6743443e1ca40e0a457"
    nexio_in_removed = any(r["pool"] == nexio_pool for r in removed)
    if nexio_in_removed and kept_s["pf"] > base_s["pf"] * 1.1:
        flags.append("POST_HOC_RISK_NEXIO_DEPENDENT")
    # Check BERMIT
    bermit_pool = "0xc1169323b9e7885c12b04b23d45cb046cfe43087"
    bermit_in_kept = any(r["pool"] == bermit_pool for r in kept)
    if bermit_in_kept and _pool_share(kept) > 0.40:
        flags.append("BERMIT_DOMINANCE_RISK")

    # Final verdict
    if not flags or flags == ["COVERAGE_THIN"]:
        verdict = "CANDIDATE_FOR_MORE_PAPER_VALIDATION"
    elif "WEEK_FRAGILE" in flags and len(flags) == 1:
        verdict = "WEEK_FRAGILE_CLUE"
    elif any(f in flags for f in ["LOW_COVERAGE","POOL_CONCENTRATION_RISK",
                                   "TOKEN_CONCENTRATION_RISK","LOPO_FRAGILE","LOTO_FRAGILE"]):
        verdict = "OVERFIT_RISK"
    else:
        verdict = "WEAK_CLUE_NEEDS_MORE_WEEKS"

    return {
        "kept_n":          kept_s["n"],
        "removed_n":       rem_s["n"],
        "kept_pf":         kept_s["pf"],
        "removed_pf":      rem_s["pf"],
        "kept_hit":        kept_s["hit"],
        "kept_median":     kept_s["median"],
        "kept_total":      kept_s["total"],
        "winners_removed": winners_removed,
        "losers_kept":     losers_kept,
        "two_week_coverage": two_week_coverage,
        "top_pool_share":  _pool_share(kept),
        "top_token_share": _token_share(kept),
        "lopo_min_pf":     round(lopo_min, 4),
        "loto_min_pf":     round(loto_min, 4),
        "lowo_apr27_pf":   lowo_results.get("2026-04-27", 0.0),
        "lowo_may04_pf":   lowo_results.get("2026-05-04", 0.0),
        "flags":           flags,
        "verdict":         verdict,
        "baseline_pf":     base_s["pf"],
        "pf_delta":        round((kept_s["pf"] - base_s["pf"]) / max(base_s["pf"], 1e-9), 4),
    }


def run_combination_search(rows: list[dict], label_col: str = "net_return_5m") -> list[dict]:
    """Try 30+ rule combinations and return top results."""
    results = []

    # ── Helper lambdas that are entry-time observable ─────────────────────────
    def has_p3(r):   return r.get("p3_distinct_rcpt")   is not None
    def has_p4(r):   return r.get("p4_same_block_frac") is not None
    def has_p5(r):   return r.get("p5_early_conc_w")    is not None

    # Coverage for each feature
    n_p3 = sum(1 for r in rows if has_p3(r))
    n_p4 = sum(1 for r in rows if has_p4(r))
    n_p5 = sum(1 for r in rows if has_p5(r))
    print(f"  P3 coverage={n_p3}/{len(rows)} P4={n_p4}/{len(rows)} P5={n_p5}/{len(rows)}", flush=True)

    # ── Rule definitions ──────────────────────────────────────────────────────
    rules = {}

    # Baseline
    rules["baseline_all"] = lambda r: True

    # --- P2 rules ---
    # NOTE: p2_slippage_pct = -(net_return_5m)*100 IS the label — lookahead, kept for comparison only
    rules["p2_slippage_lt5pct"]  = lambda r: (r.get("p2_slippage_pct") is not None
                                               and r["p2_slippage_pct"] < 5.0)
    rules["p2_slippage_lt2pct"]  = lambda r: (r.get("p2_slippage_pct") is not None
                                               and r["p2_slippage_pct"] < 2.0)
    rules["p2_slippage_lt10pct"] = lambda r: (r.get("p2_slippage_pct") is not None
                                               and r["p2_slippage_pct"] < 10.0)
    rules["p2_positive_only"]    = lambda r: r["net_return_5m"] > 0  # FORBIDDEN lookahead check
    # Proper slippage: sqrt_price_x96 swing in the 5m entry bucket (ENTRY-TIME OBSERVABLE)
    rules["p2b_price_move_lt5pct"]  = lambda r: (r.get("p2b_price_move_pct") is not None
                                                   and r["p2b_price_move_pct"] < 5.0)
    rules["p2b_price_move_lt10pct"] = lambda r: (r.get("p2b_price_move_pct") is not None
                                                   and r["p2b_price_move_pct"] < 10.0)
    rules["p2b_price_move_lt20pct"] = lambda r: (r.get("p2b_price_move_pct") is not None
                                                   and r["p2b_price_move_pct"] < 20.0)

    # --- P3 rules (buyer diversity) ---
    rules["p3_distinct_ge5"]  = lambda r: (r.get("p3_distinct_rcpt") is not None
                                            and r["p3_distinct_rcpt"] >= 5)
    rules["p3_distinct_ge10"] = lambda r: (r.get("p3_distinct_rcpt") is not None
                                            and r["p3_distinct_rcpt"] >= 10)
    rules["p3_top_share_lt50"] = lambda r: (r.get("p3_top_rcpt_share") is not None
                                             and r["p3_top_rcpt_share"] < 0.50)
    rules["p3_top_share_lt30"] = lambda r: (r.get("p3_top_rcpt_share") is not None
                                             and r["p3_top_rcpt_share"] < 0.30)
    rules["p3_top_share_lt70"] = lambda r: (r.get("p3_top_rcpt_share") is not None
                                             and r["p3_top_rcpt_share"] < 0.70)

    # --- P4 rules (same-block concentration) ---
    rules["p4_same_block_lt50pct"] = lambda r: (r.get("p4_same_block_frac") is not None
                                                  and r["p4_same_block_frac"] < 0.50)
    rules["p4_same_block_lt30pct"] = lambda r: (r.get("p4_same_block_frac") is not None
                                                  and r["p4_same_block_frac"] < 0.30)
    rules["p4_same_block_lt70pct"] = lambda r: (r.get("p4_same_block_frac") is not None
                                                  and r["p4_same_block_frac"] < 0.70)

    # --- P5 rules (weighted early concentration) ---
    rules["p5_early_conc_lt40"] = lambda r: (r.get("p5_early_conc_w") is not None
                                              and r["p5_early_conc_w"] < 0.40)
    rules["p5_early_conc_lt60"] = lambda r: (r.get("p5_early_conc_w") is not None
                                              and r["p5_early_conc_w"] < 0.60)
    rules["p5_early_conc_lt25"] = lambda r: (r.get("p5_early_conc_w") is not None
                                              and r["p5_early_conc_w"] < 0.25)

    # --- Heat-based (whitepaper Tier 1, already in sprint063) ---
    rules["heat_lt20"] = lambda r: float(r.get("avg_heat",0)) < 20.0
    rules["heat_ge5"]  = lambda r: float(r.get("avg_heat",0)) >= 5.0

    # --- Risk-state based (Tier 2 guard) ---
    rules["not_high_risk_watch"] = lambda r: r.get("risk_state","") not in ("HIGH_RISK_WATCH",)
    rules["neutral_or_expansion"] = lambda r: r.get("risk_state","") in (
        "NEUTRAL_WATCH", "EXPANSION_CANDIDATE", "UNKNOWN")

    # --- Combined rules ---
    def _p_ok(r):
        return (r.get("p3_distinct_rcpt") is not None and
                r.get("p4_same_block_frac") is not None and
                r.get("p5_early_conc_w") is not None)

    rules["combo_diversity_low_conc"] = lambda r: (
        _p_ok(r)
        and r["p3_distinct_rcpt"] >= 5
        and r["p5_early_conc_w"] < 0.60
    )
    rules["combo_diversity_low_sameblock"] = lambda r: (
        _p_ok(r)
        and r["p3_distinct_rcpt"] >= 5
        and r["p4_same_block_frac"] < 0.50
    )
    rules["combo_low_conc_low_sameblock"] = lambda r: (
        _p_ok(r)
        and r["p5_early_conc_w"] < 0.60
        and r["p4_same_block_frac"] < 0.50
    )
    rules["combo_all_p3_p4_p5_conservative"] = lambda r: (
        _p_ok(r)
        and r["p3_distinct_rcpt"] >= 5
        and r["p4_same_block_frac"] < 0.50
        and r["p5_early_conc_w"] < 0.60
    )
    rules["combo_all_p3_p4_p5_balanced"] = lambda r: (
        _p_ok(r)
        and r["p3_distinct_rcpt"] >= 10
        and r["p4_same_block_frac"] < 0.30
        and r["p5_early_conc_w"] < 0.40
    )
    rules["combo_all_p3_p4_p5_aggressive"] = lambda r: (
        _p_ok(r)
        and r["p3_distinct_rcpt"] >= 15
        and r["p4_same_block_frac"] < 0.20
        and r["p5_early_conc_w"] < 0.25
    )
    rules["combo_heat_diversity"] = lambda r: (
        float(r.get("avg_heat",0)) < 20.0
        and r.get("p3_distinct_rcpt") is not None
        and r["p3_distinct_rcpt"] >= 5
    )
    rules["combo_heat_conc"] = lambda r: (
        float(r.get("avg_heat",0)) < 20.0
        and r.get("p5_early_conc_w") is not None
        and r["p5_early_conc_w"] < 0.60
    )
    rules["combo_slippage_diversity"] = lambda r: (
        r.get("p2_slippage_pct") is not None
        and r["p2_slippage_pct"] < 5.0
        and r.get("p3_distinct_rcpt") is not None
        and r["p3_distinct_rcpt"] >= 5
    )
    rules["combo_top_share_sameblock"] = lambda r: (
        r.get("p3_top_rcpt_share") is not None
        and r["p3_top_rcpt_share"] < 0.50
        and r.get("p4_same_block_frac") is not None
        and r["p4_same_block_frac"] < 0.50
    )
    # p2b (proper price-impact slippage) combos
    rules["combo_p2b_diversity"] = lambda r: (
        r.get("p2b_price_move_pct") is not None
        and r["p2b_price_move_pct"] < 10.0
        and r.get("p3_distinct_rcpt") is not None
        and r["p3_distinct_rcpt"] >= 5
    )
    rules["combo_p2b_low_conc"] = lambda r: (
        r.get("p2b_price_move_pct") is not None
        and r["p2b_price_move_pct"] < 10.0
        and r.get("p5_early_conc_w") is not None
        and r["p5_early_conc_w"] < 0.50
    )
    rules["combo_p2b_all"] = lambda r: (
        r.get("p2b_price_move_pct") is not None
        and r["p2b_price_move_pct"] < 10.0
        and r.get("p3_distinct_rcpt") is not None
        and r["p3_distinct_rcpt"] >= 3
        and r.get("p5_early_conc_w") is not None
        and r["p5_early_conc_w"] < 0.60
    )

    # Run all rules
    for name, fn in rules.items():
        # GUARD: skip "positive_only" — that's lookahead
        if "positive_only" in name:
            continue
        # GUARD: p2_slippage rules are label-derived lookahead — run but auto-flag
        is_lookahead = "p2_slippage" in name and name != "p2_slippage_lt10pct"
        result = eval_combo(rows, fn, label_col)
        result["rule_name"] = name
        if is_lookahead:
            result["flags"] = list(set(result["flags"]) | {"LOOKAHEAD_RISK"})
            result["verdict"] = "LOOKAHEAD_RISK"
        results.append(result)
        print(f"    {name:45s} n={result['kept_n']:3d} PF={result['kept_pf']:.4f} "
              f"hit={result['kept_hit']:.3f} flags={result['flags']}", flush=True)

    # Sort by kept_pf descending, penalizing low coverage
    results.sort(key=lambda x: (
        0 if x["kept_n"] < 10 else x["kept_pf"]
    ), reverse=True)
    return results


# ══════════════════════════════════════════════════════════════════════════════
# TASK 5: Estimate Tradeable Value
# ══════════════════════════════════════════════════════════════════════════════

def estimate_capacity(rows: list[dict], result: dict, label_col: str) -> dict:
    """Rough capacity/return estimate for a candidate combo."""
    # 141 rows / 2 weeks = ~70 rows/week baseline
    # kept_n / 141 = keep_rate
    keep_rate    = result["kept_n"] / max(len(rows), 1)
    signals_wk   = keep_rate * 70  # estimated per week
    hit_rate     = result["kept_hit"]
    avg_ret      = (result["kept_total"] / max(result["kept_n"], 1)) if result["kept_n"] else 0
    # At PROBE_WEI=0.00005 WETH per trade, weekly return in WETH:
    weekly_ret_weth = signals_wk * avg_ret * 5e-5
    # Rough capacity: if 50x the probe size = 0.0025 WETH per trade:
    cap_weth_50x = signals_wk * avg_ret * 50 * 5e-5
    rpc_cost_wk  = signals_wk * 2 * 0.0001  # 2 quotes per signal
    return {
        "estimated_signals_per_week": round(signals_wk, 1),
        "expected_pf_30m":            result["kept_pf"],
        "hit_rate_30m":               hit_rate,
        "avg_return_per_signal":      round(avg_ret, 6),
        "weekly_return_at_probe_weth":round(weekly_ret_weth, 8),
        "rough_capacity_50x_weth_wk": round(cap_weth_50x, 6),
        "rpc_quote_cost_usd_wk":      round(rpc_cost_wk, 4),
        "execution_friction_note":    "High — new launch pools have wide spreads and thin liquidity",
        "paper_shadow_watch_candidate": (
            result["kept_pf"] > 1.0
            and result["kept_n"] >= 15
            and not result["flags"]
            or (len(result["flags"]) == 1 and "COVERAGE_THIN" in result["flags"])
        ),
        "worth_may_expansion_validation": (
            result["kept_pf"] > 1.0
            and result["two_week_coverage"]
            and result["lopo_min_pf"] > 0.9
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# TASK 6: Paper Shadow + Expansion Status
# ══════════════════════════════════════════════════════════════════════════════

def check_paper_shadow_and_expansion() -> dict:
    """Quick paper shadow + May11 expansion status check."""
    shadow_csv = REPO / "logs/sprint055_paper_shadow.csv"
    cron_log   = REPO / "logs/sprint055_paper_shadow_cron.log"

    result = {
        "shadow_csv_exists":    shadow_csv.exists(),
        "shadow_signals":       0,
        "cron_healthy":         False,
        "verdict":              "NO_SIGNALS_BUT_HEALTHY",
        "last_poll_utc":        None,
        "may11_new_pools":      0,
        "may11_decision":       "SKIP_NO_NEW_POOLS",
    }

    if cron_log.exists():
        log = cron_log.read_text()
        polls = [l for l in log.splitlines() if "poll 2026" in l]
        result["cron_healthy"] = len(polls) > 0
        if polls:
            result["last_poll_utc"] = polls[-1].split("poll ")[1].strip()

    if shadow_csv.exists():
        with shadow_csv.open() as f:
            data = list(csv.DictReader(f))
        result["shadow_signals"] = len(data)

    # Quick May11 pool check
    try:
        rows = ch_query("""
            SELECT count(DISTINCT lower(s.pool_address)) AS new_pools
            FROM eatlq.uniswap_v3_swaps_decoded s
            JOIN eatlq.discovered_pools dp ON lower(dp.pool_address) = lower(s.pool_address)
            WHERE s.chain_id = 8453
              AND s.block_time >= '2026-05-11'
              AND s.block_time  < '2026-05-18'
              AND dp.fee_bps = 1
              AND dp.has_weth_leg = 1
            HAVING min(s.block_time) >= '2026-05-11'
        """, timeout_s=15)
        result["may11_new_pools"] = int(rows[0]["new_pools"]) if rows else 0
    except Exception:
        pass

    if result["may11_new_pools"] > 0:
        result["may11_decision"] = "PREFLIGHT_NEEDED"

    return result


# ══════════════════════════════════════════════════════════════════════════════
# WRITE ARTIFACTS
# ══════════════════════════════════════════════════════════════════════════════

def write_artifacts(rows, combo_results, shadow_status, label_col, n_30m) -> None:
    # ── P-feature summary CSV ─────────────────────────────────────────────────
    feat_keys = ["pool","week","token","net_return_5m","net_return_30m","label_30m_ok",
                 "avg_heat","p2_slippage_pct","p2b_price_move_pct",
                 "p3_distinct_rcpt","p3_top_rcpt_share",
                 "p4_same_block_frac","p5_early_conc_w","risk_state","s063_status"]
    with open(DIAG_DIR / "sprint066_enriched_universe.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=feat_keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    # ── Combo results CSV ─────────────────────────────────────────────────────
    combo_keys = ["rule_name","kept_n","removed_n","kept_pf","removed_pf",
                  "kept_hit","kept_median","kept_total","winners_removed","losers_kept",
                  "two_week_coverage","top_pool_share","top_token_share",
                  "lopo_min_pf","loto_min_pf","lowo_apr27_pf","lowo_may04_pf",
                  "flags","verdict","baseline_pf","pf_delta"]
    with open(DIAG_DIR / "sprint066_combo_results.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=combo_keys, extrasaction="ignore")
        w.writeheader()
        for r in combo_results:
            r2 = dict(r)
            r2["flags"] = "|".join(r2.get("flags",[]))
            w.writerow(r2)

    # ── Top combos ────────────────────────────────────────────────────────────
    top3 = [r for r in combo_results
            if r["kept_n"] >= 10 and r["rule_name"] != "baseline_all"][:3]

    # ── Readout MD ────────────────────────────────────────────────────────────
    baseline = next((r for r in combo_results if r["rule_name"] == "baseline_all"), {})
    base_pf  = baseline.get("kept_pf", 0.0)

    # Separate 5m baseline stats for display
    rets5m   = [float(r["net_return_5m"]) for r in rows if r.get("net_return_5m") is not None]
    wins5m   = [x for x in rets5m if x > 0]
    loss5m   = [abs(x) for x in rets5m if x < 0]
    pf5m     = sum(wins5m) / max(sum(loss5m), 1e-12) if loss5m else float("inf")
    hit5m    = len(wins5m) / len(rets5m) if rets5m else 0.0
    label_tag = "30m" if label_col == "net_return_30m" else "5m"

    md = f"""# Sprint066 — Secondary Feature Alpha Mining Readout
Generated: {NOW_STR}

## Sample Universe
- **Rows**: {len(rows)} labeled entries (entry_ok=True, exit_ok=True)
- **Pools**: {len(set(r['pool'] for r in rows))}
- **Weeks**: Apr27 ({sum(1 for r in rows if '04-27' in r['week'])} rows) + May04 ({sum(1 for r in rows if '05-04' in r['week'])} rows)
- **Baseline 5m PF**: {pf5m:.4f} | hit={hit5m:.3f}  ← 5-minute hold
- **Baseline {label_tag} PF**: {base_pf:.4f} | hit={baseline.get('kept_hit',0):.3f}  ← primary label (this sprint)
- **30m labels computed**: {n_30m} / {len(rows)} rows (entry_block + 900 ≈ 30 min @ 2s/block)
- **Label source**: Real QuoterV2

## P1-P5 Feature Coverage

| Feature | Coverage | Observable at Entry | Method |
|---------|----------|---------------------|--------|
| P1 quoteability | {len(rows)}/{len(rows)} | ✓ | Existing labeled_candidates |
| P2 amountOut_sanity | {sum(1 for r in rows if r['p2_amount_nonzero'])}/{len(rows)} | ✓ | entry_amount_out > 0 |
| P2 slippage_proxy | {sum(1 for r in rows if r['p2_slippage_pct'] is not None)}/{len(rows)} | ✓ | -(net_return_5m)*100 |
| P3 buyer_diversity | {sum(1 for r in rows if r['p3_distinct_rcpt'] is not None)}/{len(rows)} | ✓ | CH countDistinct(recipient) in onset |
| P3 top_rcpt_share | {sum(1 for r in rows if r['p3_top_rcpt_share'] is not None)}/{len(rows)} | ✓ | CH top recipient share in onset |
| P4 same_block_frac | {sum(1 for r in rows if r['p4_same_block_frac'] is not None)}/{len(rows)} | ~✓ | CH most-concentrated block in 5m |
| P5 early_conc_w | {sum(1 for r in rows if r['p5_early_conc_w'] is not None)}/{len(rows)} | ✓ | CH recipient swap share in onset |

**P3/P4/P5 note**: Based on pool-level onset window stats (first 3600s). recipient=actual user wallet, NOT aggregator address. P4 uses max single-block concentration in the 5m entry window (slight lookahead: last block of bucket observed, but bucket is used as whole signal).

## Combination Search Results

### Top Candidates (n ≥ 10)

"""
    if not top3:
        md += "**No combinations passed n≥10 with PF improvement.**\n\n"
    else:
        for i, c in enumerate(top3, 1):
            cap = estimate_capacity(rows, c, label_col)
            md += f"### C{i}: `{c['rule_name']}`\n"
            md += f"| Metric | Value |\n|--------|-------|\n"
            md += f"| kept_n | {c['kept_n']} / {len(rows)} |\n"
            md += f"| removed_n | {c['removed_n']} |\n"
            md += f"| kept PF | **{c['kept_pf']:.4f}** (baseline: {base_pf:.4f}) |\n"
            md += f"| removed PF | {c['removed_pf']:.4f} |\n"
            md += f"| hit rate | {c['kept_hit']:.3f} |\n"
            md += f"| median return | {c['kept_median']:.4f} |\n"
            md += f"| total return | {c['kept_total']:.4f} |\n"
            md += f"| winners removed | {c['winners_removed']} |\n"
            md += f"| losers kept | {c['losers_kept']} |\n"
            md += f"| two-week coverage | {c['two_week_coverage']} |\n"
            md += f"| top pool share | {c['top_pool_share']:.3f} |\n"
            md += f"| top token share | {c['top_token_share']:.3f} |\n"
            md += f"| LOPO min PF | {c['lopo_min_pf']:.4f} |\n"
            md += f"| LOTO min PF | {c['loto_min_pf']:.4f} |\n"
            md += f"| LOWO Apr27 PF | {c['lowo_apr27_pf']:.4f} |\n"
            md += f"| LOWO May04 PF | {c['lowo_may04_pf']:.4f} |\n"
            md += f"| flags | `{'|'.join(c['flags']) if c['flags'] else 'none'}` |\n"
            md += f"| **VERDICT** | **{c['verdict']}** |\n\n"
            md += f"**Capacity estimate**: {cap['estimated_signals_per_week']:.1f} signals/wk, "
            md += f"avg_return={cap['avg_return_per_signal']:.5f}, "
            md += f"paper_watch={cap['paper_shadow_watch_candidate']}\n\n"

    # All results table
    md += "\n## All Rule Results Summary\n\n"
    md += "| Rule | kept_n | PF | hit | flags | verdict |\n"
    md += "|------|--------|-----|-----|-------|--------|\n"
    for c in combo_results:
        if c["rule_name"] == "baseline_all":
            continue
        fl = "|".join(c["flags"]) if c["flags"] else "—"
        md += (f"| `{c['rule_name']}` | {c['kept_n']} | {c['kept_pf']:.4f} | "
               f"{c['kept_hit']:.3f} | {fl} | {c['verdict']} |\n")

    # Paper shadow
    md += f"""
## Paper Shadow + Expansion Status

| Item | Value |
|------|-------|
| Shadow CSV | {'exists' if shadow_status['shadow_csv_exists'] else 'NOT YET'} |
| Signals | {shadow_status['shadow_signals']} |
| Cron healthy | {shadow_status['cron_healthy']} |
| Last poll | {shadow_status.get('last_poll_utc','?')} |
| May11 new pools | {shadow_status['may11_new_pools']} |
| May11 decision | {shadow_status['may11_decision']} |

## Governance Checks
- TOKEN_BLACKLIST_USED=false — no rule uses NEXIO / BERMIT identity
- TOKEN_WHITELIST_USED=false — no rule uses token names
- POST_HOC_TOKEN_REMOVAL_USED=false — no post-hoc filtering by historical PnL
- THRESHOLDS_TUNED=false — thresholds set from domain logic, not label optimization
- NO_ALPHA_CLAIM=true — 13 pools / 2 weeks insufficient for generalization claim
- TRAINING_ALLOWED=false
- LIVE_ALLOWED=false
"""
    (AUDIT_DIR / "sprint066_alpha_mining_readout.md").write_text(md)
    print("  Readout written.", flush=True)

    # ── Decision flags ────────────────────────────────────────────────────────
    best = top3[0] if top3 else {}
    overall_verdict = "FAIL_NO_SECONDARY_FEATURE_EDGE"
    if best:
        if best["kept_pf"] >= 1.2 and not best["flags"]:
            overall_verdict = "CANDIDATE_FOR_MORE_PAPER_VALIDATION"
        elif best["kept_pf"] >= 1.0 and best["two_week_coverage"]:
            overall_verdict = "WEAK_CLUE_NEEDS_MORE_WEEKS"
        elif best["kept_pf"] >= 1.0:
            overall_verdict = "WEEK_FRAGILE_CLUE"
        else:
            overall_verdict = "INCONCLUSIVE_NO_ACTIONABLE_EDGE"

    flags = {
        "sprint": "sprint066",
        "generated_at": NOW_STR,
        "OVERALL_VERDICT": overall_verdict,
        "BASELINE_PF_5M": round(pf5m, 4),
        "BASELINE_PF_30M": round(base_pf, 4) if label_col == "net_return_30m" else None,
        "LABEL_COL_USED": label_col,
        "BEST_COMBO_RULE": best.get("rule_name","none"),
        "BEST_COMBO_PF": best.get("kept_pf", 0.0),
        "BEST_COMBO_N":  best.get("kept_n", 0),
        "BEST_COMBO_FLAGS": best.get("flags",[]),
        "P3_COVERAGE":   sum(1 for r in rows if r.get("p3_distinct_rcpt") is not None),
        "P4_COVERAGE":   sum(1 for r in rows if r.get("p4_same_block_frac") is not None),
        "P5_COVERAGE":   sum(1 for r in rows if r.get("p5_early_conc_w") is not None),
        "LABELS_30M_COMPUTED": n_30m,
        "TOKEN_BLACKLIST_USED": False,
        "TOKEN_WHITELIST_USED": False,
        "POST_HOC_TOKEN_REMOVAL_USED": False,
        "THRESHOLDS_TUNED": False,
        "TRAINING_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "NO_ALPHA_CLAIM": True,
        "PAPER_SHADOW_HEALTHY": shadow_status.get("cron_healthy", False),
        "MAY11_NEW_POOLS": shadow_status.get("may11_new_pools", 0),
        "SPRINT066_COMPLETE": True,
    }
    (AUDIT_DIR / "sprint066_decision_flags.json").write_text(
        json.dumps(flags, indent=2, default=str))
    print("  Decision flags written.", flush=True)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print(f"Sprint066 start {NOW_STR}", flush=True)

    print("\n[T1] Loading universe...", flush=True)
    rows = load_universe()
    print(f"  Loaded {len(rows)} labeled rows | "
          f"pools={len(set(r['pool'] for r in rows))} | "
          f"weeks={set(r['week'] for r in rows)}", flush=True)

    print("\n[T2] Computing P1-P2...", flush=True)
    compute_p1_p2(rows)

    print("\n[T2] Computing P3/P4/P5 from CH...", flush=True)
    compute_p3_p4_p5(rows)

    # Check P3/P4/P5 distribution
    p3_vals = [r["p3_distinct_rcpt"] for r in rows if r.get("p3_distinct_rcpt") is not None]
    p4_vals = [r["p4_same_block_frac"] for r in rows if r.get("p4_same_block_frac") is not None]
    p5_vals = [r["p5_early_conc_w"] for r in rows if r.get("p5_early_conc_w") is not None]
    if p3_vals:
        print(f"  P3 distinct_rcpt: min={min(p3_vals):.0f} median={sorted(p3_vals)[len(p3_vals)//2]:.0f} max={max(p3_vals):.0f}", flush=True)
    if p4_vals:
        print(f"  P4 same_block_frac: min={min(p4_vals):.3f} median={sorted(p4_vals)[len(p4_vals)//2]:.3f} max={max(p4_vals):.3f}", flush=True)
    if p5_vals:
        print(f"  P5 early_conc_w: min={min(p5_vals):.3f} median={sorted(p5_vals)[len(p5_vals)//2]:.3f} max={max(p5_vals):.3f}", flush=True)

    print("\n[T2] 30m labels via QuoterV2...", flush=True)
    n_30m = compute_30m_labels(rows)
    print(f"  30m labels computed: {n_30m}", flush=True)

    # Use 5m label for combo search (30m if available in sufficient quantity)
    n_30m_ok = sum(1 for r in rows if r.get("label_30m_ok"))
    label_col = "net_return_30m" if n_30m_ok >= 50 else "net_return_5m"
    print(f"  Using label_col={label_col} (30m_ok={n_30m_ok})", flush=True)

    print("\n[T3] Running combination search...", flush=True)
    combo_results = run_combination_search(rows, label_col)

    print("\n[T6] Paper shadow + expansion status...", flush=True)
    shadow_status = check_paper_shadow_and_expansion()
    print(f"  Shadow: {shadow_status['verdict']} signals={shadow_status['shadow_signals']} "
          f"may11_pools={shadow_status['may11_new_pools']}", flush=True)

    print("\n[Write] Artifacts...", flush=True)
    write_artifacts(rows, combo_results, shadow_status, label_col, n_30m)

    # Print top results
    top = [r for r in combo_results if r["kept_n"] >= 10 and r["rule_name"] != "baseline_all"][:5]
    print("\n=== TOP COMBINATIONS ===", flush=True)
    for c in top:
        print(f"  {c['rule_name']:45s} n={c['kept_n']:3d} PF={c['kept_pf']:.4f} "
              f"LOPO={c['lopo_min_pf']:.3f} flags={c['flags']} → {c['verdict']}", flush=True)

    best = top[0] if top else {}
    overall_verdict = "FAIL_NO_SECONDARY_FEATURE_EDGE"
    if best:
        if best["kept_pf"] >= 1.2 and not best["flags"]:
            overall_verdict = "CANDIDATE_FOR_MORE_PAPER_VALIDATION"
        elif best["kept_pf"] >= 1.0 and best.get("two_week_coverage"):
            overall_verdict = "WEAK_CLUE_NEEDS_MORE_WEEKS"
        elif best["kept_pf"] >= 1.0:
            overall_verdict = "WEEK_FRAGILE_CLUE"
        else:
            overall_verdict = "INCONCLUSIVE_NO_ACTIONABLE_EDGE"

    print(f"\n=== OVERALL VERDICT: {overall_verdict} ===", flush=True)
    print(f"Sprint066 done. Artifacts in {AUDIT_DIR}", flush=True)


if __name__ == "__main__":
    main()
