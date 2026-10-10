"""Finalize DIAGNOSTIC_REPLAY, freeze-readiness gate, prospective T0, review MD."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .common import CFG, OUT, ROOT, load_freeze, save_json, sha256_file, utcnow


def _code_hash() -> str:
    h = hashlib.sha256()
    for p in sorted((ROOT / "src").glob("*.py")):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def write_review_md(payload: dict) -> str:
    lines = [
        "# LARGE_BUY_ABSORPTION_v1 — DIAGNOSTIC_REPLAY Review",
        "",
        f"Generated: `{payload.get('generated_at')}`",
        "",
        "> Historical label: **DIAGNOSTIC_REPLAY**. Does **not** inherit v0 OOS certification.",
        "> Narrative: **order-flow**, not smart money. Legacy alias: WHALE_EVENT.",
        "",
        "## Freeze-readiness gate",
        "",
        f"- Lookahead: `{payload.get('lookahead_verdict')}`",
        f"- Loss truth: `{payload.get('loss_truth_verdict')}`",
        f"- Primary scheduled verdict: `{payload.get('primary_scheduled_verdict')}`",
        f"- Gate: `{payload.get('freeze_gate')}`",
        f"- Live: `NO_GO`",
        f"- Monthly $1000 target: `{payload.get('target_verdict')}`",
        "",
        "## Scorecard (scheduled primary)",
        "",
        "| Arm | Scheduled verdict | Selected | Fills | Skip | Sched PnL WETH | Cum withdrawn USD | Unconst diag WETH | Paired Δ p05 day |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in payload.get("scorecard_rows", []):
        lines.append(
            f"| {r.get('arm')} | {r.get('scheduled_verdict')} | {r.get('selected_trades')} | "
            f"{r.get('n_fills_scheduled')} | {r.get('skip_rate')} | "
            f"{r.get('scheduled_trading_pnl_weth')} | {r.get('scheduled_cum_withdrawn_usd')} | "
            f"{r.get('UNCONSTRAINED_OPPORTUNITY_DIAGNOSTIC_weth')} | {r.get('paired_delta_p05_day')} |"
        )
    lines += [
        "",
        "## Governance",
        "",
        f"- Code hash: `{payload.get('code_hash')}`",
        f"- Config hash: `{payload.get('config_hash')}`",
        f"- Data hash: `{payload.get('data_hash')}`",
        f"- Prospective T0: `{payload.get('prospective_t0')}`",
        f"- Shadow: observer-only (`python3 -m src.shadow_recorder`)",
        "",
        "## Hard bans still in force",
        "",
        "- No live trading",
        "- No retune after seeing this replay",
        "- No smart-money narrative",
        "- Unconstrained PnL is diagnostic only",
        "",
    ]
    return "\n".join(lines) + "\n"


def finalize(
    scorecard,
    summary: dict,
    lookahead: dict,
    loss_report: dict,
    calendar_report: dict,
    reconcile_report: dict,
) -> dict[str, Any]:
    freeze = load_freeze()
    code_h = _code_hash()
    cfg_h = sha256_file(CFG / "large_buy_absorption_freeze_v1.json")
    data_path = ROOT / "data" / "resolved_swaps.parquet"
    data_h = sha256_file(data_path.resolve()) if data_path.exists() else None

    la_ok = bool(lookahead.get("pass"))
    loss_ok = loss_report.get("verdict") == "LOSS_TRUTH_OK"
    primary_v = summary.get("primary_scheduled_verdict")
    # Freeze ready if objective fixes hold; scheduled edge can be UNCONFIRMED and still freeze for shadow
    freeze_ready = la_ok and loss_ok
    gate = "FREEZE_READY" if freeze_ready else "NOT_READY"

    prospective_t0 = None
    if freeze_ready:
        prospective_t0 = utcnow()
        save_json(OUT / "FREEZE_READY.json", {
            "status": "FREEZE_READY",
            "code_hash": code_h,
            "config_hash": cfg_h,
            "data_hash": data_h,
            "schema_version": "lba_v1_1",
            "historical_label": "DIAGNOSTIC_REPLAY",
            "inherits_v0_oos": False,
            "stamped_at": prospective_t0,
        })
        save_json(OUT / "PROSPECTIVE_T0.json", {
            "prospective_t0": prospective_t0,
            "mode": "observer_only_shadow",
            "live_trading": False,
            "arm": "LARGE_BUY_ABSORPTION",
            "code_hash": code_h,
            "config_hash": cfg_h,
            "data_hash": data_h,
            "note": "Forward accumulation requires live ingestion beyond resolved data end (~2026-07-02).",
        })
        # Flip freeze status
        freeze_path = CFG / "large_buy_absorption_freeze_v1.json"
        import json
        cfg = json.loads(freeze_path.read_text())
        cfg["status"] = "FROZEN_FOR_PROSPECTIVE_SHADOW"
        cfg["code_hash_at_freeze"] = code_h
        cfg["prospective_t0"] = prospective_t0
        freeze_path.write_text(json.dumps(cfg, indent=2) + "\n")

    rows = scorecard.to_dict(orient="records") if scorecard is not None and len(scorecard) else []
    payload = {
        "phase": "LARGE_BUY_ABSORPTION_V1",
        "historical_label": "DIAGNOSTIC_REPLAY",
        "inherits_v0_oos": False,
        "narrative": "order_flow_not_smart_money",
        "legacy_alias": "WHALE_EVENT",
        "lookahead_verdict": lookahead.get("verdict"),
        "loss_truth_verdict": loss_report.get("verdict"),
        "primary_scheduled_verdict": primary_v,
        "target_verdict": summary.get("target_verdict"),
        "freeze_gate": gate,
        "deploy_allowed": False,
        "code_hash": code_h,
        "config_hash": cfg_h,
        "data_hash": data_h,
        "prospective_t0": prospective_t0,
        "scorecard_rows": rows,
        "calendar_coverage_frac": calendar_report.get("coverage_frac_scoped"),
        "calendar_missing_days_n": calendar_report.get("missing_data_days_n"),
        "generated_at": utcnow(),
    }
    save_json(OUT / "LARGE_BUY_ABSORPTION_v1_REPLAY.json", payload)
    md = write_review_md(payload)
    (OUT / "LARGE_BUY_ABSORPTION_v1_REVIEW.md").write_text(md)
    (ROOT / "LARGE_BUY_ABSORPTION_v1_REVIEW.md").write_text(md)
    print(f"[finalize] gate={gate} lookahead={payload['lookahead_verdict']} "
          f"loss={payload['loss_truth_verdict']} primary={primary_v}", flush=True)
    return payload
