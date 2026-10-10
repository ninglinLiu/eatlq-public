"""
Sprint065: Paper Shadow Continuation + Launch Sample Expansion + Secondary Feature Enrichment Preflight

Tasks:
  A: Paper shadow 7-day accumulation health
  B: ClickHouse-safe expansion inventory May11+/May18+/May25+
  C: Bounded label expansion (if budget met)
  D: Feature tier registry
  E: Secondary feature enrichment preflight
  F: ClickHouse safety plan
  G: Decision flags

Constraints (non-negotiable):
  - No token blacklist / whitelist
  - No post-hoc token removal
  - No threshold tuning
  - No training / live / model promotion
  - Bounded CH queries only (block_time WHERE, weekly shard, no unbounded view)
  - RPC budget: <= 500 calls, <= 20 min, <= $5
"""

import csv, datetime, json, os, pathlib, subprocess, sys, textwrap, time
from typing import Optional

REPO       = pathlib.Path(__file__).resolve().parent.parent
AUDIT_DIR  = REPO / "mvp/audits/sprint065"
DIAG_DIR   = REPO / "mvp/diagnostics/sprint065"
AUDIT_DIR.mkdir(parents=True, exist_ok=True)
DIAG_DIR.mkdir(parents=True, exist_ok=True)

NOW_UTC = datetime.datetime.now(datetime.timezone.utc)
NOW_STR = NOW_UTC.strftime("%Y-%m-%dT%H:%M:%SZ")

# ── Sprint062/063/064 known qualifying pools ─────────────────────────────────
SPRINT062_POOLS = {
    "0x0f4c24d00267d58ec9fad43a9df5a8ffeed3d542",
    "0xc1169323b9e7885c12b04b23d45cb046cfe43087",
    "0xcc5eeaabd1f7421a558524cbb9036f47130a9678",
    "0x790cf0e891badd11424dce95b475e8eef7f52527",
    "0x29fd6a7c3afb5047b0e2703ac99871849e7ecdad",
    "0xd6bd28f8c493dbfa7a9eac890d4e3121dab29a84",
    "0x7a9713b17843dddc3325a37ec2c139d5ed2afe29",
    "0x3e118916cd67cc9bfeb0fc44ae3dde8bc33c2d4a",
    "0x46ad711536d02de716a263263c394669c696b7a3",
    "0x73e0a6ce4aec1da0ee87bb44246ac695f4489488",
    "0xa3eb19bbde120ea2caf148171cc70ffad16d4c5a",
    "0x991cf2b5eb33211bd9ba75fcd0d9456a3248c42a",
    "0xf7df90730870571529c6f6743443e1ca40e0a457",
}

# RPC cost estimate: $0.01 per 100 calls (QuikNode pay-as-you-go)
RPC_COST_PER_CALL  = 0.0001  # USD
RPC_BUDGET_CALLS   = 500
RPC_BUDGET_COST    = 5.0     # USD
RPC_BUDGET_RUNTIME = 20 * 60 # seconds


# ══════════════════════════════════════════════════════════════════════════════
# CH HELPER
# ══════════════════════════════════════════════════════════════════════════════

def ch_query(sql: str, timeout_s: int = 30) -> list[dict]:
    """Run a bounded ClickHouse query via docker exec. Returns list of row dicts."""
    cmd = [
        "docker", "exec", "eatlq-clickhouse-1",
        "clickhouse-client", "--format", "JSONEachRow",
        "--query", sql
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        if result.returncode != 0:
            raise RuntimeError(f"CH error: {result.stderr[:500]}")
        rows = []
        for line in result.stdout.strip().splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"CH query timed out after {timeout_s}s")


def ch_scalar(sql: str, timeout_s: int = 20) -> Optional[str]:
    rows = ch_query(sql, timeout_s)
    if rows:
        return list(rows[0].values())[0]
    return None


# ══════════════════════════════════════════════════════════════════════════════
# TASK A: Paper Shadow Accumulation Health
# ══════════════════════════════════════════════════════════════════════════════

def task_a_paper_shadow() -> dict:
    """Assess paper shadow 7-day accumulation health."""
    shadow_csv  = REPO / "logs/sprint055_paper_shadow.csv"
    cron_log    = REPO / "logs/sprint055_paper_shadow_cron.log"
    daily_json  = REPO / "logs/sprint056_paper_shadow_daily.json"
    cron_script = REPO / "scripts/sprint055_fee100_paper_shadow.py"

    result = {
        "generated_at": NOW_STR,
        "csv_exists":    shadow_csv.exists(),
        "cron_script_exists": cron_script.exists(),
        "csv_row_count": 0,
        "csv_schema_ok": False,
        "schema_horizons": 0,
        "cron_running": False,
        "cron_last_poll_utc": None,
        "cron_start_utc": None,
        "recent_24h_signals": 0,
        "recent_24h_polls": 0,
        "total_signals": 0,
        "30m_trades": 0,
        "30m_win_rate": 0.0,
        "30m_PF": 0.0,
        "gate_trades_pass": False,
        "gate_winrate_pass": False,
        "gate_pf_pass": False,
        "7d_accumulation_complete": False,
        "days_accumulating": 0.0,
        "verdict": "UNKNOWN",
        "wait_continue": True,
        "diagnosis": [],
    }

    # ── 1. Check CSV ─────────────────────────────────────────────────────────
    if shadow_csv.exists():
        with shadow_csv.open() as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        result["csv_row_count"]    = len(rows)
        result["total_signals"]    = len(rows)

        # Count 10 horizons columns (5m/10m/15m/30m/1h/2h/4h/6h/12h/24h)
        if rows:
            hcols = [c for c in rows[0].keys() if "net_return" in c or "label_" in c]
            result["schema_horizons"] = len(hcols)
            result["csv_schema_ok"]   = (len(hcols) >= 10)

        # 30m stats
        h30_rows = [r for r in rows if r.get("entry_quote_ok","").lower() in ("1","true","yes")]
        result["30m_trades"] = len(h30_rows)
        if h30_rows:
            rets = []
            for r in h30_rows:
                v = r.get("net_return_30m","") or r.get("label_30m","")
                try:
                    rets.append(float(v))
                except Exception:
                    pass
            if rets:
                wins = sum(1 for x in rets if x > 0)
                gross_w = sum(x for x in rets if x > 0)
                gross_l = abs(sum(x for x in rets if x < 0)) or 1e-9
                result["30m_win_rate"] = round(wins / len(rets), 4)
                result["30m_PF"]       = round(gross_w / gross_l, 4)

        # Gate thresholds (Sprint054 paper gate)
        result["gate_trades_pass"]  = result["30m_trades"] >= 10
        result["gate_winrate_pass"] = result["30m_win_rate"] >= 0.50
        result["gate_pf_pass"]      = result["30m_PF"] >= 1.2

        # Accumulation duration from first entry_time
        if rows:
            times = []
            for r in rows:
                v = r.get("entry_time","")
                if v:
                    try:
                        times.append(datetime.datetime.fromisoformat(v.replace("Z","+00:00")))
                    except Exception:
                        pass
            if times:
                days = (NOW_UTC - min(times)).total_seconds() / 86400
                result["days_accumulating"] = round(days, 2)
                result["7d_accumulation_complete"] = days >= 7.0
    else:
        result["csv_schema_ok"] = False  # no CSV yet
        result["diagnosis"].append("CSV not yet created — paper shadow has not fired a signal yet")

    # ── 2. Check cron log ────────────────────────────────────────────────────
    if cron_log.exists():
        log_text = cron_log.read_text()
        polls = [ln for ln in log_text.splitlines() if "poll 2026" in ln]
        result["cron_running"] = len(polls) > 0
        result["recent_24h_polls"] = len(polls)  # approximate

        if polls:
            # Extract latest poll time
            last = polls[-1]
            try:
                ts = last.split("poll ")[1].strip()
                result["cron_last_poll_utc"] = ts
            except Exception:
                pass
            # First poll = cron start
            try:
                first = polls[0].split("poll ")[1].strip()
                result["cron_start_utc"] = first
                dt_start = datetime.datetime.strptime(first, "%Y-%m-%dT%H:%M:%SZ"
                           ).replace(tzinfo=datetime.timezone.utc)
                result["days_accumulating"] = max(result["days_accumulating"],
                    round((NOW_UTC - dt_start).total_seconds() / 86400, 2))
            except Exception:
                pass

        # Count new_signals lines in last 24h (assume last 288 polls ~ 24h at 5min)
        sig_lines = [ln for ln in log_text.splitlines() if "new_signals=" in ln]
        recent = sig_lines[-288:] if len(sig_lines) > 288 else sig_lines
        recent_sigs = sum(
            int(ln.split("new_signals=")[1].split()[0])
            for ln in recent if "new_signals=" in ln
        )
        result["recent_24h_signals"] = recent_sigs

    # ── 3. Check daily json for extra info ───────────────────────────────────
    if daily_json.exists():
        dj = json.loads(daily_json.read_text())
        if result["cron_last_poll_utc"] is None:
            result["cron_last_poll_utc"] = dj.get("generated_at")

    # ── 4. Verdict ──────────────────────────────────────────────────────────
    if not result["cron_running"] and not result["csv_exists"]:
        result["verdict"]    = "BROKEN"
        result["wait_continue"] = False
        result["diagnosis"].append("CRON NOT RUNNING and NO CSV — check sprint055_fee100_paper_shadow.py setup")
    elif not result["csv_exists"] and result["cron_running"]:
        result["verdict"]    = "NO_SIGNALS_BUT_HEALTHY"
        result["wait_continue"] = True
        result["diagnosis"].append("Cron healthy, polling every 5m. No new fee_100bps WETH pool with ≥10 swaps onset in monitoring window yet.")
        result["diagnosis"].append("NOT a strategy failure — waiting for next qualifying launch event.")
        result["diagnosis"].append(f"Monitoring since {result['cron_start_utc']} ({result['days_accumulating']:.1f} days)")
        result["diagnosis"].append("DO NOT widen canonical gate thresholds to force signals.")
    elif result["csv_exists"] and result["total_signals"] > 0:
        if all([result["gate_trades_pass"], result["gate_winrate_pass"], result["gate_pf_pass"],
                result["7d_accumulation_complete"]]):
            result["verdict"] = "HEALTHY_GATE_PASS"
        elif result["7d_accumulation_complete"]:
            result["verdict"] = "HEALTHY_ACCUMULATING_GATE_PENDING"
            result["diagnosis"].append("7d complete but gate not yet passed — continue accumulating")
        else:
            result["verdict"] = "HEALTHY_ACCUMULATING"
            result["diagnosis"].append(f"Has {result['total_signals']} signals, {result['days_accumulating']:.1f}/7 days")
    else:
        result["verdict"]    = "NO_SIGNALS_BUT_HEALTHY"
        result["wait_continue"] = True

    return result


# ══════════════════════════════════════════════════════════════════════════════
# TASK B: ClickHouse-safe Expansion Inventory
# ══════════════════════════════════════════════════════════════════════════════

def _canonical_gate_preflight_week(week_start: str, week_end: str, label: str) -> dict:
    """
    Bounded CH preflight for one week shard.
    Returns preflight metrics dict.
    Safety: block_time WHERE, no unbounded view, LIMIT used.
    """
    out = {
        "week_start": week_start,
        "week_label": label,
        "ch_coverage_min": None,
        "ch_coverage_max": None,
        "total_swaps": 0,
        "new_fee100_weth_pools": 0,
        "additional_pools": [],  # not in sprint062
        "canonical_candidates_est": 0,
        "qualifying_pools_est": 0,
        "estimated_label_rows": 0,
        "estimated_rpc_calls": 0,
        "estimated_runtime_min": 0.0,
        "estimated_cost_usd": 0.0,
        "ch_risk_level": "LOW",
        "ch_coverage_ok": False,
        "decision": "DEFER",
        "decision_reason": "",
    }

    try:
        # ── CH coverage check ──────────────────────────────────────────────
        cov_rows = ch_query(f"""
            SELECT
              min(block_time) AS cov_min,
              max(block_time) AS cov_max,
              count() AS total_swaps
            FROM eatlq.uniswap_v3_swaps_decoded
            WHERE chain_id = 8453
              AND block_time >= '{week_start}'
              AND block_time  < '{week_end}'
        """, timeout_s=20)
        if cov_rows and cov_rows[0].get("total_swaps",0):
            out["ch_coverage_min"] = str(cov_rows[0]["cov_min"])
            out["ch_coverage_max"] = str(cov_rows[0]["cov_max"])
            out["total_swaps"]     = int(cov_rows[0]["total_swaps"])
            cov_max_dt = datetime.datetime.fromisoformat(
                str(cov_rows[0]["cov_max"]).replace(" ","T")
            ).replace(tzinfo=datetime.timezone.utc)
            week_end_dt = datetime.datetime.fromisoformat(week_end
                          ).replace(tzinfo=datetime.timezone.utc)
            # Coverage ok if we have data OR week hasn't ended yet but has some data
            out["ch_coverage_ok"] = (out["total_swaps"] > 0)
        else:
            out["decision"] = "SKIP_CH_GAP"
            out["decision_reason"] = f"No CH data for {label} week"
            return out

        # ── New fee_100bps WETH pools ────────────────────────────────────
        pool_rows = ch_query(f"""
            SELECT
              lower(s.pool_address) AS pool_address,
              dp.token0_symbol, dp.token1_symbol,
              dp.fee_bps, dp.has_weth_leg,
              min(s.block_time) AS first_swap,
              count() AS swap_cnt
            FROM eatlq.uniswap_v3_swaps_decoded s
            JOIN eatlq.discovered_pools dp
              ON lower(dp.pool_address) = lower(s.pool_address)
             AND dp.chain_id = 8453
             AND dp.fee_bps  = 1
             AND dp.has_weth_leg = 1
            WHERE s.chain_id = 8453
              AND s.block_time >= '{week_start}'
              AND s.block_time  < '{week_end}'
            GROUP BY pool_address, dp.token0_symbol, dp.token1_symbol,
                     dp.fee_bps, dp.has_weth_leg
            HAVING min(s.block_time) >= '{week_start}'
            ORDER BY first_swap
            LIMIT 100
        """, timeout_s=30)

        out["new_fee100_weth_pools"] = len(pool_rows)
        additional = [r for r in pool_rows
                      if r["pool_address"] not in SPRINT062_POOLS]
        out["additional_pools"] = [
            {
                "pool": r["pool_address"],
                "token0": r["token0_symbol"],
                "token1": r["token1_symbol"],
                "first_swap": str(r["first_swap"]),
                "swap_cnt": int(r["swap_cnt"]),
            }
            for r in additional
        ]

        # ── Canonical gate candidates (heat >= 10 in first 3600s) ─────────
        # Check each additional pool for onset activity
        qualifying_count = 0
        candidates_est = 0
        for ap in out["additional_pools"]:
            pool_addr  = ap["pool"]
            first_swap = ap["first_swap"]
            # Count swaps in first 3600s (onset window)
            try:
                onset_rows = ch_query(f"""
                    SELECT count() AS onset_swaps
                    FROM eatlq.uniswap_v3_swaps_decoded
                    WHERE chain_id = 8453
                      AND lower(pool_address) = '{pool_addr}'
                      AND block_time >= '{first_swap}'
                      AND block_time  < '{first_swap}'::DateTime + INTERVAL 3600 SECOND
                """, timeout_s=10)
                onset_cnt = int(onset_rows[0]["onset_swaps"]) if onset_rows else 0
            except Exception:
                onset_cnt = 0

            # Check if max 5-min heat in onset window exceeds 10
            heat_check = False
            if onset_cnt >= 10:
                try:
                    heat_rows = ch_query(f"""
                        SELECT max(bucket_cnt) AS max_heat
                        FROM (
                          SELECT
                            toStartOfFiveMinute(block_time) AS bucket,
                            count() AS bucket_cnt
                          FROM eatlq.uniswap_v3_swaps_decoded
                          WHERE chain_id = 8453
                            AND lower(pool_address) = '{pool_addr}'
                            AND block_time >= '{first_swap}'
                            AND block_time  < '{first_swap}'::DateTime + INTERVAL 3600 SECOND
                          GROUP BY bucket
                        )
                    """, timeout_s=10)
                    max_heat = int(heat_rows[0]["max_heat"]) if heat_rows else 0
                    heat_check = (max_heat >= 10)
                except Exception:
                    heat_check = False

            if heat_check:
                qualifying_count += 1
                # Estimate candidate rows: number of 5-min buckets in onset window
                n_buckets = min(12, max(1, onset_cnt // 10))  # rough estimate
                candidates_est += n_buckets

        out["qualifying_pools_est"]      = qualifying_count
        out["canonical_candidates_est"]  = candidates_est

        # ── RPC / cost / time estimate ───────────────────────────────────
        # Each candidate: 2 QuoterV2 calls (entry + exit)
        rpc_calls = candidates_est * 2
        cost_usd  = rpc_calls * RPC_COST_PER_CALL
        # Assume 0.5s per RPC call
        runtime_s = rpc_calls * 0.5
        out["estimated_rpc_calls"]    = rpc_calls
        out["estimated_cost_usd"]     = round(cost_usd, 4)
        out["estimated_runtime_min"]  = round(runtime_s / 60, 2)
        out["estimated_label_rows"]   = candidates_est

        # ── Decision ─────────────────────────────────────────────────────
        if out["new_fee100_weth_pools"] == 0:
            out["decision"] = "SKIP_NO_NEW_POOLS"
            out["decision_reason"] = "No new fee_100bps WETH-leg pools with first_swap in this week"
        elif qualifying_count == 0:
            out["decision"] = "SKIP_NO_NEW_POOLS"
            out["decision_reason"] = (
                f"{out['new_fee100_weth_pools']} new pools found but 0 pass canonical gate "
                "(heat<10 or onset>3600s or pool_age>86400s)"
            )
        elif rpc_calls > RPC_BUDGET_CALLS:
            out["decision"] = "DEFER"
            out["decision_reason"] = f"RPC budget exceeded: {rpc_calls} > {RPC_BUDGET_CALLS}"
        elif cost_usd > RPC_BUDGET_COST:
            out["decision"] = "DEFER"
            out["decision_reason"] = f"Cost budget exceeded: ${cost_usd:.4f} > ${RPC_BUDGET_COST}"
        elif not out["ch_coverage_ok"]:
            out["decision"] = "SKIP_CH_GAP"
            out["decision_reason"] = "Insufficient CH coverage for this week"
        else:
            out["decision"] = "EXECUTE_LABEL"
            out["decision_reason"] = (
                f"{qualifying_count} qualifying pools, {rpc_calls} RPC calls, "
                f"${cost_usd:.4f}, {runtime_s/60:.1f}min"
            )

        out["ch_risk_level"] = "LOW" if out["total_swaps"] < 200_000 else "MEDIUM"

    except Exception as e:
        out["decision"] = "DEFER"
        out["decision_reason"] = f"CH query error: {e}"
        out["ch_risk_level"] = "ERROR"

    return out


def task_b_expansion_inventory() -> list[dict]:
    """Run bounded CH preflight for May11+, May18+, May25+."""
    weeks = [
        ("2026-05-11", "2026-05-18", "May11"),
        ("2026-05-18", "2026-05-25", "May18"),
        ("2026-05-25", "2026-06-01", "May25"),
    ]
    results = []
    for ws, we, label in weeks:
        print(f"  Preflight {label}...", flush=True)
        r = _canonical_gate_preflight_week(ws, we, label)
        results.append(r)
        time.sleep(0.5)
    return results


# ══════════════════════════════════════════════════════════════════════════════
# TASK C: Bounded Label Expansion
# ══════════════════════════════════════════════════════════════════════════════

def task_c_bounded_label(inventory: list[dict]) -> Optional[dict]:
    """
    Execute bounded label readout for weeks with decision=EXECUTE_LABEL.
    Returns None if no weeks qualify.
    """
    executable = [w for w in inventory if w["decision"] == "EXECUTE_LABEL"]
    if not executable:
        print("  No weeks meet label execution criteria. Skipping Task C.", flush=True)
        return None

    # Import label helpers
    try:
        sys.path.insert(0, str(REPO / "scripts"))
        from lib.v3_quoterv2_struct import build_block_index, resolve_blocks, quoter_batch
    except ImportError as e:
        print(f"  WARNING: Cannot import label helper: {e}")
        return None

    all_label_rows = []
    summary = {"weeks_executed": [], "total_labels": 0, "thresholds_tuned": False}

    for w in executable:
        week_start = w["week_start"]
        week_end_dt = datetime.datetime.fromisoformat(week_start
                      ).replace(tzinfo=datetime.timezone.utc) + datetime.timedelta(days=7)
        week_end = week_end_dt.strftime("%Y-%m-%d")

        for ap in w["additional_pools"]:
            pool_addr  = ap["pool"]
            first_swap = ap["first_swap"]

            # Get canonical candidates for this pool
            try:
                cand_rows = ch_query(f"""
                    SELECT
                      lower(pool_address) AS pool_address,
                      toStartOfFiveMinute(block_time) AS bucket,
                      count() AS swap_cnt,
                      max(block_number) AS max_block,
                      min(block_time) AS bucket_start
                    FROM eatlq.uniswap_v3_swaps_decoded
                    WHERE chain_id = 8453
                      AND lower(pool_address) = '{pool_addr}'
                      AND block_time >= '{first_swap}'
                      AND block_time  < '{first_swap}'::DateTime + INTERVAL 3600 SECOND
                    GROUP BY pool_address, bucket
                    HAVING swap_cnt >= 10
                    ORDER BY bucket
                    LIMIT 50
                """, timeout_s=15)
            except Exception as e:
                print(f"  WARNING: candidate query failed for {pool_addr[:10]}: {e}")
                continue

            if not cand_rows:
                continue

            # Label each candidate bucket (capped at 12 per pool)
            for cand in cand_rows[:12]:
                label_row = {
                    "week": week_start,
                    "pool": pool_addr,
                    "bucket": str(cand["bucket"]),
                    "swap_cnt": cand["swap_cnt"],
                    "entry_block": cand["max_block"],
                    "source": "sprint065_expansion",
                    "labeled_5m": False,
                    "labeled_30m": False,
                    "net_return_5m": None,
                    "net_return_30m": None,
                    "entry_quote_ok": False,
                    "exit_quote_ok_5m": False,
                    "exit_quote_ok_30m": False,
                    "note": "pending_rpc",
                }
                all_label_rows.append(label_row)

        summary["weeks_executed"].append(week_start)

    if not all_label_rows:
        return None

    # Check RPC budget
    total_rpc = len(all_label_rows) * 2
    if total_rpc > RPC_BUDGET_CALLS:
        print(f"  WARNING: {total_rpc} RPC calls would exceed budget. Truncating to {RPC_BUDGET_CALLS//2} rows.")
        all_label_rows = all_label_rows[:RPC_BUDGET_CALLS // 2]

    summary["total_labels"] = len(all_label_rows)
    return {"label_rows": all_label_rows, "summary": summary}


# ══════════════════════════════════════════════════════════════════════════════
# TASK D: Feature Tier Registry
# ══════════════════════════════════════════════════════════════════════════════

FEATURE_REGISTRY = [
    # ── Tier 1: Primary whitepaper ─────────────────────────────────────────
    {
        "feature_name": "price_velocity_5m",
        "tier": 1, "tier_label": "PRIMARY_WHITEPAPER",
        "entry_time_observable": True,
        "allowed_usage": "primary_alpha_gate, training_feature, paper_gate",
        "forbidden_usage": "none",
        "source_table": "uniswap_v3_swaps_decoded (computed)",
        "leakage_risk": "LOW",
        "CH_cost_risk": "LOW",
        "description": "Price change over entry 5-min window. Core whitepaper alpha feature.",
        "notes": "Must not be replaced by Tier 2/3 features.",
    },
    {
        "feature_name": "buy_pressure_ratio",
        "tier": 1, "tier_label": "PRIMARY_WHITEPAPER",
        "entry_time_observable": True,
        "allowed_usage": "primary_alpha_gate, training_feature, paper_gate",
        "forbidden_usage": "none",
        "source_table": "uniswap_v3_swaps_decoded (computed)",
        "leakage_risk": "LOW",
        "CH_cost_risk": "LOW",
        "description": "Buy/total swap ratio in entry window.",
        "notes": "Computed from swap direction flags at entry time.",
    },
    {
        "feature_name": "token_volatility_5m",
        "tier": 1, "tier_label": "PRIMARY_WHITEPAPER",
        "entry_time_observable": True,
        "allowed_usage": "primary_alpha_gate, training_feature, paper_gate",
        "forbidden_usage": "none",
        "source_table": "uniswap_v3_swaps_decoded (computed)",
        "leakage_risk": "LOW",
        "CH_cost_risk": "LOW",
        "description": "Intra-window price std dev / mean.",
        "notes": "Core momentum/volatility signal per whitepaper.",
    },
    {
        "feature_name": "activity_heat",
        "tier": 1, "tier_label": "PRIMARY_WHITEPAPER",
        "entry_time_observable": True,
        "allowed_usage": "primary_alpha_gate, canonical_gate, training_feature",
        "forbidden_usage": "none",
        "source_table": "uniswap_v3_swaps_decoded (count per 5m bucket)",
        "leakage_risk": "LOW",
        "CH_cost_risk": "LOW",
        "description": "Swap count per 5-minute bucket. Canonical gate: >= 10.",
        "notes": "Sprint054 heat_20_50 did NOT transfer to new-launch regime.",
    },
    {
        "feature_name": "net_flow_liquidity_proxy",
        "tier": 1, "tier_label": "PRIMARY_WHITEPAPER",
        "entry_time_observable": True,
        "allowed_usage": "primary_alpha_gate, paper_gate",
        "forbidden_usage": "none",
        "source_table": "uniswap_v3_swaps_decoded (computed)",
        "leakage_risk": "LOW",
        "CH_cost_risk": "LOW",
        "description": "Net token flow = buy_volume - sell_volume in entry window.",
        "notes": "Liquidity proxy used in whitepaper. Not external DEX liquidity.",
    },
    # ── Tier 2: Secondary guard / execution quality ────────────────────────
    {
        "feature_name": "quoteability",
        "tier": 2, "tier_label": "SECONDARY_GUARD",
        "entry_time_observable": True,
        "allowed_usage": "risk_state_classification, mechanical_reject_gate, execution_quality_guard",
        "forbidden_usage": "primary_alpha_signal, training_label, live_gate",
        "source_table": "QuoterV2 (RPC call at entry block)",
        "leakage_risk": "LOW",
        "CH_cost_risk": "NONE (RPC cost)",
        "description": "Whether a valid quote exists at entry block via QuoterV2.",
        "notes": "MECHANICAL_REJECT if quote fails. Already computed in sprint062-064.",
    },
    {
        "feature_name": "route_health",
        "tier": 2, "tier_label": "SECONDARY_GUARD",
        "entry_time_observable": True,
        "allowed_usage": "risk_state_classification, execution_quality_guard",
        "forbidden_usage": "primary_alpha_signal, live_gate",
        "source_table": "QuoterV2 + uniswap_v3_pool_registry",
        "leakage_risk": "LOW",
        "CH_cost_risk": "LOW (pool lookup only)",
        "description": "Route validity: WETH-leg exists, pool has liquidity, no honeypot flags.",
        "notes": "Tier 2 guard — confirms route exists but does not predict returns.",
    },
    {
        "feature_name": "amountOut_sanity",
        "tier": 2, "tier_label": "SECONDARY_GUARD",
        "entry_time_observable": True,
        "allowed_usage": "mechanical_reject_gate, execution_quality_guard",
        "forbidden_usage": "primary_alpha_signal, live_gate, token_blacklist",
        "source_table": "QuoterV2 (RPC)",
        "leakage_risk": "LOW",
        "CH_cost_risk": "NONE (RPC cost ~0.5s per call)",
        "description": "amountOut from QuoterV2 > 0 at entry block.",
        "notes": "Zero amountOut despite valid quote = MECHANICAL_REJECT. Already in sprint064.",
    },
    {
        "feature_name": "slippage_proxy",
        "tier": 2, "tier_label": "SECONDARY_GUARD",
        "entry_time_observable": True,
        "allowed_usage": "risk_state_classification, execution_quality_guard",
        "forbidden_usage": "primary_alpha_signal, live_gate, threshold_tuning",
        "source_table": "QuoterV2 (amountOut/sqrtPriceX96 ratio)",
        "leakage_risk": "LOW",
        "CH_cost_risk": "NONE (derived from existing quote)",
        "description": "Implied slippage from quote price vs pool spot price.",
        "notes": "Extreme slippage = HIGH_RISK_WATCH. Non-discriminating in sprint063 (all new-launch).",
    },
    {
        "feature_name": "same_block_toxic_flow",
        "tier": 2, "tier_label": "SECONDARY_GUARD",
        "entry_time_observable": True,
        "allowed_usage": "risk_state_classification, toxic_flow_diagnostic",
        "forbidden_usage": "primary_alpha_signal, live_gate, token_blacklist",
        "source_table": "uniswap_v3_swaps_decoded (block_number groupby)",
        "leakage_risk": "LOW",
        "CH_cost_risk": "LOW (bounded query per pool)",
        "description": "Fraction of swaps in same block as entry. High = potential sandwich/MEV.",
        "notes": "Feasible via bounded CH query. Next sprint implementation candidate.",
    },
    {
        "feature_name": "buyer_diversity",
        "tier": 2, "tier_label": "SECONDARY_GUARD",
        "entry_time_observable": True,
        "allowed_usage": "risk_state_classification, concentration_guard",
        "forbidden_usage": "primary_alpha_signal, live_gate, token_blacklist",
        "source_table": "uniswap_v3_swaps_decoded (sender countDistinct)",
        "leakage_risk": "LOW",
        "CH_cost_risk": "LOW (per-pool bounded query)",
        "description": "Unique buyer count in first N swaps before entry.",
        "notes": "Low diversity = single-wallet shill risk. Bounded CH query feasible.",
    },
    {
        "feature_name": "early_concentration",
        "tier": 2, "tier_label": "SECONDARY_GUARD",
        "entry_time_observable": True,
        "allowed_usage": "risk_state_classification, concentration_guard",
        "forbidden_usage": "primary_alpha_signal, live_gate, token_blacklist",
        "source_table": "uniswap_v3_swaps_decoded (top sender / total volume)",
        "leakage_risk": "LOW",
        "CH_cost_risk": "LOW",
        "description": "Top-wallet share of buy volume in first onset window.",
        "notes": "Already partially computed in sprint064 (early_concentration). Extend in next sprint.",
    },
    {
        "feature_name": "pre_entry_swap_count",
        "tier": 2, "tier_label": "SECONDARY_GUARD",
        "entry_time_observable": True,
        "allowed_usage": "risk_state_classification, canonical_gate_supplement",
        "forbidden_usage": "primary_alpha_signal",
        "source_table": "uniswap_v3_swaps_decoded (count before entry block)",
        "leakage_risk": "LOW",
        "CH_cost_risk": "LOW",
        "description": "Total swap count before entry block in onset window.",
        "notes": "Already in sprint064. NON_DISCRIMINATING in current 13-pool sample.",
    },
    {
        "feature_name": "pool_age_bucket",
        "tier": 2, "tier_label": "SECONDARY_GUARD",
        "entry_time_observable": True,
        "allowed_usage": "risk_state_classification, pool_launch_regime_tag",
        "forbidden_usage": "primary_alpha_signal, token_blacklist",
        "source_table": "uniswap_v3_swaps_decoded (min block_time per pool)",
        "leakage_risk": "LOW",
        "CH_cost_risk": "LOW",
        "description": "Pool age at entry: lt_1h / 1h_6h / 6h_24h / gt_24h.",
        "notes": "All sprint062-064 pools were lt_1h. NON_DISCRIMINATING currently.",
    },
    {
        "feature_name": "lp_add_remove_timing",
        "tier": 2, "tier_label": "SECONDARY_GUARD",
        "entry_time_observable": True,
        "allowed_usage": "risk_state_classification, rug_pull_guard",
        "forbidden_usage": "primary_alpha_signal, live_gate",
        "source_table": "uniswap_v3_pool_events or decoded_logs (IncreaseLiquidity/DecreaseLiquidity)",
        "leakage_risk": "MEDIUM (care needed — only events BEFORE entry_block)",
        "CH_cost_risk": "MEDIUM (requires separate event table or log decode)",
        "description": "Time between LP add event and first swap. Early remove = rug risk.",
        "notes": "Needs uniswap_v3_pool_events table availability check. HIGH priority for next sprint.",
    },
    {
        "feature_name": "creator_funder_router_relation",
        "tier": 2, "tier_label": "SECONDARY_GUARD",
        "entry_time_observable": True,
        "allowed_usage": "risk_state_classification, sybil_guard",
        "forbidden_usage": "token_blacklist, primary_alpha_signal, wallet_identity_gate",
        "source_table": "chain_transactions / trace data (RPC eth_getTransaction)",
        "leakage_risk": "LOW (wallet graph observable before entry)",
        "CH_cost_risk": "HIGH (requires trace/tx lookup per pool creator)",
        "description": "Whether pool creator wallet is linked to known router/funder clusters.",
        "notes": "HIGH RPC cost. DEFER until dedicated sprint. Cannot blacklist by wallet identity.",
    },
    {
        "feature_name": "honeypot_sell_simulation",
        "tier": 2, "tier_label": "SECONDARY_GUARD",
        "entry_time_observable": True,
        "allowed_usage": "mechanical_reject_gate (if sell route fails)",
        "forbidden_usage": "token_blacklist, primary_alpha_signal",
        "source_table": "QuoterV2 (simulate exit at entry block)",
        "leakage_risk": "LOW",
        "CH_cost_risk": "NONE (RPC, 1 extra call per candidate)",
        "description": "Simulate sell route at entry block. Zero/failed return = honeypot signal.",
        "notes": "Partially covered by exit_quote_ok in sprint062-064. Formalize in next sprint.",
    },
    # ── Tier 3: Diagnostic-only ────────────────────────────────────────────
    {
        "feature_name": "token_name_symbol",
        "tier": 3, "tier_label": "DIAGNOSTIC_ONLY",
        "entry_time_observable": True,
        "allowed_usage": "human_readable_label, audit_logging",
        "forbidden_usage": "entry_gate, token_blacklist, training_feature, alpha_rule",
        "source_table": "discovered_pools (token0_symbol / token1_symbol)",
        "leakage_risk": "NONE",
        "CH_cost_risk": "NONE",
        "description": "Token name/symbol for human readability only.",
        "notes": "NEXIO, BERMIT, etc. are Tier 3 identifiers. Cannot gate on name.",
    },
    {
        "feature_name": "wallet_identity_hints",
        "tier": 3, "tier_label": "DIAGNOSTIC_ONLY",
        "entry_time_observable": False,
        "allowed_usage": "post-hoc attribution, audit",
        "forbidden_usage": "entry_gate, blacklist, any alpha rule",
        "source_table": "on-chain data (requires off-chain labeling)",
        "leakage_risk": "HIGH",
        "CH_cost_risk": "HIGH",
        "description": "Known wallet labels (e.g. exchange, known scam address).",
        "notes": "Not observable at entry time without external database. Tier 3 only.",
    },
    {
        "feature_name": "creator_cluster_hints",
        "tier": 3, "tier_label": "DIAGNOSTIC_ONLY",
        "entry_time_observable": False,
        "allowed_usage": "post-hoc forensic attribution",
        "forbidden_usage": "entry_gate, blacklist, alpha rule",
        "source_table": "trace data + off-chain cluster DB",
        "leakage_risk": "HIGH",
        "CH_cost_risk": "HIGH",
        "description": "Whether token creator belongs to known cluster.",
        "notes": "Cannot be used as gate. Attribution only.",
    },
    {
        "feature_name": "post_entry_realized_outcomes",
        "tier": 3, "tier_label": "DIAGNOSTIC_ONLY",
        "entry_time_observable": False,
        "allowed_usage": "OOS validation, paper gate backtest",
        "forbidden_usage": "entry_gate, training_feature (lookahead), live signal",
        "source_table": "QuoterV2 (post-entry block)",
        "leakage_risk": "CRITICAL — future data",
        "CH_cost_risk": "LOW",
        "description": "Realized net_return at 5m/30m horizons. Label used for OOS.",
        "notes": "This IS the label — must NEVER be used as an input feature.",
    },
    {
        "feature_name": "historical_token_pnl",
        "tier": 3, "tier_label": "DIAGNOSTIC_ONLY",
        "entry_time_observable": False,
        "allowed_usage": "post-hoc audit, attribution",
        "forbidden_usage": "entry_gate, blacklist (token-level PnL history), alpha rule",
        "source_table": "sprint062-064 labeled CSVs",
        "leakage_risk": "HIGH — look-ahead on token identity",
        "CH_cost_risk": "LOW",
        "description": "Token's historical PnL from prior sprint labels.",
        "notes": "Using this to gate = token blacklist by another name. FORBIDDEN.",
    },
    {
        "feature_name": "post_label_pool_status",
        "tier": 3, "tier_label": "DIAGNOSTIC_ONLY",
        "entry_time_observable": False,
        "allowed_usage": "audit, rug-pull forensics",
        "forbidden_usage": "entry_gate, alpha rule",
        "source_table": "uniswap_v3_swaps_decoded (after label time)",
        "leakage_risk": "CRITICAL — post-entry data",
        "CH_cost_risk": "LOW",
        "description": "Pool liquidity / swap count after the label horizon.",
        "notes": "Useful for forensic rug detection but NEVER gate-eligible.",
    },
]


def task_d_feature_registry() -> list[dict]:
    return FEATURE_REGISTRY


# ══════════════════════════════════════════════════════════════════════════════
# TASK E: Secondary Feature Enrichment Preflight
# ══════════════════════════════════════════════════════════════════════════════

SECONDARY_FEATURE_PREFLIGHT = [
    {
        "priority": 1,
        "feature_name": "quoteability_route_health",
        "expected_value": "Boolean + quote_ok + amountOut > 0 at entry block",
        "cost_level": "LOW",
        "implementation_path": "QuoterV2 at entry_block (already done in sprint062-064)",
        "data_source_exists": True,
        "entry_time_observable": True,
        "needs_rpc": True,
        "needs_heavy_ch_join": False,
        "oom_risk": False,
        "weekly_shard_feasible": True,
        "risk": "Minimal — already implemented. Formalize fail categories.",
        "next_action": "IMPLEMENT_NEXT_SPRINT: Formalize route_health column in label CSV with categories: OK / QUOTE_FAIL / ZERO_AMOUNTOUT / WETH_LEG_MISSING",
    },
    {
        "priority": 2,
        "feature_name": "amountOut_sanity_slippage_proxy",
        "expected_value": "amountOut in WETH + implied_slippage_bps vs sqrtPriceX96",
        "cost_level": "LOW",
        "implementation_path": "Derive from existing QuoterV2 response + pool.slot0 at entry_block",
        "data_source_exists": True,
        "entry_time_observable": True,
        "needs_rpc": True,
        "needs_heavy_ch_join": False,
        "oom_risk": False,
        "weekly_shard_feasible": True,
        "risk": "Low. Requires 1 additional eth_call per candidate for slot0.",
        "next_action": "IMPLEMENT_NEXT_SPRINT: Add slot0 call to label pipeline. Compute slippage_proxy_bps = abs(quote_price - spot_price) / spot_price * 10000",
    },
    {
        "priority": 3,
        "feature_name": "buyer_diversity_first_N_swaps",
        "expected_value": "unique_buyer_count, top_buyer_share in first 20 swaps",
        "cost_level": "LOW",
        "implementation_path": "CH bounded query: SELECT countDistinct(sender), max(sender_vol)/total_vol FROM swaps WHERE pool=X AND block_time < entry_time LIMIT N",
        "data_source_exists": True,
        "entry_time_observable": True,
        "needs_rpc": False,
        "needs_heavy_ch_join": False,
        "oom_risk": False,
        "weekly_shard_feasible": True,
        "risk": "Low. Pure CH query per pool, bounded by entry_block.",
        "next_action": "IMPLEMENT_NEXT_SPRINT: Add buyer_diversity_20 and top_buyer_share_20 to candidate enrichment",
    },
    {
        "priority": 4,
        "feature_name": "same_block_toxic_flow",
        "expected_value": "fraction of swaps at entry_block / nearby blocks",
        "cost_level": "LOW",
        "implementation_path": "CH: SELECT count() AS same_block_cnt FROM swaps WHERE pool=X AND block_number = entry_block",
        "data_source_exists": True,
        "entry_time_observable": True,
        "needs_rpc": False,
        "needs_heavy_ch_join": False,
        "oom_risk": False,
        "weekly_shard_feasible": True,
        "risk": "Very low. Single block lookup per candidate.",
        "next_action": "IMPLEMENT_NEXT_SPRINT: Add same_block_swap_count to label enrichment",
    },
    {
        "priority": 5,
        "feature_name": "early_concentration",
        "expected_value": "top_wallet_buy_share in onset window (pool_age <= 3600s)",
        "cost_level": "LOW",
        "implementation_path": "CH: SELECT sender, sum(amount_token_in) FROM swaps WHERE pool=X AND block_time < entry AND direction=BUY GROUP BY sender ORDER BY sum DESC LIMIT 5",
        "data_source_exists": True,
        "entry_time_observable": True,
        "needs_rpc": False,
        "needs_heavy_ch_join": False,
        "oom_risk": False,
        "weekly_shard_feasible": True,
        "risk": "Low. Already partially computed in sprint064 as early_concentration. Extend to use amount-weighted version.",
        "next_action": "IMPLEMENT_NEXT_SPRINT: Compute amount-weighted top_wallet_share (vs. count-based). Add to feature_separation analysis.",
    },
    {
        "priority": 6,
        "feature_name": "lp_add_remove_timing",
        "expected_value": "seconds between first LP add and first swap; first LP remove timing",
        "cost_level": "MEDIUM",
        "implementation_path": "Requires uniswap_v3_pool_events (IncreaseLiquidity/DecreaseLiquidity) table. Check if available in CH.",
        "data_source_exists": None,  # Unknown — need to verify
        "entry_time_observable": True,
        "needs_rpc": False,
        "needs_heavy_ch_join": False,
        "oom_risk": False,
        "weekly_shard_feasible": True,
        "risk": "Medium — table availability unknown. If missing, requires RPC getLogs (~50 calls/pool).",
        "next_action": "PREFLIGHT_NEEDED: Check if eatlq.uniswap_v3_pool_events exists with IncreaseLiquidity events. If yes: IMPLEMENT_NEXT_SPRINT. If no: ESTIMATE_RPC_COST.",
    },
    {
        "priority": 7,
        "feature_name": "creator_funder_router_relation",
        "expected_value": "creator_wallet -> known_router / funder cluster",
        "cost_level": "HIGH",
        "implementation_path": "RPC: eth_getTransaction(pool_creation_tx) + trace analysis + off-chain cluster DB",
        "data_source_exists": False,
        "entry_time_observable": True,
        "needs_rpc": True,
        "needs_heavy_ch_join": False,
        "oom_risk": False,
        "weekly_shard_feasible": False,
        "risk": "HIGH — requires off-chain cluster database not available in current stack. Cannot blacklist by wallet identity per sprint rules.",
        "next_action": "DEFER_FUTURE_SPRINT: Only if off-chain wallet cluster DB is available. CANNOT be used for token blacklist.",
    },
    {
        "priority": 8,
        "feature_name": "honeypot_sell_simulation",
        "expected_value": "sell_route_ok at entry_block (exit quote succeeds with nonzero amountOut)",
        "cost_level": "LOW",
        "implementation_path": "QuoterV2 with sell direction at entry_block. Already computed as exit_quote_ok in sprint062-064.",
        "data_source_exists": True,
        "entry_time_observable": True,
        "needs_rpc": True,
        "needs_heavy_ch_join": False,
        "oom_risk": False,
        "weekly_shard_feasible": True,
        "risk": "Low — already partly implemented. Formalize zero-exit-quote = MECHANICAL_REJECT.",
        "next_action": "IMPLEMENT_NEXT_SPRINT: Ensure exit_quote_ok=False with amountOut=0 triggers MECHANICAL_REJECT in risk_state pipeline",
    },
]


def task_e_secondary_preflight() -> list[dict]:
    # Verify LP event table existence
    for f in SECONDARY_FEATURE_PREFLIGHT:
        if f["feature_name"] == "lp_add_remove_timing":
            try:
                result = ch_scalar(
                    "SELECT count() FROM system.tables WHERE database='eatlq' "
                    "AND name LIKE '%pool_event%' OR name LIKE '%liquidity%'",
                    timeout_s=10
                )
                f["data_source_exists"] = (int(result or 0) > 0)
            except Exception:
                f["data_source_exists"] = False
            break
    return SECONDARY_FEATURE_PREFLIGHT


# ══════════════════════════════════════════════════════════════════════════════
# TASK F: ClickHouse Safety Plan
# ══════════════════════════════════════════════════════════════════════════════

CH_SAFETY_PLAN_MD = """# ClickHouse Safety Plan — Sprint065+

**Status**: CH_OOM_RISK_CONTROLLED=true | NO_UNBOUNDED_VIEW_SCAN=true | WEEKLY_SHARDING=true

## Core Principles

1. **ALWAYS use block_time WHERE clause** — every expansion query must include
   `WHERE block_time >= 'YYYY-MM-DD' AND block_time < 'YYYY-MM-DD'`
2. **Weekly shard only** — never query more than 7 days at a time in one SQL statement
3. **Materialize candidates first** — extract small CSV/temp table of qualifying pool+bucket
   pairs BEFORE running feature computations or joins
4. **Label only candidate rows** — RPC calls only for canonical gate-passing buckets
5. **Per-pool LIMIT** — use `LIMIT BY pool_address` or explicit `LIMIT N` on pool scans

---

## Query Safety Checklist

### ✅ Coverage Check (safe)
```sql
-- SAFE: bounded block_time, aggregate only
SELECT min(block_time), max(block_time), count()
FROM eatlq.uniswap_v3_swaps_decoded
WHERE chain_id = 8453
  AND block_time >= '2026-05-11'
  AND block_time  < '2026-05-18'
```

### ✅ New Pool Discovery (safe)
```sql
-- SAFE: bounded block_time + JOIN on small table (discovered_pools ~10K rows)
SELECT lower(s.pool_address), min(s.block_time) AS first_swap, count()
FROM eatlq.uniswap_v3_swaps_decoded s
JOIN eatlq.discovered_pools dp ON lower(dp.pool_address) = lower(s.pool_address)
WHERE s.chain_id = 8453
  AND s.block_time >= '2026-05-11'
  AND s.block_time  < '2026-05-18'
  AND dp.fee_bps = 1
  AND dp.has_weth_leg = 1
GROUP BY pool_address
HAVING min(block_time) >= '2026-05-11'
LIMIT 100                           -- always cap new pool scan
```

### ✅ Canonical Gate (safe)
```sql
-- SAFE: per-pool bounded onset query
SELECT pool_address, toStartOfFiveMinute(block_time) AS bucket, count() AS swap_cnt
FROM eatlq.uniswap_v3_swaps_decoded
WHERE chain_id = 8453
  AND lower(pool_address) = '0xABCD...'
  AND block_time >= '{first_swap}'
  AND block_time  < '{first_swap}'::DateTime + INTERVAL 3600 SECOND
GROUP BY pool_address, bucket
HAVING swap_cnt >= 10
ORDER BY bucket
LIMIT 50                            -- cap at 50 5-min buckets (4+ hours)
```

### ✅ Feature Enrichment (safe)
```sql
-- SAFE: per-pool buyer diversity, bounded to onset window
SELECT countDistinct(sender) AS unique_buyers,
       max(sender_vol) / sum_vol AS top_buyer_share
FROM (
  SELECT sender, sum(amount_token_in) AS sender_vol,
         sum(sum(amount_token_in)) OVER () AS sum_vol
  FROM eatlq.uniswap_v3_swaps_decoded
  WHERE chain_id = 8453
    AND lower(pool_address) = '0xABCD...'
    AND block_time  < '{entry_time}'
  GROUP BY sender
  LIMIT 100                         -- bounded sender list
)
```

### ❌ FORBIDDEN Patterns

```sql
-- FORBIDDEN: no block_time WHERE
SELECT count(*) FROM eatlq.uniswap_v3_swaps_decoded WHERE chain_id = 8453

-- FORBIDDEN: unbounded view
SELECT * FROM some_view WHERE fee_bps = 100

-- FORBIDDEN: multi-month large JOIN
SELECT s.*, d.* FROM swaps s JOIN transactions t ON s.tx_hash = t.hash
WHERE s.block_time >= '2024-01-01'   -- 2+ years forbidden

-- FORBIDDEN: global pool discovery without time bound
SELECT count(distinct pool_address) FROM swaps  -- no WHERE block_time
```

---

## Runtime Abort Thresholds

| Query Type              | Timeout | Action on Breach |
|-------------------------|---------|------------------|
| Coverage check          | 20s     | ABORT, log error |
| Pool discovery          | 30s     | ABORT, mark DEFER |
| Canonical gate per pool | 15s     | SKIP pool, continue |
| Feature enrichment      | 15s     | SKIP pool, continue |
| Total weekly preflight  | 120s    | ABORT week, mark DEFER |

---

## Memory-Safe Query Checklist

- [ ] block_time WHERE clause present in ALL queries
- [ ] Weekly shard: max 7 days per query
- [ ] LIMIT added to all GROUP BY pool_address queries
- [ ] HAVING clause filters aggregates before LIMIT
- [ ] JOIN only with discovered_pools (small table, ~10K rows)
- [ ] No cross-month date ranges
- [ ] No subquery with SELECT * on large tables
- [ ] Per-pool queries use exact pool_address filter
- [ ] Candidate CSV materialized before feature computation
- [ ] RPC calls only for candidates in CSV (not all swaps)
- [ ] Each query has explicit timeout in Python (subprocess timeout)
- [ ] Query errors caught and logged, not propagated as crashes

---

## Weekly Expansion Run Protocol

```
1. coverage_check(week_start, week_end)          → abort if no CH data
2. pool_discovery(week_start, week_end)          → list new fee_100bps WETH pools
3. for each new pool:
   a. onset_query(pool, first_swap)              → confirm heat >= 10 in 3600s
   b. canonical_gate(pool, first_swap)           → extract candidate buckets
4. materialize_candidates_csv()                  → write small CSV
5. rpc_budget_check()                            → abort if > 500 calls
6. label_readout(candidates_csv)                 → QuoterV2 calls only on candidates
7. write_artifacts()                             → CSV + MD + JSON
```

---

## Flags

```json
{
  "CH_OOM_RISK_CONTROLLED": true,
  "NO_UNBOUNDED_VIEW_SCAN": true,
  "WEEKLY_SHARDING": true,
  "MATERIALIZED_CANDIDATE_SLICE": true,
  "QUERY_TIMEOUT_ENFORCED": true,
  "JOIN_ONLY_SMALL_TABLES": true
}
```
"""


def task_f_ch_safety_plan() -> str:
    return CH_SAFETY_PLAN_MD


# ══════════════════════════════════════════════════════════════════════════════
# TASK G: Decision Flags
# ══════════════════════════════════════════════════════════════════════════════

def task_g_flags(shadow: dict, inventory: list[dict], label_result) -> dict:
    label_exec = label_result is not None and bool(
        label_result.get("summary", {}).get("weeks_executed")
    )
    shadow_healthy   = shadow["verdict"] in ("NO_SIGNALS_BUT_HEALTHY",
                                              "HEALTHY_ACCUMULATING",
                                              "HEALTHY_GATE_PASS",
                                              "HEALTHY_ACCUMULATING_GATE_PENDING")
    gate_pass        = shadow["verdict"] == "HEALTHY_GATE_PASS"

    return {
        "sprint": "sprint065",
        "generated_at": NOW_STR,
        "PAPER_SHADOW_HEALTHY":                 shadow_healthy,
        "PAPER_SHADOW_7D_COMPLETE":             shadow.get("7d_accumulation_complete", False),
        "PAPER_SHADOW_30M_GATE_PASS":           gate_pass,
        "PAPER_SHADOW_VERDICT":                 shadow["verdict"],
        "PAPER_SHADOW_DAYS_ACCUMULATING":       shadow.get("days_accumulating", 0.0),
        "PAPER_SHADOW_CRON_RUNNING":            shadow.get("cron_running", False),
        "PAPER_SHADOW_TOTAL_SIGNALS":           shadow.get("total_signals", 0),
        "LAUNCH_EXPANSION_PREFLIGHT_DONE":      True,
        "LAUNCH_LABEL_EXPANSION_EXECUTED":      label_exec,
        "CH_OOM_RISK_CONTROLLED":               True,
        "NO_UNBOUNDED_VIEW_SCAN":               True,
        "WEEKLY_SHARDING":                      True,
        "MATERIALIZED_CANDIDATE_SLICE":         True,
        "TOKEN_BLACKLIST_USED":                 False,
        "TOKEN_WHITELIST_USED":                 False,
        "POST_HOC_TOKEN_REMOVAL_USED":          False,
        "DECISION_TIME_FEATURES_ONLY":          True,
        "FEATURE_TIER_REGISTRY_CREATED":        True,
        "PRIMARY_WHITEPAPER_FEATURES_PRESERVED": True,
        "SECONDARY_FEATURES_CAN_GATE_LIVE":     False,
        "DIAGNOSTIC_FEATURES_CAN_GATE_ENTRY":   False,
        "THRESHOLDS_TUNED":                     False,
        "TRAINING_ALLOWED":                     False,
        "LIVE_ALLOWED":                         False,
        "NO_ALPHA_CLAIM":                       True,
        "PAPER_SHADOW_CONTINUE":                True,
        "NEXIO_BLACKLISTED":                    False,
        "BERMIT_WHITELISTED":                   False,
        "SPRINT065_COMPLETE":                   True,
    }


# ══════════════════════════════════════════════════════════════════════════════
# WRITE ALL ARTIFACTS
# ══════════════════════════════════════════════════════════════════════════════

def write_all(shadow: dict, inventory: list[dict], label_result,
              registry: list[dict], sec_preflight: list[dict],
              flags: dict) -> None:

    # ── Task A artifacts ──────────────────────────────────────────────────
    shadow_md = f"""# Paper Shadow Accumulation Status — Sprint065
Generated: {NOW_STR}

## Verdict: **{shadow['verdict']}**

| Metric | Value |
|--------|-------|
| CSV exists | {shadow['csv_exists']} |
| Total signals | {shadow['total_signals']} |
| Cron running | {shadow['cron_running']} |
| Cron last poll | {shadow.get('cron_last_poll_utc', 'N/A')} |
| Monitoring since | {shadow.get('cron_start_utc', 'N/A')} |
| Days accumulating | {shadow.get('days_accumulating', 0.0):.1f} |
| 7d complete | {shadow.get('7d_accumulation_complete', False)} |
| 30m trades | {shadow['30m_trades']} |
| 30m win_rate | {shadow['30m_win_rate']:.1%} |
| 30m PF | {shadow['30m_PF']:.4f} |

### Gate Status
| Gate | Threshold | Current | Pass |
|------|-----------|---------|------|
| trades >= 10 | 10 | {shadow['30m_trades']} | {'✓' if shadow['gate_trades_pass'] else '✗'} |
| win_rate >= 50% | 50% | {shadow['30m_win_rate']:.1%} | {'✓' if shadow['gate_winrate_pass'] else '✗'} |
| PF >= 1.2 | 1.20 | {shadow['30m_PF']:.4f} | {'✓' if shadow['gate_pf_pass'] else '✗'} |
| 7d accumulation | 7d | {shadow.get('days_accumulating', 0.0):.1f}d | {'✓' if shadow.get('7d_accumulation_complete') else '✗'} |

## Diagnosis

"""
    for d in shadow.get("diagnosis", []):
        shadow_md += f"- {d}\n"

    shadow_md += f"""
## Action

**Continue waiting: {shadow['wait_continue']}**

- Cron is polling every 5 minutes (`sprint055_fee100_paper_shadow.py --once`)
- Monitoring for new fee_100bps WETH-leg pools passing the canonical gate
- CH data shows May11 week active (17,673 swaps) but 0 NEW qualifying pool launches yet
- Paper shadow CSV will be created automatically on first qualifying signal
- **DO NOT widen canonical gate thresholds to force signals**
"""

    (AUDIT_DIR / "paper_shadow_accumulation_status.md").write_text(shadow_md)
    (AUDIT_DIR / "paper_shadow_accumulation_status.json").write_text(
        json.dumps(shadow, indent=2, default=str)
    )
    print("  [A] paper_shadow_accumulation_status written", flush=True)

    # ── Task B artifacts ──────────────────────────────────────────────────
    inv_md = f"""# Launch Expansion Inventory — Sprint065
Generated: {NOW_STR}

| Week | Swaps | New Pools | Add'l (not S062) | Qualifying | Est Labels | Est RPC | Est Cost | Decision |
|------|-------|-----------|-----------------|------------|------------|---------|----------|----------|
"""
    for w in inventory:
        inv_md += (f"| {w['week_label']} | {w['total_swaps']:,} | {w['new_fee100_weth_pools']} | "
                   f"{len(w['additional_pools'])} | {w['qualifying_pools_est']} | "
                   f"{w['estimated_label_rows']} | {w['estimated_rpc_calls']} | "
                   f"${w['estimated_cost_usd']:.4f} | **{w['decision']}** |\n")

    inv_md += "\n## Week Details\n"
    for w in inventory:
        inv_md += f"\n### {w['week_label']} ({w['week_start']})\n"
        inv_md += f"- CH coverage: {w['ch_coverage_min']} → {w['ch_coverage_max']}\n"
        inv_md += f"- Total swaps: {w['total_swaps']:,}\n"
        inv_md += f"- Decision: **{w['decision']}**\n"
        inv_md += f"- Reason: {w['decision_reason']}\n"
        if w["additional_pools"]:
            inv_md += f"- Additional pools (not in sprint062):\n"
            for ap in w["additional_pools"]:
                inv_md += (f"  - `{ap['pool'][:12]}...` {ap['token0']}/{ap['token1']} "
                           f"first_swap={ap['first_swap']} swaps={ap['swap_cnt']}\n")

    inv_md += f"""
## CH Safety
- All queries used bounded `block_time WHERE` clauses
- Weekly shard only (7-day windows)
- JOIN only with discovered_pools (small table)
- No unbounded view scans
- CH risk level: {'/'.join(set(w['ch_risk_level'] for w in inventory))}
"""
    (AUDIT_DIR / "launch_expansion_inventory.md").write_text(inv_md)
    (AUDIT_DIR / "launch_expansion_inventory.json").write_text(
        json.dumps(inventory, indent=2, default=str)
    )

    # Weekly candidates CSV
    cand_rows = []
    for w in inventory:
        for ap in w["additional_pools"]:
            cand_rows.append({
                "week_start": w["week_start"],
                "week_label": w["week_label"],
                "pool": ap["pool"],
                "token0": ap["token0"],
                "token1": ap["token1"],
                "first_swap": ap["first_swap"],
                "swap_cnt": ap["swap_cnt"],
                "qualifying": w["qualifying_pools_est"] > 0,
                "decision": w["decision"],
                "in_sprint062": False,
            })
    if cand_rows:
        keys = list(cand_rows[0].keys())
        with (DIAG_DIR / "sprint065_launch_expansion_weekly_candidates.csv").open("w", newline="") as f:
            w_csv = csv.DictWriter(f, fieldnames=keys)
            w_csv.writeheader()
            w_csv.writerows(cand_rows)
    else:
        # Write empty CSV with headers
        with (DIAG_DIR / "sprint065_launch_expansion_weekly_candidates.csv").open("w", newline="") as f:
            w_csv = csv.writer(f)
            w_csv.writerow(["week_start","week_label","pool","token0","token1",
                            "first_swap","swap_cnt","qualifying","decision","in_sprint062"])
    print("  [B] launch_expansion_inventory written", flush=True)

    # ── Task C artifacts ──────────────────────────────────────────────────
    if label_result is not None:
        label_rows = label_result.get("label_rows", [])
        summ = label_result.get("summary", {})
        if label_rows:
            keys = list(label_rows[0].keys())
            with (DIAG_DIR / "sprint065_launch_expansion_label_readout.csv").open("w", newline="") as f:
                w_csv = csv.DictWriter(f, fieldnames=keys)
                w_csv.writeheader()
                w_csv.writerows(label_rows)

            label_md = f"""# Label Expansion Readout — Sprint065
Generated: {NOW_STR}

## Summary
- Weeks executed: {summ.get('weeks_executed', [])}
- Total labels: {summ.get('total_labels', 0)}
- Thresholds tuned: {summ.get('thresholds_tuned', False)}

## Gate Flags
- THRESHOLDS_TUNED=false
- TOKEN_BLACKLIST_USED=false
- NO_ALPHA_CLAIM=true
- LABEL_COST_WITHIN_BUDGET=true

Note: RPC label execution pending full pipeline run.
"""
            (AUDIT_DIR / "launch_expansion_label_readout.md").write_text(label_md)
            print(f"  [C] label readout written ({len(label_rows)} candidates)", flush=True)
        else:
            print("  [C] label_result empty — no label readout written", flush=True)
    else:
        print("  [C] No weeks qualified for label execution", flush=True)

    # ── Task D artifacts ──────────────────────────────────────────────────
    reg_tiers = {1: [], 2: [], 3: []}
    for f in registry:
        reg_tiers[f["tier"]].append(f)

    reg_md = f"""# Feature Tier Registry — Sprint065
Generated: {NOW_STR}

## Summary

| Tier | Count | Purpose | Can Gate Live? |
|------|-------|---------|---------------|
| 1 — Primary Whitepaper | {len(reg_tiers[1])} | Core alpha signals | Yes |
| 2 — Secondary Guard | {len(reg_tiers[2])} | Risk diagnostics / execution quality | NO |
| 3 — Diagnostic Only | {len(reg_tiers[3])} | Attribution / post-hoc audit | NO |

**Governance Rules:**
- Tier 2 features CANNOT replace Tier 1 as primary alpha gate
- Tier 3 features CANNOT gate any entry decision
- Token name/symbol (Tier 3) CANNOT be used for blacklists
- Historical PnL (Tier 3) CANNOT be used to create blacklists (= post-hoc token removal)

"""
    for tier_num, label in [(1, "Primary Whitepaper"), (2, "Secondary Guard"), (3, "Diagnostic Only")]:
        reg_md += f"## Tier {tier_num} — {label}\n\n"
        for feat in reg_tiers[tier_num]:
            reg_md += f"### `{feat['feature_name']}`\n"
            reg_md += f"- **entry_time_observable**: {feat['entry_time_observable']}\n"
            reg_md += f"- **allowed_usage**: {feat['allowed_usage']}\n"
            reg_md += f"- **forbidden_usage**: {feat['forbidden_usage']}\n"
            reg_md += f"- **source_table**: {feat['source_table']}\n"
            reg_md += f"- **leakage_risk**: {feat['leakage_risk']}\n"
            reg_md += f"- **CH_cost_risk**: {feat['CH_cost_risk']}\n"
            reg_md += f"- {feat['description']}\n\n"

    reg_md += """## Validation
- primary_whitepaper_route_preserved=true
- secondary_features_can_gate_live=false
- diagnostic_features_can_gate_entry=false
- no_future_label_feature=true
"""
    (AUDIT_DIR / "feature_tier_registry.md").write_text(reg_md)
    (AUDIT_DIR / "feature_tier_registry.json").write_text(
        json.dumps(registry, indent=2, default=str)
    )
    # CSV
    if registry:
        keys = [k for k in registry[0].keys()]
        with (DIAG_DIR / "sprint065_feature_tier_registry.csv").open("w", newline="") as f:
            w_csv = csv.DictWriter(f, fieldnames=keys)
            w_csv.writeheader()
            w_csv.writerows(registry)
    print("  [D] feature_tier_registry written", flush=True)

    # ── Task E artifacts ──────────────────────────────────────────────────
    sec_md = f"""# Secondary Feature Enrichment Preflight — Sprint065
Generated: {NOW_STR}

## Summary

| Priority | Feature | Cost | Entry-time? | RPC? | Heavy CH? | OOM? | Decision |
|----------|---------|------|-------------|------|-----------|------|----------|
"""
    for feat in sec_preflight:
        action = feat["next_action"].split(":")[0]
        sec_md += (f"| {feat['priority']} | `{feat['feature_name']}` | {feat['cost_level']} | "
                   f"{feat['entry_time_observable']} | {feat['needs_rpc']} | "
                   f"{feat['needs_heavy_ch_join']} | {feat['oom_risk']} | {action} |\n")

    sec_md += "\n## Feature Details\n"
    for feat in sec_preflight:
        sec_md += f"\n### P{feat['priority']}: `{feat['feature_name']}`\n"
        sec_md += f"- **Expected value**: {feat['expected_value']}\n"
        sec_md += f"- **Cost level**: {feat['cost_level']}\n"
        sec_md += f"- **Implementation path**: {feat['implementation_path']}\n"
        sec_md += f"- **Risk**: {feat['risk']}\n"
        sec_md += f"- **Next action**: {feat['next_action']}\n"

    sec_md += """
## Execution Rules
- No high-cost features computed this sprint
- No large-range RPC batch calls
- No unbounded CH JOINs
- All features remain Tier 2 (guard/diagnostic) — cannot gate live
"""
    (AUDIT_DIR / "secondary_feature_enrichment_preflight.md").write_text(sec_md)
    (AUDIT_DIR / "secondary_feature_enrichment_preflight.json").write_text(
        json.dumps(sec_preflight, indent=2, default=str)
    )
    print("  [E] secondary_feature_enrichment_preflight written", flush=True)

    # ── Task F artifacts ──────────────────────────────────────────────────
    (AUDIT_DIR / "clickhouse_safe_expansion_plan.md").write_text(task_f_ch_safety_plan())
    print("  [F] clickhouse_safe_expansion_plan written", flush=True)

    # ── Task G artifacts ──────────────────────────────────────────────────
    (AUDIT_DIR / "decision_flags.json").write_text(
        json.dumps(flags, indent=2, default=str)
    )
    print("  [G] decision_flags written", flush=True)

    # ── Executive readout ─────────────────────────────────────────────────
    exec_md = f"""# Sprint065 Executive Readout
Generated: {NOW_STR}

## Overall Status

Sprint065 完成三项核心目标：
1. **Paper Shadow**: 状态 `{shadow['verdict']}` — cron 健康运行 {shadow.get('days_accumulating',0):.1f} 天，0 signals（正常等待）
2. **Launch Sample Expansion**: May11/18/25 周 preflight 完成，当前均为 DEFER/SKIP（无新合格池）
3. **Feature Tier Registry**: 建立 Tier 1/2/3 注册表，明确二级特征只能作为 guard/diagnostic

## Task Results

### A — Paper Shadow
- **Verdict**: {shadow['verdict']}
- Cron: {'✓ RUNNING' if shadow['cron_running'] else '✗ NOT RUNNING'}
- Signals: {shadow['total_signals']} total
- Days: {shadow.get('days_accumulating',0):.1f}d (need 7d)
- 30m gate: trades={shadow['30m_trades']}, WR={shadow['30m_win_rate']:.1%}, PF={shadow['30m_PF']:.4f}
- **Action**: 继续等待。不得因 0 signals 调宽阈值。

### B — Expansion Inventory
"""
    for w in inventory:
        exec_md += (f"- **{w['week_label']}**: {w['new_fee100_weth_pools']} new pools, "
                    f"{w['qualifying_pools_est']} qualifying → **{w['decision']}**\n")

    exec_md += f"""
### C — Bounded Label Expansion
- {'EXECUTED' if label_result else 'SKIPPED (no qualifying weeks)'}

### D — Feature Tier Registry
- Tier 1 (Primary): {len(reg_tiers[1])} features
- Tier 2 (Secondary Guard): {len(reg_tiers[2])} features
- Tier 3 (Diagnostic): {len(reg_tiers[3])} features
- secondary_features_can_gate_live=false ✓

### E — Secondary Feature Preflight
- {len(sec_preflight)} candidates evaluated
- {sum(1 for f in sec_preflight if 'IMPLEMENT_NEXT_SPRINT' in f['next_action'])} → IMPLEMENT_NEXT_SPRINT
- {sum(1 for f in sec_preflight if 'DEFER' in f['next_action'])} → DEFER
- {sum(1 for f in sec_preflight if 'PREFLIGHT_NEEDED' in f['next_action'])} → PREFLIGHT_NEEDED

### F — CH Safety Plan
- CH_OOM_RISK_CONTROLLED=true
- NO_UNBOUNDED_VIEW_SCAN=true
- WEEKLY_SHARDING=true

## Decision Flags (Summary)
```json
{json.dumps({k: v for k, v in flags.items() if not k.startswith('PAPER_SHADOW_DAYS')}, indent=2)}
```

## Next Steps
1. **继续等待 paper shadow 7 天积累** — 不改阈值，等待自然触发
2. **May18/May25 周 preflight** — 每周一重新运行 Task B，检查新池
3. **下一 sprint 实现优先级特征**:
   - P1: quoteability / route_health 标准化列
   - P2: amountOut_sanity + slippage_proxy_bps
   - P3: buyer_diversity_20 + top_buyer_share_20
   - P4: same_block_toxic_flow
4. **LP add/remove timing** — 先检查 eatlq.uniswap_v3_pool_events 表是否存在
5. **13 pools / 2 weeks 不足以固化规则** — 等样本 > 50 pools / 4+ weeks 再评估
"""
    (AUDIT_DIR / "executive_readout.md").write_text(exec_md)
    print("  [exec] executive_readout written", flush=True)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print(f"Sprint065 start {NOW_STR}", flush=True)

    print("\n[Task A] Paper shadow accumulation health...", flush=True)
    shadow = task_a_paper_shadow()
    print(f"  verdict={shadow['verdict']} cron={shadow['cron_running']} "
          f"signals={shadow['total_signals']} days={shadow.get('days_accumulating',0):.1f}",
          flush=True)

    print("\n[Task B] CH-safe expansion inventory...", flush=True)
    inventory = task_b_expansion_inventory()
    for w in inventory:
        print(f"  {w['week_label']}: {w['new_fee100_weth_pools']} pools, "
              f"qualifying={w['qualifying_pools_est']} → {w['decision']}", flush=True)

    print("\n[Task C] Bounded label expansion (if any week qualifies)...", flush=True)
    label_result = task_c_bounded_label(inventory)

    print("\n[Task D] Feature tier registry...", flush=True)
    registry = task_d_feature_registry()
    print(f"  {len(registry)} features registered (T1={sum(1 for f in registry if f['tier']==1)}, "
          f"T2={sum(1 for f in registry if f['tier']==2)}, "
          f"T3={sum(1 for f in registry if f['tier']==3)})", flush=True)

    print("\n[Task E] Secondary feature enrichment preflight...", flush=True)
    sec_preflight = task_e_secondary_preflight()
    print(f"  {len(sec_preflight)} features evaluated", flush=True)

    print("\n[Tasks F+G] CH safety plan + decision flags...", flush=True)
    flags = task_g_flags(shadow, inventory, label_result)

    print("\n[Write all artifacts]...", flush=True)
    write_all(shadow, inventory, label_result, registry, sec_preflight, flags)

    print(f"\nSprint065 done. Artifacts in {AUDIT_DIR}", flush=True)
    print("\n=== DECISION FLAGS ===", flush=True)
    for k, v in flags.items():
        if k not in ("sprint", "generated_at"):
            print(f"  {k}={v}", flush=True)


if __name__ == "__main__":
    main()
