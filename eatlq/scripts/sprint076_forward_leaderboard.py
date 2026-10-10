#!/usr/bin/env python3
"""
Sprint076 — Forward race scaling + ranking discipline
=====================================================
Reads Sprint075 + Sprint077 expansion harness outputs (state JSON + scoreboards)
and the Sprint074 factory registry. Emits forward-only / historical / side-by-side
combined
views without blending metrics into a single \"alpha\" score.

Policy:
  - forward_n < 20 → PLUMBING_ONLY / EARLY_HINT; PROMOTE forbidden.
  - Do not treat historical + forward as one conclusion (combined view is
    explicitly labeled side-by-side only).

Cron (optional, every 15 minutes):
  */15 * * * * cd /opt/EATLQ && python3 scripts/sprint076_forward_leaderboard.py \\
      >> logs/sprint076/leaderboard_cron.log 2>&1

No Redis, no live trading, no secrets in outputs.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATE075 = REPO / "logs" / "sprint075"
STATE077 = REPO / "logs" / "sprint077"
SCOREBOARD075 = REPO / "mvp" / "audits" / "sprint075_forward_harness" / "scoreboard.json"
SCOREBOARD077 = REPO / "mvp" / "audits" / "sprint077_forward_expansion" / "scoreboard.json"
EXPANSION_PLAN077 = REPO / "mvp" / "audits" / "sprint077_forward_expansion" / "sprint077_expansion_plan.json"
RESOURCE077 = REPO / "mvp" / "audits" / "sprint077_forward_expansion" / "sprint077_resource_snapshot.json"
REGISTRY074 = REPO / "mvp" / "audits" / "sprint074_alpha_factory" / "alpha_factory_registry.json"
WATCHLIST074 = REPO / "mvp" / "audits" / "sprint074_alpha_factory" / "alpha_factory_dryrun_watchlist.json"
OUT_DIR = REPO / "mvp" / "audits" / "sprint076_forward_race"
LOG_DIR = REPO / "logs" / "sprint076"

OUT_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

STUCK_GRACE_SEC = 900  # 15m past expected_exit counts as stuck-waiting-label


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def parse_dt(s: str) -> datetime:
    s = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def confidence_bucket(n: int) -> str:
    if n < 20:
        return "N_LT_20_PLUMBING_ONLY"
    if n < 50:
        return "N_20_50_WEAK_EVIDENCE"
    if n < 100:
        return "N_50_100_RANKABLE"
    return "N_GE_100_REVIEWABLE"


def evidence_tag(n: int) -> str:
    return "EARLY_HINT" if n < 20 else "ACTIONABLE_EVIDENCE_THRESHOLD_MET"


def classify_family(entry_description: str, registry_entry_rule: str) -> tuple[str, list[str]]:
    """
    Primary family label (one of): momentum, pv, baseline, fee, age, diversity, other
    All Sprint075 candidates share the same age band — \"age\" is used only when
    no other distinguishing gate applies beyond implicit age (same as baseline).
    """
    desc = (entry_description or "").lower()
    rule = (registry_entry_rule or "").lower()
    tags: list[str] = []
    if "diversity" in rule or "buyer_diversity" in rule:
        return "diversity", ["diversity"]
    if "momentum" in desc or "momentum" in rule:
        tags.append("momentum")
    if "pv" in desc or "velocity" in rule:
        tags.append("pv")
    if "fee" in desc or "fee<=" in rule:
        tags.append("fee")
    tags.append("age_gated")

    if "momentum" in tags:
        return "momentum", tags
    if "pv" in tags:
        return "pv", tags
    if "fee" in tags and "momentum" not in tags and "pv" not in tags:
        return "fee", tags
    # Implicit age window only → baseline (age band is universal here)
    return "baseline", tags


def forward_metrics_from_trades(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {
            "forward_n": 0,
            "pf": None,
            "wr": None,
            "ev": None,
            "payoff_ratio": None,
            "max_drawdown": None,
            "top_pool_share": None,
            "top_sender_share": None,
        }

    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [abs(t["pnl"]) for t in trades if t["pnl"] < 0]
    rets = [t["ret_ag"] for t in trades]
    pf = round(sum(wins) / sum(losses), 6) if losses else None
    wr = round(len(wins) / n, 6)
    ev = round(statistics.mean(rets), 6)
    mean_win = statistics.mean(wins) if wins else 0.0
    mean_loss = statistics.mean(losses) if losses else 0.0
    payoff = round(mean_win / mean_loss, 6) if mean_loss > 0 else None

    by_pool: Counter[str] = Counter(t["pool"] for t in trades)
    top_share = round(max(by_pool.values()) / n, 6) if by_pool else None
    sender_values = [
        str(t.get("dominant_sender") or t.get("sender") or "").lower()
        for t in trades
        if t.get("dominant_sender") or t.get("sender")
    ]
    by_sender: Counter[str] = Counter(sender_values)
    top_sender_share = round(max(by_sender.values()) / n, 6) if by_sender else None

    eq = 100.0
    peak = eq
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: x["signal_time"]):
        eq += t["pnl"]
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    return {
        "forward_n": n,
        "pf": pf,
        "wr": wr,
        "ev": ev,
        "payoff_ratio": payoff,
        "max_drawdown": round(max_dd, 6),
        "top_pool_share": top_share,
        "top_sender_share": top_sender_share,
    }


def remove_top_pool_ev(trades: list[dict]) -> float | None:
    if len(trades) < 2:
        return None
    cnt = Counter(t["pool"] for t in trades)
    top_n = max(cnt.values())
    top_pools = {p for p, c in cnt.items() if c == top_n}
    rest = [t for t in trades if t["pool"] not in top_pools]
    if len(rest) < 1:
        return None
    return round(statistics.mean(t["ret_ag"] for t in rest), 6)


def quote_fail_rate(stats: dict, forward_n: int) -> float:
    skipped = int(stats.get("skipped_no_label", 0) or 0)
    denom = max(forward_n + skipped, 1)
    return round(skipped / denom, 6)


def open_position_stuck(open_positions: dict, now: datetime) -> tuple[int, float | None]:
    stuck = 0
    max_lateness_sec = 0.0
    for pos in open_positions.values():
        exp = parse_dt(pos["expected_exit"])
        lateness = (now - exp).total_seconds()
        if lateness > STUCK_GRACE_SEC:
            stuck += 1
            max_lateness_sec = max(max_lateness_sec, lateness)
    return stuck, round(max_lateness_sec, 1) if stuck else None


def load_registry() -> dict[str, dict]:
    data = json.loads(REGISTRY074.read_text())
    out: dict[str, dict] = {}
    for s in data.get("strategies", []):
        sid = s.get("strategy_id")
        if sid:
            out[sid] = s
    return out


def load_merged_scoreboard() -> dict[str, dict]:
    m: dict[str, dict] = {}
    if SCOREBOARD075.exists():
        for r in json.loads(SCOREBOARD075.read_text()):
            row = dict(r)
            row.setdefault("harness", "sprint075_top10")
            m[row["strategy_id"]] = row
    if SCOREBOARD077.exists():
        for r in json.loads(SCOREBOARD077.read_text()):
            row = dict(r)
            row.setdefault("harness", "sprint077_expansion")
            m[row["strategy_id"]] = row
    return m


def load_expansion_plan_candidates() -> dict[str, dict]:
    if not EXPANSION_PLAN077.exists():
        return {}
    doc = json.loads(EXPANSION_PLAN077.read_text())
    return {c["strategy_id"]: c for c in doc.get("candidates", [])}


def sb_from_plan_candidate(c: dict) -> dict:
    hor = c["exit_horizon"]
    return {
        "strategy_id": c["strategy_id"],
        "harness": "sprint077_expansion",
        "family": c.get("family"),
        "exit_horizon": hor,
        "entry_description": (c.get("entry_rule") or "")[:240],
        "HARD_EXIT_REQUIRED": hor in ("4h", "6h", "12h", "24h"),
        "hard_horizon_exit_enabled": c.get("hard_horizon_exit", False),
        "hist_n": c.get("hist_n"),
        "hist_pf": c.get("hist_pf"),
        "hist_wr": c.get("hist_wr"),
        "hist_composite": c.get("hist_composite"),
    }


def sb_from_registry_fallback(entry: dict) -> dict:
    import re

    xr = entry.get("exit_rule") or ""
    m = re.search(r"label_(\d+m|\d+h)_", xr)
    hor = m.group(1) if m else "2h"
    metrics = entry.get("metrics") or {}
    scores = entry.get("scores") or {}
    return {
        "strategy_id": entry["strategy_id"],
        "harness": "registry_fallback",
        "exit_horizon": hor,
        "entry_description": (entry.get("entry_rule") or "")[:240],
        "HARD_EXIT_REQUIRED": hor in ("4h", "6h", "12h", "24h"),
        "hist_n": metrics.get("n_trades"),
        "hist_pf": metrics.get("pf"),
        "hist_wr": metrics.get("wr"),
        "hist_composite": scores.get("composite_score"),
    }


def load_state_any(sid: str) -> dict | None:
    for d in (STATE075, STATE077):
        p = d / f"{sid}_state.json"
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                return None
    return None


def build_strategy_row(
    sid: str,
    sb: dict,
    state: dict | None,
    reg: dict | None,
    now: datetime,
) -> dict:
    entry_desc = sb.get("entry_description", "")
    reg_rule = (reg or {}).get("entry_rule", "")
    if sb.get("family"):
        family = sb["family"]
        family_tags = [family, "age_gated"]
    else:
        family, family_tags = classify_family(entry_desc, reg_rule)

    hist_m = (reg or {}).get("metrics") or {}
    hist_n = int(hist_m.get("n_trades") or sb.get("hist_n") or 0)
    hist_pf = hist_m.get("pf") or sb.get("hist_pf")
    hist_wr = hist_m.get("wr") or sb.get("hist_wr")
    hist_ev = hist_m.get("mean_ret")
    hist_payoff = hist_m.get("payoff_ratio")
    hist_dd = hist_m.get("max_dd")
    hist_top_pool = hist_m.get("pool_top_share")

    trades = (state or {}).get("trades") or []
    stats = (state or {}).get("stats") or {}
    open_pos = (state or {}).get("open_positions") or {}

    fm = forward_metrics_from_trades(trades)
    fwd_n = fm["forward_n"]
    fwd_ev = fm["ev"]
    hist_ev_f = float(hist_ev) if hist_ev is not None else None

    decay_ev = None
    if fwd_ev is not None and hist_ev_f is not None:
        decay_ev = round(fwd_ev - hist_ev_f, 6)

    decay_pf_ratio = None
    if fm["pf"] is not None and hist_pf not in (None, 0):
        try:
            decay_pf_ratio = round(float(fm["pf"]) / float(hist_pf), 6)
        except (TypeError, ValueError):
            pass

    qfr = quote_fail_rate(stats, fwd_n)
    rm_top_ev = remove_top_pool_ev(trades)
    stuck_n, stuck_max_sec = open_position_stuck(open_pos, now)

    bucket = confidence_bucket(fwd_n)
    promo_blocked = fwd_n < 20
    concentration_flags: list[str] = []
    if fm["top_pool_share"] is not None and fm["top_pool_share"] > 0.50:
        concentration_flags.append("POOL_CONCENTRATION_DOWNGRADE")
    if hist_top_pool is not None:
        try:
            if float(hist_top_pool) > 0.50:
                concentration_flags.append("HIST_POOL_CONCENTRATION_DOWNGRADE")
        except (TypeError, ValueError):
            pass
    if fm["top_sender_share"] is not None and fm["top_sender_share"] > 0.70:
        concentration_flags.append("SUBJECT_CONDITIONED_ALPHA")
    desc_blob = " ".join(str(x or "").lower() for x in (
        sid,
        entry_desc,
        reg_rule,
        sb.get("sender_0x6ff5_only"),
        sb.get("harness"),
    ))
    if "0x6ff5" in desc_blob or "is_0x6ff5" in desc_blob:
        concentration_flags.append("UNKNOWN_PUBLIC_ROUTER_TREATED_AS_SMART_MONEY_REVIEW")
        concentration_flags.append("SUBJECT_CONDITIONED_ALPHA")
    concentration_flags = sorted(set(concentration_flags))
    sop_status = "PASS_REVIEWABLE"
    if "SUBJECT_CONDITIONED_ALPHA" in concentration_flags:
        sop_status = "SUBJECT_CONDITIONED_ALPHA"
    elif any("POOL_CONCENTRATION" in f for f in concentration_flags):
        sop_status = "REGIME_SPECIFIC_ALPHA"
    if concentration_flags:
        promo_blocked = True

    return {
        "strategy_id": sid,
        "family": family,
        "family_tags": family_tags,
        "exit_horizon": sb.get("exit_horizon"),
        "HARD_EXIT_REQUIRED": sb.get("HARD_EXIT_REQUIRED", False),
        "forward_n": fwd_n,
        "confidence_bucket": bucket,
        "evidence_tag": evidence_tag(fwd_n),
        "promotion_allowed_forward": not promo_blocked,
        "promotion_note": "PLUMBING_ONLY_NO_PROMOTE" if promo_blocked else "MEETS_MIN_SAMPLE_FOR_RANKING_REVIEW",
        "pf": fm["pf"],
        "wr": fm["wr"],
        "ev": fwd_ev,
        "payoff_ratio": fm["payoff_ratio"],
        "max_drawdown": fm["max_drawdown"],
        "top_pool_share": fm["top_pool_share"],
        "top_sender_share": fm["top_sender_share"],
        "sop_review_status": sop_status,
        "sop_concentration_flags": concentration_flags,
        "quote_fail_rate": qfr,
        "remove_top_pool_EV": rm_top_ev,
        "forward_vs_historical_decay_ev": decay_ev,
        # Alias: primary scalar decay metric (EV delta forward minus historical factory mean_ret).
        "forward_vs_historical_decay": decay_ev,
        "forward_vs_historical_pf_ratio": decay_pf_ratio,
        "open_positions_open": len(open_pos),
        "open_position_stuck_count": stuck_n,
        "open_position_stuck_max_lateness_sec": stuck_max_sec,
        "forward_equity": round((state or {}).get("equity", 100.0), 4) if state else None,
        "last_harness_run_utc": (state or {}).get("last_run"),
        # Historical snapshot (factory / pre-cutoff labels — not merged into forward)
        "hist_n": hist_n,
        "hist_pf": hist_pf,
        "hist_wr": hist_wr,
        "hist_ev": hist_ev_f,
        "hist_payoff_ratio": hist_payoff,
        "hist_max_drawdown": hist_dd,
        "hist_top_pool_share": hist_top_pool,
        "hist_composite": sb.get("hist_composite"),
        "harness_segment": sb.get("harness"),
    }


def historical_only_rows(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        out.append({
            "strategy_id": r["strategy_id"],
            "family": r["family"],
            "hist_n": r["hist_n"],
            "hist_pf": r["hist_pf"],
            "hist_wr": r["hist_wr"],
            "hist_ev": r["hist_ev"],
            "hist_payoff_ratio": r["hist_payoff_ratio"],
            "hist_max_drawdown": r["hist_max_drawdown"],
            "hist_top_pool_share": r["hist_top_pool_share"],
            "hist_composite": r["hist_composite"],
            "note": "HISTORICAL_LABEL_BACKTEST_ONLY_NOT_FORWARD_PROOF",
        })
    return sorted(out, key=lambda x: -(x["hist_composite"] or 0))


def forward_only_rows(rows: list[dict]) -> list[dict]:
    keys = [
        "strategy_id",
        "family",
        "forward_n",
        "confidence_bucket",
        "evidence_tag",
        "promotion_allowed_forward",
        "pf",
        "wr",
        "ev",
        "payoff_ratio",
        "max_drawdown",
        "top_pool_share",
        "top_sender_share",
        "sop_review_status",
        "sop_concentration_flags",
        "quote_fail_rate",
        "remove_top_pool_EV",
        "forward_vs_historical_decay_ev",
        "forward_vs_historical_decay",
        "forward_vs_historical_pf_ratio",
        "open_position_stuck_count",
    ]
    fwd = [{k: r[k] for k in keys} for r in rows]
    return sorted(
        fwd,
        key=lambda x: (
            0 if x["forward_n"] >= 20 else 1,
            -(x["ev"] or float("-inf")),
        ),
    )


def combined_side_by_side(rows: list[dict]) -> list[dict]:
    """Same rows with explicit fields; no blended alpha scalar."""
    out = []
    for r in rows:
        out.append({
            "strategy_id": r["strategy_id"],
            "family": r["family"],
            "combined_view_policy": "SIDE_BY_SIDE_ONLY_NO_BLENDED_ALPHA",
            "forward_block": {
                "forward_n": r["forward_n"],
                "confidence_bucket": r["confidence_bucket"],
                "evidence_tag": r["evidence_tag"],
                "pf": r["pf"],
                "wr": r["wr"],
                "ev": r["ev"],
                "payoff_ratio": r["payoff_ratio"],
                "max_drawdown": r["max_drawdown"],
                "top_pool_share": r["top_pool_share"],
                "top_sender_share": r["top_sender_share"],
                "sop_review_status": r["sop_review_status"],
                "sop_concentration_flags": r["sop_concentration_flags"],
                "quote_fail_rate": r["quote_fail_rate"],
                "remove_top_pool_EV": r["remove_top_pool_EV"],
                "forward_vs_historical_decay_ev": r["forward_vs_historical_decay_ev"],
                "forward_vs_historical_decay": r["forward_vs_historical_decay"],
                "forward_vs_historical_pf_ratio": r["forward_vs_historical_pf_ratio"],
                "open_position_stuck_count": r["open_position_stuck_count"],
            },
            "historical_block": {
                "hist_n": r["hist_n"],
                "hist_pf": r["hist_pf"],
                "hist_wr": r["hist_wr"],
                "hist_ev": r["hist_ev"],
                "hist_payoff_ratio": r["hist_payoff_ratio"],
                "hist_max_drawdown": r["hist_max_drawdown"],
                "hist_top_pool_share": r["hist_top_pool_share"],
            },
        })
    return out


def family_aggregate(rows: list[dict]) -> list[dict]:
    by_f: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_f[r["family"]].append(r)

    fam_rows = []
    for fam, members in sorted(by_f.items()):
        total_n = sum(m["forward_n"] for m in members)
        weighted_ev = None
        if total_n > 0:
            num = sum((m["ev"] or 0) * m["forward_n"] for m in members if m["ev"] is not None)
            weighted_ev = round(num / total_n, 6)
        all_plumbing = all(m["forward_n"] < 20 for m in members)
        evs = [m["ev"] for m in members if m["ev"] is not None and m["forward_n"] > 0]

        fam_rows.append({
            "family": fam,
            "strategy_count": len(members),
            "strategy_ids": sorted(m["strategy_id"] for m in members),
            "total_forward_n_across_strategies": total_n,
            "weighted_forward_ev": weighted_ev,
            "median_forward_ev_among_active": round(statistics.median(evs), 6) if evs else None,
            "family_confidence": "N_LT_20_PLUMBING_ONLY_ALL_MEMBERS" if all_plumbing else "MIXED_OR_RANKABLE",
            "family_evidence_tag": "EARLY_HINT" if all_plumbing else "PARTIAL_OR_ACTIONABLE",
            "mean_hist_ev": round(
                statistics.mean([float(m["hist_ev"]) for m in members if m["hist_ev"] is not None]),
                6,
            )
            if any(m["hist_ev"] is not None for m in members)
            else None,
        })

    def sort_key(fr: dict):
        plumbing = 1 if fr["family_confidence"].startswith("N_LT") else 0
        n = fr["total_forward_n_across_strategies"]
        ev = fr["weighted_forward_ev"] if fr["weighted_forward_ev"] is not None else float("-inf")
        return (plumbing, -n, -ev)

    return sorted(fam_rows, key=sort_key)


def pick_top_strategy(forward_rows: list[dict]) -> dict:
    """Ranking discipline: only strategies with n>=20 compete for 'top'."""
    eligible = [r for r in forward_rows if r["forward_n"] >= 20]
    if eligible:
        best = max(eligible, key=lambda r: (r["ev"] or float("-inf"), r["pf"] or 0))
        return {
            "strategy_id": best["strategy_id"],
            "basis": "FORWARD_EV_THEN_PF_AMONG_N_GE_20",
            "confidence_bucket": best["confidence_bucket"],
            "forward_n": best["forward_n"],
        }
    best_hint = max(forward_rows, key=lambda r: (r["forward_n"], r["ev"] or float("-inf")))
    return {
        "strategy_id": best_hint["strategy_id"],
        "basis": "EARLY_HINT_MAX_FORWARD_N_THEN_EV_ALL_PLUMBING",
        "confidence_bucket": best_hint["confidence_bucket"],
        "forward_n": best_hint["forward_n"],
        "warning": "ALL_STRATEGIES_PLUMBING_SAMPLE_DO_NOT_RANK_AS_ALPHA",
    }


def pick_top_family(fam_rows: list[dict]) -> dict:
    eligible = [f for f in fam_rows if f["total_forward_n_across_strategies"] >= 20]
    if eligible:
        best = max(
            eligible,
            key=lambda f: (
                f["weighted_forward_ev"] if f["weighted_forward_ev"] is not None else float("-inf"),
                f["total_forward_n_across_strategies"],
            ),
        )
        return {
            "family": best["family"],
            "basis": "WEIGHTED_FORWARD_EV_AMONG_FAMILIES_WITH_TOTAL_N_GE_20",
            "total_forward_n": best["total_forward_n_across_strategies"],
            "weighted_forward_ev": best["weighted_forward_ev"],
        }
    best = max(fam_rows, key=lambda f: f["total_forward_n_across_strategies"])
    return {
        "family": best["family"],
        "basis": "EARLY_HINT_MAX_TOTAL_FORWARD_N_ACROSS_FAMILY",
        "total_forward_n": best["total_forward_n_across_strategies"],
        "weighted_forward_ev": best["weighted_forward_ev"],
        "warning": "ALL_FAMILIES_PLUMBING_AGGREGATE_SAMPLE",
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    flat_rows = []
    for r in rows:
        if isinstance(r.get("forward_block"), dict):
            flat = {"strategy_id": r["strategy_id"], "family": r["family"], "combined_view_policy": r["combined_view_policy"]}
            for k, v in r["forward_block"].items():
                flat[f"fwd_{k}"] = v
            for k, v in r["historical_block"].items():
                flat[f"hist_{k.replace('hist_', '')}" if k.startswith("hist_") else f"hist_{k}"] = v
            flat_rows.append(flat)
        else:
            flat_rows.append(r)
    fields = sorted({k for row in flat_rows for k in row.keys()})
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(flat_rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", default=True)
    args = ap.parse_args()

    now = datetime.now(tz=timezone.utc)
    reg = load_registry()
    sb_map = load_merged_scoreboard()
    plan_c = load_expansion_plan_candidates()

    watch_ids: list[str] = []
    if WATCHLIST074.exists():
        wl = json.loads(WATCHLIST074.read_text())
        watch_ids = [x["strategy_id"] for x in wl.get("top_dryrun_candidates", [])]

    if not watch_ids:
        watch_ids = sorted(sb_map.keys())

    expansion_ids = sorted(plan_c.keys())
    all_ids = sorted(set(watch_ids) | set(expansion_ids))

    rows = []
    for sid in all_ids:
        sb = sb_map.get(sid)
        if sb is None:
            if sid in plan_c:
                sb = sb_from_plan_candidate(plan_c[sid])
            elif reg.get(sid):
                sb = sb_from_registry_fallback(reg[sid])
            else:
                continue
        state = load_state_any(sid)
        rows.append(build_strategy_row(sid, sb, state, reg.get(sid), now))

    rows.sort(key=lambda r: r["strategy_id"])
    forward_only = forward_only_rows(rows)
    historical_only = historical_only_rows(rows)
    combined = combined_side_by_side(rows)
    families = family_aggregate(rows)

    top_s = pick_top_strategy(forward_only)
    top_f = pick_top_family(families)

    live_flag = __import__("os").environ.get("LIVE_TRADING_MODE", "").lower() in ("1", "true", "yes")

    resource_impact: dict = {
        "state_file_counts": {
            "sprint075_logs": len(list(STATE075.glob("*_state.json"))) if STATE075.exists() else 0,
            "sprint077_logs": len(list(STATE077.glob("*_state.json"))) if STATE077.exists() else 0,
        },
    }
    if RESOURCE077.exists():
        try:
            resource_impact["sprint077_last_run"] = json.loads(RESOURCE077.read_text())
        except Exception:
            resource_impact["sprint077_last_run"] = {"error": "parse_failed"}

    bundle = {
        "generated_at_utc": utc_now_iso(),
        "inputs": {
            "state_dirs": [str(STATE075), str(STATE077)],
            "scoreboard075": str(SCOREBOARD075),
            "scoreboard077": str(SCOREBOARD077),
            "expansion_plan077": str(EXPANSION_PLAN077),
            "registry074": str(REGISTRY074),
            "watchlist074": str(WATCHLIST074),
        },
        "policy_notes": [
            "Forward metrics drive deployment gates; historical is label backtest only.",
            "combined_side_by_side carries two blocks; never multiply or average into one alpha.",
            "forward_n < 20 → PLUMBING_ONLY / EARLY_HINT; PROMOTE forbidden.",
            "SOP concentration gates enforced: top_pool_share > 50% or top_sender_share > 70% downgrades review status and blocks promotion.",
            "0x6ff5-gated rows are subject-conditioned/router-artifact candidates until a true/false counterfactual proves differential edge.",
        ],
        "views": {
            "forward_only": forward_only,
            "historical_only": historical_only,
            "combined_side_by_side": combined,
        },
        "top_strategy_forward_discipline": top_s,
        "top_family_forward_discipline": top_f,
        "strategy_count": len(rows),
        "watchlist_count": len(watch_ids),
        "expansion_plan_count": len(expansion_ids),
        "resource_impact": resource_impact,
        "live_trading_mode_env_detected": live_flag,
    }
    (OUT_DIR / "sprint076_forward_leaderboard.json").write_text(json.dumps(bundle, indent=2, default=str))

    forward_flat = []
    for r in rows:
        forward_flat.append({
            "strategy_id": r["strategy_id"],
            "family": r["family"],
            "forward_n": r["forward_n"],
            "confidence_bucket": r["confidence_bucket"],
            "evidence_tag": r["evidence_tag"],
            "promotion_allowed_forward": r["promotion_allowed_forward"],
            "pf": r["pf"],
            "wr": r["wr"],
            "ev": r["ev"],
            "payoff_ratio": r["payoff_ratio"],
            "max_drawdown": r["max_drawdown"],
            "top_pool_share": r["top_pool_share"],
            "top_sender_share": r["top_sender_share"],
            "sop_review_status": r["sop_review_status"],
            "sop_concentration_flags": ",".join(r["sop_concentration_flags"]),
            "quote_fail_rate": r["quote_fail_rate"],
            "remove_top_pool_EV": r["remove_top_pool_EV"],
            "forward_vs_historical_decay_ev": r["forward_vs_historical_decay_ev"],
            "forward_vs_historical_decay": r["forward_vs_historical_decay"],
            "forward_vs_historical_pf_ratio": r["forward_vs_historical_pf_ratio"],
            "open_position_stuck_count": r["open_position_stuck_count"],
            "open_positions_open": r["open_positions_open"],
            "harness_segment": r.get("harness_segment"),
        })
    write_csv(OUT_DIR / "sprint076_forward_leaderboard.csv", forward_flat)

    (OUT_DIR / "sprint076_family_scoreboard.json").write_text(json.dumps(families, indent=2, default=str))
    write_csv(OUT_DIR / "sprint076_family_scoreboard.csv", families)

    all_plumbing = all(r["forward_n"] < 20 for r in rows)
    flags = {
        "PROMOTE_BLOCKED_ALL_FORWARD_PLUMBING": all_plumbing,
        "NO_PROMOTION_UNDER_N_20": True,
        "HISTORICAL_NOT_USED_AS_FORWARD_PROOF": True,
        "LIVE_TRADING_MODE_ENV_ON": live_flag,
        "OPEN_POSITION_STUCK_ANY": any(r["open_position_stuck_count"] > 0 for r in rows),
        "QUOTE_FAIL_NONZERO_ANY": any((r["quote_fail_rate"] or 0) > 0 for r in rows),
        "TOP_STRATEGY_IS_EARLY_HINT": top_s.get("warning") is not None,
        "TOP_FAMILY_IS_EARLY_HINT": top_f.get("warning") is not None,
    }

    (OUT_DIR / "sprint076_decision_flags.json").write_text(json.dumps(flags, indent=2))

    readout = "\n".join(
        [
            "# Sprint076 readout",
            "",
            f"- generated_at_utc: {bundle['generated_at_utc']}",
            f"- strategy_count: {len(rows)}",
            f"- top_strategy (disciplined): {json.dumps(top_s, ensure_ascii=False)}",
            f"- top_family (disciplined): {json.dumps(top_f, ensure_ascii=False)}",
            f"- all_forward_plumbing_lt_20: {all_plumbing}",
            f"- decision_flags: {json.dumps(flags, ensure_ascii=False)}",
            "",
            "## Artifacts",
            "",
            f"- {OUT_DIR / 'sprint076_forward_leaderboard.json'}",
            f"- {OUT_DIR / 'sprint076_forward_leaderboard.csv'}",
            f"- {OUT_DIR / 'sprint076_family_scoreboard.json'}",
            f"- {OUT_DIR / 'sprint076_family_scoreboard.csv'}",
            f"- {OUT_DIR / 'sprint076_decision_flags.json'}",
            "",
        ]
    )
    (OUT_DIR / "sprint076_readout.md").write_text(readout)

    print(json.dumps({"ok": True, "strategy_count": len(rows), "out_dir": str(OUT_DIR)}, indent=2))


if __name__ == "__main__":
    main()
