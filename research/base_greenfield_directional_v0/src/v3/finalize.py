"""Write RESCUE_FINAL.json, REPRODUCE.md, and diagnostic crosswalk."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from src.common import sha256_file, utcnow
from src.v3.oof_eval import ALLOWED_VERDICTS

ROOT = Path("/root/base_greenfield_directional_v0")
OUT = ROOT / "out_v3"


def phase0_crosswalk(freeze: dict) -> pd.DataFrame:
    census_path = ROOT / "out_v2" / "PHASE0_UNIVERSE_CENSUS.csv"
    if not census_path.exists():
        return pd.DataFrame()
    c = pd.read_csv(census_path)
    gates = freeze["phase0_gates_diagnostic_only"]
    rows = []
    for r in c.itertuples(index=False):
        checks = {
            "min_independent_opportunities": r.independent_opportunities >= gates["min_independent_opportunities"],
            "min_opportunities_per_week": r.opportunities_per_week >= gates["min_opportunities_per_week"],
            "min_atomic_buy_success": r.atomic_buy_success >= gates["min_atomic_buy_success"],
            "max_future_permanent_unsellable_rate": r.future_permanent_unsellable_rate <= gates["max_future_permanent_unsellable_rate"],
            "min_production_depth_pass_rate": r.production_depth_pass_rate >= gates["min_production_depth_pass_rate"],
        }
        rows.append(
            {
                "age_bucket": r.age_bucket,
                "phase0_viability_pass": bool(r.viability_pass),
                "rescue_action": "DIAGNOSTIC_ONLY_NOT_VENUE_VETO",
                "stop_research_triggered": False,
                **{f"gate_{k}": v for k, v in checks.items()},
                "independent_opportunities": r.independent_opportunities,
                "atomic_buy_success": r.atomic_buy_success,
                "future_permanent_unsellable_rate": r.future_permanent_unsellable_rate,
            }
        )
    return pd.DataFrame(rows)


def overall_verdict(scorecard: pd.DataFrame, confirmation: dict[str, Any], freeze: dict) -> str:
    if not freeze.get("latchpi", {}).get("enabled", False):
        # LatchPi absence is recorded but does not alone decide edge
        pass
    conf_v = confirmation.get("final_confirmation_verdict")
    if confirmation.get("untouched_executed") and conf_v in ALLOWED_VERDICTS:
        return conf_v
    if confirmation.get("holdout_executed") and conf_v in ALLOWED_VERDICTS:
        # holdout-only path
        if conf_v == "PROSPECTIVE_CONFIRMATION_REQUIRED":
            return "ORTHOGONAL_EDGE_NOT_CONFIRMED"
        return conf_v
    # fall back to best OOF route (excluding baselines)
    routes = scorecard[scorecard["verdict"] != "BASELINE"] if len(scorecard) else scorecard
    if len(routes) == 0:
        return "ORTHOGONAL_EDGE_NOT_CONFIRMED"
    # prefer any PROSPECTIVE / confirmed-like; else majority not confirmed
    if (routes["verdict"] == "ORTHOGONAL_EDGE_CONFIRMED").any():
        return "ORTHOGONAL_EDGE_CONFIRMED"
    if (routes["verdict"] == "PROSPECTIVE_CONFIRMATION_REQUIRED").any():
        return "PROSPECTIVE_CONFIRMATION_REQUIRED"
    # pick most severe among failures present
    for v in [
        "EXECUTION_RISK_DOMINATES",
        "LOTTERY_DEPENDENT_REJECT",
        "INSUFFICIENT_INDEPENDENT_RIGHT_TAIL_EVENTS",
        "DATA_LINEAGE_INSUFFICIENT",
        "ORTHOGONAL_EDGE_NOT_CONFIRMED",
    ]:
        if (routes["verdict"] == v).any():
            return v
    return "ORTHOGONAL_EDGE_NOT_CONFIRMED"


def write_reproduce(freeze: dict) -> None:
    text = f"""# Base Greenfield V3 Narrow-Market Rescue — Reproduce

Role: **Base Uniswap New-Pool Narrow-Market Rescue Research Agent**  
Phase: `{freeze["phase"]}`  
`deploy_allowed={freeze["deploy_allowed"]}`  
Venue rejection: **forbidden**

```bash
cd /root/base_greenfield_directional_v0
.venv/bin/python -m src.v3.run_rescue 2>&1 | tee out_v3/RUN.log
```

## Frozen inputs

- `configs/v3_rescue_freeze.json`
- `configs/v3_hypothesis_registry.json`
- `configs/hard_bans.json`
- Source panel: `{freeze["source_panel"]}`
- Source swaps: `{freeze["source_swaps"]}`

## Capital

- initial_usd={freeze["capital"]["initial_capital_usd"]} @ eth_usd_freeze={freeze["capital"]["eth_usd_freeze"]}
- initial_weth={freeze["capital"]["initial_weth"]}
- monthly withdraw target USD={freeze["capital"]["monthly_withdraw_target_usd"]}
- compound_profits={freeze["capital"]["compound_profits"]}

## Outputs

- `out_v3/RESCUE_FINAL.json`
- `out_v3/HYPOTHESIS_LEDGER.csv`
- `out_v3/OOF_SCORECARD.csv`
- `out_v3/MONTHLY_WITHDRAW.csv`
- `out_v3/WALLET_EQUITY_CURVE.parquet`
- `out_v3/RIGHT_TAIL_AUDIT.csv`
- `out_v3/DIAGNOSTIC_PHASE0_CROSSWALK.csv`

LatchPi features: disabled (`DATA_LINEAGE_INSUFFICIENT` for narrative hypotheses).
"""
    (OUT / "REPRODUCE.md").write_text(text)


def finalize(
    freeze: dict,
    scorecard: pd.DataFrame,
    route_details: dict[str, Any],
    confirmation: dict[str, Any],
    registry: dict[str, Any],
    panel_meta: dict[str, Any],
    curve: pd.DataFrame,
    monthly: pd.DataFrame,
) -> dict[str, Any]:
    OUT.mkdir(parents=True, exist_ok=True)
    cross = phase0_crosswalk(freeze)
    cross.to_csv(OUT / "DIAGNOSTIC_PHASE0_CROSSWALK.csv", index=False)
    scorecard.to_csv(OUT / "OOF_SCORECARD.csv", index=False)
    if len(monthly):
        monthly.to_csv(OUT / "MONTHLY_WITHDRAW.csv", index=False)
    else:
        pd.DataFrame(columns=["month", "withdrawn_weth", "withdrawn_usd"]).to_csv(OUT / "MONTHLY_WITHDRAW.csv", index=False)
    if len(curve):
        curve.to_parquet(OUT / "WALLET_EQUITY_CURVE.parquet", index=False)

    # hypothesis ledger
    ledger_rows = []
    for h in registry.get("hypotheses", []):
        m = route_details.get("routes", {}).get(h["id"], {})
        ledger_rows.append(
            {
                "hypothesis_id": h["id"],
                "mechanism": h["mechanism"],
                "max_searches": h["max_searches"],
                "lifecycle_buckets": "|".join(h["lifecycle_buckets"]),
                "verdict": m.get("verdict", "ORTHOGONAL_EDGE_NOT_CONFIRMED"),
                "selected_trades": m.get("selected_trades"),
                "total_wallet_pnl_weth": m.get("total_wallet_pnl_weth"),
                "haircut_pnl_weth": m.get("conservative_tail_haircut_pnl_weth"),
                "independent_right_tail_events": (m.get("right_tail") or {}).get("independent_right_tail_events"),
                "failure_condition": h["failure_condition"],
            }
        )
    # full dual
    if "H_FULL_DUAL" in route_details.get("routes", {}):
        m = route_details["routes"]["H_FULL_DUAL"]
        ledger_rows.append(
            {
                "hypothesis_id": "H_FULL_DUAL",
                "mechanism": "Combined risk+opportunity heads on full orthogonal feature set",
                "max_searches": 1,
                "lifecycle_buckets": "|".join(freeze["formal_buckets"]),
                "verdict": m.get("verdict"),
                "selected_trades": m.get("selected_trades"),
                "total_wallet_pnl_weth": m.get("total_wallet_pnl_weth"),
                "haircut_pnl_weth": m.get("conservative_tail_haircut_pnl_weth"),
                "independent_right_tail_events": (m.get("right_tail") or {}).get("independent_right_tail_events"),
                "failure_condition": "Same as strategy promotion gates",
            }
        )
    ledger = pd.DataFrame(ledger_rows)
    ledger.to_csv(OUT / "HYPOTHESIS_LEDGER.csv", index=False)

    # right tail audit table
    rt_rows = []
    for name, m in route_details.get("routes", {}).items():
        rt = m.get("right_tail") or {}
        rt_rows.append({"policy": name, **rt})
    pd.DataFrame(rt_rows).to_csv(OUT / "RIGHT_TAIL_AUDIT.csv", index=False)

    verdict = overall_verdict(scorecard, confirmation, freeze)
    assert verdict in ALLOWED_VERDICTS, verdict

    closing = (
        "在现有数据、现有特征与已检验假设范围内，尚未确认可重复的 Base Uniswap 新池小容量优势。"
        if verdict != "ORTHOGONAL_EDGE_CONFIRMED"
        else "OOF+confirmation 支持条件性小容量优势进入 prospective shadow；deploy_allowed 仍为 false。"
    )

    final = {
        "created_utc": utcnow(),
        "role": freeze["role"],
        "phase": freeze["phase"],
        "verdict": verdict,
        "deploy_allowed": False,
        "venue_rejection": False,
        "stop_research": False,
        "stop_research_note": "Phase0 gates diagnostic only; research continues inside fixed venue",
        "old_line_inputs_used": False,
        "latchpi_used": False,
        "latchpi_status": "DATA_LINEAGE_INSUFFICIENT",
        "panel_meta": panel_meta,
        "best_oof_policy": route_details.get("best_oof_policy"),
        "confirmation": {
            "holdout_executed": confirmation.get("holdout_executed"),
            "untouched_executed": confirmation.get("untouched_executed"),
            "final_confirmation_verdict": confirmation.get("final_confirmation_verdict"),
            "holdout_verdict": (confirmation.get("holdout") or {}).get("verdict"),
            "untouched_verdict": (confirmation.get("untouched") or {}).get("verdict"),
            "untouched_skip_reason": confirmation.get("untouched_skip_reason"),
        },
        "closing_statement": closing,
        "forbidden_claim": "Must not claim the entire Base Uniswap new-pool market has no structure; must not recommend changing venue.",
        "freeze_sha256": sha256_file(ROOT / "configs" / "v3_rescue_freeze.json"),
        "registry_sha256": sha256_file(ROOT / "configs" / "v3_hypothesis_registry.json"),
        "hard_bans_sha256": sha256_file(ROOT / "configs" / "hard_bans.json"),
        "hashes": {
            "OOF_SCORECARD.csv": sha256_file(OUT / "OOF_SCORECARD.csv") if (OUT / "OOF_SCORECARD.csv").exists() else None,
            "HYPOTHESIS_LEDGER.csv": sha256_file(OUT / "HYPOTHESIS_LEDGER.csv") if (OUT / "HYPOTHESIS_LEDGER.csv").exists() else None,
        },
    }
    (OUT / "RESCUE_FINAL.json").write_text(json.dumps(final, indent=2, default=str) + "\n")
    write_reproduce(freeze)
    print(f"[v3.finalize] verdict={verdict} deploy_allowed=false", flush=True)
    return final
