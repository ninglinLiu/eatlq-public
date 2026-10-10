#!/usr/bin/env python3
"""Unified Keystone risk rebacktest.

Outputs:
  - old vs external-risk overlay
  - dynamic $100/$300/$1000 sizing with size-specific real-gas replay
  - MEV/latency haircuts
  - capital curves and fatal-rate stress

The external API overlay is current-state scanner data. It is useful for live
policy and conservative overlays, but it is not a perfect historical-causal gate.
"""
from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import keystone_live as K  # noqa: E402

FRIC_THR = -0.05
SIZES = [1000, 300, 100]
GAS_USD_RT = 0.15
OUT_JSON = os.path.join(HERE, "_risk_rebacktest_report.json")
OUT_MD = os.path.join(HERE, "risk_rebacktest_report.md")


@dataclass
class Trade:
    pool: str
    token: str
    t0: pd.Timestamp
    size: float
    ret: float
    raw_ret: float
    fric: float
    risk_mode: str
    risk_reason: str


def fit_model(df: pd.DataFrame):
    X, y = K.build(df)
    cut = df.t0.quantile(0.6)
    tr = (df.t0 <= cut).values
    te = ~tr
    clf = HistGradientBoostingClassifier(
        max_iter=300,
        max_depth=4,
        learning_rate=0.05,
        l2_regularization=1.0,
        min_samples_leaf=20,
        random_state=0,
    )
    clf.fit(X[tr], y[tr])
    return clf, X, tr, te


def load_exposure():
    p = os.path.join(HERE, "_active_exposure.json")
    if not os.path.exists(p):
        return None
    return json.load(open(p))


def exposure_stress(trades, exposure, start, cap_usd, frac, margin, share_thr=0.05, runs=2000):
    """Monte Carlo using a DATA-GROUNDED rug base rate plus a confound margin.

    base rug prob  = observed rug rate among live pools (empirical, not guessed).
    extra prob     = `margin` applied ONLY to trades whose $-size exceeds
                     `share_thr` of pool WETH depth -> these are the trades where
                     we become a big fish and the observational data cannot prove
                     safety (selection confound). margin sweeps our ignorance.
    """
    if not trades or exposure is None:
        return {"runs": 0}
    per_pool = exposure.get("per_pool", {})
    n_live = max(1, exposure.get("n_pools_live", 1))
    # observed rug among live pools as the empirical base
    base_rug = 4 / n_live if exposure.get("n_pools_live") else 0.007
    rng = np.random.default_rng(7)
    observed = np.array([t.ret for t in trades], dtype=float)
    nonfatal = observed[observed > -0.5]
    if len(nonfatal) == 0:
        nonfatal = np.array([0.0])
    # per-trade big-fish flag from $-size share of depth
    big = []
    sizes = []
    for t in trades:
        pp = per_pool.get(t.pool, {})
        s1000 = pp.get("share_1000usd")
        share = (t.size / 1000.0) * s1000 if s1000 else 0.0
        big.append(share > share_thr)
        sizes.append(min(t.size, cap_usd))
    big = np.array(big)
    sizes = np.array(sizes, dtype=float)
    finals, mdds, bankrupt = [], [], 0
    n = len(trades)
    for _ in range(runs):
        eq, peak, dd = start, start, 0.0
        for i in range(n):
            p_rug = base_rug + (margin if big[i] else 0.0)
            r = -1.0 if rng.random() < p_rug else float(rng.choice(nonfatal))
            stake = min(sizes[i], eq * frac, cap_usd)
            eq += stake * r
            peak = max(peak, eq)
            dd = min(dd, eq / peak - 1.0)
            if eq <= 0:
                bankrupt += 1; eq = 0; break
        finals.append(eq); mdds.append(dd)
    finals = np.array(finals)
    return {
        "runs": runs, "margin": margin, "share_thr": share_thr,
        "base_rug_rate": base_rug,
        "frac_big_fish": float(big.mean()),
        "median_end": float(np.median(finals)),
        "p10_end": float(np.quantile(finals, 0.10)),
        "p01_end": float(np.quantile(finals, 0.01)),
        "bankrupt_rate": bankrupt / runs,
        "median_mdd": float(np.median(mdds)),
    }


def load_inputs():
    df, _ = K.load()
    clf, X, tr, te = fit_model(df)
    df = df.copy()
    df["p"] = clf.predict_proba(X)[:, 1]
    priced = json.load(open(K.PCACHE))
    realgas = json.load(open(os.path.join(HERE, "_realgas_replay.json")))
    dynamic = json.load(open(os.path.join(HERE, "_dynamic_size_realgas.json")))
    risk = json.load(open(os.path.join(HERE, "_external_risk_current.json"))) if os.path.exists(os.path.join(HERE, "_external_risk_current.json")) else {}
    df["fric100"] = df.pool.map(lambda x: priced.get(x, {}).get("fric"))
    df["why"] = df.pool.map(lambda x: priced.get(x, {}).get("why"))
    df["net100"] = df.pool.map(lambda x: priced.get(x, {}).get("net"))
    df["real_net100"] = df.pool.map(lambda x: realgas.get(x, {}).get("real_net", priced.get(x, {}).get("net")))
    gtr = df[tr & (df.why == "ok") & df.fric100.notna() & (df.fric100 >= FRIC_THR)]
    p_cut = gtr.p.quantile(0.6)
    test = df[te & (df.why == "ok") & (df.p <= p_cut)].copy().sort_values("t0")
    return df, test, dynamic, risk, p_cut


def risk_ok(token: str, risk: Dict, mode: str) -> tuple[bool, str]:
    if mode == "none":
        return True, "risk_disabled"
    rec = risk.get(str(token).lower())
    if not rec:
        return False, "risk_missing"
    node = rec.get(mode) or {}
    return bool(node.get("ok", False)), str(node.get("reason") or "unknown")


def choose_trade(row, dynamic: Dict, risk: Dict, mode: str, cap_usd: float, equity: float, frac: float, haircut: float) -> Optional[Trade]:
    ok, reason = risk_ok(row.alt, risk, mode)
    if not ok:
        return None
    max_stake = min(cap_usd, equity * frac)
    for size in SIZES:
        if size > max_stake:
            continue
        rec = dynamic.get(f"{row.pool}:{size}")
        if not rec:
            continue
        fric = rec.get("fric")
        raw = rec.get("real_net")
        if fric is None or raw is None:
            continue
        if fric < FRIC_THR:
            continue
        # Quoter includes LP fee + price impact. Add explicit gas and MEV/latency haircut.
        ret = float(raw) - (GAS_USD_RT / float(size)) - haircut
        return Trade(
            pool=row.pool,
            token=str(row.alt).lower(),
            t0=row.t0,
            size=float(size),
            ret=ret,
            raw_ret=float(raw),
            fric=float(fric),
            risk_mode=mode,
            risk_reason=reason,
        )
    return None


def build_trades(test: pd.DataFrame, dynamic: Dict, risk: Dict, mode: str, cap_usd: float, start_equity: float, frac: float, haircut: float) -> List[Trade]:
    # First pass uses start equity so the trade set is comparable. The portfolio
    # simulator still enforces live equity at execution time.
    out: List[Trade] = []
    for row in test.itertuples(index=False):
        t = choose_trade(row, dynamic, risk, mode, cap_usd, start_equity, frac, haircut)
        if t is not None:
            out.append(t)
    return out


def simulate(trades: List[Trade], start: float, cap_usd: float, frac: float, max_concurrent: int = 1) -> Dict:
    # Holds are short and current research serializes exits; keep max_concurrent in
    # the output contract but use time-ordered sequential equity for now.
    equity = float(start)
    peak = equity
    mdd = 0.0
    taken = []
    skipped = 0
    for t in sorted(trades, key=lambda x: x.t0):
        stake = min(t.size, cap_usd, equity * frac)
        if stake <= 0:
            skipped += 1
            continue
        pnl = stake * t.ret
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            mdd = min(mdd, equity / peak - 1.0)
        taken.append({**t.__dict__, "stake": stake, "pnl": pnl, "equity": equity})
        if equity <= 0:
            break
    span_days = max((max([t.t0 for t in trades]) - min([t.t0 for t in trades])).days, 1) if trades else 1
    monthly = (equity / start) ** (30.0 / span_days) - 1.0 if equity > 0 else -1.0
    rets = np.array([t["ret"] for t in taken], dtype=float) if taken else np.array([])
    return {
        "start": start,
        "end": equity,
        "total_return": equity / start - 1.0,
        "monthly_return": monthly,
        "max_drawdown": mdd,
        "n": len(taken),
        "skipped": skipped,
        "avg_size": float(np.mean([t["stake"] for t in taken])) if taken else 0.0,
        "mean_ret": float(rets.mean()) if len(rets) else None,
        "median_ret": float(np.median(rets)) if len(rets) else None,
        "win_rate": float((rets > 0).mean()) if len(rets) else None,
        "fatal_rate": float((rets <= -0.5).mean()) if len(rets) else None,
        "worst_trade": float(rets.min()) if len(rets) else None,
        "best_trade": float(rets.max()) if len(rets) else None,
        "trades": taken,
        "max_concurrent": max_concurrent,
    }


def monte_carlo_stress(trades: List[Trade], start: float, cap_usd: float, frac: float, fatal_multiplier: float, runs: int = 2000) -> Dict:
    if not trades:
        return {"runs": 0}
    rng = np.random.default_rng(42)
    observed = np.array([t.ret for t in trades], dtype=float)
    fatal = observed <= -0.5
    p_obs = float(fatal.mean())
    p = min(0.97, p_obs * fatal_multiplier)
    nonfatal = observed[~fatal]
    if len(nonfatal) == 0:
        nonfatal = np.array([0.0])
    finals = []
    mdds = []
    bankrupt = 0
    n = len(trades)
    sizes = np.array([min(t.size, cap_usd) for t in trades], dtype=float)
    for _ in range(runs):
        eq = start
        peak = start
        dd = 0.0
        for i in range(n):
            r = -1.0 if rng.random() < p else float(rng.choice(nonfatal))
            stake = min(sizes[i], eq * frac, cap_usd)
            eq += stake * r
            peak = max(peak, eq)
            dd = min(dd, eq / peak - 1.0)
            if eq <= 0:
                bankrupt += 1
                eq = 0
                break
        finals.append(eq)
        mdds.append(dd)
    finals = np.array(finals)
    mdds = np.array(mdds)
    return {
        "runs": runs,
        "fatal_multiplier": fatal_multiplier,
        "observed_fatal_rate": p_obs,
        "assumed_fatal_rate": p,
        "median_end": float(np.median(finals)),
        "p10_end": float(np.quantile(finals, 0.10)),
        "p01_end": float(np.quantile(finals, 0.01)),
        "bankrupt_rate": bankrupt / runs,
        "median_mdd": float(np.median(mdds)),
        "p10_mdd": float(np.quantile(mdds, 0.10)),
    }


def summarize_scenario(name: str, trades: List[Trade], configs: Iterable[tuple[float, float, float]]) -> Dict:
    caps = []
    for start, cap, frac in configs:
        sim = simulate(trades, start, cap, frac)
        row = {k: v for k, v in sim.items() if k != "trades"}
        row.update({"capital": start, "position_cap": cap, "position_frac": frac})
        caps.append(row)
    rets = np.array([t.ret for t in trades], dtype=float) if trades else np.array([])
    return {
        "name": name,
        "trade_count": len(trades),
        "sizes": {str(s): sum(1 for t in trades if int(t.size) == s) for s in SIZES},
        "mean_ret": float(rets.mean()) if len(rets) else None,
        "median_ret": float(np.median(rets)) if len(rets) else None,
        "fatal_rate": float((rets <= -0.5).mean()) if len(rets) else None,
        "capital": caps,
    }


def write_markdown(report: Dict) -> None:
    lines = [
        "# Keystone Risk Rebacktest",
        "",
        "Methodology caveat: GoPlus/Honeypot.is overlay is current-state scanner data, not historical block-state.",
        "",
        "## Scenario Summary",
        "",
        "| scenario | trades | sizes | mean | median | fatal | $1000 cap monthly | $1000 cap MDD |",
        "|---|---:|---|---:|---:|---:|---:|---:|",
    ]
    for s in report["scenarios"]:
        cap_row = next((x for x in s["capital"] if x["capital"] == 1000 and x["position_cap"] == 1000 and abs(x["position_frac"] - 0.10) < 1e-9), None)
        lines.append(
            f"| {s['name']} | {s['trade_count']} | {s['sizes']} | "
            f"{(s['mean_ret'] or 0):+.1%} | {(s['median_ret'] or 0):+.1%} | "
            f"{(s['fatal_rate'] or 0):.1%} | "
            f"{(cap_row['monthly_return'] if cap_row else 0):+.1%} | "
            f"{(cap_row['max_drawdown'] if cap_row else 0):.1%} |"
        )
    lines += [
        "",
        "## Fatal Multiplier Stress",
        "",
        "| multiplier | assumed fatal | median end | p10 end | bankruptcy | median MDD |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["fatal_stress"]:
        lines.append(
            f"| {row['fatal_multiplier']:.0f}x | {row['assumed_fatal_rate']:.1%} | "
            f"${row['median_end']:,.0f} | ${row['p10_end']:,.0f} | "
            f"{row['bankrupt_rate']:.1%} | {row['median_mdd']:.1%} |"
        )
    ae = report.get("active_exposure") or {}
    summ = ae.get("summary") or {}
    if summ:
        lines += [
            "",
            "## Active Exposure (Butterfly) Model",
            "",
            f"- Pools: {summ.get('n_pools_all')} total, {summ.get('n_pools_live')} with >=3 real buys.",
            f"- True rug/unsellable (real-gas revert): {summ.get('n_rug')} total; among live pools rug is rare.",
            f"- Fraction of pools where $1000 exceeds 5% of WETH depth: {summ.get('frac_1000_over_5pct_depth')}.",
            f"- Observed exposure penalty by buyer share: rug {summ.get('size_penalty_rug')}, death {summ.get('size_penalty_death')} (confounded lower bound).",
            f"- {summ.get('confound_note')}",
            "",
            "### Exposure-adjusted stress (data base rate + confound margin)",
            "",
            "| confound margin | base rug | big-fish frac | median end | p10 end | bankruptcy | median MDD |",
            "|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for s in ae.get("stress", []):
            if not s or s.get("runs", 0) == 0:
                continue
            lines.append(
                f"| {s['margin']:.0%} | {s['base_rug_rate']:.1%} | {s['frac_big_fish']:.0%} | "
                f"${s['median_end']:,.0f} | ${s['p10_end']:,.0f} | {s['bankrupt_rate']:.1%} | {s['median_mdd']:.1%} |"
            )
    lines += [
        "",
        "## Known Risks",
        "",
        "- Modeled: size-dependent slippage, QuoterV2 friction, real-gas sell reverts, API scanner overlay, gas cost, MEV/latency haircuts.",
        "- Partially modeled: larger size attracting adversarial behavior via fatal multipliers; current-state scanner data as a conservative proxy.",
        "- Not fully modeled: future owner toggle, proxy upgrade after our buy, address-specific blacklist after first few buyers, private mempool failure, and adaptive attackers reacting to our wallet.",
    ]
    open(OUT_MD, "w").write("\n".join(lines) + "\n")


def main() -> None:
    _, test, dynamic, risk, p_cut = load_inputs()
    configs = [(c, cap, frac) for c in (100, 300, 1000, 3000, 10000) for cap in (100, 300, 1000) for frac in (0.05, 0.10, 0.20)]
    scenarios = []
    for mode in ("none", "scanner_only", "strict"):
        for haircut in (0.0, 0.02, 0.05, 0.10):
            trades = build_trades(test, dynamic, risk, mode, cap_usd=1000, start_equity=10000, frac=0.20, haircut=haircut)
            scenarios.append(summarize_scenario(f"{mode}_haircut_{haircut:.0%}", trades, configs))
    base_trades = build_trades(test, dynamic, risk, "none", cap_usd=1000, start_equity=10000, frac=0.20, haircut=0.02)
    stress = [monte_carlo_stress(base_trades, start=1000, cap_usd=1000, frac=0.10, fatal_multiplier=m) for m in (1, 2, 5, 10, 25)]
    exposure = load_exposure()
    exposure_scen = [exposure_stress(base_trades, exposure, start=1000, cap_usd=1000, frac=0.10, margin=m)
                     for m in (0.0, 0.02, 0.05, 0.10)]
    report = {
        "p_cut": float(p_cut),
        "test_candidates_pass_model": int(len(test)),
        "source": {
            "dynamic_size_realgas": os.path.join(HERE, "_dynamic_size_realgas.json"),
            "external_risk_current": os.path.join(HERE, "_external_risk_current.json"),
            "note": "external risk is current-state overlay, not historical-causal",
        },
        "scenarios": scenarios,
        "fatal_stress": stress,
        "active_exposure": {
            "summary": {k: exposure.get(k) for k in
                        ("n_pools_all", "n_pools_live", "n_rug", "n_death",
                         "size_penalty_rug", "size_penalty_death", "frac_1000_over_5pct_depth",
                         "confound_note")} if exposure else None,
            "stress": exposure_scen,
        },
    }
    json.dump(report, open(OUT_JSON, "w"), indent=2, sort_keys=True)
    write_markdown(report)
    print(f"wrote {OUT_JSON}")
    print(f"wrote {OUT_MD}")
    for s in scenarios[:8]:
        cap_row = next((x for x in s["capital"] if x["capital"] == 1000 and x["position_cap"] == 1000 and abs(x["position_frac"] - 0.10) < 1e-9), None)
        print(
            f"{s['name']:24s} n={s['trade_count']:3d} sizes={s['sizes']} "
            f"mean={(s['mean_ret'] or 0):+.2f} fatal={(s['fatal_rate'] or 0):.1%} "
            f"mret={(cap_row['monthly_return'] if cap_row else 0):+.1%} "
            f"mdd={(cap_row['max_drawdown'] if cap_row else 0):.1%}"
        )


if __name__ == "__main__":
    main()
