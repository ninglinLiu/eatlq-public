#!/usr/bin/env python3
"""Generate SOP-selected Batch-25 manifest for paper shadow + forward dry-run."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "mvp" / "audits" / "sprint078_pipeline_batch25"
OUT_PATH = OUT_DIR / "batch25_manifest.json"

REG068 = REPO / "mvp/diagnostics/sprint068/alpha_registry.json"
REG069 = REPO / "mvp/diagnostics/sprint069/sprint069_candidate_registry.json"
SCOREBOARD = REPO / "mvp/audits/sprint074_alpha_factory/alpha_factory_scoreboard.json"
WATCHLIST = REPO / "mvp/audits/sprint074_alpha_factory/alpha_factory_dryrun_watchlist.json"
REG074 = REPO / "mvp/audits/sprint074_alpha_factory/alpha_factory_registry.json"


def pick_paper_shadow_alphas() -> list[str]:
    reg068 = json.loads(REG068.read_text())
    reg069 = json.loads(REG069.read_text())
    ids: list[str] = []
    for a in reg068["alphas"]:
        if a.get("status") in ("ACTIVE_SHADOW", "SHADOW_ONLY"):
            ids.append(a["alpha_id"])
    for aid in reg069.get("active_ids", []):
        if aid not in ids:
            ids.append(aid)
    for aid in reg069.get("shadow_ids", []):
        if len(ids) >= 25:
            break
        if aid not in ids:
            ids.append(aid)
    return ids[:25]


def sp072_specs() -> list[dict]:
    return [
        {
            "strategy_id": "SP072_A3_age_4h_3600_100k",
            "source": "sprint072_exit_horizon_sweep",
            "exit_horizon": "4h",
            "family": "exit_horizon",
            "age_min_sec": 3600,
            "age_max_sec": 100000,
            "hard_horizon_exit": True,
            "promotion_band": "WATCH_ONLY",
        },
        {
            "strategy_id": "SP072_A1_6h_extended",
            "source": "sprint072_exit_horizon_sweep",
            "exit_horizon": "6h",
            "family": "exit_horizon",
            "hard_horizon_exit": True,
            "promotion_band": "WATCH_ONLY",
        },
        {
            "strategy_id": "SP072_A1_4h_extended",
            "source": "sprint072_exit_horizon_sweep",
            "exit_horizon": "4h",
            "family": "exit_horizon",
            "hard_horizon_exit": True,
            "promotion_band": "WATCH_ONLY",
        },
        {
            "strategy_id": "SP072_A1_1h_control",
            "source": "sprint072_exit_horizon_sweep",
            "exit_horizon": "1h",
            "family": "exit_horizon",
            "hard_horizon_exit": False,
            "promotion_band": "WATCH_ONLY",
        },
        {
            "strategy_id": "SP072_D_30m_baseline",
            "source": "sprint072_exit_horizon_sweep",
            "exit_horizon": "30m",
            "family": "exit_horizon",
            "hard_horizon_exit": False,
            "promotion_band": "WATCH_ONLY",
        },
    ]


def registry_entry_to_spec(entry: dict) -> dict:
    import re

    xr = entry.get("exit_rule") or ""
    m = re.search(r"label_(\d+m|\d+h)_", xr)
    horizon = m.group(1) if m else "2h"
    rule = entry.get("entry_rule") or ""
    fee_max = None
    if "fee<=" in rule:
        import re as _re

        fm = _re.search(r"fee<=([0-9.]+)", rule)
        if fm:
            fee_max = float(fm.group(1))
    pv_min = 0.0 if "pv_5m>=" in rule else None
    momentum = "momentum(" in rule
    return {
        "strategy_id": entry["strategy_id"],
        "source": "sprint074_alpha_factory",
        "exit_horizon": horizon,
        "family": entry.get("family") or "baseline",
        "fee_max_bps": fee_max,
        "pv_min": pv_min,
        "momentum_gate": momentum,
        "hard_horizon_exit": horizon in ("4h", "6h", "12h", "24h"),
        "promotion_band": entry.get("promotion_band"),
        "hist_n": entry.get("scores", {}).get("n_trades") or entry.get("n_trades"),
        "hist_pf": entry.get("scores", {}).get("pf"),
        "hist_composite": entry.get("scores", {}).get("composite_score"),
    }


def pick_dryrun_strategies() -> list[dict]:
    scoreboard = json.loads(SCOREBOARD.read_text())
    watchlist = json.loads(WATCHLIST.read_text())
    reg074 = {e["strategy_id"]: e for e in json.loads(REG074.read_text())["strategies"]}

    by_id = {r["strategy_id"]: r for r in scoreboard}
    chosen_ids: list[str] = []
    specs: list[dict] = []

    for row in watchlist.get("top_dryrun_candidates", []):
        sid = row["strategy_id"]
        if sid not in chosen_ids:
            chosen_ids.append(sid)

    for row in sorted(scoreboard, key=lambda x: -(x.get("composite_score") or 0)):
        if row.get("promotion_band") == "LIVE_QUEUE_CANDIDATE" and row["strategy_id"] not in chosen_ids:
            chosen_ids.append(row["strategy_id"])
        if len(chosen_ids) >= 14:
            break

    for sp in sp072_specs():
        if sp["strategy_id"] not in chosen_ids:
            chosen_ids.append(sp["strategy_id"])
            specs.append(sp)

    for row in sorted(scoreboard, key=lambda x: -(x.get("composite_score") or 0)):
        sid = row["strategy_id"]
        if sid in chosen_ids:
            continue
        if row.get("promotion_band") not in ("KEEP_SAMPLING", "PROMOTE_DRYRUN", "LIVE_QUEUE_CANDIDATE"):
            continue
        chosen_ids.append(sid)
        if len(chosen_ids) >= 25:
            break

    out_specs: list[dict] = []
    seen: set[str] = set()
    for sid in chosen_ids:
        if sid in seen:
            continue
        seen.add(sid)
        if sid.startswith("SP072_"):
            out_specs.append(next(s for s in sp072_specs() if s["strategy_id"] == sid))
        elif sid in reg074:
            spec = registry_entry_to_spec(reg074[sid])
            if sid in by_id:
                spec["hist_composite"] = by_id[sid].get("composite_score")
                spec["hist_pf"] = by_id[sid].get("pf")
                spec["hist_n"] = by_id[sid].get("n_trades")
            out_specs.append(spec)
        if len(out_specs) >= 25:
            break

    return out_specs[:25]


def main() -> None:
    paper = pick_paper_shadow_alphas()
    dry = pick_dryrun_strategies()
    doc = {
        "generated_at_utc": datetime.now(tz=timezone.utc).isoformat(),
        "sprint": "078",
        "policy": (
            "SOP Batch-25 pipeline: 25 entry-filter paper shadows + 25 forward dry-run "
            "virtual accounts. Historical replay alone does NOT count as forward proof."
        ),
        "paper_shadow_alphas": paper,
        "paper_shadow_count": len(paper),
        "dryrun_strategies": dry,
        "dryrun_count": len(dry),
        "cron": {
            "paper_shadow": (
                "scripts/sprint068_multi_alpha_runner.py --once "
                "--batch25-manifest mvp/audits/sprint078_pipeline_batch25/batch25_manifest.json "
                "--candidate-registry mvp/diagnostics/sprint069/sprint069_candidate_registry.json"
            ),
            "forward_dryrun_batch25": (
                "scripts/sprint078_batch25_harness.py --once"
            ),
            "forward_dryrun_legacy": (
                "scripts/sprint069_forward_dryrun.py --once --quiet"
            ),
        },
        "sop_gates": {
            "forward_n_target": 100,
            "live_allowed": False,
            "training_allowed": False,
        },
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(doc, indent=2, default=str))
    print(json.dumps({"ok": True, "path": str(OUT_PATH), "paper": len(paper), "dry": len(dry)}, indent=2))


if __name__ == "__main__":
    main()
