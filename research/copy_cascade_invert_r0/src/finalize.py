"""Honest closeout. GO is never true."""
from __future__ import annotations

from src.common import ROOT, read_json, utc_now_iso, write_json


def finalize() -> dict:
    boards = _load(ROOT / "data/normalized/boards.json")
    recon = _load(ROOT / "out/reconcile_vendor_vs_base.json")
    live_base = _load(ROOT / "out/reconcile_gmgn_vs_live_base.json")
    live_bsc = _load(ROOT / "out/reconcile_gmgn_vs_live_bsc.json")
    debot_hl = _load(ROOT / "out/reconcile_debot_vs_hyperliquid.json")
    tax = _load(ROOT / "out/latency_tax.json")
    arm_a = _load(ROOT / "out/arm_a.json")
    arm_b = _load(ROOT / "out/arm_b.json")
    copy_m = _load(ROOT / "data/raw/base_offline/copy_manifest.json")

    source_status = (boards or {}).get("source_status") or {}
    n_cap = sum(1 for s, st in source_status.items() if st == "CAPTURED")
    lineage = (boards or {}).get("lineage") or "DATA_LINEAGE_INSUFFICIENT"

    verdict = "COPY_EDGE_NO_GO"
    reasons = []
    if lineage == "DATA_LINEAGE_INSUFFICIENT" and n_cap == 0:
        verdict = "DATA_LINEAGE_INSUFFICIENT"
        reasons.append("no vendor board rows from GMGN/Debot/Fomo")
    if (recon or {}).get("status") == "VENDOR_LEDGER_GAP":
        reasons.append("July offline FIFO ∩ today's board is empty or mismatched (old tape)")
        if verdict != "DATA_LINEAGE_INSUFFICIENT":
            verdict = "VENDOR_LEDGER_GAP"
    live_st = (live_base or {}).get("status")
    if live_st:
        reasons.append(f"live Base GMGN vs public-index FIFO: {live_st} sign7={((live_base or {}).get('sign_agree_rate_7d'))}")
        if live_st == "VENDOR_LEDGER_GAP":
            verdict = "VENDOR_LEDGER_GAP"
        elif live_st == "LIVE_RECONCILED_DESCRIPTIVE" and verdict == "COPY_EDGE_NO_GO":
            verdict = "LIVE_RECONCILED_COPY_STILL_NO_GO"
    if (live_bsc or {}).get("status"):
        reasons.append(f"live BSC: {(live_bsc or {}).get('status')}")
    elif live_base and not live_bsc:
        reasons.append("BSC live reconstruct pending or index unavailable")
    if source_status.get("debot") == "CAPTURED_WRONG_VENUE":
        reasons.append(
            "Debot open board is Hyperliquid perp, not Base/BSC spot; its spot "
            "smart-money board is SOURCE_BLOCKED_AUTH"
        )
    if (debot_hl or {}).get("status"):
        reasons.append(
            f"Debot vs Hyperliquid own ledger: {(debot_hl or {}).get('status')} "
            f"sign_clean={(debot_hl or {}).get('sign_agree_rate_clean')} "
            f"within10%={(debot_hl or {}).get('frac_within_10pct')}"
        )
    if source_status.get("fomo") == "SOURCE_BLOCKED_AUTH":
        reasons.append("Fomo prod-api requires a Privy session; no anonymous board exists")
    tax_arm = (tax or {}).get("same_direction_copy_arm")
    if tax_arm == "CLOSED_TAX_EATS_EDGE":
        reasons.append("delay tax eats same-direction copy at reachable delay")
    elif tax_arm == "INCONCLUSIVE_SMALL_N":
        reasons.append("vendor∩tape delay-tax n below powered gate (do not close on n<20)")
    elif tax_arm == "INCONCLUSIVE_NO_OVERLAP":
        reasons.append("no vendor∩tape entries for delay tax")
    if (tax or {}).get("inconclusive_no_rpc"):
        reasons.append("atomic sell-sim skipped (INCONCLUSIVE_NO_RPC)")
    if not (arm_a or {}).get("passed"):
        reasons.append("Arm A did not pass preregistered gates")
    if not (arm_b or {}).get("passed"):
        reasons.append("Arm B did not pass preregistered gates")
    if not (live_bsc or {}).get("n_ok"):
        reasons.append("BSC replay INCONCLUSIVE_BSC_NO_LEDGER")
    else:
        reasons.append("BSC used GMGN activity hashes + public RPC receipts (no free Blockscout index)")
    reasons.append("prospective window after captured_at is empty on this tape")

    if (arm_a or {}).get("passed") or (arm_b or {}).get("passed"):
        # still not GO
        extra = "PROSPECTIVE_CONFIRMATION_REQUIRED" if verdict == "COPY_EDGE_NO_GO" else verdict
        verdict = extra

    payload = {
        "generated_at": utc_now_iso(),
        "project": "copy_cascade_invert_r0",
        "GO": False,
        "deploy_allowed": False,
        "live_allowed": False,
        "training_allowed": False,
        "self_go_allowed": False,
        "verdict": verdict,
        "reasons": reasons,
        "source_status": source_status,
        "board_lineage": lineage,
        "n_vendor_rows": (boards or {}).get("n_rows"),
        "n_vendor_addresses": (boards or {}).get("n_unique_addresses"),
        "reconcile_status": (recon or {}).get("status"),
        "same_direction_copy_arm": (tax or {}).get("same_direction_copy_arm"),
        "arm_a_passed": (arm_a or {}).get("passed"),
        "arm_b_passed": (arm_b or {}).get("passed"),
        "offline_copied": (copy_m or {}).get("n_copied"),
        "rpc_used": bool((live_base or {}).get("rpc_head") or (live_bsc or {}).get("rpc_head")),
        "rpc_kind": "public_https_only" if (live_base or live_bsc) else None,
        "eatlq_archive_used": False,
        "live_base": {
            "status": (live_base or {}).get("status"),
            "n_ok": (live_base or {}).get("n_ok"),
            "both_7d": (live_base or {}).get("both_7d"),
            "sign_agree_rate_7d": (live_base or {}).get("sign_agree_rate_7d"),
            "sign_agree_rate_7d_active": (live_base or {}).get("sign_agree_rate_7d_active"),
            "pearson_7d": (live_base or {}).get("pearson_7d"),
            "pearson_7d_active": (live_base or {}).get("pearson_7d_active"),
            "rpc_head": (live_base or {}).get("rpc_head"),
        },
        "live_bsc": {
            "status": (live_bsc or {}).get("status"),
            "n_ok": (live_bsc or {}).get("n_ok"),
            "both_7d": (live_bsc or {}).get("both_7d"),
            "sign_agree_rate_7d": (live_bsc or {}).get("sign_agree_rate_7d"),
            "sign_agree_rate_7d_active": (live_bsc or {}).get("sign_agree_rate_7d_active"),
        },
        "debot_hyperliquid": {
            "status": (debot_hl or {}).get("status"),
            "venue": (debot_hl or {}).get("venue"),
            "n_ok": (debot_hl or {}).get("n_ok"),
            "n_clean_active_uncapped": (debot_hl or {}).get("n_clean_active_uncapped"),
            "sign_agree_rate_clean": (debot_hl or {}).get("sign_agree_rate_clean"),
            "frac_within_10pct": (debot_hl or {}).get("frac_within_10pct"),
            "median_abs_ratio": (debot_hl or {}).get("median_abs_ratio"),
        },
        "venue_counts": (boards or {}).get("venue_counts"),
        "n_spot_reconcilable_addresses": (boards or {}).get("n_spot_reconcilable_addresses"),
    }
    write_json(ROOT / "out/FINAL.json", payload)
    _md(payload, tax, arm_a, arm_b, recon, boards, live_base, live_bsc, debot_hl)
    return payload


def _load(path):
    if not path.exists():
        return None
    return read_json(path)


def _md(p, tax, arm_a, arm_b, recon, boards=None, live_base=None, live_bsc=None, debot_hl=None) -> None:
    lines = [
        "# COPY_CASCADE_INVERT r0 — FINAL",
        "",
        f"**GO = false**. verdict = `{p['verdict']}`",
        "",
        f"generated_at: {p['generated_at']}",
        "",
        "## What was collected",
        "",
        f"- vendor source status: `{p['source_status']}`",
        f"- board lineage: `{p['board_lineage']}`",
        f"- vendor rows / unique addresses: {p['n_vendor_rows']} / {p['n_vendor_addresses']}",
        f"- unique by source: {(boards or {}).get('unique_by_source')}",
        f"- venue counts: {p.get('venue_counts')}",
        f"- spot-reconcilable addresses: {p.get('n_spot_reconcilable_addresses')}",
        f"- offline files copied: {p['offline_copied']}",
        f"- RPC used: {p['rpc_used']} ({p.get('rpc_kind')})",
        f"- EATLQ archive used: {p.get('eatlq_archive_used')}",
        "",
        "## Live reconstruct (public index, through today)",
        "",
        f"- Base status: `{(live_base or {}).get('status')}` n_ok={(live_base or {}).get('n_ok')} both7={(live_base or {}).get('both_7d')} sign_agree_7d={(live_base or {}).get('sign_agree_rate_7d')} active={(live_base or {}).get('sign_agree_rate_7d_active')} pearson={(live_base or {}).get('pearson_7d')} head={(live_base or {}).get('rpc_head')}",
        f"- BSC status: `{(live_bsc or {}).get('status')}` n_ok={(live_bsc or {}).get('n_ok')} both7={(live_bsc or {}).get('both_7d')} sign_agree_7d={(live_bsc or {}).get('sign_agree_rate_7d')} active={(live_bsc or {}).get('sign_agree_rate_7d_active')}",
        "",
        "## Debot board vs Hyperliquid own ledger (different venue)",
        "",
        f"- status: `{(debot_hl or {}).get('status')}` venue=`{(debot_hl or {}).get('venue')}`",
        f"- clean rows: {(debot_hl or {}).get('n_clean_active_uncapped')} sign_agree={(debot_hl or {}).get('sign_agree_rate_clean')} within10%={(debot_hl or {}).get('frac_within_10pct')} median_ratio={(debot_hl or {}).get('median_abs_ratio')}",
        "",
        "## Offline tape reconcile (July FIFO, not contemporaneous)",
        "",
        f"- status: `{(recon or {}).get('status')}`",
        f"- both (vendor ∩ our FIFO): {(recon or {}).get('n_both')}",
        f"- tape coverage: {(recon or {}).get('tape_coverage')}",
        f"- sign agree/disagree: {(recon or {}).get('sign_agree')} / {(recon or {}).get('sign_disagree')}",
        "",
        "## Delay tax",
        "",
        f"- subset: `{(tax or {}).get('subset')}`",
        f"- same-direction copy arm: `{(tax or {}).get('same_direction_copy_arm')}`",
        "",
        "## Arms",
        "",
        f"- Arm A passed: {p['arm_a_passed']} (n={(arm_a or {}).get('n_signals')}, mean={(arm_a or {}).get('mean')})",
        f"- Arm B passed: {p['arm_b_passed']} (n={(arm_b or {}).get('n_signals')}, mean={(arm_b or {}).get('mean')}, first_seen={(arm_b or {}).get('first_seen_status')})",
        "",
        "## Reasons",
        "",
    ]
    for r in p["reasons"]:
        lines.append(f"- {r}")
    lines += [
        "",
        "Dune is overlay only and was not renamed to GMGN/Debot/Fomo.",
        "Today's public board is fame_now, not historical fame.",
        "",
    ]
    (ROOT / "out/FINAL.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    print(finalize()["verdict"])
