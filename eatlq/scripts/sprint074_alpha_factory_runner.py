#!/usr/bin/env python3
"""
Sprint074 — Ultra Alpha Factory / Portfolio-Style Paper Evaluation
===================================================================
Generates up to 100 strategy specs, runs bounded paper-shadow-style sims on
logs/sprint055_paper_shadow.csv, emits continuous scores (no PF/WR cliffs),
promotion bands, and dry-run watchlist. Does NOT touch live trading or main
paper shadow cron/state.

Outputs (mvp/audits/sprint074_alpha_factory/):
  alpha_factory_registry.json
  alpha_factory_scoreboard.json
  alpha_factory_scoreboard.csv
  alpha_factory_decision_flags.json
  alpha_factory_dryrun_watchlist.json
  sprint074_readout.md
  micro_live_sleeve_framework.md
"""
from __future__ import annotations

import csv
import json
import math
import pathlib
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from itertools import product

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC_CSV = REPO / "logs" / "sprint055_paper_shadow.csv"
OUT_DIR = REPO / "mvp" / "audits" / "sprint074_alpha_factory"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Sprint artifact references (read-only)
S072_DIR = REPO / "mvp" / "audits" / "sprint072_alpha_waterfall"
S073_REVIEW = REPO / "mvp" / "audits" / "sprint073_live_review"

GAS_PCT = 0.002
TRADE_SIZE = 10.0
INITIAL_EQ = 100.0
MAX_CANDIDATES = 100

HORIZONS = ["30m", "1h", "2h", "4h", "6h", "12h", "24h"]
TRAILING_STOP_PCT = -0.20


def load_rows() -> list[dict]:
    rows = []
    with open(SRC_CSV) as f:
        for r in csv.DictReader(f):
            if r.get("entry_quote_ok") != "True":
                continue
            row = {
                "signal_time": r["signal_time"],
                "pool": r["pool"],
                "fee_bps": float(r["fee_bps"]) if r.get("fee_bps") else 0.0,
                "pool_age_sec": float(r["pool_age_sec"]) if r.get("pool_age_sec") else None,
                "onset_lag_sec": float(r["onset_lag_sec"]) if r.get("onset_lag_sec") else 0.0,
                "activity_heat": float(r["activity_heat"]) if r.get("activity_heat") else 0.0,
                "buy_pressure_ratio": float(r["buy_pressure_ratio"])
                if r.get("buy_pressure_ratio")
                else 0.0,
                "price_velocity_5m": float(r["price_velocity_5m"])
                if r.get("price_velocity_5m")
                else 0.0,
                "composite_flag": (r.get("composite_flag") or "").strip(),
                "entry_fail_reason": (r.get("entry_fail_reason") or "").strip(),
            }
            for h in HORIZONS:
                ok_k = f"exit_{h}_ok"
                row[f"ok_{h}"] = r.get(ok_k) == "True"
                raw_k = f"net_return_{h}"
                ag_k = f"net_return_{h}_ag"
                row[f"ret_{h}"] = float(r[raw_k]) if r.get(raw_k) else None
                row[f"retag_{h}"] = float(r[ag_k]) if r.get(ag_k) else None
            rows.append(row)
    return sorted(rows, key=lambda x: x["signal_time"])


def _parse_ts(st: str) -> datetime:
    s = st.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def simulate(
    rows: list[dict],
    *,
    strategy_id: str,
    exit_horizon: str,
    cooldown_sec: int,
    cooldown_anchor: str,
    age_gate: tuple[float, float] | None,
    heat_min: float,
    bpr_min: float,
    pv_min: float | None,
    fee_max_bps: float | None,
    onset_lag_max: float | None,
    trailing_stop: bool,
    trailing_stop_pct: float,
    momentum_gate: bool,
) -> dict:
    """Paper-shadow-style single-pass sim (entry-anchor cooldown model)."""
    cash = INITIAL_EQ
    open_pos: dict[str, str] = {}
    pool_last_action: dict[str, str] = {}
    trades: list[dict] = []
    skipped = {
        "cooldown": 0,
        "concurrent": 0,
        "capital": 0,
        "no_exit": 0,
        "age_gate": 0,
        "heat": 0,
        "bpr": 0,
        "fee": 0,
        "onset": 0,
        "pv": 0,
        "momentum": 0,
    }
    max_concurrent = 3
    rows_seen = 0

    for row in rows:
        rows_seen += 1
        pool = row["pool"]
        st = row["signal_time"]

        if fee_max_bps is not None and row["fee_bps"] > fee_max_bps:
            skipped["fee"] += 1
            continue
        if onset_lag_max is not None and row["onset_lag_sec"] > onset_lag_max:
            skipped["onset"] += 1
            continue
        if age_gate is not None:
            age = row.get("pool_age_sec")
            if age is None or not (age_gate[0] <= age <= age_gate[1]):
                skipped["age_gate"] += 1
                continue
        if row["activity_heat"] < heat_min:
            skipped["heat"] += 1
            continue
        if row["buy_pressure_ratio"] < bpr_min:
            skipped["bpr"] += 1
            continue
        if pv_min is not None and row["price_velocity_5m"] < pv_min:
            skipped["pv"] += 1
            continue
        if momentum_gate:
            if not (
                row["buy_pressure_ratio"] >= 0.55 and row["price_velocity_5m"] > 0
            ):
                skipped["momentum"] += 1
                continue

        ok_h = row.get(f"ok_{exit_horizon}")
        ret_ag = row.get(f"retag_{exit_horizon}")
        if not ok_h or ret_ag is None:
            skipped["no_exit"] += 1
            continue

        if cash < TRADE_SIZE:
            skipped["capital"] += 1
            continue
        if pool in open_pos:
            skipped["concurrent"] += 1
            continue
        if len(open_pos) >= max_concurrent:
            skipped["concurrent"] += 1
            continue

        last_act = pool_last_action.get(pool)
        if last_act is not None:
            elapsed = (_parse_ts(st) - _parse_ts(last_act)).total_seconds()
            if elapsed < cooldown_sec:
                skipped["cooldown"] += 1
                continue

        open_pos[pool] = st
        cash -= TRADE_SIZE
        if cooldown_anchor == "entry":
            pool_last_action[pool] = st

        ret_eff = ret_ag
        eff_h = exit_horizon
        if trailing_stop:
            h_order = ["30m", "1h", "2h", "4h", "6h", "12h", "24h"]
            try:
                h_idx = h_order.index(exit_horizon)
            except ValueError:
                h_idx = len(h_order) - 1
            for h_i in range(h_idx + 1):
                h = h_order[h_i]
                r_h = row.get(f"ret_{h}")
                if r_h is not None and r_h < trailing_stop_pct:
                    ret_eff = r_h - GAS_PCT
                    eff_h = h
                    break

        pnl = TRADE_SIZE * ret_eff
        cash += TRADE_SIZE + pnl
        open_pos.pop(pool, None)

        if cooldown_anchor == "exit":
            pool_last_action[pool] = st

        trades.append(
            {
                "strategy_id": strategy_id,
                "signal_time": st,
                "pool": pool,
                "fee_bps": row["fee_bps"],
                "ret_ag": round(ret_eff, 6),
                "pnl": round(pnl, 6),
                "win": pnl > 0,
                "eff_horizon": eff_h,
            }
        )

    n = len(trades)
    if n == 0:
        return {
            "strategy_id": strategy_id,
            "n_trades": 0,
            "trades": [],
            "skipped": skipped,
            "rows_seen": rows_seen,
            "equity": INITIAL_EQ,
            "mean_ret": None,
            "median_ret": None,
            "wr": None,
            "pf": None,
            "max_dd": None,
            "pool_top_share": None,
            "payoff_ratio": None,
        }

    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [abs(t["pnl"]) for t in trades if t["pnl"] <= 0]
    rets = [t["ret_ag"] for t in trades]
    wr = len(wins) / n
    pf = sum(wins) / sum(losses) if losses else float("inf")
    mean_win = statistics.mean(wins) if wins else 0.0
    mean_loss = statistics.mean(losses) if losses else 0.0
    payoff_ratio = (mean_win / mean_loss) if mean_loss > 0 else float("inf")

    by_pool: dict[str, int] = defaultdict(int)
    for t in trades:
        by_pool[t["pool"]] += 1
    top_share = max(by_pool.values()) / n

    eq = INITIAL_EQ
    peak = eq
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: x["signal_time"]):
        eq += t["pnl"]
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    equity = INITIAL_EQ + sum(t["pnl"] for t in trades)

    return {
        "strategy_id": strategy_id,
        "n_trades": n,
        "trades": trades,
        "skipped": skipped,
        "rows_seen": rows_seen,
        "equity": round(equity, 4),
        "mean_ret": round(statistics.mean(rets), 6),
        "median_ret": round(statistics.median(rets), 6),
        "wr": round(wr, 4),
        "pf": round(pf, 4) if pf != float("inf") else None,
        "max_dd": round(max_dd, 4),
        "pool_top_share": round(top_share, 4),
        "payoff_ratio": round(payoff_ratio, 4) if payoff_ratio != float("inf") else None,
    }


def executability(exit_horizon: str, trailing: bool) -> dict:
    """Label executor alignment; 4h+ requires hard exit — see Operations.md."""
    long_h = exit_horizon in ("4h", "6h", "12h", "24h")
    aligned_30m = exit_horizon == "30m" and not trailing
    # Executor default monitoring uses time_decay_min=240 but NOT hard winner exit.
    return {
        "live_executor_hold_alignment": "30m_baseline" if aligned_30m else "extended_hold",
        "hard_horizon_exit_required": long_h or trailing,
        "blocks_live_queue_until": (
            "EXECUTOR_HARD_EXIT_OR_STRATEGY_SCHEDULED_CLOSE"
            if (long_h or exit_horizon in ("2h", "1h"))
            else None
        ),
        "research_horizon_labels_ok": True,
    }


def build_specs() -> list[dict]:
    """Exactly MAX_CANDIDATES deterministic specs from CSV-only features."""
    exits = ["30m", "1h", "2h", "4h", "6h"]
    cooldowns = [
        (1800, "entry"),
        (2700, "entry"),
        (3600, "entry"),
        (5400, "exit"),
    ]
    ages: list[tuple[float, float] | None] = [
        None,
        (3600.0, 100_000.0),
        (1800.0, 86_400.0),
        (7200.0, 150_000.0),
    ]
    heat_mins = [0.0, 4.0, 8.0]
    bpr_mins = [0.0, 0.42, 0.52]
    fee_caps: list[float | None] = [None, 100.0]
    onset_caps: list[float | None] = [None, 600.0]
    pv_modes: list[float | None] = [None, 0.0]  # None=no filter; 0 = mild bullish
    momentum = [False, True]

    raw = list(
        product(exits, cooldowns, ages, heat_mins, bpr_mins, fee_caps, onset_caps, pv_modes, momentum)
    )
    raw.sort(
        key=lambda x: (
            x[0],
            x[1][0],
            x[1][1],
            str(x[2]),
            x[3],
            x[4],
            x[5] if x[5] is not None else -1,
            x[6] if x[6] is not None else 9e18,
            float("-inf") if x[7] is None else x[7],
            x[8],
        )
    )

    # Stratified round-robin so all horizons appear (sorted raw biases first 100 to 30m-only).
    by_exit: dict[str, list] = {e: [] for e in exits}
    for tup in raw:
        by_exit[tup[0]].append(tup)

    stratified: list = []
    ptr = 0
    while len(stratified) < MAX_CANDIDATES and any(by_exit.values()):
        ex = exits[ptr % len(exits)]
        ptr += 1
        if by_exit[ex]:
            stratified.append(by_exit[ex].pop(0))

    specs: list[dict] = []
    for i, x in enumerate(stratified[:MAX_CANDIDATES]):
        (
            ex,
            (cd_sec, cd_anchor),
            age,
            hm,
            bm,
            fee_cap,
            onset_cap,
            pv_min,
            mom,
        ) = x
        sid = f"S074_{i:03d}"
        trail = i % 17 == 0 and ex in ("4h", "6h")  # sparse trailing variants
        specs.append(
            {
                "strategy_id": sid,
                "exit_horizon": ex,
                "cooldown_sec": cd_sec,
                "cooldown_anchor": cd_anchor,
                "age_gate": list(age) if age else None,
                "heat_min": hm,
                "bpr_min": bm,
                "fee_max_bps": fee_cap,
                "onset_lag_max_sec": onset_cap,
                "pv_min": pv_min,
                "momentum_gate": mom,
                "trailing_stop": trail,
                "trailing_stop_pct": TRAILING_STOP_PCT if trail else None,
                "feature_sources": [
                    "sprint055_paper_shadow.csv: pool_age_sec, activity_heat, buy_pressure_ratio, price_velocity_5m, fee_bps, onset_lag_sec, net_return_*_ag",
                ],
                "data_source": str(SRC_CSV.relative_to(REPO)),
                "notes": "Simulation uses entry-anchor cooldown; exit-anchor is approximate (same signal_time last-action update). Historical paper labels — not mixed with forward-only metrics.",
            }
        )
    return specs


def continuous_scores(res: dict) -> dict:
    """Soft scoring — no hard PF/WR eliminate."""
    n = res["n_trades"]
    pf = res["pf"]
    wr = res["wr"]
    mean_ret = res["mean_ret"] or 0.0
    dd = res["max_dd"] or 0.0
    conc = res["pool_top_share"] or 1.0
    skipped = res["skipped"]
    rows_seen = max(res["rows_seen"], 1)
    no_exit_rate = skipped.get("no_exit", 0) / rows_seen

    sample_conf = 1.0 - math.exp(-n / 35.0)
    ev_score = math.tanh(mean_ret * 12.0)
    pf_score = min((pf or 0.0) / 8.0, 1.0) if pf else 0.0
    wr_score = wr or 0.0
    pr = res["payoff_ratio"] or 0.0
    pr_score = min(pr / 5.0, 1.0) if pr else 0.0
    dd_score = max(0.0, 1.0 - dd * 3.0)
    conc_score = max(0.0, 1.0 - conc * 1.2)
    label_health = max(0.0, 1.0 - min(no_exit_rate * 4.0, 1.0))

    risk_penalty = 0.15 * conc + 0.10 * dd + 0.10 * no_exit_rate
    composite = (
        0.22 * ev_score
        + 0.18 * pf_score
        + 0.12 * wr_score
        + 0.12 * pr_score
        + 0.14 * dd_score
        + 0.12 * conc_score
        + 0.10 * label_health
    )
    composite *= sample_conf
    composite *= max(0.35, 1.0 - risk_penalty)

    return {
        "sample_confidence": round(sample_conf, 4),
        "ev_score": round(ev_score, 4),
        "pf_score": round(pf_score, 4),
        "wr_score": round(wr_score, 4),
        "payoff_ratio_score": round(pr_score, 4),
        "drawdown_score": round(dd_score, 4),
        "pool_concentration_score": round(conc_score, 4),
        "label_coverage_score": round(label_health, 4),
        "quote_miss_proxy": round(no_exit_rate, 4),
        "risk_penalty": round(risk_penalty, 4),
        "composite_score": round(composite, 4),
    }


def promotion_band(spec: dict, scores: dict, res: dict) -> str:
    """Soft bands — not cliff gates."""
    n = res["n_trades"]
    cs = scores["composite_score"]
    ex = spec["exit_horizon"]
    hard = executability(ex, spec["trailing_stop"])["hard_horizon_exit_required"]

    if n < 8:
        return "EXPLORE"
    if scores["sample_confidence"] < 0.35:
        return "KEEP_SAMPLING"
    if scores["ev_score"] < -0.3 and (res["pf"] or 0) < 0.95:
        return "PAUSE"

    if cs >= 0.52 and n >= 22 and not hard:
        prelim = "PROMOTE_DRYRUN"
    elif cs >= 0.48 and n >= 18:
        prelim = "PROMOTE_DRYRUN" if not hard else "KEEP_SAMPLING"
    elif hard:
        prelim = "KEEP_SAMPLING"
    elif cs >= 0.38:
        prelim = "KEEP_SAMPLING"
    elif cs >= 0.22:
        prelim = "EXPLORE"
    else:
        prelim = "EXPLORE"

    if live_queue_candidate(spec, scores, prelim):
        return "LIVE_QUEUE_CANDIDATE"

    return prelim


def live_queue_candidate(spec: dict, scores: dict, preliminary_band: str) -> bool:
    if preliminary_band == "PAUSE":
        return False
    if executability(spec["exit_horizon"], spec["trailing_stop"])["hard_horizon_exit_required"]:
        return False
    if spec["exit_horizon"] not in ("30m",):
        return False
    if spec["trailing_stop"]:
        return False
    return scores["composite_score"] >= 0.45 and scores["sample_confidence"] >= 0.5


def rationale(spec: dict, res: dict, scores: dict) -> str:
    parts = [
        f"exit={spec['exit_horizon']}",
        f"n={res['n_trades']}",
        f"EV~{res['mean_ret']}",
        f"PF={res['pf']}",
        f"WR={res['wr']}",
        f"top_pool={res['pool_top_share']}",
        f"dd={res['max_dd']}",
        f"composite={scores['composite_score']}",
    ]
    if spec["momentum_gate"]:
        parts.append("momentum_filter=BPR>=0.55&PV>0")
    if spec["fee_max_bps"]:
        parts.append(f"fee<={spec['fee_max_bps']}")
    return "; ".join(str(p) for p in parts)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = load_rows()
    specs = build_specs()

    registry = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "candidate_target": MAX_CANDIDATES,
        "candidate_actual": len(specs),
        "csv_rows_loaded": len(rows),
        "candidate_sampling": "stratified_round_robin_by_exit_horizon",
        "artifacts_consumed": [
            str(S072_DIR.relative_to(REPO)) + "/*",
            str(S073_REVIEW.relative_to(REPO)) + "/*",
        ],
        "strategies": [],
    }

    score_rows: list[dict] = []

    for spec in specs:
        sid = spec["strategy_id"]
        res = simulate(
            rows,
            strategy_id=sid,
            exit_horizon=spec["exit_horizon"],
            cooldown_sec=spec["cooldown_sec"],
            cooldown_anchor=spec["cooldown_anchor"],
            age_gate=tuple(spec["age_gate"]) if spec["age_gate"] else None,
            heat_min=spec["heat_min"],
            bpr_min=spec["bpr_min"],
            pv_min=spec["pv_min"],
            fee_max_bps=spec["fee_max_bps"],
            onset_lag_max=spec["onset_lag_max_sec"],
            trailing_stop=bool(spec["trailing_stop"]),
            trailing_stop_pct=spec["trailing_stop_pct"] or TRAILING_STOP_PCT,
            momentum_gate=spec["momentum_gate"],
        )
        scores = continuous_scores(res)
        exe = executability(spec["exit_horizon"], spec["trailing_stop"])
        band = promotion_band(spec, scores, res)
        lqc = live_queue_candidate(spec, scores, band)

        entry_rule = (
            f"fee<={spec['fee_max_bps']}" if spec["fee_max_bps"] else "fee:any"
        )
        entry_rule += f"; heat>={spec['heat_min']}; bpr>={spec['bpr_min']}"
        if spec["age_gate"]:
            entry_rule += f"; age∈[{spec['age_gate'][0]},{spec['age_gate'][1]}]"
        if spec["onset_lag_max_sec"] is not None:
            entry_rule += f"; onset_lag<={spec['onset_lag_max_sec']}"
        if spec["pv_min"] is not None:
            entry_rule += f"; pv_5m>={spec['pv_min']}"
        if spec["momentum_gate"]:
            entry_rule += "; momentum(BPR>=0.55 & PV>0)"
        if spec["trailing_stop"]:
            entry_rule += "; trailing_stop_proxy"

        risk_labels = []
        if (res["pool_top_share"] or 0) > 0.55:
            risk_labels.append("POOL_CONCENTRATION")
        if spec["exit_horizon"] in ("4h", "6h", "12h", "24h"):
            risk_labels.append("LONG_HOLD_NOT_HARD_EXIT_LIVE")
        if spec["exit_horizon"] in ("1h", "2h"):
            risk_labels.append("EXTENDED_HOLD_VS_DEFAULT_EXECUTOR_MONITOR")
        if scores["quote_miss_proxy"] > 0.25:
            risk_labels.append("LABEL_SPARSE")
        if res["max_dd"] and res["max_dd"] > 0.35:
            risk_labels.append("DEEP_DRAWDOWN")

        reg_entry = {
            "strategy_id": sid,
            "feature_sources": spec["feature_sources"],
            "entry_rule": entry_rule,
            "exit_rule": f"close_at_label_{spec['exit_horizon']}_ag"
            + ("+trail_proxy" if spec["trailing_stop"] else ""),
            "executability": exe,
            "data_source": spec["data_source"],
            "risk_labels": risk_labels,
            "cooldown_sec": spec["cooldown_sec"],
            "cooldown_anchor_model": spec["cooldown_anchor"],
            "promotion_band": band,
            "live_queue_candidate_flag": lqc,
            "scores": scores,
            "metrics": {k: res[k] for k in res if k not in ("trades",)},
        }
        registry["strategies"].append(reg_entry)

        flat = {
            "strategy_id": sid,
            "exit_horizon": spec["exit_horizon"],
            "n_trades": res["n_trades"],
            "equity": res["equity"],
            "mean_ret": res["mean_ret"],
            "pf": res["pf"],
            "wr": res["wr"],
            "max_dd": res["max_dd"],
            "pool_top_share": res["pool_top_share"],
            "payoff_ratio": res["payoff_ratio"],
            "promotion_band": band,
            "live_queue_candidate_flag": lqc,
            **scores,
        }
        flat["rationale"] = rationale(spec, res, scores)
        score_rows.append((flat, spec, res))

    score_rows.sort(key=lambda x: -x[0]["composite_score"])

    promote_sorted = sorted(
        [x for x in score_rows if x[0]["promotion_band"] == "PROMOTE_DRYRUN"],
        key=lambda x: -x[0]["composite_score"],
    )
    picked: list = []
    seen = set()
    for item in promote_sorted:
        if len(picked) >= 10:
            break
        picked.append(item)
        seen.add(item[0]["strategy_id"])
    for item in score_rows:
        if len(picked) >= 10:
            break
        sid = item[0]["strategy_id"]
        if sid in seen:
            continue
        if item[0]["promotion_band"] == "PAUSE":
            continue
        picked.append(item)
        seen.add(sid)

    top10 = picked[:10]

    watchlist = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "policy": "Forward dry-run racing pool — PROMOTE_DRYRUN first, then highest composite; no Redis signals.",
        "top_dryrun_candidates": [],
    }
    for flat, spec, res in top10:
        watchlist["top_dryrun_candidates"].append(
            {
                "strategy_id": flat["strategy_id"],
                "composite_score": flat["composite_score"],
                "promotion_band": flat["promotion_band"],
                "why": flat["rationale"],
                "risk_labels": next(
                    (
                        e["risk_labels"]
                        for e in registry["strategies"]
                        if e["strategy_id"] == flat["strategy_id"]
                    ),
                    [],
                ),
                "executable_note": (
                    "Requires hard exit implementation before any live sleeve"
                    if spec["exit_horizon"] in ("4h", "6h", "12h", "24h")
                    else "Shorter horizon — still verify executor timer vs labels"
                ),
            }
        )

    hard_exit_required = sorted(
        {
            s["strategy_id"]
            for s in registry["strategies"]
            if s["executability"]["hard_horizon_exit_required"]
        }
    )

    flags = {
        "sprint": "074",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "MAIN_PAPER_SHADOW_UNTOUCHED": True,
        "NO_LIVE_TRADING_MODE": True,
        "NO_PRODUCTION_SIGNALS": True,
        "candidate_count": len(specs),
        "bands_histogram": {},
        "hard_exit_strategy_ids_count": len(hard_exit_required),
        "hard_exit_strategy_ids": hard_exit_required,
        "top_composite": score_rows[0][0]["composite_score"] if score_rows else None,
        "next_actions": [
            "Wire sprint069-style forward harness per PROMOTE_DRYRUN ids (isolated logs only).",
            "Implement EXECUTOR_HARD_EXIT_MIN or strategy-scheduled SELL for long-hold winners.",
            "Refresh Alpha Factory after CSV grows; rolling dashboard: re-run this runner.",
            "Keep A6 / fee-expanded crons unchanged.",
        ],
    }
    for flat, _spec, __res in score_rows:
        b = flat["promotion_band"]
        flags["bands_histogram"][b] = flags["bands_histogram"].get(b, 0) + 1

    (OUT_DIR / "alpha_factory_registry.json").write_text(
        json.dumps(registry, indent=2, default=str)
    )
    (OUT_DIR / "alpha_factory_scoreboard.json").write_text(
        json.dumps([x[0] for x in score_rows], indent=2, default=str)
    )

    csv_path = OUT_DIR / "alpha_factory_scoreboard.csv"
    if score_rows:
        keys = list(score_rows[0][0].keys())
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            for flat, _, _ in score_rows:
                w.writerow(flat)

    (OUT_DIR / "alpha_factory_decision_flags.json").write_text(
        json.dumps(flags, indent=2, default=str)
    )
    (OUT_DIR / "alpha_factory_dryrun_watchlist.json").write_text(
        json.dumps(watchlist, indent=2, default=str)
    )

    sleeve = """# Micro-Live Sleeve Framework (design only — NOT ENABLED)

## Principles
- Max 1 concurrent micro sleeve; notional cap **10 USD**; kill switch via Redis `SYSTEM_PANIC`.
- Sleeve registry lists pool allowlist + strategy_id + executor env fingerprint (no secrets).
- Two-stage promotion: `PROMOTE_DRYRUN` forward positive EV → human ack → sleeve dry-run on fork/anvil → optional micro-live.

## Preconditions (all required)
1. Strategy exit horizon matches executor behavior OR hard exit shipped + smoke log archived.
2. `EXECUTOR_DRY_RUN=true` until explicit ops toggle; never commit `.env`.
3. Rolling dashboard band ∈ {`PROMOTE_DRYRUN`, `LIVE_QUEUE_CANDIDATE`} with sample_confidence ≥ 0.5 forward slice (kept separate from historical composite).

## Forbidden
- No automatic promotion to LIVE_TRADING_MODE.
- No blending forward and historical scores in one headline number.

---
Generated by sprint074_alpha_factory_runner.py (research only).
"""
    (OUT_DIR / "micro_live_sleeve_framework.md").write_text(sleeve)

    changed_files = [
        "alpha_factory/README.md",
        "scripts/sprint074_alpha_factory_runner.py",
        "mvp/audits/sprint074_alpha_factory/*",
    ]
    readout = f"""# Sprint074 Readout (contract summary)

## Commands run
`python3 scripts/sprint074_alpha_factory_runner.py`  
`python3 scripts/sprint074_alpha_factory_dashboard.py`

## Changed / added files
- `alpha_factory/README.md`
- `scripts/sprint074_alpha_factory_runner.py`
- `scripts/sprint074_alpha_factory_dashboard.py`
- `mvp/audits/sprint074_alpha_factory/alpha_factory_registry.json`
- `mvp/audits/sprint074_alpha_factory/alpha_factory_scoreboard.json`
- `mvp/audits/sprint074_alpha_factory/alpha_factory_scoreboard.csv`
- `mvp/audits/sprint074_alpha_factory/alpha_factory_decision_flags.json`
- `mvp/audits/sprint074_alpha_factory/alpha_factory_dryrun_watchlist.json`
- `mvp/audits/sprint074_alpha_factory/alpha_factory_rolling_dashboard.json`
- `mvp/audits/sprint074_alpha_factory/micro_live_sleeve_framework.md`
- `mvp/audits/sprint074_alpha_factory/sprint074_readout.md`

## Candidate count
**{len(specs)}** strategies (target {MAX_CANDIDATES})

## Top candidates (composite)
"""
    for i, (flat, _, _) in enumerate(top10, 1):
        readout += f"{i}. `{flat['strategy_id']}` score={flat['composite_score']} band={flat['promotion_band']} — {flat['rationale']}\n"

    readout += "\n## Decision flags (see alpha_factory_decision_flags.json)\n"
    readout += json.dumps(flags, indent=2)
    readout += "\n\n## Next actions\n"
    for a in flags["next_actions"]:
        readout += f"- {a}\n"
    readout += f"\n## Hard 4h+ exit required (live queue blocked): **{len(hard_exit_required)}** ids (listed in registry executability).\n"

    (OUT_DIR / "sprint074_readout.md").write_text(readout)

    print(json.dumps(flags, indent=2))


if __name__ == "__main__":
    main()
