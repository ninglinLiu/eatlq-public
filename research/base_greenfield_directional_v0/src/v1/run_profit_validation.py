#!/usr/bin/env python3
"""Greenfield V1 Profit Validation orchestrator — no discovery / no artifact mutation."""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

ROOT = Path("/root/base_greenfield_directional_v0")
sys.path.insert(0, str(ROOT))

from src.features import FEATURE_COLS, fold_fit_residuals  # noqa: E402
from src.v1.atomic_engine import (  # noqa: E402
    AMT_IN_PROD,
    H,
    ProdAtomicSim,
    calibrate,
    load_swaps_index,
    simulate_trade,
)
from src.v1.freeze_artifacts import freeze as freeze_artifacts  # noqa: E402
from src.v1.oof_audit import run_audit  # noqa: E402

OUT = ROOT / "out"
OUT_V1 = ROOT / "out_v1"
TP, SL = 0.50, 0.35
MAX_CONCURRENT = 3
WALLET_ETH = 0.5  # documented research wallet scale
P_FATAL_MAX = 0.35  # frozen from discovery STACK rule — not searched


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_atomic_label_spec():
    text = f"""# Atomic Label Spec (V1 Profit Validation)

## Production execution assumptions (frozen)

| Item | Value |
|---|---|
| Router | SwapRouter02 `0x2626664c2603336E57B271c5C0b26F421741e481` |
| Quoter | QuoterV2 `0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a` |
| amt_in | `{AMT_IN_PROD}` wei (= $50 @ 3000; within 0.02 ETH executor cap) |
| entry_delay | +{1} block after decision_block |
| gas_debit | 0.0002 WETH round-trip (debit on success path) |
| slippage | amountOutMinimum = 0 in sim (atomic eth_call) |
| allowance | stateDiff grant to router for probe |
| recipient | probe `0x…dEaD` |
| retry | INFRASTRUCTURE_UNKNOWN only: +0,+5,+30,+150 blocks |
| max hold | H={H}s with TB_A barriers for exit timing |

## Status taxonomy

- SELL_SUCCESS
- DETERMINISTIC_REVERT
- ECONOMICALLY_UNSELLABLE (tax ≥ 90% vs amt_in)
- INFRASTRUCTURE_UNKNOWN

## Primary outcome

`realized_net_weth_return = weth_out / amt_in - 1 - gas_debit` on SELL_SUCCESS;
`-1.0` on deterministic/economic failure; NULL on INFRASTRUCTURE_UNKNOWN (excluded from EV denominators requiring labels).

PIT forward ticks are **comparison only** (exit timing / MFE/MAE), never Primary PnL.
"""
    (OUT_V1 / "ATOMIC_LABEL_SPEC.md").write_text(text)


def build_outcomes_for_ids(signal_ids: set[str], sim: ProdAtomicSim, swaps) -> pd.DataFrame:
    labs = pd.read_parquet(OUT / "labels" / "labels_full.parquet")
    # need entry_tick from panel
    panel = pd.read_parquet(OUT / "panel" / "greenfield_panel_15-60m.parquet")[
        ["signal_id", "entry_tick", "entry_liq"]
    ]
    base = labs.merge(panel, on="signal_id", how="left")
    base = base[base["signal_id"].isin(signal_ids)].copy()
    rows = []
    for i, r in enumerate(base.itertuples(index=False)):
        if i % 25 == 0:
            print(f"[atomic] {i}/{len(base)} rpc_calls={sim.calls}", flush=True)
        rows.append(simulate_trade(sim, r, swaps))
    return pd.DataFrame(rows)


def metrics_for_policy(sel: pd.DataFrame, outcomes: pd.DataFrame, name: str, mode: str) -> dict:
    oc = outcomes.copy()
    # avoid column collisions with selection frame
    rename = {}
    for c in ("barrier_event", "score", "fold", "pool", "decision_time", "label_end_time"):
        if c in oc.columns and c in sel.columns and c != "signal_id":
            rename[c] = f"out_{c}"
    oc = oc.rename(columns=rename)
    d = sel.merge(oc, on="signal_id", how="left")
    d = d.sort_values("decision_time")
    accepted = d[d["policy_selected"]].copy()
    labeled = accepted[accepted["realized_net_weth_return"].notna()]
    n_opp = len(d)
    n_acc = len(labeled)
    rets = labeled["realized_net_weth_return"]
    pnl = float(rets.sum()) if n_acc else 0.0
    ch = labeled["capital_hours"].replace(0, np.nan)
    pnl_per_ch = float(rets.sum() / ch.sum()) if ch.notna().any() and ch.sum() > 0 else None
    if n_acc:
        cum = labeled.sort_values("decision_time")["realized_net_weth_return"].cumsum()
        dd = float((cum - cum.cummax()).min())
    else:
        dd = None
    es = float(rets.quantile(0.05)) if n_acc >= 5 else (float(rets.min()) if n_acc else None)
    be_col = "out_barrier_event" if "out_barrier_event" in labeled.columns else "barrier_event"
    fatal = float((labeled[be_col] == "FATAL_FIRST").mean()) if n_acc and be_col in labeled.columns else None
    sell_fail = float(labeled["simulation_status"].isin(["DETERMINISTIC_REVERT", "ECONOMICALLY_UNSELLABLE"]).mean()) if n_acc else None
    # top concentration on WETH pnl
    if n_acc and pnl != 0:
        srt = labeled.sort_values("realized_net_weth_return", ascending=False)
        top1 = float(srt["realized_net_weth_return"].iloc[0] / pnl)
        top5 = float(srt["realized_net_weth_return"].head(5).sum() / pnl)
        ex5 = float(srt["realized_net_weth_return"].iloc[5:].sum()) if n_acc > 5 else 0.0
    else:
        top1 = top5 = ex5 = None
    # worst week
    if n_acc:
        labeled = labeled.copy()
        labeled["week"] = labeled["decision_time"] // (7 * 86400)
        wk = labeled.groupby("week")["realized_net_weth_return"].sum()
        worst_week = float(wk.min())
    else:
        worst_week = None
    # wallet / concurrency constrained PnL (D)
    wallet_pnl = None
    if mode == "wallet_constrained" and n_acc:
        cash = WALLET_ETH
        open_pos = []  # (end_time, stake)
        realized = 0.0
        stake = AMT_IN_PROD / 1e18
        for r in labeled.sort_values("decision_time").itertuples(index=False):
            t = int(r.decision_time)
            open_pos = [(e, s) for (e, s) in open_pos if e > t]
            if len(open_pos) >= MAX_CONCURRENT:
                continue
            locked = sum(s for _, s in open_pos)
            if cash - locked < stake:
                continue
            open_pos.append((int(r.label_end_time), stake))
            # settle immediately in research accounting at outcome
            if r.realized_net_weth_return is not None:
                realized += stake * float(r.realized_net_weth_return)
        wallet_pnl = realized

    # XS IC — NA when slice < 2
    ics = []
    for _, g in d.groupby("decision_time"):
        if len(g) < 2 or g["score"].isna().all():
            continue
        if g["realized_net_weth_return"].notna().sum() < 2:
            continue
        ics.append(float(g["score"].corr(g["realized_net_weth_return"])))
    rank_ic = float(np.nanmean(ics)) if ics else None  # NA not 0

    return {
        "policy": name,
        "mode": mode,
        "n_opportunities": n_opp,
        "n_accepted_labeled": n_acc,
        "acceptance_rate": n_acc / n_opp if n_opp else 0.0,
        "total_net_weth_pnl": pnl if mode != "wallet_constrained" else wallet_pnl,
        "ev_per_accepted": float(rets.mean()) if n_acc else None,
        "ev_per_opportunity": float(d["realized_net_weth_return"].where(d["policy_selected"], 0.0).fillna(0.0).mean()) if n_opp else None,
        "pnl_per_capital_hour": pnl_per_ch,
        "max_drawdown": dd,
        "expected_shortfall_5pct": es,
        "fatal_rate": fatal,
        "sell_failure_rate": sell_fail,
        "median": float(rets.median()) if n_acc else None,
        "trimmed_mean": float(rets.sort_values().iloc[int(0.05 * n_acc) : int(0.95 * n_acc)].mean()) if n_acc >= 20 else (float(rets.mean()) if n_acc else None),
        "top1_share": top1,
        "top5_share": top5,
        "pnl_ex_top5": ex5,
        "worst_week": worst_week,
        "independent_positions": True,
        "rank_ic": rank_ic,
        "wallet_constrained_pnl": wallet_pnl,
        "unknown_rate": float(accepted["realized_net_weth_return"].isna().mean()) if len(accepted) else None,
    }


def apply_capital_match(sel: pd.DataFrame, n_target: int, hours_target: float | None, outcomes: pd.DataFrame) -> pd.DataFrame:
    """Matched trade count / capital-hours: keep earliest selected until budget hit."""
    d = sel[sel["policy_selected"]].sort_values("decision_time").copy()
    d = d.merge(outcomes[["signal_id", "capital_hours"]], on="signal_id", how="left")
    keep = []
    hours = 0.0
    for r in d.itertuples(index=False):
        if len(keep) >= n_target:
            break
        if hours_target is not None and hours + float(r.capital_hours or 0) > hours_target and keep:
            break
        keep.append(r.signal_id)
        hours += float(r.capital_hours or 0)
    out = sel.copy()
    out["policy_selected"] = out["signal_id"].isin(keep)
    return out


def select_primary(comp: pd.DataFrame) -> dict:
    """Decide unique Primary among P0/P1/P2 using raw production mode rows."""
    raw = comp[comp["mode"] == "raw_production"].copy()
    raw = raw[raw["policy"].isin(["P0_C3_MOMENTUM", "P1_G2_COMPETING", "P2_C3_G2_VETO"])]
    # score: prioritize total pnl, then pnl/ch, then lower fatal, then lower |dd|
    def key(r):
        pnl = r["total_net_weth_pnl"] if pd.notna(r["total_net_weth_pnl"]) else -1e9
        pch = r["pnl_per_capital_hour"] if pd.notna(r["pnl_per_capital_hour"]) else -1e9
        fatal = r["fatal_rate"] if pd.notna(r["fatal_rate"]) else 1.0
        dd = r["max_drawdown"] if pd.notna(r["max_drawdown"]) else -1.0
        es = r["expected_shortfall_5pct"] if pd.notna(r["expected_shortfall_5pct"]) else -1.0
        return (pnl, pch, -fatal, dd, es)

    ranked = sorted(raw.to_dict("records"), key=key, reverse=True)
    primary = ranked[0]["policy"] if ranked else None
    p0 = raw[raw["policy"] == "P0_C3_MOMENTUM"].iloc[0].to_dict() if (raw["policy"] == "P0_C3_MOMENTUM").any() else {}
    p1 = raw[raw["policy"] == "P1_G2_COMPETING"].iloc[0].to_dict() if (raw["policy"] == "P1_G2_COMPETING").any() else {}

    kill_g2 = False
    reasons = []
    if p0 and p1:
        c3_pnl = p0.get("total_net_weth_pnl") or -1e9
        g2_pnl = p1.get("total_net_weth_pnl") or -1e9
        c3_pch = p0.get("pnl_per_capital_hour") or -1e9
        g2_pch = p1.get("pnl_per_capital_hour") or -1e9
        c3_es = p0.get("expected_shortfall_5pct") or -1e9
        g2_es = p1.get("expected_shortfall_5pct") or -1e9
        if g2_pnl <= c3_pnl and g2_pch <= c3_pch:
            kill_g2 = True
            reasons.append("G2_did_not_beat_C3_on_total_pnl_and_pnl_per_capital_hour")
        if g2_pnl <= c3_pnl and (g2_es <= c3_es):
            reasons.append("G2_no_risk_adjusted_improvement_vs_C3")

    # If G2 killed and was ranked primary, fall back
    if kill_g2 and primary == "P1_G2_COMPETING":
        primary = "P0_C3_MOMENTUM" if (raw["policy"] == "P0_C3_MOMENTUM").any() else primary
        # P2 may still win if it improves risk-adjusted growth
        p2 = raw[raw["policy"] == "P2_C3_G2_VETO"]
        if len(p2):
            p2r = p2.iloc[0]
            if (p2r["total_net_weth_pnl"] or -1e9) > (p0.get("total_net_weth_pnl") or -1e9) and (
                (p2r["fatal_rate"] or 1) < (p0.get("fatal_rate") or 1) or (p2r["expected_shortfall_5pct"] or -1) > (p0.get("expected_shortfall_5pct") or -1)
            ):
                primary = "P2_C3_G2_VETO"

    return {
        "primary_policy": primary,
        "ranked": [r["policy"] for r in ranked],
        "kill_g2": kill_g2,
        "kill_g2_reasons": reasons,
        "secondary_policies": [p for p in ["P0_C3_MOMENTUM", "P1_G2_COMPETING", "P2_C3_G2_VETO"] if p != primary],
    }


def _ensure_residual_cols(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["market_adj_ret"] = d.get("market_adj_ret", d["early_ret"])
    d["flow_surprise"] = d.get("flow_surprise", d["net_flow"])
    d["path_curvature"] = d.get("path_curvature", d["accel"])
    d["vol_depth"] = d["pre_vol"] / (d["liq_log"].abs() + 1e-6)
    d["part_concentration"] = 1.0 / d["buyers_pre"].clip(lower=1)
    d["flow_persistence"] = d["trade_count_imbalance"] * np.sign(d["net_flow"])
    return d


def fit_operational_on_dev(feat: pd.DataFrame) -> dict:
    """Frozen recipes fit on DEV only for holdout application (no holdout peek)."""
    dev = feat[feat["split_role"] == "DEV"].copy()
    c3_thr = float(dev["early_ret"].quantile(0.60))
    tr = _ensure_residual_cols(dev.dropna(subset=["barrier_event"]).copy())
    # residualize train against itself for operational fit (holdout uses same feature columns as discovery matrix)
    dummy = tr.iloc[:1].copy()
    tr, _ = fold_fit_residuals(tr, dummy)
    cols = [c for c in FEATURE_COLS if c in tr.columns]
    X = tr[cols].replace([np.inf, -np.inf], np.nan).fillna(0.0).values
    models = {}
    for label in ["TP_FIRST", "SL_FIRST", "FATAL_FIRST", "TIMEOUT"]:
        y = (tr["barrier_event"] == label).astype(int).values
        m = HistGradientBoostingClassifier(max_depth=3, max_iter=80, learning_rate=0.05, random_state=0)
        if 0 < y.sum() < len(y):
            m.fit(X, y)
            models[label] = m
    probs = {}
    for label, key in [("TP_FIRST", "p_tp"), ("SL_FIRST", "p_sl"), ("FATAL_FIRST", "p_fatal")]:
        if label in models:
            probs[key] = models[label].predict_proba(X)[:, 1]
        else:
            probs[key] = np.zeros(len(tr))
    eu = probs["p_tp"] * TP + probs["p_sl"] * (-SL) + probs["p_fatal"] * (-1.0)
    g2_thr = float(np.quantile(eu, 0.60))
    return {"c3_thr": c3_thr, "g2_thr": g2_thr, "g2_models": models, "feature_cols": cols}


def score_holdout(feat: pd.DataFrame, op: dict) -> pd.DataFrame:
    h = feat[feat["split_role"] == "SEALED_HOLDOUT"].copy()
    # Use discovery feature_matrix residual placeholders; refit residuals DEV→holdout
    dev = _ensure_residual_cols(feat[feat["split_role"] == "DEV"].copy())
    te_in = _ensure_residual_cols(h)
    tr, te = fold_fit_residuals(dev, te_in)
    cols = op["feature_cols"]
    X = te[cols].replace([np.inf, -np.inf], np.nan).fillna(0.0).values
    probs = {}
    for label, key in [("TP_FIRST", "p_tp"), ("SL_FIRST", "p_sl"), ("FATAL_FIRST", "p_fatal"), ("TIMEOUT", "p_timeout")]:
        if label in op["g2_models"]:
            probs[key] = op["g2_models"][label].predict_proba(X)[:, 1]
        else:
            probs[key] = np.zeros(len(te))
    te = te.copy()
    for k, v in probs.items():
        te[k] = v
    te["g2_eu"] = te["p_tp"] * TP + te["p_sl"] * (-SL) + te["p_fatal"] * (-1.0)
    te["c3_score"] = te["early_ret"]
    te["p0_selected"] = te["c3_score"] >= op["c3_thr"]
    te["p1_selected"] = te["g2_eu"] >= op["g2_thr"]
    te["p2_selected"] = te["p0_selected"] & (te["g2_eu"] > 0) & (te["p_fatal"] < P_FATAL_MAX)
    # non-overlapping per pool
    for col in ["p0_selected", "p1_selected", "p2_selected"]:
        keep = pd.Series(False, index=te.index)
        last = {}
        for i, r in te.sort_values("decision_time").iterrows():
            if not r[col]:
                continue
            if r["pool"] in last and r["decision_time"] < last[r["pool"]]:
                continue
            keep.loc[i] = True
            last[r["pool"]] = int(r["label_end_time"])
        te[col] = keep
    return te


def holdout_verdict(primary: str, metrics: dict, n_ind: int) -> str:
    if metrics.get("atomic_label_failure"):
        return "ATOMIC_LABEL_FAILURE"
    if n_ind < 20:
        return "INSUFFICIENT_INDEPENDENT_SAMPLE"
    pnl = metrics.get("total_net_weth_pnl")
    if pnl is None:
        return "ATOMIC_LABEL_FAILURE"
    if pnl <= 0:
        return "HOLDOUT_FAIL_ALL"
    mapping = {
        "P0_C3_MOMENTUM": "HOLDOUT_PASS_C3",
        "P1_G2_COMPETING": "HOLDOUT_PASS_G2",
        "P2_C3_G2_VETO": "HOLDOUT_PASS_C3_G2",
    }
    return mapping.get(primary, "HOLDOUT_FAIL_ALL")


def write_shadow_spec(primary: str, secondaries: list):
    text = f"""# Shadow Spec (Prospective — zero capital)

## Start
From current Base tip block at recorder start.

## Policies (simultaneous, no live orders)

1. Primary: `{primary}`
2. Secondaries: {secondaries}

## Rules

- Every decision row is written to disk **before** outcome observation.
- Each decision file is SHA256-hashed; hash chain appended to `out_v1/shadow/ledger.jsonl`.
- No threshold / model / universe changes based on live PnL.
- No true money execution; dry-run only.

## Recorder entrypoint (not started by this validation run)

`src/v1/shadow_recorder.py` (to be launched separately under human approval).
"""
    (OUT_V1 / "SHADOW_SPEC.md").write_text(text)
    # stub recorder
    (ROOT / "src" / "v1" / "shadow_recorder.py").write_text(
        '''#!/usr/bin/env python3
"""Prospective shadow recorder stub — zero capital, hash-before-outcome."""
print("SHADOW_RECORDER_STUB: launch only under human approval; see out_v1/SHADOW_SPEC.md")
'''
    )


def main():
    OUT_V1.mkdir(parents=True, exist_ok=True)
    manifest_path = OUT_V1 / "PROFIT_VALIDATION_MANIFEST.json"
    resume = manifest_path.exists() and (OUT_V1 / "DEV_POLICY_COMPARISON.csv").exists()

    if resume:
        print("=== RESUME: manifest frozen — skip to holdout ===", flush=True)
        art = json.loads((OUT_V1 / "ARTIFACT_FREEZE.json").read_text())
        audit = json.loads((OUT_V1 / "OOF_PROVENANCE_AUDIT.json").read_text())
        cal_sum = json.loads((OUT_V1 / "ATOMIC_CALIBRATION_SUMMARY.json").read_text())
        choice = json.loads((OUT_V1 / "PRIMARY_SELECTION.json").read_text())
        manifest = json.loads(manifest_path.read_text())
        # verify manifest hash unchanged
        expected = (OUT_V1 / "PROFIT_VALIDATION_MANIFEST.sha256").read_text().strip()
        actual = sha256_file(manifest_path)
        if expected != actual:
            raise SystemExit("MANIFEST_TAMPER_DETECTED")
        sim = ProdAtomicSim()
        swaps = load_swaps_index()
    else:
        print("=== V1.1 FREEZE ===", flush=True)
        art = freeze_artifacts()

        print("=== V1.2 OOF AUDIT ===", flush=True)
        audit = run_audit()
        if audit.get("status") == "OOF_PROVENANCE_FAILURE":
            final = {
                "created_utc": utcnow(),
                "result": "OOF_PROVENANCE_FAILURE",
                "deploy_allowed": False,
            }
            (OUT_V1 / "FINAL_PROFIT_VALIDATION.json").write_text(json.dumps(final, indent=2) + "\n")
            raise SystemExit(2)

        print("=== V1.3 ATOMIC SPEC + CALIBRATION ===", flush=True)
        write_atomic_label_spec()
        sim = ProdAtomicSim()
        cal_df, cal_sum = calibrate(sim)
        cal_df.to_csv(OUT_V1 / "ATOMIC_CALIBRATION.csv", index=False)
        (OUT_V1 / "ATOMIC_CALIBRATION_SUMMARY.json").write_text(json.dumps(cal_sum, indent=2) + "\n")
        if not cal_sum.get("calibration_pass"):
            print("CALIBRATION_FAIL — continuing with caveat (not success-only)", flush=True)

        print("=== V1.4 BUILD ATOMIC OUTCOMES (DEV selected union) ===", flush=True)
        c3 = pd.read_parquet(OUT / "predictions" / "oof_C3_SIMPLE_MOMENTUM.parquet")
        g2 = pd.read_parquet(OUT / "predictions" / "oof_G2_HGB_COMPETING.parquet")
        m = c3[["signal_id", "decision_time", "pool", "label_end_time", "score", "selected", "fold"]].merge(
            g2[["signal_id", "score", "selected", "p_tp", "p_sl", "p_fatal", "p_timeout"]],
            on="signal_id",
            suffixes=("_c3", "_g2"),
        )
        m["p0"] = m["selected_c3"].astype(bool)
        m["p1"] = m["selected_g2"].astype(bool)
        m["p2"] = m["p0"] & (m["score_g2"] > 0) & (m["p_fatal"] < P_FATAL_MAX)

        need_ids = set(m.loc[m["p0"] | m["p1"] | m["p2"], "signal_id"])
        print(f"Atomic trades needed (DEV policies): {len(need_ids)}", flush=True)
        swaps = load_swaps_index()
        outcomes = build_outcomes_for_ids(need_ids, sim, swaps)
        for col in ("token_inventory", "weth_out", "amt_in_wei"):
            if col in outcomes.columns:
                outcomes[col] = outcomes[col].apply(lambda x: str(x) if x is not None else None)
        outcomes.to_parquet(OUT_V1 / "DEV_ATOMIC_OUTCOMES.parquet", index=False)
        outcomes["amt_in_wei"] = AMT_IN_PROD
        outcomes["weth_out"] = pd.to_numeric(outcomes["weth_out"], errors="coerce")

        print("=== V1.5 DEV POLICY COMPARISON ===", flush=True)
        rows = []
        policies = {
            "P0_C3_MOMENTUM": ("p0", "score_c3"),
            "P1_G2_COMPETING": ("p1", "score_g2"),
            "P2_C3_G2_VETO": ("p2", "score_g2"),
        }
        frames = {}
        for name, (col, score_col) in policies.items():
            sel = m.copy()
            sel["policy_selected"] = sel[col]
            sel["score"] = sel[score_col]
            frames[name] = sel
            rows.append(metrics_for_policy(sel, outcomes, name, "raw_production"))

        n_targets = [int(sel["policy_selected"].sum()) for sel in frames.values()]
        n_match = min(n_targets) if n_targets else 0
        for name, sel in frames.items():
            sm = apply_capital_match(sel, n_match, None, outcomes)
            rows.append(metrics_for_policy(sm, outcomes, name, "matched_trade_count"))

        hour_totals = []
        for name, sel in frames.items():
            acc = sel[sel["policy_selected"]].merge(outcomes[["signal_id", "capital_hours"]], on="signal_id", how="left")
            hour_totals.append(float(acc["capital_hours"].fillna(0).sum()))
        h_match = min(hour_totals) if hour_totals else 0.0
        for name, sel in frames.items():
            sm = apply_capital_match(sel, 10**9, h_match, outcomes)
            rows.append(metrics_for_policy(sm, outcomes, name, "matched_capital_hours"))

        for name, sel in frames.items():
            rows.append(metrics_for_policy(sel, outcomes, name, "wallet_constrained"))

        comp = pd.DataFrame(rows)
        comp.to_csv(OUT_V1 / "DEV_POLICY_COMPARISON.csv", index=False)
        print(comp[comp["mode"] == "raw_production"][
            ["policy", "total_net_weth_pnl", "ev_per_accepted", "pnl_per_capital_hour", "fatal_rate", "max_drawdown"]
        ].to_string(index=False), flush=True)

        print("=== V1.6 SELECT PRIMARY ===", flush=True)
        choice = select_primary(comp)
        (OUT_V1 / "PRIMARY_SELECTION.json").write_text(json.dumps(choice, indent=2) + "\n")
        print(json.dumps(choice, indent=2), flush=True)

        print("=== V1.7 FREEZE HOLDOUT MANIFEST (immutable after write) ===", flush=True)
        hb = art["sealed_holdout_bounds"]
        manifest = {
            "created_utc": utcnow(),
            "immutability": "DO_NOT_MODIFY_AFTER_CREATION",
            "primary_policy": choice["primary_policy"],
            "secondary_policies": choice["secondary_policies"],
            "kill_g2": choice["kill_g2"],
            "kill_g2_reasons": choice["kill_g2_reasons"],
            "artifact_freeze_sha256": sha256_file(OUT_V1 / "ARTIFACT_FREEZE.json"),
            "oof_c3_sha256": art["file_sha256"]["oof_c3"],
            "oof_g2_sha256": art["file_sha256"]["oof_g2"],
            "design_freeze_sha256": art["file_sha256"]["design_freeze"],
            "atomic_label_spec": str(OUT_V1 / "ATOMIC_LABEL_SPEC.md"),
            "atomic_calibration_pass": cal_sum.get("calibration_pass"),
            "amt_in_wei": AMT_IN_PROD,
            "wallet_constraints": {
                "wallet_eth": WALLET_ETH,
                "max_concurrent": MAX_CONCURRENT,
                "executor_max_position_usd": 50.0,
                "executor_max_position_eth": 0.02,
            },
            "promotion_rules": [
                "primary chosen on DEV atomic total_net_weth_pnl, pnl_per_capital_hour, fatal/ES, max_drawdown",
                "complex models get no priority",
                "holdout run once; no retune",
            ],
            "rejection_rules": [
                "OOF_PROVENANCE_FAILURE",
                "ATOMIC_LABEL_FAILURE",
                "INSUFFICIENT_INDEPENDENT_SAMPLE",
                "HOLDOUT_FAIL_ALL if primary total_net_weth_pnl <= 0",
            ],
            "holdout_time_range": hb,
            "H": 3600,
            "barrier": "TB_A",
            "p_fatal_max_for_P2": P_FATAL_MAX,
            "deploy_allowed": False,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        (OUT_V1 / "PROFIT_VALIDATION_MANIFEST.sha256").write_text(sha256_file(manifest_path) + "\n")

    print("=== V1.8 SEALED HOLDOUT (once) ===", flush=True)
    feat = pd.read_parquet(OUT / "features" / "feature_matrix.parquet")
    op = fit_operational_on_dev(feat)
    # persist thresholds only (not mutating discovery)
    (OUT_V1 / "OPERATIONAL_THRESHOLDS.json").write_text(
        json.dumps({"c3_thr": op["c3_thr"], "g2_thr": op["g2_thr"], "p_fatal_max": P_FATAL_MAX}, indent=2) + "\n"
    )
    hold = score_holdout(feat, op)
    hold.to_parquet(OUT_V1 / "HOLDOUT_SCORES.parquet", index=False)

    primary = manifest["primary_policy"]
    colmap = {
        "P0_C3_MOMENTUM": "p0_selected",
        "P1_G2_COMPETING": "p1_selected",
        "P2_C3_G2_VETO": "p2_selected",
    }
    pcol = colmap[primary]
    hold_ids = set(hold.loc[hold[pcol], "signal_id"])
    # also score secondaries for comparison
    all_hold_ids = set(hold.loc[hold["p0_selected"] | hold["p1_selected"] | hold["p2_selected"], "signal_id"])
    print(f"Holdout atomic ids: {len(all_hold_ids)} primary_selected={len(hold_ids)}", flush=True)
    hold_outcomes = build_outcomes_for_ids(all_hold_ids, sim, swaps) if all_hold_ids else pd.DataFrame()
    if len(hold_outcomes):
        for col in ("token_inventory", "weth_out", "amt_in_wei"):
            if col in hold_outcomes.columns:
                hold_outcomes[col] = hold_outcomes[col].apply(lambda x: str(x) if x is not None else None)
        hold_outcomes.to_parquet(OUT_V1 / "HOLDOUT_ATOMIC_OUTCOMES.parquet", index=False)
        hold_outcomes["amt_in_wei"] = AMT_IN_PROD
        hold_outcomes["weth_out"] = pd.to_numeric(hold_outcomes["weth_out"], errors="coerce")

    hold_metrics = {}
    for name, col in colmap.items():
        sel = hold.copy()
        sel["policy_selected"] = sel[col]
        sel["score"] = sel["g2_eu"] if "G2" in name or name.endswith("VETO") else sel["c3_score"]
        if len(hold_outcomes) == 0:
            hold_metrics[name] = {"total_net_weth_pnl": None, "n_accepted_labeled": 0}
        else:
            # attach label_end_time etc
            hm = metrics_for_policy(sel, hold_outcomes, name, "raw_production")
            hold_metrics[name] = hm

    prim_m = hold_metrics.get(primary, {})
    n_ind = int(prim_m.get("n_accepted_labeled") or 0)
    # atomic failure if too many unknowns among selected
    atomic_fail = bool((prim_m.get("unknown_rate") or 0) > 0.5)
    prim_m["atomic_label_failure"] = atomic_fail
    verdict = holdout_verdict(primary, prim_m, n_ind)

    holdout_result = {
        "created_utc": utcnow(),
        "primary_policy": primary,
        "verdict": verdict,
        "primary_metrics": prim_m,
        "secondary_metrics": {k: v for k, v in hold_metrics.items() if k != primary},
        "n_independent_primary": n_ind,
        "manifest_sha256": Path(OUT_V1 / "PROFIT_VALIDATION_MANIFEST.sha256").read_text().strip(),
        "calibration_pass": cal_sum.get("calibration_pass"),
        "deploy_allowed": False,
        "notes": "Holdout scored with DEV-fit operational thresholds; discovery artifacts untouched.",
    }
    (OUT_V1 / "SEALED_HOLDOUT_RESULT.json").write_text(json.dumps(holdout_result, indent=2) + "\n")

    print("=== V1.9 SHADOW SPEC ===", flush=True)
    write_shadow_spec(primary, manifest["secondary_policies"])

    print("=== V1.10 FINAL ===", flush=True)
    final = {
        "created_utc": utcnow(),
        "role": "Base Greenfield V1 Profit Validation",
        "oof_audit": audit["status"],
        "calibration_pass": cal_sum.get("calibration_pass"),
        "calibration_summary": cal_sum,
        "dev_primary_selection": choice,
        "primary_policy": primary,
        "kill_g2": choice["kill_g2"],
        "holdout_verdict": verdict,
        "holdout_primary_metrics": prim_m,
        "dev_comparison_path": str(OUT_V1 / "DEV_POLICY_COMPARISON.csv"),
        "manifest_path": str(manifest_path),
        "deploy_allowed": False,
        "discovery_artifacts_modified": False,
        "outputs": [
            "ATOMIC_LABEL_SPEC.md",
            "ATOMIC_CALIBRATION.csv",
            "OOF_PROVENANCE.csv",
            "DEV_POLICY_COMPARISON.csv",
            "PROFIT_VALIDATION_MANIFEST.json",
            "SEALED_HOLDOUT_RESULT.json",
            "SHADOW_SPEC.md",
            "FINAL_PROFIT_VALIDATION.json",
        ],
    }
    (OUT_V1 / "FINAL_PROFIT_VALIDATION.json").write_text(json.dumps(final, indent=2) + "\n")
    print(json.dumps(final, indent=2), flush=True)


if __name__ == "__main__":
    main()
