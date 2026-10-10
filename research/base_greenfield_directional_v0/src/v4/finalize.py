"""V4 finalize artifacts."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from src.common import sha256_file, utcnow
from src.v3.oof_eval import ALLOWED_VERDICTS
from src.v3.finalize import overall_verdict, phase0_crosswalk

ROOT = Path("/root/base_greenfield_directional_v0")
OUT = ROOT / "out_v4"


def write_reproduce(freeze: dict) -> None:
    text = f"""# Base Greenfield V4 Orthogonal Hypothesis Round — Reproduce

Parent: V3 `{freeze.get("parent_verdict")}`  
Phase: `{freeze["phase"]}`  
`deploy_allowed={freeze["deploy_allowed"]}`  
Expand-search-after-failure: **forbidden**  
One-shot orthogonal round only.

```bash
cd /root/base_greenfield_directional_v0
.venv/bin/python -m src.v4.run_orthogonal 2>&1 | tee out_v4/RUN.log
```

## Re-seal

- method: salted_signal_hash
- salt: `{freeze["splits"]["salt"]}`
- V3 INTERNAL_HOLDOUT/UNTOUCHED ids burned out of V4 confirmation

## Frozen configs

- `configs/v4_orthogonal_freeze.json`
- `configs/v4_hypothesis_registry.json`
"""
    (OUT / "REPRODUCE.md").write_text(text)


def finalize_v4(
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
    # reuse V3 diagnostic gate definitions for crosswalk only
    v3f = json.loads((ROOT / "configs" / "v3_rescue_freeze.json").read_text())
    cross = phase0_crosswalk(v3f)
    cross.to_csv(OUT / "DIAGNOSTIC_PHASE0_CROSSWALK.csv", index=False)
    scorecard.to_csv(OUT / "OOF_SCORECARD.csv", index=False)
    if len(monthly):
        monthly.to_csv(OUT / "MONTHLY_WITHDRAW.csv", index=False)
    else:
        pd.DataFrame(columns=["month", "withdrawn_weth", "withdrawn_usd"]).to_csv(OUT / "MONTHLY_WITHDRAW.csv", index=False)
    if len(curve):
        curve.to_parquet(OUT / "WALLET_EQUITY_CURVE.parquet", index=False)

    ledger_rows = []
    for h in registry.get("hypotheses", []):
        m = route_details.get("routes", {}).get(h["id"], {})
        ledger_rows.append(
            {
                "hypothesis_id": h["id"],
                "orthogonal_to": h.get("orthogonal_to", ""),
                "mechanism": h["mechanism"],
                "max_searches": h["max_searches"],
                "verdict": m.get("verdict", "ORTHOGONAL_EDGE_NOT_CONFIRMED"),
                "selected_trades": m.get("selected_trades"),
                "total_wallet_pnl_weth": m.get("total_wallet_pnl_weth"),
                "haircut_pnl_weth": m.get("conservative_tail_haircut_pnl_weth"),
                "independent_right_tail_events": (m.get("right_tail") or {}).get("independent_right_tail_events"),
            }
        )
    ledger = pd.DataFrame(ledger_rows)
    ledger.to_csv(OUT / "HYPOTHESIS_LEDGER.csv", index=False)

    rt_rows = []
    for name, m in route_details.get("routes", {}).items():
        rt_rows.append({"policy": name, **(m.get("right_tail") or {})})
    pd.DataFrame(rt_rows).to_csv(OUT / "RIGHT_TAIL_AUDIT.csv", index=False)

    verdict = overall_verdict(scorecard, confirmation, freeze)
    assert verdict in ALLOWED_VERDICTS

    all_failed = verdict != "ORTHOGONAL_EDGE_CONFIRMED"
    closing = (
        "V4 正交假设一轮已完成且未确认可重复小容量优势。"
        "在现有数据、现有特征与 V3+V4 已检验假设范围内如实结案；禁止继续扩大搜索硬凑赢家；禁止建议换市场。"
        if all_failed
        else "V4 正交路线在 confirmation 上支持进入 prospective shadow；deploy_allowed 仍为 false。"
    )

    final = {
        "created_utc": utcnow(),
        "role": freeze["role"],
        "phase": freeze["phase"],
        "parent_phase": freeze.get("parent_phase"),
        "parent_verdict": freeze.get("parent_verdict"),
        "verdict": verdict,
        "deploy_allowed": False,
        "venue_rejection": False,
        "stop_research": False,
        "expand_search_after_failure": False,
        "one_shot_round_complete": True,
        "honest_closeout_if_failed": all_failed,
        "old_line_inputs_used": False,
        "v3_hypotheses_reused": False,
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
            "resealed": confirmation.get("resealed"),
        },
        "closing_statement": closing,
        "forbidden_next_step": "Do not launch V5 solely to fish until positive OOF; new round requires new pre-registered orthogonal mechanisms and fresh seal.",
        "freeze_sha256": sha256_file(ROOT / "configs" / "v4_orthogonal_freeze.json"),
        "registry_sha256": sha256_file(ROOT / "configs" / "v4_hypothesis_registry.json"),
        "hashes": {
            "OOF_SCORECARD.csv": sha256_file(OUT / "OOF_SCORECARD.csv"),
            "HYPOTHESIS_LEDGER.csv": sha256_file(OUT / "HYPOTHESIS_LEDGER.csv"),
        },
    }
    (OUT / "RESCUE_FINAL.json").write_text(json.dumps(final, indent=2, default=str) + "\n")
    write_reproduce(freeze)
    print(f"[v4.finalize] verdict={verdict} one_shot_complete=true", flush=True)
    return final
