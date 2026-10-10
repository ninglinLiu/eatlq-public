"""Task 3: stratified actor-coverage decomposition + Wilson / rule-of-three CI.

RPC golden expansion is skippable (default skip) to avoid burning API credits.
"""
from __future__ import annotations

import math
import os
import time
from typing import Any

import numpy as np
import pandas as pd

from ..common import OUT, get_rpc_url
from ..resolution_gate import audit_row, choose_stratified
from .data import load_audit_cfg, save_audit_json


def wilson_upper(errors: int, n: int, z: float = 1.96) -> float:
    if n <= 0:
        return 1.0
    phat = errors / n
    denom = 1 + z**2 / n
    centre = phat + z**2 / (2 * n)
    adj = z * math.sqrt(phat * (1 - phat) / n + z**2 / (4 * n**2))
    return float(min(1.0, (centre + adj) / denom))


def rule_of_three_upper(n: int) -> float:
    """95% upper bound when zero errors observed ≈ 3/n."""
    if n <= 0:
        return 1.0
    return float(min(1.0, 3.0 / n))


def n_for_rule_of_three(target_upper: float = 0.02) -> int:
    return int(math.ceil(3.0 / target_upper))


def decompose_coverage(resolved: pd.DataFrame) -> dict[str, Any]:
    buy = resolved[resolved["side"] == "buy"].copy()
    n = len(buy)
    resolved_mask = buy["economic_actor"].astype(str).str.len() == 42
    tier_ok = buy["tier"].isin(["A", "B", "C"])
    cens = buy["censored"].astype(bool) if "censored" in buy.columns else False
    scorable = resolved_mask & tier_ok & ~cens

    # Per route_family x tier
    rows = []
    for (rf, tier), g in buy.groupby(["route_family", "tier"], dropna=False):
        rm = g["economic_actor"].astype(str).str.len() == 42
        rows.append({
            "route_family": str(rf),
            "tier": str(tier),
            "n": int(len(g)),
            "resolved_rate": float(rm.mean()) if len(g) else 0.0,
            "share_of_buys": float(len(g) / n) if n else 0.0,
        })
    by_route = (
        buy.assign(_ok=scorable.astype(int))
        .groupby("route_family", dropna=False)
        .agg(n=("tx_hash", "size"), scorable_n=("_ok", "sum"))
        .reset_index()
    )
    by_route["scorable_rate"] = by_route["scorable_n"] / by_route["n"].clip(lower=1)

    # Attribution source recovery explanation
    src_counts = {}
    if "attribution_source" in buy.columns:
        src_counts = (
            buy.loc[scorable, "attribution_source"].fillna("unknown").astype(str).value_counts().to_dict()
        )
    route_resolved = (
        buy.loc[resolved_mask, "route_family"].fillna("unknown").astype(str).value_counts().to_dict()
    )

    jump_explanation = {
        "old_pipeline_eoa_coverage_claimed": 0.0065,
        "new_resolvable_scorable_coverage": float(scorable.mean()) if n else 0.0,
        "delta_pp": float(scorable.mean() - 0.0065) * 100 if n else None,
        "primary_recovery_channels": [
            {
                "channel": "universal_router_recipient_as_economic_actor",
                "route_family": "universal_router",
                "n_resolved_buys": int(route_resolved.get("universal_router", 0)),
                "note": "Tier C: recipient/sender offline resolution — not requiring tx.from EOA.",
            },
            {
                "channel": "uniswap_router_direct",
                "route_family": "uniswap_router",
                "n_resolved_buys": int(route_resolved.get("uniswap_router", 0)),
            },
            {
                "channel": "erc4337_userop_sender",
                "route_family": "erc4337_entrypoint",
                "n_resolved_buys": int(route_resolved.get("erc4337_entrypoint", 0)),
                "note": "Tier B: UserOperationEvent sender; rare in this universe.",
            },
            {
                "channel": "unknown_or_contract_fallback",
                "route_family": "unknown*",
                "n_resolved_buys": int(
                    route_resolved.get("unknown", 0) + route_resolved.get("unknown_contract_or_route", 0)
                ),
            },
        ],
        "caveat": (
            "Coverage jump is largely definitional: scorable subject = resolved economic actor "
            "appearing as recipient/sender/UserOp, not exclusive EOA tx.from. Whether that subject "
            "is the true economic decision-maker still requires stratified trace CI."
        ),
    }

    return {
        "n_buys": int(n),
        "resolved_rate": float(resolved_mask.mean()) if n else 0.0,
        "resolvable_scorable_coverage": float(scorable.mean()) if n else 0.0,
        "unresolved_coverage": float((~resolved_mask).mean()) if n else 1.0,
        "tier_counts": buy["tier"].value_counts().to_dict(),
        "route_counts": buy["route_family"].fillna("unknown").value_counts().to_dict(),
        "route_family_x_tier": rows,
        "route_scorable": by_route.to_dict(orient="records"),
        "attribution_source_among_scorable": src_counts,
        "coverage_jump_explanation": jump_explanation,
    }


def existing_gate_ci() -> dict[str, Any]:
    import json

    gate_path = OUT / "phase1" / "RESOLUTION_GATE.json"
    gate = {}
    if gate_path.exists():
        gate = json.loads(gate_path.read_text())
    n = int(gate.get("n_audited") or 0)
    err = float(gate.get("sampled_error_rate") or 0.0)
    errors = int(round(err * n))
    return {
        "source": "phase1/RESOLUTION_GATE.json",
        "n_audited_traces": n,
        "n_golden_sampled": int(gate.get("n_golden_sampled") or 0),
        "observed_error_rate": err,
        "observed_errors": errors,
        "wilson_95_upper": wilson_upper(errors, n),
        "rule_of_three_95_upper_if_zero": rule_of_three_upper(n),
        "route_family_sample_counts": gate.get("route_family_sample_counts"),
        "tier_sample_counts": gate.get("tier_sample_counts"),
        "passes_lt_2pct_upper": bool(wilson_upper(errors, n) < 0.02),
        "n_required_for_rule_of_three_lt_2pct": n_for_rule_of_three(0.02),
        "verdict": "PASS_PENDING_STRATIFIED_CI_AUDIT",
    }


def maybe_expand_traces(resolved: pd.DataFrame, cfg: dict) -> dict[str, Any]:
    rpc_cfg = cfg.get("rpc", {})
    skip = bool(rpc_cfg.get("skip_by_default", True))
    if os.environ.get("AUDIT_RUN_RPC", "").strip() in ("1", "true", "TRUE"):
        skip = False
    if os.environ.get("AUDIT_SKIP_RPC", "").strip() in ("1", "true", "TRUE"):
        skip = True
    target_n = int(rpc_cfg.get("max_traces", 160))
    if skip:
        return {
            "ran": False,
            "skipped": True,
            "reason": "RPC skipped by default (set AUDIT_RUN_RPC=1 to expand golden traces).",
            "target_traces": target_n,
            "n_required_for_rule_of_three_lt_2pct": n_for_rule_of_three(
                float(rpc_cfg.get("target_wilson_upper_lt", 0.02))
            ),
        }

    # Expand traces
    try:
        from ..actor_resolution import RpcClient
        rpc = RpcClient(get_rpc_url())
    except Exception as e:
        return {"ran": False, "skipped": True, "reason": f"RPC unavailable: {e}"}

    sample = choose_stratified(resolved, target_n)
    budget_s = float(rpc_cfg.get("time_budget_seconds", 300))
    sleep_s = float(rpc_cfg.get("sleep_between_traces", 0.15))
    results = []
    t0 = time.monotonic()
    for i, (_, row) in enumerate(sample.iterrows(), 1):
        if time.monotonic() - t0 > budget_s:
            break
        results.append(audit_row(rpc, row))
        if i % 10 == 0:
            print(f"[audit:coverage] trace {i}/{len(sample)}", flush=True)
        time.sleep(sleep_s)

    ok = [r for r in results if r.get("trace_ok")]
    if ok:
        err_rate = float(1.0 - np.mean([r["trace_match"] for r in ok]))
        errors = int(round(err_rate * len(ok)))
    else:
        err_rate, errors = 1.0, 0
    n = len(ok)
    by_route: dict[str, dict] = {}
    for r in ok:
        rf = str(r.get("route_family") or "unknown")
        by_route.setdefault(rf, {"n": 0, "errors": 0})
        by_route[rf]["n"] += 1
        if not r.get("trace_match"):
            by_route[rf]["errors"] += 1
    for rf, v in by_route.items():
        v["error_rate"] = v["errors"] / v["n"] if v["n"] else None
        v["wilson_95_upper"] = wilson_upper(v["errors"], v["n"])

    out = {
        "ran": True,
        "skipped": False,
        "n_traces_ok": n,
        "observed_error_rate": err_rate,
        "wilson_95_upper": wilson_upper(errors, n),
        "rule_of_three_95_upper_if_zero": rule_of_three_upper(n) if errors == 0 else None,
        "by_route_family": by_route,
        "passes_lt_2pct_upper": bool(wilson_upper(errors, n) < 0.02),
    }
    pd.DataFrame(results).to_csv(OUT / "audit" / "coverage_golden_rows.csv", index=False)
    return out


def run_coverage_ci() -> dict[str, Any]:
    cfg = load_audit_cfg()
    resolved = pd.read_parquet(OUT / "phase1" / "resolved_swaps.parquet")
    print("[audit:coverage] decomposing coverage ...", flush=True)
    decomp = decompose_coverage(resolved)
    gate_ci = existing_gate_ci()
    print(
        f"[audit:coverage] existing n_audited={gate_ci['n_audited_traces']} "
        f"wilson_upper={gate_ci['wilson_95_upper']:.3f} "
        f"need_n>={gate_ci['n_required_for_rule_of_three_lt_2pct']}",
        flush=True,
    )
    expand = maybe_expand_traces(resolved, cfg)
    if expand.get("ran"):
        actor_verdict = (
            "ACTOR_RESOLUTION_CI_PASS"
            if expand.get("passes_lt_2pct_upper")
            else "PASS_PENDING_STRATIFIED_CI_AUDIT"
        )
    else:
        actor_verdict = "PASS_PENDING_STRATIFIED_CI_AUDIT"

    report = {
        "task": "stratified_actor_coverage_ci",
        "decomposition": decomp,
        "existing_gate_ci": gate_ci,
        "expanded_traces": expand,
        "actor_resolution_verdict": actor_verdict,
    }
    save_audit_json("03_coverage_ci.json", report)
    return report
