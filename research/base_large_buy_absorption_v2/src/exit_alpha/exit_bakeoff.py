"""Random-entry scheduled bakeoff: each exit vs FIXED_TP_4H under net-of-cost."""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..alpha.cost_attach import attach_pnl_net
from ..arms_v1 import prepare_splits
from ..common import load_freeze, save_json
from ..execution_depth import compute_pool_kappa
from ..scheduler import simulate_scheduled
from ..scorecard_v1 import paired_scheduled_delta
from ..v3_common import bh_correct, p_from_boot_frac_positive
from .common_exit import EXIT_OUT, ensure_exit_out, load_exit_cfg, utcnow
from .exit_labels import label_all_exits


def _eval_scheduled(frame: pd.DataFrame, *, max_concurrent: int) -> dict[str, Any]:
    d = frame.copy()
    d["wallet_pnl_weth"] = pd.to_numeric(d["pnl_net"], errors="coerce").fillna(0.0)
    d["policy_selected"] = True
    fills, monthly, summary = simulate_scheduled(
        d,
        score_col="_sched_score",
        max_concurrent=max_concurrent,
        one_position_per_token=True,
        max_per_token=1,
    )
    return {"summary": summary, "fills": fills, "monthly": monthly}


def _prepare_universe(panel: pd.DataFrame) -> pd.DataFrame:
    """Eligible entry universe + fixed random scores + walk-forward splits."""
    cfg = load_exit_cfg()
    freeze = load_freeze()
    d = panel.copy()
    if "decision_time" not in d.columns:
        d["decision_time"] = d["entry_t"].astype(int)
    rng = np.random.default_rng(int(cfg["entry"]["random_seed"]))
    d["_sched_score"] = rng.random(len(d))
    d = prepare_splits(d, freeze)
    return d


def run_exit_bakeoff(
    panel: pd.DataFrame | None = None,
    *,
    resolved: pd.DataFrame | None = None,
    force_relabel: bool = False,
) -> dict[str, Any]:
    ensure_exit_out()
    cfg = load_exit_cfg()
    from ..common import OUT

    if panel is None:
        panel = pd.read_parquet(OUT / "panel" / "labeled_wait_absorption.parquet")
    if force_relabel:
        for policy in cfg["exits"]:
            p = EXIT_OUT / f"labeled_{policy}.parquet"
            if p.exists():
                p.unlink()

    base = _prepare_universe(panel)
    print(f"[exit/bakeoff] labeling {len(cfg['exits'])} exits on n={len(base):,}…", flush=True)
    labeled = label_all_exits(base, resolved)

    # attach cost once per exit; compute kappa once
    print("[exit/bakeoff] computing pool kappa…", flush=True)
    kappa = compute_pool_kappa()
    costed: dict[str, pd.DataFrame] = {}
    for policy, df in labeled.items():
        # preserve random scores / splits from base (same row order)
        df = df.copy()
        for col in ("_sched_score", "split", "fold", "is_sealed", "decision_time"):
            if col in base.columns:
                df[col] = base[col].to_numpy()
        c = attach_pnl_net(df, kappa=kappa)
        # eligible: scorable SUCCESS with known cost
        elig = (
            c["scorable_primary"].astype(bool)
            & c["buy_status"].eq("SUCCESS")
            & c["cost_known"].astype(bool)
            & c["pnl_net"].notna()
        )
        costed[policy] = c.loc[elig].copy()
        print(
            f"[exit/bakeoff] {policy}: eligible={elig.sum():,} "
            f"mean_pnl_net={costed[policy]['pnl_net'].mean():.6f}",
            flush=True,
        )

    baseline = cfg["baseline_exit"]
    max_c = int(cfg["entry"]["max_concurrent"])
    n_boot = int(cfg["bakeoff"]["n_boot"])
    seed = int(cfg["bakeoff"]["seed"])
    challengers = [e for e in cfg["exits"] if e != baseline]

    report: dict[str, Any] = {
        "task": "EXIT_ALPHA_V0_BAKEOFF",
        "generated_at": utcnow(),
        "baseline": baseline,
        "entry": cfg["entry"],
        "outcome": "pnl_net",
        "exits": {},
        "bh": {},
    }

    # Run scheduler per exit × split
    sched: dict[str, dict[str, dict]] = {}
    for policy, df in costed.items():
        sched[policy] = {}
        for split_name in ("TEST", "SEALED"):
            sub = df[df["split"] == split_name]
            if sub.empty and split_name == "SEALED":
                cut = df["decision_time"].quantile(0.85)
                sub = df[df["decision_time"] > cut]
            elif sub.empty and split_name == "TEST":
                cut = df["decision_time"].quantile(0.85)
                sub = df[df["decision_time"] <= cut]
            print(
                f"[exit/bakeoff] schedule {policy}/{split_name} n={len(sub):,}…",
                flush=True,
            )
            ev = _eval_scheduled(sub, max_concurrent=max_c)
            sched[policy][split_name] = ev
            report["exits"].setdefault(policy, {})[split_name] = {
                "n_eligible": int(len(sub)),
                "summary": ev["summary"],
            }

    # Paired day Δ vs baseline + BH on TEST
    pvals = []
    names = []
    for policy in challengers:
        for split_name in ("TEST", "SEALED"):
            treat = sched[policy][split_name]["fills"]
            ctrl = sched[baseline][split_name]["fills"]
            paired = paired_scheduled_delta(
                treat, ctrl, n_boot=n_boot, seed=seed + hash(policy) % 1000
            )
            report["exits"].setdefault(policy, {})[split_name]["paired_vs_baseline"] = {
                k: v for k, v in paired.items() if k != "bootstrap_token"
            }
            report["exits"].setdefault(baseline, {}).setdefault(split_name, {})
        # BH uses TEST day bootstrap
        test_paired = report["exits"][policy]["TEST"]["paired_vs_baseline"]
        frac = (test_paired.get("bootstrap_day") or {}).get("frac_positive", 0.0)
        p = p_from_boot_frac_positive(float(frac), n_boot)
        pvals.append(p)
        names.append(policy)

    reject = bh_correct(pvals, alpha=float(cfg["multiple_testing"]["alpha"]))
    report["bh"] = {
        "method": cfg["multiple_testing"]["method"],
        "alpha": cfg["multiple_testing"]["alpha"],
        "pvals": dict(zip(names, pvals)),
        "reject": dict(zip(names, reject)),
    }

    # Per-exit verdicts
    for policy in cfg["exits"]:
        if policy == baseline:
            report["exits"][policy]["verdict"] = "BASELINE"
            continue
        test_p = report["exits"][policy]["TEST"]["paired_vs_baseline"]
        sealed_p = report["exits"][policy]["SEALED"]["paired_vs_baseline"]
        test_ok = bool(
            test_p.get("p05_day") is not None
            and float(test_p["p05_day"]) > 0
            and float(test_p.get("mean_daily_delta") or 0) > 0
        )
        sealed_ok = bool(
            sealed_p.get("p05_day") is not None
            and float(sealed_p["p05_day"]) > 0
            and float(sealed_p.get("mean_daily_delta") or 0) > 0
        )
        bh_ok = bool(report["bh"]["reject"].get(policy, False))
        if test_ok and sealed_ok and bh_ok:
            verdict = "PROVEN"
        elif (test_p.get("mean_daily_delta") or 0) <= 0 and not test_ok:
            verdict = "REFUTED"
        elif test_ok and not sealed_ok:
            verdict = "UNCONFIRMED"
        elif test_ok and sealed_ok and not bh_ok:
            verdict = "UNCONFIRMED"
        else:
            verdict = "REFUTED" if (test_p.get("sum_delta") or 0) <= 0 else "UNCONFIRMED"
        report["exits"][policy]["verdict"] = verdict
        report["exits"][policy]["test_ok"] = test_ok
        report["exits"][policy]["sealed_ok"] = sealed_ok
        report["exits"][policy]["bh_reject"] = bh_ok

    # Baseline summary block
    for split_name in ("TEST", "SEALED"):
        report["exits"].setdefault(baseline, {}).setdefault(split_name, {})
        if "summary" not in report["exits"][baseline].get(split_name, {}):
            report["exits"][baseline][split_name]["summary"] = sched[baseline][split_name]["summary"]
            report["exits"][baseline][split_name]["n_eligible"] = int(
                len(costed[baseline][costed[baseline]["split"] == split_name])
            )

    save_json(EXIT_OUT / "exit_bakeoff.json", report)
    # also stash fills for finalize diagnostics
    for policy in cfg["exits"]:
        for split_name in ("TEST", "SEALED"):
            fills = sched[policy][split_name]["fills"]
            if fills is not None and len(fills):
                fills.to_parquet(
                    EXIT_OUT / f"fills_{policy}_{split_name}.parquet", index=False
                )
    print("[exit/bakeoff] done", flush=True)
    return report


if __name__ == "__main__":
    run_exit_bakeoff()
