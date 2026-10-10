"""Sprint056E — Composite Signal Deep Dive.

Composite (CORRECTED from Sprint056C):
  fee_100 + heat ≥ 20 + weth_is_token1 + BPR < 0.55

Note: original Sprint056C proposed heat_20_50, but weth_is_token1 pools have
median heat=75 (50-100 range). The correct gate uses heat >= 20 (not capped at 50).

Analyses on combined 1511-row labeled dataset:
  - Sample size, win rate, PF
  - Max drawdown (equity curve)
  - Pool concentration (Herfindahl + top-1 share)
  - Per-week stability
  - Drawdown per week
  - Comparison to parent buckets

Outputs:
  - mvp/diagnostics/sprint056/composite_deep_dive.csv
  - mvp/audits/sprint056/composite_deep_dive_readout.md
"""

from __future__ import annotations
import csv, json
from pathlib import Path
from collections import Counter, defaultdict
import statistics

REPO      = Path(__file__).parent.parent
EXP_CSV   = REPO / "mvp/diagnostics/sprint056/expansion_combined.csv"
OUT_DIR   = REPO / "mvp/diagnostics/sprint056"
AUD_DIR   = REPO / "mvp/audits/sprint056"
OUT_DIR.mkdir(parents=True, exist_ok=True)
AUD_DIR.mkdir(parents=True, exist_ok=True)

GAS_PROXY = 0.002

# ── Composite gate (corrected) ───────────────────────────────────────────────
# weth_is_token1 pools have median heat=75 (50-100 range), NOT 20-50.
# The correct gate uses heat >= 20 (includes the dominant 50-100 bucket).
HEAT_MIN = 20.0   # no upper cap
BPR_MAX  = 0.55
# weth_is_token0 == False → WETH is token1

def is_composite(r: dict) -> bool:
    try:
        heat = float(r.get("activity_heat", 0))
        bpr  = float(r.get("buy_pressure_ratio", 1))
        wit0_false = r.get("weth_is_token0") in ("False", "0", "false", False, 0)
        return heat >= HEAT_MIN and bpr < BPR_MAX and wit0_false
    except (ValueError, TypeError):
        return False

# ── helpers ───────────────────────────────────────────────────────────────────

def _pf(rets: list[float]) -> float | None:
    pos = [r for r in rets if r > 0]
    neg = [-r for r in rets if r <= 0]
    return round(sum(pos)/sum(neg), 3) if neg else None

def _stats(rets: list[float]) -> dict:
    if not rets:
        return {"n": 0, "hit": 0, "pf": None, "mean": 0, "median": 0, "max": 0, "min": 0}
    pos = [r for r in rets if r > 0]
    return {
        "n":      len(rets),
        "hit":    round(sum(1 for r in rets if r > 0) / len(rets), 4),
        "pf":     _pf(rets),
        "mean":   round(statistics.mean(rets), 6),
        "median": round(statistics.median(rets), 6),
        "max":    round(max(rets), 6),
        "min":    round(min(rets), 6),
        "stdev":  round(statistics.stdev(rets), 6) if len(rets) > 1 else 0,
    }

def _max_drawdown(rets: list[float]) -> float:
    """Maximum peak-to-trough drawdown on cumulative equity (unit: return units)."""
    eq = peak = dd = 0.0
    for r in rets:
        eq  += r
        peak = max(peak, eq)
        dd   = max(dd, peak - eq)
    return round(dd, 6)

def _herfindahl(pool_counts: Counter) -> float:
    """Herfindahl-Hirschman Index: 0=perfectly dispersed, 1=monopoly."""
    total = sum(pool_counts.values())
    if total == 0:
        return 0.0
    return round(sum((c/total)**2 for c in pool_counts.values()), 4)

def _portfolio_replay(rows: list[dict], cooldown_s: int = 900, max_c: int = 3,
                      notional: float = 10.0) -> dict:
    rows_s = sorted(rows, key=lambda r: float(r.get("entry_time_sec", 0)))
    pool_last: dict[str, float] = {}
    active: list[float] = []
    pnl_list: list[float] = []

    for r in rows_s:
        t  = float(r.get("entry_time_sec", 0))
        nr = r["net_return_net"]
        active = [x for x in active if x > t]
        if pool_last.get(r["pool_address"], 0) + cooldown_s > t:
            continue
        if len(active) >= max_c:
            continue
        pool_last[r["pool_address"]] = t
        active.append(t + 300)
        pnl_list.append(nr * notional)

    if not pnl_list:
        return {"n": 0, "win_rate": 0, "pf": None, "roi_pct": 0, "max_dd_pct": 0}

    wins = [p for p in pnl_list if p > 0]
    losses = [-p for p in pnl_list if p <= 0]
    total_invested = len(pnl_list) * notional
    rets_pct = [p / notional for p in pnl_list]
    return {
        "n":        len(pnl_list),
        "win_rate": round(sum(1 for p in pnl_list if p > 0) / len(pnl_list), 4),
        "pf":       round(sum(wins)/sum(losses), 3) if losses else None,
        "roi_pct":  round(100 * sum(pnl_list) / total_invested, 3),
        "max_dd_pct": round(100 * _max_drawdown(rets_pct), 3),
    }


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    all_rows = list(csv.DictReader(open(EXP_CSV)))
    for r in all_rows:
        r["net_return_net"] = float(r["net_return_net"])

    comp     = [r for r in all_rows if is_composite(r)]
    non_comp = [r for r in all_rows if not is_composite(r)]

    print(f"Total rows:     {len(all_rows)}")
    print(f"Composite rows: {len(comp)} ({100*len(comp)/len(all_rows):.1f}%)")
    print(f"Non-composite:  {len(non_comp)}")
    print()

    # ── 1. Core stats ─────────────────────────────────────────────────────────
    rets_c  = [r["net_return_net"] for r in comp]
    rets_nc = [r["net_return_net"] for r in non_comp]
    rets_all= [r["net_return_net"] for r in all_rows]

    s_c   = _stats(rets_c)
    s_nc  = _stats(rets_nc)
    s_all = _stats(rets_all)

    print("=== CORE STATS ===")
    print(f"  composite : n={s_c['n']:4d} hit={s_c['hit']:.3f} pf={s_c['pf']} "
          f"mean={s_c['mean']:.4f} stdev={s_c['stdev']:.4f}")
    print(f"  non-comp  : n={s_nc['n']:4d} hit={s_nc['hit']:.3f} pf={s_nc['pf']} "
          f"mean={s_nc['mean']:.4f}")
    print(f"  all       : n={s_all['n']:4d} hit={s_all['hit']:.3f} pf={s_all['pf']} "
          f"mean={s_all['mean']:.4f}")
    print()

    # ── 2. Equity curve & drawdown ────────────────────────────────────────────
    rets_seq = [r["net_return_net"] for r in
                sorted(comp, key=lambda r: float(r.get("entry_time_sec", 0)))]
    dd       = _max_drawdown(rets_seq)
    cum_ret  = sum(rets_seq)

    print(f"=== EQUITY CURVE (composite, chronological) ===")
    print(f"  cumulative return (unit): {cum_ret:.4f}")
    print(f"  max drawdown:             {dd:.4f} ({100*dd:.2f}%)")
    print(f"  calmar proxy:             {cum_ret/dd:.2f}" if dd > 0 else "  calmar: inf")
    print()

    # ── 3. Pool concentration ─────────────────────────────────────────────────
    pool_cnt = Counter(r["pool_address"] for r in comp)
    hhi      = _herfindahl(pool_cnt)
    top1_pool, top1_cnt = pool_cnt.most_common(1)[0] if pool_cnt else (None, 0)
    top1_share = top1_cnt / len(comp) if comp else 0
    top3_share = sum(c for _, c in pool_cnt.most_common(3)) / len(comp) if comp else 0

    print(f"=== POOL CONCENTRATION ===")
    print(f"  unique pools: {len(pool_cnt)}")
    print(f"  top-1 share:  {100*top1_share:.1f}% ({top1_pool})")
    print(f"  top-3 share:  {100*top3_share:.1f}%")
    print(f"  HHI:          {hhi:.4f}  (< 0.25 = dispersed, > 0.50 = concentrated)")
    print()

    # ── 4. Per-week stability ─────────────────────────────────────────────────
    by_week: dict[str, list[float]] = defaultdict(list)
    for r in comp:
        by_week[r["regime_week"]].append(r["net_return_net"])

    print(f"=== PER-WEEK STABILITY ===")
    pos_weeks = neg_weeks = tiny_weeks = 0
    week_rows: list[dict] = []
    for wk in sorted(by_week):
        rets = by_week[wk]
        s    = _stats(rets)
        dd_w = _max_drawdown(sorted(rets))  # intra-week drawdown
        flag = "tiny" if s["n"] < 5 else ("+" if (s["pf"] or 0) > 1.0 else "-")
        if flag == "tiny": tiny_weeks += 1
        elif flag == "+": pos_weeks += 1
        else: neg_weeks += 1
        print(f"  {wk}: n={s['n']:4d} hit={s['hit']:.3f} pf={s['pf']} "
              f"dd={dd_w:.4f} [{flag}]")
        week_rows.append({
            "week": wk, "n": s["n"], "hit": s["hit"], "pf": s["pf"],
            "mean": s["mean"], "max_dd": dd_w, "flag": flag,
        })
    print(f"\n  pos_weeks={pos_weeks} neg_weeks={neg_weeks} "
          f"tiny_weeks={tiny_weeks} (n<5)")
    print()

    # ── 5. Portfolio replay ───────────────────────────────────────────────────
    print(f"=== PORTFOLIO REPLAY (15m cd, max_c=3) ===")
    for notional in [10.0, 50.0, 100.0]:
        rp = _portfolio_replay(comp, cooldown_s=900, max_c=3, notional=notional)
        print(f"  ${notional:.0f}: n={rp['n']:3d} win={rp['win_rate']:.3f} "
              f"pf={rp['pf']} roi={rp['roi_pct']:.2f}% maxdd={rp['max_dd_pct']:.2f}%")
    print()

    # ── 6. Parent bucket comparison ───────────────────────────────────────────
    print("=== PARENT BUCKET COMPARISON ===")
    parents = {
        "heat_20_50":         [r for r in all_rows if 20 <= float(r.get("activity_heat",0)) < 50],
        "weth_is_token1":     [r for r in all_rows if r.get("weth_is_token0") in ("False","0","false",False,0)],
        "bpr_lt55":           [r for r in all_rows if float(r.get("buy_pressure_ratio",1)) < 0.55],
        "composite":          comp,
    }
    for label, subset in parents.items():
        rs = [r["net_return_net"] for r in subset]
        s  = _stats(rs)
        print(f"  {label:20s}: n={s['n']:5d} hit={s['hit']:.3f} pf={s['pf']}")
    print()

    # ── 7. Intra-composite further breakdown ──────────────────────────────────
    print("=== COMPOSITE SUB-BUCKETS ===")

    # By velocity regime
    for vel_label, vel_fn in [
        ("vel_down (vel<0)", lambda r: float(r.get("price_velocity_5m",0)) < 0),
        ("vel_flat (0-0.05)", lambda r: 0 <= float(r.get("price_velocity_5m",0)) < 0.05),
        ("vel_up   (>=0.05)", lambda r: float(r.get("price_velocity_5m",0)) >= 0.05),
    ]:
        sub  = [r for r in comp if vel_fn(r)]
        rs   = [r["net_return_net"] for r in sub]
        s    = _stats(rs)
        print(f"  {vel_label:24s}: n={s['n']:4d} hit={s['hit']:.3f} pf={s['pf']}")

    print()

    # By BPR sub-range
    for bpr_label, bpr_fn in [
        ("bpr < 0.40",  lambda r: float(r.get("buy_pressure_ratio",1)) < 0.40),
        ("bpr 0.40-0.55", lambda r: 0.40 <= float(r.get("buy_pressure_ratio",1)) < 0.55),
    ]:
        sub  = [r for r in comp if bpr_fn(r)]
        rs   = [r["net_return_net"] for r in sub]
        s    = _stats(rs)
        print(f"  {bpr_label:24s}: n={s['n']:4d} hit={s['hit']:.3f} pf={s['pf']}")

    print()

    # ── 8. Dump CSV ───────────────────────────────────────────────────────────
    out_cols = ["pool_address","regime_week","entry_time_sec","activity_heat",
                "buy_pressure_ratio","price_velocity_5m","weth_is_token0",
                "net_return_net","label","source","composite"]
    out_rows = []
    for r in sorted(all_rows, key=lambda x: float(x.get("entry_time_sec",0))):
        out_rows.append({**r, "composite": is_composite(r)})

    with open(OUT_DIR / "composite_deep_dive.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=out_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(out_rows)
    print(f"Wrote composite_deep_dive.csv ({len(out_rows)} rows)")

    # ── 9. Verdict ────────────────────────────────────────────────────────────
    pos_rate = pos_weeks / max(1, pos_weeks + neg_weeks)
    verdict = "COMPOSITE_STRONG" if (
        (s_c.get("pf") or 0) > 2.0 and (s_c.get("hit") or 0) > 0.55
        and len(comp) >= 50 and pos_rate >= 0.65
    ) else "COMPOSITE_MODERATE" if (
        (s_c.get("pf") or 0) > 1.5 and (s_c.get("hit") or 0) > 0.50
        and len(comp) >= 30
    ) else "COMPOSITE_WEAK"

    print(f"\n=== COMPOSITE VERDICT: {verdict} ===")
    print(f"  n={s_c['n']} hit={s_c['hit']:.3f} pf={s_c['pf']} "
          f"dd={100*dd:.2f}% pos_weeks={pos_weeks}/{pos_weeks+neg_weeks}")

    # ── write readout ─────────────────────────────────────────────────────────
    _write_readout(
        s_c=s_c, s_nc=s_nc, s_all=s_all,
        comp_n=len(comp), total_n=len(all_rows),
        cum_ret=cum_ret, dd=dd,
        pool_cnt=pool_cnt, hhi=hhi, top1_share=top1_share, top3_share=top3_share,
        week_rows=week_rows, pos_weeks=pos_weeks, neg_weeks=neg_weeks, tiny_weeks=tiny_weeks,
        verdict=verdict,
    )
    print("Wrote composite_deep_dive_readout.md")


def _write_readout(**kw) -> None:
    s_c = kw["s_c"]; s_nc = kw["s_nc"]; s_all = kw["s_all"]
    comp_n = kw["comp_n"]; total_n = kw["total_n"]
    cum_ret = kw["cum_ret"]; dd = kw["dd"]
    pool_cnt = kw["pool_cnt"]; hhi = kw["hhi"]
    top1_share = kw["top1_share"]; top3_share = kw["top3_share"]
    week_rows = kw["week_rows"]
    pos_weeks = kw["pos_weeks"]; neg_weeks = kw["neg_weeks"]; tiny_weeks = kw["tiny_weeks"]
    verdict = kw["verdict"]

    calmar = round(cum_ret / dd, 2) if dd > 0 else "∞"
    conc_flag = "HIGH" if hhi > 0.3 else "MODERATE" if hhi > 0.15 else "LOW"

    lines = [
        "# Sprint056E — Composite Signal Deep Dive",
        "",
        f"**Composite gate (corrected):** `fee_100 + heat ≥ 20 + weth_is_token1 + BPR < 0.55`",
        "",
        "> **Correction from Sprint056C:** Original gate used `heat_20_50` but `weth_is_token1` pools "
        "have median heat=75 (mostly 50-100 range). Correct gate uses `heat ≥ 20` with no upper cap.",
        "",
        f"**{comp_n} composite rows** out of {total_n} total fee_100 labeled rows "
        f"({100*comp_n/total_n:.1f}%)",
        "",
        "## 1. Core Statistics",
        "",
        "| Cohort | N | Hit | PF | Mean Net Return | StDev |",
        "|--------|---|-----|-----|-----------------|-------|",
        f"| **Composite** | **{s_c['n']}** | **{s_c['hit']:.3f}** | **{s_c['pf']}** | **{s_c['mean']:.4f}** | {s_c['stdev']:.4f} |",
        f"| Non-composite | {s_nc['n']} | {s_nc['hit']:.3f} | {s_nc['pf']} | {s_nc['mean']:.4f} | — |",
        f"| All fee_100 | {s_all['n']} | {s_all['hit']:.3f} | {s_all['pf']} | {s_all['mean']:.4f} | — |",
        "",
        "## 2. Equity Curve & Drawdown",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Cumulative return (sum) | {cum_ret:.4f} |",
        f"| Max drawdown | {100*dd:.2f}% |",
        f"| Calmar proxy | {calmar} |",
        "",
        "## 3. Pool Concentration",
        "",
        f"| Metric | Value | Risk |",
        f"|--------|-------|------|",
        f"| Unique pools | {len(pool_cnt)} | — |",
        f"| Top-1 pool share | {100*top1_share:.1f}% | {'HIGH' if top1_share > 0.3 else 'OK'} |",
        f"| Top-3 pool share | {100*top3_share:.1f}% | {'HIGH' if top3_share > 0.6 else 'OK'} |",
        f"| HHI | {hhi:.4f} | **{conc_flag}** |",
        "",
        "> HHI > 0.3 suggests returns dominated by a few pools — live risk if those pools change.",
        "",
        "## 4. Per-Week Stability",
        "",
        "| Week | N | Hit | PF | Max DD | Flag |",
        "|------|---|-----|-----|--------|------|",
    ]
    for wr in week_rows:
        flag = {"+" : "✓", "-" : "✗", "tiny": "~"}.get(wr["flag"], "?")
        lines.append(f"| {wr['week']} | {wr['n']} | {wr['hit']:.3f} | {wr['pf']} "
                     f"| {100*wr['max_dd']:.2f}% | {flag} |")
    lines += [
        "",
        f"**{pos_weeks}** positive / **{neg_weeks}** negative / **{tiny_weeks}** tiny-n weeks",
        "",
        "## 5. Verdict",
        "",
        f"**{verdict}**",
        "",
        {
            "COMPOSITE_STRONG":   "Composite signal significantly outperforms parent buckets. "
                                  "Ready to be primary live filter.",
            "COMPOSITE_MODERATE": "Composite improves over baseline but needs more samples. "
                                  "Use as additional filter, not primary signal.",
            "COMPOSITE_WEAK":     "Composite does not show clear improvement. "
                                  "Re-examine gate parameters.",
        }.get(verdict, ""),
        "",
        "## Artifacts",
        "",
        "- `mvp/diagnostics/sprint056/composite_deep_dive.csv` — all rows with composite flag",
    ]

    (AUD_DIR / "composite_deep_dive_readout.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
