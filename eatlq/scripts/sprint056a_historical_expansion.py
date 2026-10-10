"""Sprint056A — Historical fee_100 Expansion (1511-row combined labeled dataset).

Combines:
  - 1,398 in-bundle fee_100bps labeled rows from q4_bundle_combined (Sprint039E/G)
  - 113 OOS fee_100bps labeled rows from sprint054 real-RPC labels

Answers: is fee_100 a persistent regime edge or two coincidentally good weeks?
Outputs:
  - mvp/diagnostics/sprint056/expansion_combined.csv
  - mvp/audits/sprint056/expansion_readout.md
  - mvp/diagnostics/sprint056/expansion_matrix.csv
"""

from __future__ import annotations
import csv, json, glob, sys, os
from pathlib import Path
from collections import defaultdict
import statistics

REPO = Path(__file__).parent.parent
Q4_DIR   = REPO / "mvp/audits/sprint039a/q4_bundle_combined"
S054_JSON = REPO / "mvp/diagnostics/sprint054/labeled_candidates.json"
OUT_DIR   = REPO / "mvp/diagnostics/sprint056"
AUD_DIR   = REPO / "mvp/audits/sprint056"
OUT_DIR.mkdir(parents=True, exist_ok=True)
AUD_DIR.mkdir(parents=True, exist_ok=True)

GAS_PROXY  = 0.002          # 20bps gas+slippage proxy
HEAT_20_50 = (20, 50)
HEAT_50P   = 50

# ── helpers ──────────────────────────────────────────────────────────────────

def _pf(gains, losses):
    g, l = sum(gains), sum(losses)
    if l == 0:
        return float("inf") if g > 0 else None
    return round(g / l, 3)

def _stats(rets: list[float]) -> dict:
    if not rets:
        return {"n": 0}
    pos = [r for r in rets if r > 0]
    neg = [-r for r in rets if r <= 0]
    pf  = _pf(pos, neg)
    return {
        "n":        len(rets),
        "hit":      round(sum(1 for r in rets if r > 0) / len(rets), 4),
        "pf":       pf,
        "mean":     round(statistics.mean(rets), 6),
        "median":   round(statistics.median(rets), 6),
        "max":      round(max(rets), 6),
        "min":      round(min(rets), 6),
        "pct_pos":  round(100 * sum(1 for r in rets if r > 0) / len(rets), 1),
    }

# ── portfolio replay ──────────────────────────────────────────────────────────

def portfolio_replay(rows: list[dict], cooldown_s: int = 900, max_c: int = 3,
                     notional: float = 10.0, gas: float = GAS_PROXY) -> dict:
    """Simple event-driven replay. rows must be sorted by entry_time_sec."""
    rows_s = sorted(rows, key=lambda r: r["entry_time_sec"])
    pool_last: dict[str, float] = {}
    active: list[float] = []          # exit_time_sec of open positions
    trades: list[dict]  = []

    for r in rows_s:
        t  = r["entry_time_sec"]
        nr = r["net_return_net"]

        # expire finished positions
        active = [x for x in active if x > t]

        # same-pool cooldown
        if pool_last.get(r["pool_address"], 0) + cooldown_s > t:
            continue
        # concurrent cap
        if len(active) >= max_c:
            continue

        pool_last[r["pool_address"]] = t
        exit_t = t + 300
        active.append(exit_t)
        pnl    = nr * notional
        trades.append({
            "t":       t,
            "nr":      nr,
            "win":     nr > 0,
            "pnl":     round(pnl, 6),
            "week":    r.get("regime_week", "unknown"),
            "source":  r.get("source", "?"),
            "heat":    r.get("activity_heat", 0),
        })

    if not trades:
        return {"n": 0}

    wins   = [t for t in trades if t["win"]]
    losses = [t for t in trades if not t["win"]]
    total_pnl  = sum(t["pnl"] for t in trades)
    total_inv  = len(trades) * notional
    win_pnl    = sum(t["pnl"] for t in wins)
    loss_pnl   = -sum(t["pnl"] for t in losses)

    pf = _pf([abs(t["pnl"]) for t in wins], [abs(t["pnl"]) for t in losses])
    return {
        "n":          len(trades),
        "win_rate":   round(len(wins) / len(trades), 4),
        "pf":         pf,
        "roi_pct":    round(100 * total_pnl / total_inv, 3),
        "total_pnl":  round(total_pnl, 4),
        "per_week":   _weekly_pf(trades),
    }

def _weekly_pf(trades: list[dict]) -> dict[str, dict]:
    by_week: dict[str, list] = defaultdict(list)
    for t in trades:
        by_week[t["week"]].append(t["nr"])
    out = {}
    for wk, rets in sorted(by_week.items()):
        pos = [r for r in rets if r > 0]
        neg = [-r for r in rets if r <= 0]
        out[wk] = {
            "n":   len(rets),
            "hit": round(sum(1 for r in rets if r > 0) / len(rets), 3),
            "pf":  _pf(pos, neg),
        }
    return out

# ── data loading ─────────────────────────────────────────────────────────────

def load_q4_fee100() -> list[dict]:
    rows = []
    for fn in sorted(glob.glob(str(Q4_DIR / "*.csv"))):
        for r in csv.DictReader(open(fn)):
            if (r.get("fee_bps") == "100"
                    and r.get("exit_ok_5m") == "True"
                    and r.get("entry_quote_ok") == "True"):
                nr = float(r["net_return_5m"]) - GAS_PROXY
                rows.append({
                    "pool_address":   r["pool_address"],
                    "regime_week":    r["regime_week"],
                    "entry_time_sec": float(r["entry_time_sec"]),
                    "fee_bps":        100,
                    "activity_heat":  float(r.get("activity_heat") or 0),
                    "price_velocity_5m": float(r.get("price_velocity_5m") or 0),
                    "buy_pressure_ratio": float(r.get("buy_pressure_ratio") or 0),
                    "weth_is_token0": r.get("weth_is_token0") in ("1", "True", True, 1),
                    "avg_liquidity":  float(r.get("avg_liquidity") or 0),
                    "net_return_5m":  float(r["net_return_5m"]),
                    "net_return_net": nr,
                    "label":          int(nr > 0),
                    "source":         "in_bundle",
                    "out_of_range":   r.get("out_of_range_suspected") == "True",
                })
    return rows


def load_oos_fee100() -> list[dict]:
    raw  = json.load(open(S054_JSON))
    rows = []
    for c in raw:
        if not (c.get("fee_bps") == 100
                and c.get("exit_quote_ok")
                and c.get("net_return_5m") is not None):
            continue
        nr = c["net_return_5m"] - GAS_PROXY
        # approximate entry_time from entry_block (block ~2s on Base)
        entry_time = c.get("entry_block", 0) * 2.0
        rows.append({
            "pool_address":      c["pool_address"],
            "regime_week":       c.get("week", "oos"),
            "entry_time_sec":    float(c.get("entry_time_sec", entry_time)),
            "fee_bps":           100,
            "activity_heat":     float(c.get("activity_heat") or 0),
            "price_velocity_5m": float(c.get("price_velocity_5m") or 0),
            "buy_pressure_ratio":float(c.get("buy_pressure_ratio") or 0),
            "weth_is_token0":    bool(c.get("weth_is_token0")),
            "avg_liquidity":     float(c.get("avg_liquidity") or 0),
            "net_return_5m":     c["net_return_5m"],
            "net_return_net":    nr,
            "label":             int(nr > 0),
            "source":            "oos_sprint054",
            "out_of_range":      False,
        })
    return rows

# ── heat bucketing ────────────────────────────────────────────────────────────

def heat_bucket(h: float) -> str:
    if h < 20:  return "lt20"
    if h < 50:  return "20_50"
    if h < 100: return "50_100"
    return "ge100"

# ── main analysis ─────────────────────────────────────────────────────────────

def main() -> None:
    ib   = load_q4_fee100()
    oos  = load_oos_fee100()
    all_rows = ib + oos

    print(f"Combined fee_100 labeled rows: {len(all_rows)}")
    print(f"  in_bundle: {len(ib)}  |  oos_sprint054: {len(oos)}")
    print()

    # ── dump combined CSV ───────────────────────────────────────────────────
    cols = ["pool_address","regime_week","entry_time_sec","fee_bps","source",
            "activity_heat","price_velocity_5m","buy_pressure_ratio",
            "weth_is_token0","net_return_5m","net_return_net","label","out_of_range"]
    with open(OUT_DIR / "expansion_combined.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in sorted(all_rows, key=lambda x: x["entry_time_sec"]):
            w.writerow(r)
    print(f"Wrote expansion_combined.csv ({len(all_rows)} rows)")

    # ── overall stats ───────────────────────────────────────────────────────
    rets_all = [r["net_return_net"] for r in all_rows]
    s_all    = _stats(rets_all)

    rets_ib  = [r["net_return_net"] for r in ib]
    s_ib     = _stats(rets_ib)

    rets_oos = [r["net_return_net"] for r in oos]
    s_oos    = _stats(rets_oos)

    print("=== OVERALL ===")
    for k,v in s_all.items(): print(f"  {k}: {v}")
    print()
    print("=== IN-BUNDLE ===")
    for k,v in s_ib.items(): print(f"  {k}: {v}")
    print()
    print("=== OOS_SPRINT054 ===")
    for k,v in s_oos.items(): print(f"  {k}: {v}")
    print()

    # ── per-week breakdown ──────────────────────────────────────────────────
    by_week: dict[str, list[float]] = defaultdict(list)
    for r in all_rows:
        by_week[r["regime_week"]].append(r["net_return_net"])

    week_matrix = []
    for wk in sorted(by_week):
        wr = by_week[wk]
        s  = _stats(wr)
        src = "in_bundle" if any(r["regime_week"]==wk and r["source"]=="in_bundle" for r in all_rows) else "oos"
        week_matrix.append({"week": wk, "source": src, **s})
        print(f"  {wk} ({src}): n={s['n']:4d} hit={s.get('hit',0):.3f} pf={s.get('pf','?')}")

    print()

    # ── heat bucket breakdown ────────────────────────────────────────────────
    heat_groups: dict[str, list[float]] = defaultdict(list)
    for r in all_rows:
        heat_groups[heat_bucket(r["activity_heat"])].append(r["net_return_net"])

    heat_stats: dict[str, dict] = {}
    print("=== HEAT BUCKETS (all sources) ===")
    for bucket in ["lt20","20_50","50_100","ge100"]:
        rs = heat_groups.get(bucket, [])
        s  = _stats(rs)
        heat_stats[bucket] = s
        print(f"  {bucket:10s}: n={s.get('n',0):5d} hit={s.get('hit',0):.3f} pf={s.get('pf','?')}")

    print()

    # ── heat_20_50 vs heat_20p ────────────────────────────────────────────────
    rows_20_50 = [r for r in all_rows if 20 <= r["activity_heat"] < 50]
    rows_20p   = [r for r in all_rows if r["activity_heat"] >= 20]
    rows_50p   = [r for r in all_rows if r["activity_heat"] >= 50]

    print("=== SIGNAL SUB-BUCKETS ===")
    for label, subset in [
        ("heat_20_50", rows_20_50),
        ("heat_20p",   rows_20p),
        ("heat_50p",   rows_50p),
    ]:
        rs = [r["net_return_net"] for r in subset]
        s  = _stats(rs)
        print(f"  {label:12s}: n={s.get('n',0):5d} hit={s.get('hit',0):.3f} pf={s.get('pf','?')}")

    print()

    # ── temporal stability (half-year split) ─────────────────────────────────
    rows_early = [r for r in all_rows if r["regime_week"] < "2026-01-01"]
    rows_late  = [r for r in all_rows if r["regime_week"] >= "2026-01-01"]
    print("=== TEMPORAL STABILITY ===")
    for label, subset in [("2025-H2 (early)", rows_early), ("2026-H1 (late)", rows_late)]:
        rs = [r["net_return_net"] for r in subset]
        s  = _stats(rs)
        print(f"  {label:20s}: n={s.get('n',0):5d} hit={s.get('hit',0):.3f} pf={s.get('pf','?')}")

    print()

    # ── portfolio replay ─────────────────────────────────────────────────────
    print("=== PORTFOLIO REPLAY (fee_100_all + 15m cooldown + max_c=3 + $10) ===")
    replay_all = portfolio_replay(all_rows, cooldown_s=900, max_c=3, notional=10.0)
    print(f"  trades={replay_all['n']} win={replay_all.get('win_rate',0):.3f} "
          f"pf={replay_all.get('pf','?')} roi={replay_all.get('roi_pct',0):.2f}%")

    print("=== PORTFOLIO REPLAY (heat_20_50 + 15m cooldown + max_c=3 + $10) ===")
    replay_heat = portfolio_replay(rows_20_50, cooldown_s=900, max_c=3, notional=10.0)
    print(f"  trades={replay_heat['n']} win={replay_heat.get('win_rate',0):.3f} "
          f"pf={replay_heat.get('pf','?')} roi={replay_heat.get('roi_pct',0):.2f}%")

    print("=== PORTFOLIO REPLAY (heat_20p + 15m cooldown + max_c=3 + $10) ===")
    replay_20p = portfolio_replay(rows_20p, cooldown_s=900, max_c=3, notional=10.0)
    print(f"  trades={replay_20p['n']} win={replay_20p.get('win_rate',0):.3f} "
          f"pf={replay_20p.get('pf','?')} roi={replay_20p.get('roi_pct',0):.2f}%")

    # ── in-bundle vs OOS replay ───────────────────────────────────────────────
    print("\n=== REPLAY BY SOURCE ===")
    for src_label, subset in [("in_bundle", ib), ("oos_sprint054", oos)]:
        rp = portfolio_replay(subset, cooldown_s=900, max_c=3, notional=10.0)
        print(f"  {src_label:15s}: trades={rp.get('n',0):3d} "
              f"win={rp.get('win_rate',0):.3f} pf={rp.get('pf','?')} "
              f"roi={rp.get('roi_pct',0):.2f}%")

    # ── matrix CSV ───────────────────────────────────────────────────────────
    matrix_rows: list[dict] = []
    for heat in ["all","lt20","20_50","50_100","ge100","20_50+","20+"]:
        if heat == "all":         subset = all_rows
        elif heat == "lt20":      subset = [r for r in all_rows if r["activity_heat"] < 20]
        elif heat == "20_50":     subset = rows_20_50
        elif heat == "50_100":    subset = [r for r in all_rows if 50 <= r["activity_heat"] < 100]
        elif heat == "ge100":     subset = [r for r in all_rows if r["activity_heat"] >= 100]
        elif heat == "20_50+":    subset = rows_20p   # heat >= 20
        else:                     subset = rows_20p   # heat >= 20 (same)
        rs  = [r["net_return_net"] for r in subset]
        s   = _stats(rs)
        rp  = portfolio_replay(subset, cooldown_s=900, max_c=3, notional=10.0)
        matrix_rows.append({
            "bucket": heat, "n_raw": s.get("n",0),
            "hit": s.get("hit",0), "pf": s.get("pf",""), "mean": s.get("mean",0),
            "n_trades": rp.get("n",0), "win_rate": rp.get("win_rate",0),
            "replay_pf": rp.get("pf",""), "roi_pct": rp.get("roi_pct",0),
        })

    with open(OUT_DIR / "expansion_matrix.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(matrix_rows[0].keys()))
        w.writeheader()
        w.writerows(matrix_rows)
    print(f"\nWrote expansion_matrix.csv")

    # ── assess regime stability ───────────────────────────────────────────────
    # Only count weeks with ≥ 10 rows as statistically meaningful
    MIN_WEEK_ROWS = 10
    meaningful_weeks = {wk: rs for wk, rs in by_week.items() if len(rs) >= MIN_WEEK_ROWS}
    n_weeks_positive = sum(1 for wk, rs in meaningful_weeks.items()
                           if _stats(rs).get("pf", 0) and _stats(rs)["pf"] > 1.0)
    n_weeks_total    = len(meaningful_weeks)
    n_weeks_all      = len(by_week)
    n_weeks_tiny     = n_weeks_all - n_weeks_total  # excluded due to < 10 rows

    regime_verdict = "REGIME_STABLE" if n_weeks_positive / max(n_weeks_total, 1) >= 0.65 else "REGIME_FRAGILE"

    print(f"\n=== REGIME VERDICT ===")
    print(f"  Total weeks: {n_weeks_all} ({n_weeks_tiny} excluded: <10 rows)")
    print(f"  Meaningful weeks (n≥10): {n_weeks_total}")
    print(f"  Positive PF weeks: {n_weeks_positive}/{n_weeks_total} → {regime_verdict}")

    # ── write readout ─────────────────────────────────────────────────────────
    write_readout(
        all_rows=all_rows, ib=ib, oos=oos,
        s_all=s_all, s_ib=s_ib, s_oos=s_oos,
        week_matrix=week_matrix,
        heat_stats=heat_stats,
        replay_all=replay_all, replay_heat=replay_heat, replay_20p=replay_20p,
        regime_verdict=regime_verdict,
        n_weeks_positive=n_weeks_positive, n_weeks_total=n_weeks_total,
        n_weeks_tiny=n_weeks_tiny,
    )
    print(f"Wrote expansion_readout.md")


def write_readout(**kw) -> None:
    all_rows = kw["all_rows"]; ib = kw["ib"]; oos = kw["oos"]
    s_all = kw["s_all"]; s_ib = kw["s_ib"]; s_oos = kw["s_oos"]
    week_matrix = kw["week_matrix"]; heat_stats = kw["heat_stats"]
    replay_all = kw["replay_all"]; replay_heat = kw["replay_heat"]
    replay_20p = kw["replay_20p"]
    verdict = kw["regime_verdict"]; n_pos = kw["n_weeks_positive"]; n_tot = kw["n_weeks_total"]
    n_tiny = kw.get("n_weeks_tiny", 0)

    lines = [
        "# Sprint056A — Historical fee_100 Expansion Readout",
        "",
        f"**Total fee_100 labeled rows:** {len(all_rows)}  "
        f"({len(ib)} in-bundle + {len(oos)} OOS)",
        "",
        "## Summary Statistics",
        "",
        "| Cohort | N | Hit | PF | Mean Net Return |",
        "|--------|---|-----|-----|-----------------|",
        f"| All combined | {s_all['n']} | {s_all.get('hit',0):.3f} | {s_all.get('pf','?')} | {s_all.get('mean',0):.4f} |",
        f"| In-bundle (Sprint039G) | {s_ib['n']} | {s_ib.get('hit',0):.3f} | {s_ib.get('pf','?')} | {s_ib.get('mean',0):.4f} |",
        f"| OOS Sprint054 (real RPC) | {s_oos['n']} | {s_oos.get('hit',0):.3f} | {s_oos.get('pf','?')} | {s_oos.get('mean',0):.4f} |",
        "",
        "## Per-Week Breakdown",
        "",
        "| Week | Source | N | Hit | PF |",
        "|------|--------|---|-----|-----|",
    ]
    for row in week_matrix:
        pf = row.get("pf", "?")
        lines.append(f"| {row['week']} | {row['source']} | {row.get('n',0)} "
                     f"| {row.get('hit',0):.3f} | {pf} |")

    lines += [
        "",
        "## Heat Bucket Breakdown",
        "",
        "| Bucket | N | Hit | PF | Mean |",
        "|--------|---|-----|-----|------|",
    ]
    for bucket in ["lt20","20_50","50_100","ge100"]:
        s = heat_stats.get(bucket, {})
        lines.append(f"| {bucket} | {s.get('n',0)} | {s.get('hit',0):.3f} | {s.get('pf','?')} | {s.get('mean',0):.4f} |")

    lines += [
        "",
        "## Portfolio Replay (15m cooldown, max_c=3, $10 notional)",
        "",
        "| Strategy | Trades | Win Rate | PF | ROI% |",
        "|----------|--------|----------|----|------|",
        f"| fee_100_all | {replay_all.get('n',0)} | {replay_all.get('win_rate',0):.3f} | {replay_all.get('pf','?')} | {replay_all.get('roi_pct',0):.2f}% |",
        f"| heat_20_50 | {replay_heat.get('n',0)} | {replay_heat.get('win_rate',0):.3f} | {replay_heat.get('pf','?')} | {replay_heat.get('roi_pct',0):.2f}% |",
        f"| heat_20p | {replay_20p.get('n',0)} | {replay_20p.get('win_rate',0):.3f} | {replay_20p.get('pf','?')} | {replay_20p.get('roi_pct',0):.2f}% |",
        "",
        "## Regime Stability Verdict",
        "",
        f"**{verdict}** — {n_pos}/{n_tot} meaningful weeks (n≥10) positive PF  "
        f"({n_tiny} tiny-n weeks excluded as non-informative)",
        "",
        f"{'✓' if verdict == 'REGIME_STABLE' else '✗'} fee_100 appears to be a **{'persistent regime edge' if verdict == 'REGIME_STABLE' else 'fragile/coincidental finding'}**",
        "",
        "## Artifacts",
        "",
        "- `mvp/diagnostics/sprint056/expansion_combined.csv` — full labeled dataset",
        "- `mvp/diagnostics/sprint056/expansion_matrix.csv` — heat bucket × replay matrix",
    ]

    (AUD_DIR / "expansion_readout.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
