"""Per-leg verdicts + governance for Alpha Hunt v2."""
from __future__ import annotations

from typing import Any

from ..common import ROOT, sha256_file, save_json
from .common_v2 import ALPHA_V2_OUT, ensure_alpha_v2_out, load_alpha_v2_cfg, stamp_status, utcnow


def finalize_alpha_v2(
    matched: dict,
    ranker: dict,
    leakage: dict,
    *,
    arm_shadow: bool = True,
) -> dict[str, Any]:
    ensure_alpha_v2_out()
    families = {}
    any_proven = False
    for fam in ["actor_xpool", "clone_family", "combined"]:
        m = matched.get("families", {}).get(fam, {})
        r = ranker.get("families", {}).get(fam, {})
        m_v = m.get("verdict", "UNCONFIRMED")
        r_v = r.get("verdict", "UNCONFIRMED")
        if m_v == "PROVEN" and r_v == "PROVEN_ON_SEALED":
            overall = "PROVEN"
            any_proven = True
        elif m_v == "REFUTED" and r_v != "PROVEN_ON_SEALED":
            overall = "REFUTED"
        else:
            overall = "UNCONFIRMED"
        families[fam] = {
            "matched_att": m_v,
            "ranker": r_v,
            "overall": overall,
            "bh_reject": m.get("bh_reject"),
            "att_mean": (m.get("att") or {}).get("att_mean_delta_weth"),
            "att_day_p05": ((m.get("att") or {}).get("bootstrap_day") or {}).get("p05"),
        }

    propose_shadow = bool(any_proven and leakage.get("passed"))
    code_hash = sha256_file(ROOT / "src" / "alpha_v2" / "bakeoff_v2.py")
    t0 = utcnow() if propose_shadow else None
    both_refuted = bool(
        families["actor_xpool"]["overall"] == "REFUTED"
        and families["clone_family"]["overall"] == "REFUTED"
    )

    report = {
        "task": "ALPHA_HUNT_V2_FINAL",
        "generated_at": utcnow(),
        "leakage_gate": leakage.get("verdict"),
        "families": families,
        "propose_observer_shadow": propose_shadow,
        "live": "NO_GO",
        "t0": t0,
        "code_hash_bakeoff": code_hash,
        "stop_rule": load_alpha_v2_cfg()["stop_rule"],
        "both_primary_legs_refuted": both_refuted,
    }
    save_json(ALPHA_V2_OUT / "ALPHA_HUNT_FINAL.json", report)

    lines = [
        "# ALPHA_HUNT_v2_REVIEW",
        "",
        f"Generated: `{report['generated_at']}`",
        "",
        f"## Leakage gate: **{leakage.get('verdict')}**",
        "",
        f"## Live: **NO_GO** | Observer shadow proposed: **{propose_shadow}**",
        "",
        "## Per-leg verdicts (net-of-cost, incremental over random)",
        "",
        "| Leg | Matched ATT | Ranker | Overall | ATT mean (WETH) | day p05 |",
        "| --- | --- | --- | --- | ---: | ---: |",
    ]
    for fam, f in families.items():
        att, p05 = f["att_mean"], f["att_day_p05"]
        lines.append(
            f"| {fam} | {f['matched_att']} | {f['ranker']} | **{f['overall']}** | "
            f"{att if att is None else f'{att:.6f}'} | {p05 if p05 is None else f'{p05:.6f}'} |"
        )
    lines += [
        "",
        "## Success bar",
        "",
        load_alpha_v2_cfg()["success_bar"],
        "",
        "## Stop rule",
        "",
        report["stop_rule"],
        "",
        f"Both primary legs (A2 + N) REFUTED → stop line: **{both_refuted}**",
        "",
    ]
    if propose_shadow:
        lines += [
            "## Observer shadow",
            "",
            f"- T0: `{t0}`",
            f"- Code hash: `{code_hash}`",
            "- Mode: observer-only; no live orders.",
            "",
        ]
    else:
        lines += [
            "## Disposition",
            "",
            "No leg cleared PROVEN under sealed + matched + net-of-cost. "
            "Do not upgrade to live."
            + (
                " Both primary legs REFUTED → declare on-chain actor/launch-identity "
                "selection-alpha line closed; do not keep tuning."
                if both_refuted else ""
            ),
            "",
        ]
    lines += [
        "## Notes vs v1",
        "",
        "- v1 Leg A was crippled by 94.7% cold-start (48h-universe only); v2 uses cross-pool history.",
        "- v1 deployer leg was dead; v2 replaces it with bytecode clone-family reputation.",
        "- v1 seal (`SEALED_BOTH_REFUTED`) is untouched.",
        "",
    ]
    md = "\n".join(lines)
    (ALPHA_V2_OUT / "ALPHA_HUNT_v2_REVIEW.md").write_text(md)
    (ROOT / "ALPHA_HUNT_v2_REVIEW.md").write_text(md)

    if propose_shadow and arm_shadow:
        save_json(ALPHA_V2_OUT / "shadow" / "SHADOW_STATUS.json", {
            "mode": "OBSERVER_ONLY",
            "t0": t0,
            "code_hash": code_hash,
            "live": "NO_GO",
            "families_proven": [k for k, v in families.items() if v["overall"] == "PROVEN"],
            "status": "ARMED",
        })
        stamp_status("SHADOW_ARMED", t0=t0, code_hash_bakeoff=code_hash)
    elif both_refuted:
        stamp_status("SEALED_BOTH_REFUTED", t0=None)
    else:
        stamp_status("DIAGNOSTIC_REPLAY_COMPLETE", t0=None)

    print(f"[alpha_v2/final] shadow={propose_shadow} both_refuted={both_refuted}", flush=True)
    return report
