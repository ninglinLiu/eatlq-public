"""Per-exit verdicts + EXIT_ALPHA_v0_REVIEW.md + seal / shadow governance."""
from __future__ import annotations

from typing import Any

from ..common import ROOT, sha256_file, save_json
from .common_exit import EXIT_OUT, ensure_exit_out, load_exit_cfg, stamp_status, utcnow


def finalize_exit(bakeoff: dict, *, arm_shadow: bool = True) -> dict[str, Any]:
    ensure_exit_out()
    cfg = load_exit_cfg()
    baseline = cfg["baseline_exit"]
    exits = bakeoff.get("exits", {})

    any_proven = False
    families = {}
    for policy in cfg["exits"]:
        if policy == baseline:
            families[policy] = {"verdict": "BASELINE"}
            continue
        e = exits.get(policy, {})
        v = e.get("verdict", "UNCONFIRMED")
        if v == "PROVEN":
            any_proven = True
        test_p = (e.get("TEST") or {}).get("paired_vs_baseline") or {}
        sealed_p = (e.get("SEALED") or {}).get("paired_vs_baseline") or {}
        families[policy] = {
            "verdict": v,
            "test_ok": e.get("test_ok"),
            "sealed_ok": e.get("sealed_ok"),
            "bh_reject": e.get("bh_reject"),
            "test_sum_delta": test_p.get("sum_delta"),
            "test_p05_day": test_p.get("p05_day"),
            "test_mean_daily_delta": test_p.get("mean_daily_delta"),
            "sealed_sum_delta": sealed_p.get("sum_delta"),
            "sealed_p05_day": sealed_p.get("p05_day"),
            "sealed_mean_daily_delta": sealed_p.get("mean_daily_delta"),
            "test_sched_pnl": ((e.get("TEST") or {}).get("summary") or {}).get(
                "scheduled_trading_pnl_weth"
            ),
            "sealed_sched_pnl": ((e.get("SEALED") or {}).get("summary") or {}).get(
                "scheduled_trading_pnl_weth"
            ),
        }

    dynamic = ["TRAIL_20_FROM_PEAK", "MOMENTUM_HOLD"]
    # stop rule: both dynamic exits REFUTED under sealed+net-of-cost
    both_dynamic_refuted = all(
        families.get(p, {}).get("verdict") == "REFUTED" for p in dynamic
    )
    stop_fired = both_dynamic_refuted

    propose_shadow = bool(any_proven)
    code_hash = sha256_file(ROOT / "src" / "exit_alpha" / "exit_bakeoff.py")
    t0 = utcnow() if propose_shadow else None

    report = {
        "task": "EXIT_ALPHA_V0_FINAL",
        "generated_at": utcnow(),
        "baseline": baseline,
        "exits": families,
        "bh": bakeoff.get("bh"),
        "any_proven": any_proven,
        "propose_observer_shadow": propose_shadow,
        "live": "NO_GO",
        "t0": t0,
        "code_hash_bakeoff": code_hash,
        "stop_rule": cfg["stop_rule"],
        "stop_rule_fired": bool(stop_fired and both_dynamic_refuted),
        "line_closed": bool(both_dynamic_refuted),
        "success_bar": cfg["success_bar"],
    }
    save_json(EXIT_OUT / "EXIT_ALPHA_FINAL.json", report)

    # baseline scheduled pnl for table
    base_test = ((exits.get(baseline) or {}).get("TEST") or {}).get("summary") or {}
    base_sealed = ((exits.get(baseline) or {}).get("SEALED") or {}).get("summary") or {}

    lines = [
        "# EXIT_ALPHA_v0_REVIEW",
        "",
        f"Generated: `{report['generated_at']}`",
        "",
        f"## Framing",
        "",
        "Entry is frozen capacity-constrained **random spray** (no stock-picking).",
        "Question: can a path-causal exit beat `FIXED_TP_4H` on net-of-cost scheduled-wallet PnL?",
        "",
        f"## Live: **NO_GO** | Observer shadow proposed: **{propose_shadow}**",
        "",
        "## Baseline scheduled PnL (net-of-cost)",
        "",
        f"- TEST scheduled PnL: `{base_test.get('scheduled_trading_pnl_weth')}` WETH "
        f"(fills={base_test.get('n_fills')})",
        f"- SEALED scheduled PnL: `{base_sealed.get('scheduled_trading_pnl_weth')}` WETH "
        f"(fills={base_sealed.get('n_fills')})",
        "",
        "## Per-exit verdicts (paired day Δ vs FIXED_TP_4H)",
        "",
        "| Exit | Verdict | TEST ΣΔ | TEST day-p05 | SEALED ΣΔ | SEALED day-p05 | BH |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for policy in cfg["exits"]:
        if policy == baseline:
            lines.append(f"| {policy} | BASELINE | — | — | — | — | — |")
            continue
        f = families[policy]
        def _fmt(x):
            return "—" if x is None else f"{float(x):.6f}"
        lines.append(
            f"| {policy} | **{f['verdict']}** | {_fmt(f['test_sum_delta'])} | "
            f"{_fmt(f['test_p05_day'])} | {_fmt(f['sealed_sum_delta'])} | "
            f"{_fmt(f['sealed_p05_day'])} | {f.get('bh_reject')} |"
        )

    lines += [
        "",
        "## Success bar",
        "",
        cfg["success_bar"],
        "",
        "## Stop rule",
        "",
        cfg["stop_rule"],
        "",
        f"Dynamic exits both REFUTED → line closed: **{report['line_closed']}**",
        "",
        "## BH (TEST)",
        "",
        f"```json\n{bakeoff.get('bh')}\n```",
        "",
    ]
    if propose_shadow:
        proven = [k for k, v in families.items() if v.get("verdict") == "PROVEN"]
        lines += [
            "## Observer shadow",
            "",
            f"- T0: `{t0}`",
            f"- Proven exits: `{proven}`",
            f"- Code hash (bakeoff): `{code_hash}`",
            "- Freeze: exit params + capacity + cost model",
            "- Mode: observer-only; no live orders.",
            "",
        ]
    else:
        lines += [
            "## Disposition",
            "",
            "No exit cleared PROVEN under TEST day-p05>0 + BH + SEALED replication "
            "on the same random-entry scheduled stream. `live = NO_GO`.",
            "",
        ]
        if report["line_closed"]:
            lines += [
                "Stop rule fired: both `TRAIL_20_FROM_PEAK` and `MOMENTUM_HOLD` are REFUTED. "
                "Exit-alpha line closed for this universe — do not keep adding exit variants.",
                "",
            ]

    md = "\n".join(lines)
    (EXIT_OUT / "EXIT_ALPHA_v0_REVIEW.md").write_text(md)
    (ROOT / "EXIT_ALPHA_v0_REVIEW.md").write_text(md)

    if propose_shadow and arm_shadow:
        _arm_shadow(t0, code_hash, families)
        stamp_status(
            "SHADOW_ARMED",
            t0=t0,
            code_hash_bakeoff=code_hash,
            proven_exits=[k for k, v in families.items() if v.get("verdict") == "PROVEN"],
        )
    elif report["line_closed"]:
        stamp_status("SEALED_EXIT_LINE_CLOSED", t0=None)
    elif all(
        families.get(p, {}).get("verdict") == "REFUTED"
        for p in cfg["exits"]
        if p != baseline
    ):
        stamp_status("SEALED_ALL_REFUTED", t0=None)
    else:
        stamp_status("SEALED_NO_PROVEN", t0=None)

    print(
        f"[exit/final] any_proven={any_proven} line_closed={report['line_closed']} "
        f"status={load_exit_cfg()['status']}",
        flush=True,
    )
    return report


def _arm_shadow(t0: str, code_hash: str, families: dict) -> None:
    shadow_dir = EXIT_OUT / "shadow"
    shadow_dir.mkdir(parents=True, exist_ok=True)
    shadow = {
        "mode": "OBSERVER_ONLY",
        "t0": t0,
        "code_hash": code_hash,
        "live": "NO_GO",
        "proven_exits": [k for k, v in families.items() if v.get("verdict") == "PROVEN"],
        "entry": "random_spray max_concurrent=10",
        "cost_model": "execution_depth.kappa + LP fee",
        "status": "ARMED",
    }
    save_json(shadow_dir / "SHADOW_STATUS.json", shadow)


if __name__ == "__main__":
    import json

    b = json.loads((EXIT_OUT / "exit_bakeoff.json").read_text())
    finalize_exit(b)
