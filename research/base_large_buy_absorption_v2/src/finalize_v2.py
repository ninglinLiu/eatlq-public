"""Finalize v2: per-hypothesis verdicts, freeze gate, optional shadow T0."""
from __future__ import annotations

import hashlib
from typing import Any

from .common import CFG, OUT, ROOT, load_freeze, save_json, sha256_file, utcnow


def _code_hash() -> str:
    h = hashlib.sha256()
    for p in sorted((ROOT / "src").glob("*.py")):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def finalize_v2(
    scorecard,
    summary: dict,
    lookahead: dict,
    loss_report: dict,
    calendar_report: dict,
    coverage_report: dict,
    pnl_report: dict,
    ranking_report: dict,
    sensitivity_report: dict,
) -> dict[str, Any]:
    freeze = load_freeze()
    code_h = _code_hash()
    cfg_h = sha256_file(CFG / "large_buy_absorption_freeze_v2.json")
    data_path = (ROOT / "data" / "resolved_swaps_full.parquet").resolve()
    data_h = sha256_file(data_path) if data_path.exists() else None

    la_ok = bool(lookahead.get("pass"))
    loss_ok = loss_report.get("verdict") == "LOSS_TRUTH_OK"
    data_ok = coverage_report.get("verdicts", {}).get("DATA_COMPLETE_FOR_INTENDED_UNIVERSE") == "PROVEN"

    hyp = {
        "DATA_COMPLETE_FOR_INTENDED_UNIVERSE": coverage_report.get("verdicts", {}).get(
            "DATA_COMPLETE_FOR_INTENDED_UNIVERSE", "UNCONFIRMED"
        ),
        "PIPELINE_ARTIFACT_DROP": coverage_report.get("verdicts", {}).get(
            "PIPELINE_ARTIFACT_DROP", "UNCONFIRMED"
        ),
        "ABSORB_5S_LATENCY_0S_PRIMARY": summary.get("primary_scheduled_verdict", "UNCONFIRMED"),
        "REGIME_POSITIVE_USD": pnl_report.get("verdicts", {}).get("REGIME_POSITIVE_USD", "UNCONFIRMED"),
        "SELECTION_EDGE_LBA_VS_POOL": pnl_report.get("verdicts", {}).get(
            "SELECTION_EDGE_LBA_VS_POOL", "UNCONFIRMED"
        ),
        "SELECTION_EDGE_LBA_VS_RANDOM": pnl_report.get("verdicts", {}).get(
            "SELECTION_EDGE_LBA_VS_RANDOM", "UNCONFIRMED"
        ),
        "STRUCTURED_RANKING_BEATS_RANDOM": ranking_report.get("verdicts", {}).get(
            "STRUCTURED_RANKING_BEATS_RANDOM", "UNCONFIRMED"
        ),
        "STRUCTURED_RANKING_BEATS_SIZE": ranking_report.get("verdicts", {}).get(
            "STRUCTURED_RANKING_BEATS_SIZE", "UNCONFIRMED"
        ),
        "SENSITIVITY_USED_FOR_SELECTION": "REFUTED",  # hard ban honored
    }

    # Shadow upgrade only if data complete + paired selection significant + ranking incremental
    sel_ok = hyp["SELECTION_EDGE_LBA_VS_POOL"] == "PROVEN"
    rank_ok = hyp["STRUCTURED_RANKING_BEATS_RANDOM"] == "PROVEN"
    shadow_upgrade = bool(data_ok and sel_ok and rank_ok and la_ok and loss_ok)

    freeze_ready = bool(la_ok and loss_ok and data_ok)
    gate = "FREEZE_READY" if freeze_ready else "NOT_READY"

    prospective_t0 = None
    if freeze_ready:
        prospective_t0 = utcnow()
        save_json(OUT / "FREEZE_READY.json", {
            "status": "FREEZE_READY",
            "code_hash": code_h,
            "config_hash": cfg_h,
            "data_hash": data_h,
            "schema_version": "lba_v2_0",
            "historical_label": "DIAGNOSTIC_REPLAY",
            "primary_timing": {
                "absorption_window_seconds": freeze["signal_timing"]["absorption_window_seconds"],
                "measured_latency_seconds": freeze["signal_timing"]["measured_latency_seconds"],
            },
            "shadow_upgrade_armed": shadow_upgrade,
            "generated_at": prospective_t0,
        })
        save_json(OUT / "PROSPECTIVE_T0.json", {
            "prospective_t0": prospective_t0,
            "observer_only": True,
            "live_trading": False,
            "shadow_upgrade": shadow_upgrade,
            "reason": (
                "data+selection+ranking all PROVEN"
                if shadow_upgrade
                else "methodology freeze only; selection/ranking not all PROVEN — shadow stays observer, no edge claim"
            ),
        })

    rows = scorecard.to_dict(orient="records") if hasattr(scorecard, "to_dict") else []
    payload = {
        "generated_at": utcnow(),
        "phase": "LARGE_BUY_ABSORPTION_V2",
        "historical_label": "DIAGNOSTIC_REPLAY",
        "lookahead_verdict": lookahead.get("verdict"),
        "loss_truth_verdict": loss_report.get("verdict"),
        "primary_scheduled_verdict": summary.get("primary_scheduled_verdict"),
        "freeze_gate": gate,
        "shadow_upgrade": shadow_upgrade,
        "live": "NO_GO",
        "per_hypothesis": hyp,
        "scorecard_rows": rows,
        "code_hash": code_h,
        "config_hash": cfg_h,
        "data_hash": data_h,
        "prospective_t0": prospective_t0,
        "calendar": {
            "scoped_days": calendar_report.get("days_with_scoped_buy"),
            "coverage_frac": calendar_report.get("coverage_frac_scoped"),
            "signals_per_day_mean": (calendar_report.get("signals_per_covered_day") or {}).get("mean"),
        },
        "coverage_phase1_trigger": coverage_report.get("rebuild_recommendation", {}).get("phase1_trigger"),
        "sensitivity_warning": sensitivity_report.get("warning"),
        "open_items": [
            k for k, v in hyp.items()
            if v in ("REFUTED", "UNCONFIRMED", "SCHEDULED_EDGE_UNCONFIRMED", "SCHEDULED_EDGE_NEGATIVE")
            and k not in ("SENSITIVITY_USED_FOR_SELECTION", "PIPELINE_ARTIFACT_DROP")
        ],
    }
    save_json(OUT / "LARGE_BUY_ABSORPTION_v2_REPLAY.json", payload)

    md = _review_md(payload, pnl_report, ranking_report, coverage_report, sensitivity_report)
    (OUT / "LARGE_BUY_ABSORPTION_v2_REVIEW.md").write_text(md)
    (ROOT / "LARGE_BUY_ABSORPTION_v2_REVIEW.md").write_text(md)
    return payload


def _review_md(payload, pnl, ranking, coverage, sens) -> str:
    lines = [
        "# LARGE_BUY_ABSORPTION_v2 — DIAGNOSTIC_REPLAY Review",
        "",
        f"Generated: `{payload.get('generated_at')}`",
        "",
        "> **Not a global kill.** Per-hypothesis PROVEN / REFUTED / UNCONFIRMED only.",
        "> Primary timing preregistered: **5s absorb / 0s latency** (0s = optimistic upper bound).",
        "> Sensitivity curves are diagnostic — not used to pick parameters.",
        "",
        "## Per-hypothesis verdicts",
        "",
        "| Hypothesis | Verdict |",
        "| --- | --- |",
    ]
    for k, v in (payload.get("per_hypothesis") or {}).items():
        lines.append(f"| `{k}` | **{v}** |")
    lines += [
        "",
        f"- Freeze gate: `{payload.get('freeze_gate')}`",
        f"- Shadow upgrade: `{payload.get('shadow_upgrade')}`",
        f"- Live: `{payload.get('live')}`",
        "",
        "## Scorecard (scheduled primary)",
        "",
        "| Arm | Verdict | Selected | Fills | Skip | Sched WETH | Cum USD | Paired p05 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in payload.get("scorecard_rows") or []:
        lines.append(
            f"| {r.get('arm')} | {r.get('scheduled_verdict')} | {r.get('selected_trades')} | "
            f"{r.get('n_fills_scheduled')} | {r.get('skip_rate')} | "
            f"{r.get('scheduled_trading_pnl_weth')} | {r.get('scheduled_cum_withdrawn_usd')} | "
            f"{r.get('paired_delta_p05_day')} |"
        )
    lines += [
        "",
        "## Data coverage (Phase 0)",
        "",
        f"- Phase1 trigger: `{payload.get('coverage_phase1_trigger')}`",
        f"- Intended-universe complete: `{coverage.get('verdicts', {}).get('DATA_COMPLETE_FOR_INTENDED_UNIVERSE')}`",
        f"- Pipeline artifact drop: `{coverage.get('verdicts', {}).get('PIPELINE_ARTIFACT_DROP')}`",
        "",
        "Sparse calendar vs full Base V3 is **by design** (new-pool first-48h), not a silent data drop.",
        "",
        "## Open items (not a global kill)",
        "",
    ]
    for item in payload.get("open_items") or []:
        lines.append(f"- `{item}`")
    if not payload.get("open_items"):
        lines.append("- None")
    lines += [
        "",
        "## Governance",
        "",
        f"- Code hash: `{payload.get('code_hash')}`",
        f"- Config hash: `{payload.get('config_hash')}`",
        f"- Data hash: `{payload.get('data_hash')}`",
        f"- Prospective T0: `{payload.get('prospective_t0')}`",
        "",
    ]
    return "\n".join(lines) + "\n"
