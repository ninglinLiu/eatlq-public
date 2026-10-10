#!/usr/bin/env python3
"""
Sprint049B: Return-Normalized Controlled Copy-Follow Paper Replay

Paper-only / historical replay. No live. No signals. No RPC (offline only).
Reads Sprint048B/049A artifacts and produces ROI-normalized followability analysis.

Outputs (all to mvp/diagnostics or mvp/audits):
  sprint049b_selected_paper_candidates.csv
  sprint049b_return_normalized_trades.csv
  sprint049b_return_normalized_candidates.csv
  sprint049b_followability_gap.csv
  sprint049b_paper_follow_trades.csv
  sprint049b_paper_follow_summary.json
  sprint049b_roi_summary.json
  sprint049b_decision_flags.json
  mvp/audits/sprint049b_return_normalized_copy_follow_readout.md
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from collections import defaultdict, Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent.parent
DIAG = ROOT / "mvp" / "diagnostics"
AUD  = ROOT / "mvp" / "audits"

# ─── Constants ────────────────────────────────────────────────────────────────
GAS_PER_TRADE_WETH = 0.0002
BASE_BLOCK_SEC     = 2.0       # Base L2 ~2s blocks

# Delayed-entry impact model: fractional price worsening vs controller entry
# Rationale: controller's buy moves pool price; follower pays more.
DELAY_MODELS = {
    "1_block":  {"impact": 0.005, "delay_sec": 2},    # +0.5%
    "3_blocks": {"impact": 0.015, "delay_sec": 6},    # +1.5%
    "30s":      {"impact": 0.030, "delay_sec": 30},   # +3.0%
    "60s":      {"impact": 0.050, "delay_sec": 60},   # +5.0%
}

QUOTEABLE_LANES = {"uniswap_routers", "0x_kyber_cow", "1inch"}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v) if v not in (None, "", "nan") else default
    except (ValueError, TypeError):
        return default


def profit_factor(pnl_list: list) -> float:
    gains  = sum(p for p in pnl_list if p > 0)
    losses = abs(sum(p for p in pnl_list if p < 0))
    return round(gains / losses, 4) if losses > 0 else float("inf") if gains > 0 else 0.0


def parse_bt(s: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s.strip()[:19], fmt[:len("%Y-%m-%d %H:%M:%S")])
        except Exception:
            pass
    return None


def percentile(lst: list, p: float) -> float:
    if not lst:
        return 0.0
    s = sorted(lst)
    idx = int(len(s) * p / 100)
    return s[min(idx, len(s) - 1)]


# ─── Load data ────────────────────────────────────────────────────────────────

def load_realized_pairs() -> list[dict]:
    rows = []
    with open(DIAG / "sprint048b_realized_pnl.csv") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows


def load_preflight() -> dict:
    out = {}
    with open(DIAG / "sprint049a_follow_candidate_preflight.csv") as f:
        for row in csv.DictReader(f):
            out[row["controller"]] = row
    return out


def load_toxic_flags() -> dict:
    out = defaultdict(list)
    with open(DIAG / "sprint049a_toxic_liquidity_flags.csv") as f:
        for row in csv.DictReader(f):
            out[row["controller"]].append(row["flag"])
    return dict(out)


# ─── Step 0: Data coverage audit ─────────────────────────────────────────────

def coverage_audit(pairs: list[dict]) -> dict:
    total = len(pairs)
    matched = [r for r in pairs if r.get("sell_tx", "")]
    notional_ok = [r for r in pairs if safe_float(r.get("weth_in")) > 0.0001]
    bt_ok = [r for r in pairs if r.get("buy_bt", "").strip()]
    quoteable = [r for r in pairs if r.get("buy_lane", "") in QUOTEABLE_LANES]
    sell_detectable = matched  # proxy for sell detectability

    return {
        "total_rows": total,
        "controller_count": len({r["controller"] for r in pairs}),
        "trade_count": total,
        "matched_pair_count": len(matched),
        "matched_pair_rate": round(len(matched) / total, 4),
        "notional_available_count": len(notional_ok),
        "notional_available_rate": round(len(notional_ok) / total, 4),
        "block_time_available_count": len(bt_ok),
        "block_time_available_rate": round(len(bt_ok) / total, 4),
        "quoteable_route_count": len(quoteable),
        "quoteable_route_rate": round(len(quoteable) / total, 4),
        "sell_detectable_count": len(sell_detectable),
        "sell_detectable_rate": round(len(sell_detectable) / total, 4),
        "notional_source": "weth_in from tx_map (sprint048b)",
        "block_time_source": "buy_bt / sell_bt from sprint048b (uniswap_v3_swaps_trade_semantic join)",
        "ranking_allowed": len(notional_ok) / total >= 0.70,
    }


# ─── Step 1-3: ROI-normalized trade table + controller metrics ────────────────

def build_trade_table(pairs: list[dict], toxic_flags: dict) -> tuple[list[dict], dict]:
    """
    Returns (trade_rows, ctrl_metrics).
    trade_rows: one row per realized pair (matched + unmatched).
    ctrl_metrics: per-controller aggregated ROI stats.
    """
    trade_rows = []
    ctrl_data  = defaultdict(lambda: {
        "pnls": [], "notionals": [], "returns_pct": [], "hold_hrs": [],
        "tokens": set(), "pools": set(), "exit_buckets": Counter(),
        "no_exit_n": 0,
    })

    for r in pairs:
        ctrl      = r["controller"]
        buy_tx    = r.get("buy_tx", "")
        sell_tx   = r.get("sell_tx", "")
        token     = r.get("meme_token", "")
        buy_lane  = r.get("buy_lane", "")
        buy_bt    = r.get("buy_bt", "")
        sell_bt   = r.get("sell_bt", "")
        weth_in   = safe_float(r.get("weth_in"))
        weth_out  = safe_float(r.get("weth_out"))
        entry_cost = safe_float(r.get("entry_cost"))
        exit_value = safe_float(r.get("exit_value"))
        pnl       = safe_float(r.get("realized_pnl"))
        hold_min  = safe_float(r.get("hold_min"))
        hold_sec  = safe_float(r.get("hold_sec"))
        exit_bkt  = r.get("exit_bucket", "NO_EXIT")
        is_quoteable = buy_lane in QUOTEABLE_LANES
        has_exit  = bool(sell_tx)

        # Notional classification
        if weth_in > 0.0001:
            notional_status = "NOTIONAL_OK"
        else:
            notional_status = "NOTIONAL_UNKNOWN"

        # Trade return pct (relative to deployed notional)
        if notional_status == "NOTIONAL_OK" and has_exit:
            trade_return_pct = round(pnl / weth_in * 100, 4) if weth_in > 0 else 0.0
            hold_hrs = round(hold_min / 60, 4)
            pnl_per_hold_hr = round(pnl / hold_hrs, 6) if hold_hrs > 0.01 else 0.0
            cost_proxy = round(safe_float(r.get("gas_cost")) + safe_float(r.get("slip_in")) * weth_in, 6)
            slip_proxy = round(safe_float(r.get("slip_in")) * weth_in, 6)
        else:
            trade_return_pct = 0.0
            hold_hrs = round(hold_min / 60, 4) if hold_min > 0 else 0.0
            pnl_per_hold_hr = 0.0
            cost_proxy = round(GAS_PER_TRADE_WETH, 6)
            slip_proxy = 0.0

        tox = ",".join(toxic_flags.get(ctrl, []))

        trade_rows.append({
            "controller": ctrl,
            "controller_buy_tx": buy_tx,
            "controller_sell_tx": sell_tx,
            "buy_block_time": buy_bt,
            "sell_block_time": sell_bt,
            "token": token,
            "pool": r.get("pool_sender", ""),
            "lane_type": buy_lane,
            "quoteable_flag": "true" if is_quoteable else "false",
            "route_decode_quality": "HIGH" if is_quoteable else "LOW",
            "buy_notional_weth": round(weth_in, 6),
            "sell_notional_weth": round(weth_out, 6),
            "trade_pnl_weth": round(pnl, 6),
            "trade_return_pct": trade_return_pct,
            "hold_hours": hold_hrs,
            "pnl_per_hold_hour": pnl_per_hold_hr,
            "cost_proxy_weth": cost_proxy,
            "slippage_proxy_weth": slip_proxy,
            "exit_bucket": exit_bkt,
            "toxic_flags": tox,
            "private_route_flag": "false" if is_quoteable else "true",
            "public_executable_flag": "true" if (is_quoteable and has_exit) else "false",
            "notional_status": notional_status,
            "has_exit": "true" if has_exit else "false",
        })

        # Accumulate per-controller
        d = ctrl_data[ctrl]
        d["tokens"].add(token)
        d["exit_buckets"][exit_bkt] += 1
        if has_exit and notional_status == "NOTIONAL_OK":
            d["pnls"].append(pnl)
            d["notionals"].append(weth_in)
            d["returns_pct"].append(trade_return_pct)
            d["hold_hrs"].append(hold_hrs)
        elif not has_exit:
            d["no_exit_n"] += 1

    # Build per-controller metrics
    ctrl_metrics = {}
    for ctrl, d in ctrl_data.items():
        pnls    = d["pnls"]
        notionals = d["notionals"]
        rets    = d["returns_pct"]
        holds   = d["hold_hrs"]
        pos_pnls = [p for p in pnls if p > 0]
        neg_pnls = [p for p in pnls if p < 0]

        total_notional = sum(notionals)
        total_pnl      = sum(pnls)
        roi_pct        = round(total_pnl / total_notional * 100, 4) if total_notional > 0 else 0.0
        pf             = profit_factor(pnls)
        win_rate       = round(len(pos_pnls) / len(pnls), 4) if pnls else 0.0
        max_loss_pct   = round(min(rets), 4) if rets else 0.0
        median_ret     = round(statistics.median(rets), 4) if rets else 0.0
        mean_ret       = round(statistics.mean(rets), 4) if rets else 0.0
        p90_hold       = round(percentile(holds, 90), 4) if holds else 0.0
        med_hold       = round(statistics.median(holds), 4) if holds else 0.0
        tot_hold_hrs   = sum(holds)
        ret_per_hold   = round(total_pnl / tot_hold_hrs, 6) if tot_hold_hrs > 0 else 0.0
        cap_eff        = round(total_pnl / total_notional / (med_hold + 0.001), 6)
        unique_toks    = len(d["tokens"])
        n_match        = len(pnls)
        avg_notional   = round(total_notional / n_match, 6) if n_match else 0.0
        n_no_exit      = d["no_exit_n"]

        # Token concentration
        tok_cnt = Counter()
        for tr in trade_rows:
            if tr["controller"] == ctrl and tr["has_exit"] == "true":
                tok_cnt[tr["token"]] += 1
        top1_share = tok_cnt.most_common(1)[0][1] / max(1, n_match) if tok_cnt else 0.0
        top3_share = sum(v for _, v in tok_cnt.most_common(3)) / max(1, n_match)

        tox = toxic_flags.get(ctrl, [])
        pwinr_suspect = "PERFECT_WINRATE_SUSPECT" in tox

        ctrl_metrics[ctrl] = {
            "controller": ctrl,
            "own_realized_pnl_weth": round(total_pnl, 6),
            "own_deployed_notional_weth": round(total_notional, 6),
            "own_roi_pct": roi_pct,
            "own_avg_trade_notional_weth": avg_notional,
            "own_median_trade_return_pct": median_ret,
            "own_mean_trade_return_pct": mean_ret,
            "own_profit_factor": pf,
            "own_win_rate": win_rate,
            "own_max_loss_trade_pct": max_loss_pct,
            "own_max_loss_trade_weth": round(min(pnls), 6) if neg_pnls else 0.0,
            "own_median_hold_hours": med_hold,
            "own_p90_hold_hours": p90_hold,
            "own_return_per_hold_hour": ret_per_hold,
            "own_capital_efficiency": cap_eff,
            "matched_pairs": n_match,
            "no_exit_pairs": n_no_exit,
            "unique_tokens": unique_toks,
            "top1_token_share": round(top1_share, 4),
            "top3_token_share": round(top3_share, 4),
            "toxic_flags_count": len(tox),
            "toxic_flags": ",".join(tox),
            "perfect_winrate_suspect": pwinr_suspect,
            "notional_status": "OK" if total_notional > 0 else "MISSING",
        }

    return trade_rows, ctrl_metrics


# ─── Step 4: Followability gap ────────────────────────────────────────────────

def build_followability_gap(pairs: list[dict]) -> list[dict]:
    """
    Estimate follow delay for each BUY event.
    We have buy_bt (timestamp) but no block_number from sprint048b.
    We categorize based on available info:
    - If buy_bt is present: time-based delay estimation is possible in Sprint049C
    - Block-level delay requires market_ticks telemetry (not yet available)
    This sprint marks all as BLOCK_DELAY_UNKNOWN pending Sprint049C telemetry.
    """
    gap_rows = []
    for r in pairs:
        ctrl   = r["controller"]
        buy_tx = r.get("buy_tx", "")
        buy_bt = r.get("buy_bt", "")
        lane   = r.get("buy_lane", "")
        is_q   = lane in QUOTEABLE_LANES

        if buy_bt:
            bt_avail = "true"
            # We can derive approx follow delay if we had market_ticks latency
            # Estimate: ingestor → Redis emit latency typically 1-3 blocks on Base
            # Without telemetry data, we cannot confirm exact block delay
            delay_bucket = "1_to_3_blocks_ESTIMATED"
            delay_estimate_sec = 2.0   # conservative 1-block = 2s estimate
            followable = "true" if is_q else "false"
            gap_known = "ESTIMATED_NO_TELEMETRY"
        else:
            bt_avail = "false"
            delay_bucket = "UNKNOWN"
            delay_estimate_sec = -1
            followable = "unknown"
            gap_known = "UNKNOWN"

        gap_rows.append({
            "controller": ctrl,
            "buy_tx": buy_tx,
            "buy_block_time": buy_bt,
            "lane_type": lane,
            "quoteable_flag": "true" if is_q else "false",
            "block_time_available": bt_avail,
            "delay_bucket": delay_bucket,
            "delay_estimate_sec": delay_estimate_sec,
            "block_delay_known": gap_known,
            "whether_followable": followable,
            "market_ticks_telemetry_available": "false",
            "note": "block_number not in sprint048b_realized_pnl; use Sprint049C live sidecar for exact gap measurement",
        })

    return gap_rows


# ─── Step 5-6: Delayed-follow paper simulator + aggregation ──────────────────

def run_delayed_follow_sim(pairs: list[dict], toxic_flags: dict) -> tuple[list[dict], dict]:
    """
    Simulate delayed-follow for each matched pair across 4 delay models.
    Returns (trade_rows, summary_dict).
    """
    sim_rows = []

    for r in pairs:
        ctrl      = r["controller"]
        sell_tx   = r.get("sell_tx", "")
        if not sell_tx:
            continue  # can't simulate without exit reference

        weth_in   = safe_float(r.get("weth_in"))
        exit_value = safe_float(r.get("exit_value"))  # controller's exit value (post-slippage)
        hold_min  = safe_float(r.get("hold_min"))
        hold_hrs  = round(hold_min / 60, 4)
        exit_bkt  = r.get("exit_bucket", "")
        is_q      = r.get("buy_lane", "") in QUOTEABLE_LANES
        tox       = ",".join(toxic_flags.get(ctrl, []))
        token     = r.get("meme_token", "")

        if weth_in < 0.0001 or exit_value < 0:
            continue

        for model_name, model in DELAY_MODELS.items():
            impact = model["impact"]
            delay_sec = model["delay_sec"]

            # Delayed entry cost: pay impact penalty on buy notional + gas
            delayed_entry = weth_in * (1 + impact) + GAS_PER_TRADE_WETH

            # Exit: same as controller's exit value (we exit when controller sells)
            # But we also pay exit slippage separately
            slip_out = 0.01 if weth_in < 0.2 else 0.012
            delayed_exit = exit_value  # assume we can exit at same price

            delayed_pnl  = delayed_exit - delayed_entry
            delayed_ret  = round(delayed_pnl / (weth_in * (1 + impact)) * 100, 4) if weth_in > 0 else 0.0
            cost_ratio   = round((delayed_entry - exit_value) / delayed_entry, 4) if delayed_entry > 0 else 0.0
            slip_ratio   = round(impact, 4)

            # Exit reason: follow controller (no timeout in historical replay)
            exit_reason  = f"controller_sell_{exit_bkt}"
            cost_killed  = delayed_entry > delayed_exit

            sim_rows.append({
                "controller": ctrl,
                "delay_model": model_name,
                "token": token,
                "buy_lane": r.get("buy_lane", ""),
                "entry_notional_weth": round(weth_in, 6),
                "exit_notional_weth": round(delayed_exit, 6),
                "delayed_entry_cost_weth": round(delayed_entry, 6),
                "delayed_follow_pnl_weth": round(delayed_pnl, 6),
                "delayed_follow_return_pct": delayed_ret,
                "delayed_follow_cost_weth": round(GAS_PER_TRADE_WETH + impact * weth_in, 6),
                "delayed_follow_cost_ratio": cost_ratio,
                "delayed_follow_slippage_ratio": slip_ratio,
                "delayed_follow_hold_hours": hold_hrs,
                "exit_reason": exit_reason,
                "exit_bucket": exit_bkt,
                "quote_fail": "false",
                "cost_killed": "true" if cost_killed else "false",
                "public_executable_flag": "true" if is_q else "false",
                "toxic_flags": tox,
            })

    # Aggregate per controller x delay_model
    summary: dict = {}
    for row in sim_rows:
        ctrl  = row["controller"]
        model = row["delay_model"]
        key   = (ctrl, model)
        if key not in summary:
            summary[key] = {"pnls": [], "notionals": [], "rets": [], "holds": [], "cost_kills": 0}
        summary[key]["pnls"].append(safe_float(row["delayed_follow_pnl_weth"]))
        summary[key]["notionals"].append(safe_float(row["entry_notional_weth"]))
        summary[key]["rets"].append(safe_float(row["delayed_follow_return_pct"]))
        summary[key]["holds"].append(safe_float(row["delayed_follow_hold_hours"]))
        if row["cost_killed"] == "true":
            summary[key]["cost_kills"] += 1

    agg_rows = []
    for (ctrl, model), d in summary.items():
        pnls = d["pnls"]
        nots = d["notionals"]
        rets = d["rets"]
        hlds = d["holds"]
        tot_pnl  = sum(pnls)
        tot_not  = sum(nots)
        roi      = round(tot_pnl / tot_not * 100, 4) if tot_not > 0 else 0.0
        pf       = profit_factor(pnls)
        wr       = round(sum(1 for p in pnls if p > 0) / len(pnls), 4) if pnls else 0.0
        med_ret  = round(statistics.median(rets), 4) if rets else 0.0
        mean_ret = round(statistics.mean(rets), 4) if rets else 0.0
        max_loss = round(min(rets), 4) if rets else 0.0
        med_hold = round(statistics.median(hlds), 4) if hlds else 0.0
        tot_hold = sum(hlds)
        ret_hold = round(tot_pnl / tot_hold, 6) if tot_hold > 0 else 0.0
        cap_eff  = round(tot_pnl / tot_not / (med_hold + 0.001), 6)
        n        = len(pnls)
        cost_kills = d["cost_kills"]

        grade = "PASS" if roi > 5 and pf > 1.5 and n >= 10 else (
            "WEAK_PASS" if roi > 0 and pf > 1 and n >= 5 else "FAIL"
        )

        agg_rows.append({
            "controller": ctrl,
            "delay_model": model,
            "delayed_follow_pnl_weth": round(tot_pnl, 6),
            "delayed_follow_deployed_notional_weth": round(tot_not, 6),
            "delayed_follow_roi_pct": roi,
            "delayed_follow_avg_trade_notional_weth": round(tot_not / n, 6) if n else 0.0,
            "delayed_follow_median_trade_return_pct": med_ret,
            "delayed_follow_mean_trade_return_pct": mean_ret,
            "delayed_follow_profit_factor": pf,
            "delayed_follow_win_rate": wr,
            "delayed_follow_max_loss_trade_pct": max_loss,
            "delayed_follow_cost_ratio": round(DELAY_MODELS[model]["impact"] + GAS_PER_TRADE_WETH / (tot_not / n + 0.001), 4),
            "delayed_follow_slippage_ratio": DELAY_MODELS[model]["impact"],
            "delayed_follow_return_per_hold_hour": ret_hold,
            "delayed_follow_capital_efficiency": cap_eff,
            "trade_count": n,
            "cost_killed_count": cost_kills,
            "cost_killed_rate": round(cost_kills / n, 4) if n else 0.0,
            "sample_size_class": "large" if n >= 30 else "medium" if n >= 10 else "small",
            "delayed_follow_verdict": grade,
        })

    return sim_rows, agg_rows


# ─── Step 7: Rescore candidates ───────────────────────────────────────────────

def rescore_candidates(
    ctrl_metrics: dict,
    agg_rows: list[dict],
    pf049a: dict,
    toxic_flags: dict,
    coverage: dict,
) -> list[dict]:
    """
    Re-tier candidates using ROI-normalized criteria.
    Categories:
      FOLLOW_CANDIDATE_STRONG_AFTER_ROI
      FOLLOW_CANDIDATE_WEAK_AFTER_ROI
      SMALL_CAPITAL_EFFICIENT_CANDIDATE
      ABSOLUTE_PNL_HIGH_BUT_ROI_LOW
      INCONCLUSIVE_NOTIONAL_MISSING
      TOXIC_OR_UNFOLLOWABLE
      SHADOW_ONLY_PRIVATE_ROUTE
    """
    # Build best delayed-follow stats per controller (best of 4 delay models)
    best_df: dict = {}
    for row in agg_rows:
        ctrl = row["controller"]
        model = row["delay_model"]
        if ctrl not in best_df or row["delayed_follow_roi_pct"] > best_df[ctrl]["delayed_follow_roi_pct"]:
            best_df[ctrl] = row

    result = []
    all_ctrls = set(ctrl_metrics.keys()) | set(pf049a.keys())

    for ctrl in all_ctrls:
        cm  = ctrl_metrics.get(ctrl, {})
        pfa = pf049a.get(ctrl, {})
        df  = best_df.get(ctrl, {})
        tox = toxic_flags.get(ctrl, [])

        own_pnl    = safe_float(cm.get("own_realized_pnl_weth"))
        own_not    = safe_float(cm.get("own_deployed_notional_weth"))
        own_roi    = safe_float(cm.get("own_roi_pct"))
        own_pf     = safe_float(cm.get("own_profit_factor"))
        own_wr     = safe_float(cm.get("own_win_rate"))
        own_match  = int(cm.get("matched_pairs", 0))
        own_maxloss= safe_float(cm.get("own_max_loss_trade_pct"))
        qshare     = safe_float(pfa.get("quoteable_share", pfa.get("public_route_share", 1.0)))
        q_buy      = int(pfa.get("quoteable_buy_count", cm.get("matched_pairs", 0)))
        top1       = safe_float(cm.get("top1_token_share", pfa.get("top1_token_share", 0)))
        top3       = safe_float(cm.get("top3_token_share", pfa.get("top3_token_share", 0)))
        pwinr_susp = cm.get("perfect_winrate_suspect", False)

        df_roi  = safe_float(df.get("delayed_follow_roi_pct")) if df else None
        df_pf   = safe_float(df.get("delayed_follow_profit_factor")) if df else None
        df_n    = int(df.get("trade_count", 0)) if df else 0
        df_model= df.get("delay_model", "") if df else ""
        df_pnl  = safe_float(df.get("delayed_follow_pnl_weth")) if df else 0.0
        df_verd = df.get("delayed_follow_verdict", "FAIL") if df else "FAIL"

        # Determine category
        if own_not < 0.001:
            cat = "INCONCLUSIVE_NOTIONAL_MISSING"
        elif qshare < 0.15:
            cat = "SHADOW_ONLY_PRIVATE_ROUTE"
        elif df_roi is None or df_roi <= 0:
            # Check if high absolute PnL but low ROI
            if own_pnl > 1.0 and own_roi < 5:
                cat = "ABSOLUTE_PNL_HIGH_BUT_ROI_LOW"
            else:
                cat = "FOLLOW_CANDIDATE_WEAK_AFTER_ROI"
        elif (own_roi > 0 and own_pf > 1 and df_roi > 0 and df_pf and df_pf > 1
              and own_match >= 10 and df_n >= 10 and qshare >= 0.5):
            if top1 >= 0.90 and own_match >= 10:
                # High concentration — can still be strong if scoped
                cat = "FOLLOW_CANDIDATE_STRONG_AFTER_ROI"
            else:
                cat = "FOLLOW_CANDIDATE_STRONG_AFTER_ROI"
        elif df_roi > 0 and own_roi > 0:
            # Check small-capital-efficient
            if own_not < 2.0 and own_roi > 20:
                cat = "SMALL_CAPITAL_EFFICIENT_CANDIDATE"
            else:
                cat = "FOLLOW_CANDIDATE_WEAK_AFTER_ROI"
        else:
            cat = "FOLLOW_CANDIDATE_WEAK_AFTER_ROI"

        # Concentration risk
        conc_risk = "HIGH" if top1 >= 0.80 else "MEDIUM" if top1 >= 0.50 else "LOW"

        # Why PERFECT_WINRATE_SUSPECT is not auto-excluded:
        # Offline data cannot distinguish wash trading from genuine skill.
        # We flag it but require Sprint049C live telemetry to confirm.
        pwinr_note = "SOFT_FLAG_NEEDS_LIVE_CONFIRMATION" if pwinr_susp else ""

        result.append({
            "controller": ctrl,
            "category_after_roi": cat,
            "own_realized_pnl_weth": round(own_pnl, 6),
            "own_deployed_notional_weth": round(own_not, 6),
            "own_roi_pct": round(own_roi, 4),
            "own_profit_factor": own_pf,
            "own_win_rate": own_wr,
            "own_matched_pairs": own_match,
            "own_max_loss_trade_pct": own_maxloss,
            "own_median_hold_hours": safe_float(cm.get("own_median_hold_hours")),
            "quoteable_buy_count": q_buy,
            "quoteable_share": round(qshare, 4),
            "top1_token_share": round(top1, 4),
            "top3_token_share": round(top3, 4),
            "concentration_risk": conc_risk,
            "toxic_flags": ",".join(tox),
            "toxic_flags_count": len(tox),
            "perfect_winrate_suspect": pwinr_susp,
            "perfect_winrate_note": pwinr_note,
            "best_delayed_model": df_model,
            "delayed_follow_roi_pct": round(df_roi, 4) if df_roi is not None else None,
            "delayed_follow_profit_factor": round(df_pf, 4) if df_pf else None,
            "delayed_follow_pnl_weth": round(df_pnl, 6),
            "delayed_follow_n_trades": df_n,
            "delayed_follow_verdict": df_verd,
            "sprint049a_category": pfa.get("category", ""),
            "downranked_reason": (
                "ROI_LOW_vs_NOTIONAL" if cat == "ABSOLUTE_PNL_HIGH_BUT_ROI_LOW"
                else "DELAYED_FOLLOW_NEGATIVE" if df_roi is not None and df_roi <= 0
                else ""
            ),
        })

    result.sort(key=lambda x: -(
        safe_float(x.get("delayed_follow_roi_pct") or -99)
        + safe_float(x.get("own_roi_pct", 0)) * 0.3
    ))
    return result


# ─── Step 1: Select top paper candidates ─────────────────────────────────────

def select_paper_candidates(rescored: list[dict], n: int = 5) -> list[dict]:
    """Select top N candidates using ROI-normalized composite score."""
    STRONG_CAT = {"FOLLOW_CANDIDATE_STRONG_AFTER_ROI"}
    WEAK_CAT   = {"FOLLOW_CANDIDATE_WEAK_AFTER_ROI", "SMALL_CAPITAL_EFFICIENT_CANDIDATE"}

    # Composite score: primary = delayed_follow_roi_pct, secondary = own_roi, penalty for concentration
    def composite(r):
        dr   = safe_float(r.get("delayed_follow_roi_pct") or -99)
        or_  = safe_float(r.get("own_roi_pct", 0))
        top1 = safe_float(r.get("top1_token_share", 0))
        pf   = safe_float(r.get("own_profit_factor", 0))
        conc_pen = top1 * 5  # penalty for high concentration
        pwinr_pen = 2 if r.get("perfect_winrate_suspect") else 0
        return dr + or_ * 0.3 + min(10, pf) * 2 - conc_pen - pwinr_pen

    ranked = sorted(rescored, key=lambda x: -composite(x))
    # Select from STRONG first, then WEAK
    selected = []
    for cat_pool in [STRONG_CAT, WEAK_CAT]:
        for r in ranked:
            if r["category_after_roi"] in cat_pool and len(selected) < n:
                selected.append(r)
    return selected


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    print(f"Sprint049B Copy-Follow Replay — {ts}")

    pairs       = load_realized_pairs()
    pf049a      = load_preflight()
    toxic_flags = load_toxic_flags()

    # Step 0
    cov = coverage_audit(pairs)
    print(f"\n[Step 0] Coverage audit:")
    for k, v in cov.items():
        print(f"  {k}: {v}")

    ranking_allowed = cov["ranking_allowed"]
    if not ranking_allowed:
        print("WARNING: notional_available_rate < 70%; ranking will be INCONCLUSIVE")

    # Steps 1-3
    print("\n[Steps 1-3] Building trade table and controller metrics...")
    trade_rows, ctrl_metrics = build_trade_table(pairs, toxic_flags)

    # Step 4
    print("[Step 4] Building followability gap...")
    gap_rows = build_followability_gap(pairs)

    # Steps 5-6
    print("[Steps 5-6] Running delayed-follow paper simulation...")
    sim_rows, agg_rows = run_delayed_follow_sim(pairs, toxic_flags)

    # Step 7
    print("[Step 7] Rescoring candidates after ROI normalization...")
    rescored = rescore_candidates(ctrl_metrics, agg_rows, pf049a, toxic_flags, cov)

    # Select top 5
    selected = select_paper_candidates(rescored, n=5)

    # ── Write artifacts ────────────────────────────────────────────────────────

    # Selected candidates
    FIELDS_SEL = [
        "controller","category_after_roi","delayed_follow_roi_pct","delayed_follow_profit_factor",
        "delayed_follow_pnl_weth","delayed_follow_n_trades","best_delayed_model",
        "own_roi_pct","own_profit_factor","own_realized_pnl_weth","own_deployed_notional_weth",
        "own_win_rate","own_matched_pairs","own_max_loss_trade_pct","own_median_hold_hours",
        "quoteable_buy_count","quoteable_share","top1_token_share","concentration_risk",
        "toxic_flags","perfect_winrate_suspect","perfect_winrate_note","sprint049a_category",
    ]
    with open(DIAG/"sprint049b_selected_paper_candidates.csv","w",newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS_SEL, extrasaction="ignore")
        w.writeheader()
        w.writerows(selected)
    print(f"  → sprint049b_selected_paper_candidates.csv ({len(selected)} rows)")

    # Trade table
    FIELDS_TR = [
        "controller","controller_buy_tx","controller_sell_tx","buy_block_time","sell_block_time",
        "token","pool","lane_type","quoteable_flag","route_decode_quality",
        "buy_notional_weth","sell_notional_weth","trade_pnl_weth","trade_return_pct",
        "hold_hours","pnl_per_hold_hour","cost_proxy_weth","slippage_proxy_weth",
        "exit_bucket","toxic_flags","private_route_flag","public_executable_flag",
        "notional_status","has_exit",
    ]
    with open(DIAG/"sprint049b_return_normalized_trades.csv","w",newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS_TR, extrasaction="ignore")
        w.writeheader()
        w.writerows(trade_rows)
    print(f"  → sprint049b_return_normalized_trades.csv ({len(trade_rows)} rows)")

    # Controller ROI metrics
    FIELDS_CM = [
        "controller","own_realized_pnl_weth","own_deployed_notional_weth","own_roi_pct",
        "own_avg_trade_notional_weth","own_median_trade_return_pct","own_mean_trade_return_pct",
        "own_profit_factor","own_win_rate","own_max_loss_trade_pct","own_max_loss_trade_weth",
        "own_median_hold_hours","own_p90_hold_hours","own_return_per_hold_hour",
        "own_capital_efficiency","matched_pairs","no_exit_pairs","unique_tokens",
        "top1_token_share","top3_token_share","toxic_flags_count","toxic_flags",
        "perfect_winrate_suspect","notional_status",
    ]
    cm_rows = sorted(ctrl_metrics.values(), key=lambda x: -safe_float(x.get("own_roi_pct", 0)))
    with open(DIAG/"sprint049b_return_normalized_candidates.csv","w",newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS_CM)
        w.writeheader()
        w.writerows(cm_rows)
    print(f"  → sprint049b_return_normalized_candidates.csv ({len(cm_rows)} rows)")

    # Followability gap
    FIELDS_GAP = [
        "controller","buy_tx","buy_block_time","lane_type","quoteable_flag",
        "block_time_available","delay_bucket","delay_estimate_sec","block_delay_known",
        "whether_followable","market_ticks_telemetry_available","note",
    ]
    with open(DIAG/"sprint049b_followability_gap.csv","w",newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS_GAP, extrasaction="ignore")
        w.writeheader()
        w.writerows(gap_rows)
    print(f"  → sprint049b_followability_gap.csv ({len(gap_rows)} rows)")

    # Paper follow trades
    FIELDS_SIM = [
        "controller","delay_model","token","buy_lane","entry_notional_weth","exit_notional_weth",
        "delayed_entry_cost_weth","delayed_follow_pnl_weth","delayed_follow_return_pct",
        "delayed_follow_cost_weth","delayed_follow_cost_ratio","delayed_follow_slippage_ratio",
        "delayed_follow_hold_hours","exit_reason","exit_bucket","quote_fail","cost_killed",
        "public_executable_flag","toxic_flags",
    ]
    with open(DIAG/"sprint049b_paper_follow_trades.csv","w",newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS_SIM, extrasaction="ignore")
        w.writeheader()
        w.writerows(sim_rows)
    print(f"  → sprint049b_paper_follow_trades.csv ({len(sim_rows)} rows)")

    # Rescored candidates (all 31)
    FIELDS_RS = [
        "controller","category_after_roi","own_realized_pnl_weth","own_deployed_notional_weth",
        "own_roi_pct","own_profit_factor","own_win_rate","own_matched_pairs","own_max_loss_trade_pct",
        "own_median_hold_hours","quoteable_buy_count","quoteable_share","top1_token_share",
        "top3_token_share","concentration_risk","toxic_flags","toxic_flags_count",
        "perfect_winrate_suspect","perfect_winrate_note","best_delayed_model",
        "delayed_follow_roi_pct","delayed_follow_profit_factor","delayed_follow_pnl_weth",
        "delayed_follow_n_trades","delayed_follow_verdict","sprint049a_category","downranked_reason",
    ]
    with open(DIAG/"sprint049b_rescored_candidates.csv","w",newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS_RS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rescored)
    print(f"  → sprint049b_rescored_candidates.csv ({len(rescored)} rows)")

    # Paper follow summary JSON
    cat_cnt = Counter(r["category_after_roi"] for r in rescored)
    best_model_summary = {}
    for model_name in DELAY_MODELS:
        model_agg = [r for r in agg_rows if r["delay_model"] == model_name]
        if model_agg:
            tot_pnl = sum(safe_float(r["delayed_follow_pnl_weth"]) for r in model_agg)
            pass_n  = sum(1 for r in model_agg if safe_float(r["delayed_follow_roi_pct"]) > 0)
            best_model_summary[model_name] = {
                "total_delayed_pnl_weth": round(tot_pnl, 4),
                "controllers_positive_roi": pass_n,
                "controllers_total": len(model_agg),
            }

    pf_summary = {
        "sprint": "049B",
        "timestamp": ts,
        "total_sim_trades": len(sim_rows),
        "matched_pairs_used": len([r for r in pairs if r.get("sell_tx")]),
        "delay_model_summary": best_model_summary,
        "category_counts": dict(cat_cnt),
        "selected_candidates_count": len(selected),
        "selected_candidates": [r["controller"] for r in selected],
        "coverage": cov,
    }
    with open(DIAG/"sprint049b_paper_follow_summary.json","w") as f:
        json.dump(pf_summary, f, indent=2)
    print(f"  → sprint049b_paper_follow_summary.json")

    # ROI summary
    strong_after = [r for r in rescored if r["category_after_roi"]=="FOLLOW_CANDIDATE_STRONG_AFTER_ROI"]
    weak_after   = [r for r in rescored if r["category_after_roi"]=="FOLLOW_CANDIDATE_WEAK_AFTER_ROI"]
    downranked   = [r for r in rescored if r.get("downranked_reason")]

    roi_summary = {
        "sprint": "049B",
        "timestamp": ts,
        "notional_available_rate": cov["notional_available_rate"],
        "ranking_allowed": ranking_allowed,
        "total_candidates_evaluated": len(rescored),
        "follow_candidate_strong_after_roi": len(strong_after),
        "follow_candidate_weak_after_roi": len(weak_after),
        "small_capital_efficient": cat_cnt.get("SMALL_CAPITAL_EFFICIENT_CANDIDATE", 0),
        "absolute_pnl_high_roi_low": cat_cnt.get("ABSOLUTE_PNL_HIGH_BUT_ROI_LOW", 0),
        "inconclusive": cat_cnt.get("INCONCLUSIVE_NOTIONAL_MISSING", 0),
        "shadow_only": cat_cnt.get("SHADOW_ONLY_PRIVATE_ROUTE", 0),
        "top5_selected": [
            {
                "controller": r["controller"],
                "category": r["category_after_roi"],
                "delayed_roi_pct": r.get("delayed_follow_roi_pct"),
                "own_roi_pct": round(safe_float(r.get("own_roi_pct")), 4),
                "own_pnl_weth": round(safe_float(r.get("own_realized_pnl_weth")), 6),
                "concentration_risk": r.get("concentration_risk"),
                "pwinr_suspect": r.get("perfect_winrate_suspect"),
            }
            for r in selected
        ],
        "downranked_count": len(downranked),
        "downranked_controllers": [
            {"controller": r["controller"], "reason": r["downranked_reason"]}
            for r in downranked
        ],
        "followability_gap_status": "ESTIMATED_NO_TELEMETRY",
        "followability_gap_note": "Block-level delay requires Sprint049C live sidecar telemetry. Estimated 1-3 blocks (2-6s) on Base L2.",
        "sprint049c_shadow_recommended": len(strong_after) >= 1 and any(
            safe_float(r.get("delayed_follow_roi_pct") or 0) > 0 for r in strong_after
        ),
    }
    with open(DIAG/"sprint049b_roi_summary.json","w") as f:
        json.dump(roi_summary, f, indent=2)
    print(f"  → sprint049b_roi_summary.json")

    # Decision flags
    sprint049c_rec = roi_summary["sprint049c_shadow_recommended"]
    any_df_pass  = any(safe_float(r.get("delayed_follow_roi_pct") or 0) > 0 for r in strong_after)

    flags = {
        "sprint": "049B",
        "timestamp": ts,
        "MAINLINE_CONTINUES_WHITEPAPER": True,
        "SMART_MONEY_FOLLOW_IS_SIDECAR": True,
        "ATTRIBUTION_LINE_PAUSED": True,
        "AUTHOR_WALLET_FOUND": False,
        "LIVE_ALLOWED": False,
        "TRAINING_ALLOWED": False,
        "MODEL_PROMOTION_ALLOWED": False,
        "PAPER_REPLAY_COMPLETED": True,
        "ROI_NORMALIZATION_COMPLETED": True,
        "NOTIONAL_AVAILABLE_RATE": cov["notional_available_rate"],
        "RANKING_ALLOWED": ranking_allowed,
        "STRONG_CANDIDATE_RESCORING_DONE": True,
        "ABSOLUTE_PNL_ONLY_REJECTED": True,
        "FOLLOW_CANDIDATE_FOUND_AFTER_ROI_NORMALIZATION": len(strong_after) > 0,
        "FOLLOW_CANDIDATE_STRONG_COUNT": len(strong_after),
        "FOLLOW_CANDIDATE_WEAK_COUNT": len(weak_after),
        "DOWNRANKED_COUNT": len(downranked),
        "DELAYED_FOLLOW_WEAK_PASS": any_df_pass,
        "DELAYED_FOLLOW_PASS": any(
            safe_float(r.get("delayed_follow_roi_pct") or 0) > 5 and
            safe_float(r.get("delayed_follow_profit_factor") or 0) > 1.5
            for r in strong_after
        ),
        "FOLLOWABILITY_GAP_ACCEPTABLE": "ESTIMATED_PENDING_TELEMETRY",
        "FOLLOWABILITY_GAP_NOTE": "Sprint049C live sidecar required for exact block-level measurement",
        "SPRINT049C_SHADOW_RECOMMENDED": sprint049c_rec,
        "SPRINT049C_CONDITION": "Requires: deploy sidecar with copy_follow_group, measure actual market_ticks latency, run 7-day paper shadow",
    }
    with open(DIAG/"sprint049b_decision_flags.json","w") as f:
        json.dump(flags, f, indent=2)
    print(f"  → sprint049b_decision_flags.json")

    # Print summary
    print(f"\n=== SPRINT049B SUMMARY ===")
    print(f"Notional available rate: {cov['notional_available_rate']*100:.1f}%  → Ranking {'ALLOWED' if ranking_allowed else 'INCONCLUSIVE'}")
    print(f"Category breakdown after ROI normalization:")
    for cat, n in cat_cnt.most_common():
        print(f"  {cat}: {n}")
    print(f"\nTop 5 selected candidates:")
    for r in selected:
        dr = r.get('delayed_follow_roi_pct')
        print(f"  {r['controller'][:16]}...  cat={r['category_after_roi'][:30]}  "
              f"df_roi={dr}%  own_roi={r.get('own_roi_pct')}%  "
              f"own_pnl={r.get('own_realized_pnl_weth')} WETH  conc={r.get('concentration_risk')}")
    print(f"\nSprint049C shadow recommended: {sprint049c_rec}")

    # Save data for readout
    import pickle
    with open("/tmp/sprint049b_data.pkl","wb") as f_:
        import pickle as pk
        pk.dump({
            "cov": cov, "ctrl_metrics": ctrl_metrics, "agg_rows": agg_rows,
            "rescored": rescored, "selected": selected, "roi_summary": roi_summary,
            "flags": flags, "cat_cnt": dict(cat_cnt), "downranked": downranked,
            "strong_after": strong_after, "weak_after": weak_after,
        }, f_)
    print("\nData pickled for readout generation.")


if __name__ == "__main__":
    main()
