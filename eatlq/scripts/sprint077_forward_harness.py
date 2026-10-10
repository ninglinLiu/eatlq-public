#!/usr/bin/env python3
"""
Sprint077 — Forward race expansion harness + expansion plan generator
=======================================================================
Adds 10–14 isolated strategies (logs/sprint077/) with diversified horizons /
families from Alpha Factory registry. Enables hard horizon synthetic exit for
4h/6h/12h/24h when labels lag (paper/dry-run only).

Does NOT modify Sprint075. No Redis, no live trading.

Cron (offset :09):
  9-59/5 * * * * cd /opt/EATLQ && python3 scripts/sprint077_forward_harness.py \\
      --once >> logs/sprint077/harness_cron.log 2>&1

Generate / refresh expansion plan only:
  python3 scripts/sprint077_forward_harness.py --generate-plan
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import sys

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from forward_race_harness_core import (
    FORWARD_CUTOFF_DEFAULT,
    append_summary,
    forward_metrics,
    load_csv_split,
    run_strategy,
    utc_now,
)

REPO = Path(__file__).resolve().parent.parent
SRC_CSV = REPO / "logs" / "sprint055_paper_shadow.csv"
STATE_DIR = REPO / "logs" / "sprint077"
OUT_DIR = REPO / "mvp" / "audits" / "sprint077_forward_expansion"
REGISTRY = REPO / "mvp" / "audits" / "sprint074_alpha_factory" / "alpha_factory_registry.json"
WATCHLIST = REPO / "mvp" / "audits" / "sprint074_alpha_factory" / "alpha_factory_dryrun_watchlist.json"

STATE_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)

BAND_PRI = {
    "LIVE_QUEUE_CANDIDATE": 4,
    "KEEP_SAMPLING": 3,
    "PROMOTE_DRYRUN": 3,
    "EXPLORE": 2,
    "PAUSE": 0,
}

TARGET_EXPANSION = 14
MIN_EXPANSION = 10


def log(msg: str) -> None:
    print(f"[s077] {utc_now().strftime('%H:%M:%SZ')} {msg}", flush=True)


def watchlist_ids() -> set[str]:
    if not WATCHLIST.exists():
        return set()
    wl = json.loads(WATCHLIST.read_text())
    return {x["strategy_id"] for x in wl.get("top_dryrun_candidates", [])}


def family_from_entry_rule(rule: str) -> str:
    r = (rule or "").lower()
    if "buyer_diversity" in r or "diversity" in r:
        return "diversity"
    if "momentum(" in r:
        return "momentum"
    if "pv_5m>=" in r:
        return "pv"
    if "fee<=" in r:
        return "fee"
    return "baseline"


def parse_exit_horizon(exit_rule: str) -> str:
    import re

    m = re.search(r"label_(\d+m|\d+h)_", exit_rule or "")
    return m.group(1) if m else "2h"


def registry_to_spec(entry: dict) -> dict:
    rule = entry.get("entry_rule") or ""
    xr = entry.get("exit_rule") or ""
    fee_max = None
    if "fee<=" in rule:
        import re

        m = re.search(r"fee<=([\d.]+)", rule)
        if m:
            fee_max = float(m.group(1))
    pv_min = None
    if "pv_5m>=" in rule:
        import re

        m = re.search(r"pv_5m>=([\d.-]+)", rule)
        if m:
            pv_min = float(m.group(1))
    momentum_gate = "momentum(" in rule
    metrics = entry.get("metrics") or {}
    scores = entry.get("scores") or {}
    ex = entry.get("executability") or {}
    return {
        "strategy_id": entry["strategy_id"],
        "exit_horizon": parse_exit_horizon(xr),
        "fee_max_bps": fee_max,
        "pv_min": pv_min,
        "momentum_gate": momentum_gate,
        "hist_pf": metrics.get("pf"),
        "hist_wr": metrics.get("wr"),
        "hist_n": metrics.get("n_trades"),
        "hist_composite": scores.get("composite_score"),
        "hard_horizon_exit": bool(ex.get("hard_horizon_exit_required")),
        "promotion_band": entry.get("promotion_band"),
        "exit_rule": xr,
        "entry_rule": rule,
        "family": family_from_entry_rule(rule),
    }


def select_candidates(parsed: list[dict], top10: set[str]) -> tuple[list[dict], list[str]]:
    """Return (specs, selection_notes)."""
    notes: list[str] = []
    pool = [p for p in parsed if p["strategy_id"] not in top10]
    pool.sort(
        key=lambda x: (
            BAND_PRI.get(x["promotion_band"], 0),
            x["hist_composite"] if x["hist_composite"] is not None else -1.0,
        ),
        reverse=True,
    )

    chosen: list[dict] = []
    chosen_ids: set[str] = set()
    seen_pair: set[tuple[str, str]] = set()

    need_horizons = {"30m", "1h", "2h", "4h", "6h"}
    for h in sorted(need_horizons):
        picked = False
        for p in pool:
            if p["strategy_id"] in chosen_ids:
                continue
            if p["exit_horizon"] != h:
                continue
            chosen.append(p)
            chosen_ids.add(p["strategy_id"])
            seen_pair.add((h, p["family"]))
            notes.append(f"Horizon_cover_pick:{p['strategy_id']}:{h}:{p['family']}")
            picked = True
            break
        if not picked:
            notes.append(f"Bottleneck:no_candidate_for_horizon:{h}")

    for p in pool:
        if len(chosen) >= TARGET_EXPANSION:
            break
        if p["strategy_id"] in chosen_ids:
            continue
        pair = (p["exit_horizon"], p["family"])
        if pair in seen_pair and len(chosen) >= MIN_EXPANSION:
            continue
        hc = Counter(s["exit_horizon"] for s in chosen)
        if hc[p["exit_horizon"]] >= 3:
            continue
        chosen.append(p)
        chosen_ids.add(p["strategy_id"])
        seen_pair.add(pair)
        notes.append(f"Diversify_pick:{p['strategy_id']}:{pair}")

    for p in pool:
        if len(chosen) >= TARGET_EXPANSION:
            break
        if p["strategy_id"] in chosen_ids:
            continue
        hc = Counter(s["exit_horizon"] for s in chosen)
        if hc[p["exit_horizon"]] >= 3 and len(chosen) >= MIN_EXPANSION:
            continue
        chosen.append(p)
        chosen_ids.add(p["strategy_id"])
        notes.append(f"Fill_composite_pick:{p['strategy_id']}:{p['exit_horizon']}")

    for p in pool:
        if len(chosen) >= TARGET_EXPANSION:
            break
        if p["strategy_id"] in chosen_ids:
            continue
        chosen.append(p)
        chosen_ids.add(p["strategy_id"])
        notes.append(f"Fill_relaxed_horizon_cap:{p['strategy_id']}")

    if len(chosen) < MIN_EXPANSION:
        notes.append(f"Bottleneck:only_{len(chosen)}_candidates_below_min_{MIN_EXPANSION}")

    return chosen, notes


def write_expansion_plan(specs: list[dict], notes: list[str]) -> Path:
    doc = {
        "generated_at_utc": datetime.now(tz=timezone.utc).isoformat(),
        "policy": (
            "Expansion batch for isolated forward-only harness (logs/sprint077). "
            "Excludes Sprint074 top10 dry-run watchlist. Prioritizes horizon coverage "
            "then (horizon,family) diversity then composite score."
        ),
        "target_expansion_strategies": TARGET_EXPANSION,
        "min_expansion_strategies": MIN_EXPANSION,
        "actual_expansion_count": len(specs),
        "candidates": specs,
        "selection_notes": notes,
        "hard_exit_label_wait_sec": 3600,
        "no_redis_no_live": True,
    }
    path = OUT_DIR / "sprint077_expansion_plan.json"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, default=str))
    return path


def write_sprint077_decision_flags(specs: list[dict]) -> None:
    flags_path = REPO / "mvp" / "audits" / "sprint074_alpha_factory" / "alpha_factory_decision_flags.json"
    hard_all: set[str] = set()
    if flags_path.exists():
        hard_all = set(json.loads(flags_path.read_text()).get("hard_exit_strategy_ids", []))
    long_ids = {s["strategy_id"] for s in specs if s["exit_horizon"] in ("4h", "6h", "12h", "24h")}
    unlocked = sorted(long_ids)
    still_blocked = sorted(hard_all - long_ids)
    doc = {
        "sprint": "077",
        "hard_exit_unlocked_strategy_ids": unlocked,
        "hard_exit_unlock_note": (
            "Sprint077 expansion harness closes long-hold legs with SCHEDULED_CLOSE_SYNTHETIC_FLAT "
            "after CSV label grace (dry-run only; no Redis)."
        ),
        "still_blocked_strategy_ids": still_blocked,
        "still_blocked_note": (
            "Alpha Factory hard_exit_strategy_ids not deployed in this expansion batch remain "
            "without harness hard-close smoke coverage."
        ),
        "expansion_batch_ids": sorted(s["strategy_id"] for s in specs),
        "NO_LIVE_TRADING_MODE_REQUIRED": True,
        "NO_REDIS_PRODUCTION_SIGNALS": True,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "sprint077_decision_flags.json").write_text(json.dumps(doc, indent=2, default=str))


def generate_plan() -> Path:
    top10 = watchlist_ids()
    reg = json.loads(REGISTRY.read_text())
    parsed = [registry_to_spec(e) for e in reg.get("strategies", [])]
    chosen, notes = select_candidates(parsed, top10)
    p = write_expansion_plan(chosen, notes)
    log(f"Expansion plan → {p} ({len(chosen)} strategies)")
    return p


def build_scoreboard_rows(specs: list[dict], states: list[dict]) -> list[dict]:
    rows = []
    for spec, state in zip(specs, states):
        fm = forward_metrics(state, state["stats"])
        horizon = spec["exit_horizon"]
        hard_req = horizon in ("4h", "6h", "12h", "24h")
        entry_desc = (
            f"age[{1800},{86400}]"
            + (f" fee≤{int(spec['fee_max_bps'])}" if spec.get("fee_max_bps") else "")
            + (" pv≥0" if spec.get("pv_min") is not None else "")
            + (" momentum" if spec.get("momentum_gate") else "")
        )
        rows.append(
            {
                "strategy_id": spec["strategy_id"],
                "harness": "sprint077_expansion",
                "family": spec["family"],
                "exit_horizon": horizon,
                "entry_description": entry_desc,
                "HARD_EXIT_REQUIRED": hard_req,
                "hard_horizon_exit_enabled": spec.get("hard_horizon_exit", False),
                "hist_n": spec["hist_n"],
                "hist_pf": spec["hist_pf"],
                "hist_wr": spec["hist_wr"],
                "hist_composite": spec["hist_composite"],
                "fwd_n": fm["forward_n"],
                "fwd_pf": fm["pf"],
                "fwd_wr": fm["wr"],
                "fwd_ev": fm["ev"],
                "fwd_payoff_ratio": fm["payoff_ratio"],
                "fwd_max_drawdown": fm["max_drawdown"],
                "fwd_top_pool_share": fm["top_pool_share"],
                "fwd_quote_fail_proxy": fm["quote_fail_proxy"],
                "fwd_equity": fm["equity"],
                "fwd_open_positions": fm.get("open_positions", 0),
                "hard_exit_forced_count": fm.get("hard_exit_forced_count", 0),
                "last_run_utc": state["last_run"],
                "run_count": state["run_count"],
            }
        )
    rows.sort(key=lambda r: (r["fwd_n"] or 0) * -1)
    return rows


def run_once() -> None:
    plan_path = OUT_DIR / "sprint077_expansion_plan.json"
    if not plan_path.exists():
        generate_plan()
    plan = json.loads(plan_path.read_text())
    specs = plan["candidates"]
    if not specs:
        log("No expansion candidates; abort")
        return

    t0 = time.perf_counter()
    hist, fwd = load_csv_split(SRC_CSV, FORWARD_CUTOFF_DEFAULT)
    log(f"CSV split hist={len(hist)} fwd={len(fwd)} strategies={len(specs)}")

    states = []
    for spec in specs:
        st = run_strategy(spec, fwd, state_dir=STATE_DIR)
        fm = forward_metrics(st, st["stats"])
        append_summary(
            STATE_DIR,
            spec["strategy_id"],
            {
                "strategy_id": spec["strategy_id"],
                "last_run": st["last_run"],
                "forward_n": fm["forward_n"],
                "quote_fail_proxy": fm["quote_fail_proxy"],
                "open_positions": fm.get("open_positions", 0),
                "hard_exit_forced": fm.get("hard_exit_forced_count", 0),
            },
        )
        states.append(st)
        log(
            f"  {spec['strategy_id']} horizon={spec['exit_horizon']} "
            f"fwd_n={fm['forward_n']} hard_forced={fm.get('hard_exit_forced_count',0)} "
            f"open={fm.get('open_positions',0)}"
        )

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    rows = build_scoreboard_rows(specs, states)

    resource = {
        "run_elapsed_ms": elapsed_ms,
        "strategy_count": len(specs),
        "state_files_expected": len(specs),
        "state_dir": str(STATE_DIR),
        "csv_forward_rows": len(fwd),
        "aggregate_quote_fail_proxy_max": max((r["fwd_quote_fail_proxy"] or 0) for r in rows)
        if rows
        else 0,
        "total_hard_exit_forced": sum(r.get("hard_exit_forced_count") or 0 for r in rows),
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "scoreboard.json").write_text(json.dumps(rows, indent=2, default=str))
    (OUT_DIR / "sprint077_resource_snapshot.json").write_text(json.dumps(resource, indent=2))
    write_sprint077_decision_flags(specs)
    (OUT_DIR / "sprint077_readout.md").write_text(
        "\n".join(
            [
                "# Sprint077 forward expansion",
                "",
                f"- strategies: {len(specs)}",
                f"- resource: `{resource}`",
                f"- expansion plan: `{OUT_DIR / 'sprint077_expansion_plan.json'}`",
                f"- decision flags: `{OUT_DIR / 'sprint077_decision_flags.json'}`",
                "",
            ]
        )
    )
    log(f"Scoreboard → {OUT_DIR / 'scoreboard.json'} elapsed_ms={elapsed_ms}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--generate-plan", action="store_true")
    ap.add_argument("--loop", action="store_true")
    args = ap.parse_args()

    if args.generate_plan:
        generate_plan()
        return

    if args.loop:
        while True:
            run_once()
            time.sleep(300)
        return

    run_once()


if __name__ == "__main__":
    main()
