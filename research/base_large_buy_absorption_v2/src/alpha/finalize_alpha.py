"""Per-family verdicts + governance for Alpha Hunt v1."""
from __future__ import annotations

from typing import Any

from ..common import ROOT, sha256_file, save_json
from .common_alpha import ALPHA_OUT, ALPHA_CFG, ensure_alpha_out, load_alpha_cfg, stamp_status, utcnow


def finalize_alpha(
    matched: dict,
    ranker: dict,
    leakage: dict,
    *,
    arm_shadow: bool = True,
) -> dict[str, Any]:
    ensure_alpha_out()
    families = {}
    any_proven = False
    for fam in ["actor", "genesis", "combined"]:
        m = matched.get("families", {}).get(fam, {})
        r = ranker.get("families", {}).get(fam, {})
        m_v = m.get("verdict", "UNCONFIRMED")
        r_v = r.get("verdict", "UNCONFIRMED")
        # Family PROVEN only if matched ATT PROVEN AND ranker PROVEN_ON_SEALED
        if m_v == "PROVEN" and r_v == "PROVEN_ON_SEALED":
            overall = "PROVEN"
            any_proven = True
        elif m_v == "REFUTED" and r_v == "REFUTED":
            overall = "REFUTED"
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
    code_hash = sha256_file(ROOT / "src" / "alpha" / "alpha_bakeoff.py")
    t0 = utcnow() if propose_shadow else None

    report = {
        "task": "ALPHA_HUNT_V1_FINAL",
        "generated_at": utcnow(),
        "leakage_gate": leakage.get("verdict"),
        "families": families,
        "propose_observer_shadow": propose_shadow,
        "live": "NO_GO",
        "t0": t0,
        "code_hash_bakeoff": code_hash,
        "stop_rule": (
            "If both actor and genesis are REFUTED under matched+sealed bar net of cost, "
            "seal and stop — do not keep tuning."
        ),
        "both_refuted": bool(
            families["actor"]["overall"] == "REFUTED"
            and families["genesis"]["overall"] == "REFUTED"
        ),
    }
    save_json(ALPHA_OUT / "ALPHA_HUNT_FINAL.json", report)

    # markdown review
    lines = [
        "# ALPHA_HUNT_v1_REVIEW",
        "",
        f"Generated: `{report['generated_at']}`",
        "",
        f"## Leakage gate: **{leakage.get('verdict')}**",
        "",
        f"## Live: **NO_GO** | Observer shadow proposed: **{propose_shadow}**",
        "",
        "## Per-family verdicts (net-of-cost, incremental over random)",
        "",
        "| Family | Matched ATT | Ranker | Overall | ATT mean (WETH) | day p05 |",
        "| --- | --- | --- | --- | ---: | ---: |",
    ]
    for fam, f in families.items():
        att = f["att_mean"]
        p05 = f["att_day_p05"]
        lines.append(
            f"| {fam} | {f['matched_att']} | {f['ranker']} | **{f['overall']}** | "
            f"{att if att is None else f'{att:.6f}'} | {p05 if p05 is None else f'{p05:.6f}'} |"
        )
    lines += [
        "",
        "## Success bar",
        "",
        load_alpha_cfg()["success_bar"],
        "",
        "## Stop rule",
        "",
        report["stop_rule"],
        "",
        f"Both families REFUTED → stop line: **{report['both_refuted']}**",
        "",
    ]
    if propose_shadow:
        lines += [
            "## Observer shadow",
            "",
            f"- T0: `{t0}`",
            f"- Code hash (bakeoff): `{code_hash}`",
            "- Mode: observer-only; records PIT features + cost model + simulated fills; no live orders.",
            "",
        ]
    else:
        lines += [
            "## Disposition",
            "",
            "No family cleared PROVEN under the sealed + matched + net-of-cost bar. "
            "Do not upgrade to live. If both REFUTED, do not keep tuning the same features.",
            "",
        ]

    md = "\n".join(lines)
    (ALPHA_OUT / "ALPHA_HUNT_v1_REVIEW.md").write_text(md)
    (ROOT / "ALPHA_HUNT_v1_REVIEW.md").write_text(md)

    if propose_shadow and arm_shadow:
        _arm_shadow(t0, code_hash, families)
        stamp_status("SHADOW_ARMED", t0=t0, code_hash_bakeoff=code_hash)
    elif report["both_refuted"]:
        stamp_status("SEALED_BOTH_REFUTED", t0=None)
    else:
        stamp_status("DIAGNOSTIC_REPLAY_COMPLETE", t0=None)

    print(f"[alpha/final] shadow={propose_shadow} both_refuted={report['both_refuted']}", flush=True)
    return report


def _arm_shadow(t0: str, code_hash: str, families: dict) -> None:
    shadow = {
        "mode": "OBSERVER_ONLY",
        "t0": t0,
        "code_hash": code_hash,
        "live": "NO_GO",
        "families_proven": [k for k, v in families.items() if v["overall"] == "PROVEN"],
        "cost_model": "execution_depth.kappa + LP fee",
        "features": "PIT actor + genesis only",
        "status": "ARMED",
    }
    save_json(ALPHA_OUT / "shadow" / "SHADOW_STATUS.json", shadow)


if __name__ == "__main__":
    import json
    from pathlib import Path
    m = json.loads((ALPHA_OUT / "alpha_bakeoff.json").read_text())
    g = json.loads((ALPHA_OUT / "leakage_gate.json").read_text())
    finalize_alpha(m["matched"], m["ranker"], g)
