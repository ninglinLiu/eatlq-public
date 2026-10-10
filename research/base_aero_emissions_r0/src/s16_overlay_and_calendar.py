#!/usr/bin/env python3
"""Kill-first: (1) does AERO-emission income flip C-family LP PnL?
              (2) does the Thursday epoch leave a calendar pattern in AERO or pool flow?

Emissions are protocol subsidy — AERO holders' inflation, not trader money. C-family
T=7 net is about −50 to −110 bps. High-confidence median APR is 3.25%/yr ≈ 6 bps/week.
If even an optimistic (uncapped, all-confidence) overlay cannot flip the family, C+emissions
is dead as an unconditional strategy. A trailing-APR selection rule is the one remaining
ex-ante version, and it is the same economic bet as C4 (chase activity, eat IL).

D1 is a different claim: calendar-forced flows around Thursday 00:00 UTC. Cheapest kill
is day-of-week returns / volume, zero RPC.

Reads only. Writes under this workspace out/.
"""
from __future__ import annotations

import json
import subprocess
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path("/root/base_aero_emissions_r0/out")
POS = Path("/root/base_fee_tier_r0/out/stage3_pnl_50.json")
UNI = Path("/root/base_condmm_r0/out/universe50.jsonl")
CORR = Path("/root/base_condmm_r0/out/pos_fee_corrected.json")

# Thursday 00:00 UTC epoch origin used by census.aero_emissions (unix 0 is Thursday)
EPOCH0 = 0
WEEK = 604800
SPLIT = "2026-02-16"


def ch(q, timeout=900):
    r = subprocess.run(
        ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--query", q],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode:
        raise RuntimeError(r.stderr[:3000])
    return r.stdout


def epoch_of(d: date) -> datetime:
    ts = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
    return datetime.fromtimestamp((ts // WEEK) * WEEK, tz=timezone.utc).replace(tzinfo=None)


def load_apr():
    txt = ch("""
        SELECT pool, toString(epoch_start), weekly_return_lb, apr_lb,
               stake_confidence, emission_usd, staked_liquidity_usd_ub
        FROM census.aero_emission_apr FORMAT TabSeparated
    """)
    apr = defaultdict(dict)
    for line in txt.strip().split("\n"):
        if not line:
            continue
        pool, ep, wr, aprv, conf, em, st = line.split("\t")
        apr[pool][ep] = {
            "wr": float(wr), "apr": float(aprv), "conf": conf,
            "em_usd": float(em), "staked_ub": float(st),
        }
    return apr


def calendar():
    print("\n" + "=" * 108)
    print("D1 CALENDAR: AERO log-return and volume by ISO day-of-week")
    print("Thursday = Aerodrome epoch open (00:00 UTC). ISO: 1=Mon .. 4=Thu .. 7=Sun")
    print("=" * 108)
    print(ch("""
    SELECT toDayOfWeek(toDate(b.bar)) AS dow,
           count() AS hours,
           round(avg(log(b.close / nullIf(b.open, 0))) * 10000, 2) AS mean_bps,
           round(median(log(b.close / nullIf(b.open, 0))) * 10000, 2) AS med_bps,
           round(sum(b.vol_weth), 0) AS weth_vol
    FROM edge.bars_1h_year_50 b
    INNER JOIN edge.bars_universe_50 u ON u.pool = b.pool
    WHERE u.symbol = 'AERO'
    GROUP BY dow
    ORDER BY dow
    FORMAT PrettyCompact
    """))

    print("same, restricted to the deepest AERO CL pool 0x82321f3beb69f503380d6b233857d5c43562e2d0:")
    print(ch("""
    SELECT toDayOfWeek(toDate(bar)) AS dow,
           round(avg(log(close / nullIf(open, 0))) * 10000, 2) AS mean_bps,
           round(median(log(close / nullIf(open, 0))) * 10000, 2) AS med_bps,
           round(sum(vol_weth), 0) AS weth_vol
    FROM edge.bars_1h_year_50
    WHERE pool = '0x82321f3beb69f503380d6b233857d5c43562e2d0'
    GROUP BY dow ORDER BY dow FORMAT PrettyCompact
    """))

    print("hours relative to Thursday 00:00 UTC (epoch open), AERO CL pool, ±24h window averaged:")
    print(ch("""
    SELECT intDiv(toHour(bar) + 24 * (toDayOfWeek(toDate(bar)) - 4), 4) AS slot_4h,
           count() AS hours,
           round(avg(log(close / nullIf(open, 0))) * 10000, 2) AS mean_bps,
           round(sum(vol_weth) / count(), 1) AS weth_per_hour
    FROM edge.bars_1h_year_50
    WHERE pool = '0x82321f3beb69f503380d6b233857d5c43562e2d0'
      AND toDayOfWeek(toDate(bar)) IN (3, 4, 5)
    GROUP BY slot_4h
    ORDER BY slot_4h
    FORMAT PrettyCompact
    """))

    print("panel-wide volume by dow (is Thursday special for flow, not just AERO price?):")
    print(ch("""
    SELECT toDayOfWeek(toDate(bar)) AS dow,
           round(sum(vol_weth)/1e6, 2) AS weth_m,
           count() AS hours
    FROM edge.bars_1h_year_50
    GROUP BY dow ORDER BY dow FORMAT PrettyCompact
    """))

    print("half-year stability of AERO Thursday vs rest, deepest CL pool:")
    print(ch("""
    SELECT if(bar < toDateTime('2026-02-16 00:00:00'), 'h1', 'h2') AS half,
           if(toDayOfWeek(toDate(bar)) = 4, 'Thu', 'other') AS day,
           round(avg(log(close / nullIf(open, 0))) * 10000, 2) AS mean_bps,
           count() AS hours
    FROM edge.bars_1h_year_50
    WHERE pool = '0x82321f3beb69f503380d6b233857d5c43562e2d0'
    GROUP BY half, day
    ORDER BY half, day
    FORMAT PrettyCompact
    """))


def overlay(apr):
    print("\n" + "=" * 108)
    print("C + EMISSIONS OVERLAY")
    print("=" * 108)
    pos = pd.DataFrame(json.loads(POS.read_text()))
    uni = [json.loads(l) for l in UNI.read_text().splitlines() if l.strip()]
    venue = {p["pool"]: p["venue"] for p in uni}
    pos["venue"] = pos["pool"].map(venue).fillna("unknown")
    pos["entry_d"] = pd.to_datetime(pos["entry"]).dt.date

    if CORR.exists():
        corr = pd.DataFrame(json.loads(CORR.read_text()))
        pos = pos.merge(
            corr[["pool", "entry", "T", "c", "net_cheap_bps_true", "net_strict_bps_true", "corr"]],
            on=["pool", "entry", "T", "c"], how="left",
        )
        pos["net"] = pos["net_strict_bps_true"].fillna(pos["net_strict_bps"])
        print(f"using fee-corrected net where available; "
              f"corr covered {pos['corr'].notna().mean():.1%}")
    else:
        pos["net"] = pos["net_strict_bps"]
        print("fee-corrected file missing; using stored-fee net_strict_bps")

    # map each position to the epoch containing entry
    wrs, confs, prev = [], [], []
    for pl, ed, T in zip(pos["pool"], pos["entry_d"], pos["T"]):
        ep = epoch_of(ed)
        rec = apr.get(pl, {}).get(str(ep))
        wrs.append(rec["wr"] if rec else np.nan)
        confs.append(rec["conf"] if rec else None)
        pep = ep - timedelta(days=7)
        prec = apr.get(pl, {}).get(str(pep))
        prev.append(prec["wr"] if prec else np.nan)
    pos["wr"] = wrs
    pos["conf"] = confs
    pos["wr_lag"] = prev
    # emissions over the hold, in bps of notional, assuming the NFT is staked
    pos["em_bps"] = pos["wr"] * 1e4 * (pos["T"] / 7.0)
    pos["em_bps_lag"] = pos["wr_lag"] * 1e4 * (pos["T"] / 7.0)
    pos["net_em"] = pos["net"] + pos["em_bps"].fillna(0.0)

    aero = pos[pos["venue"] == "aero_cl"].copy()
    print(f"positions {len(pos)}  aero_cl {len(aero)}  "
          f"with current-epoch APR {aero['wr'].notna().mean():.1%}")

    def summarise(df, label):
        em = df["em_bps"].replace([np.inf, -np.inf], np.nan)
        print(f"\n{label}: n={len(df)}  symbols={df['symbol'].nunique()}  "
              f"em coverage={em.notna().mean():.1%}")
        print(f"  net without emissions  mean={df['net'].mean():8.2f}  "
              f"median={df['net'].median():8.2f}")
        print(f"  emission bps           mean={em.mean():8.2f}  "
              f"median={em.median():8.2f}  p90={em.quantile(.9):8.2f}  "
              f"p99={em.quantile(.99):8.2f}")
        print(f"  net + emissions        mean={df['net_em'].mean():8.2f}  "
              f"median={df['net_em'].median():8.2f}")
        # winsorized overlay: cap emission at p95 of observed (kills RECALL 1e6% APR)
        cap = em.quantile(0.95)
        net_w = df["net"] + em.clip(upper=cap).fillna(0.0)
        print(f"  net + em p95-capped    mean={net_w.mean():8.2f}  "
              f"(cap={cap:.1f} bps)  median={net_w.median():8.2f}")

    for T in (1, 7, 30):
        summarise(aero[aero["T"] == T], f"aero_cl T={T} all-confidence")
        hi = aero[(aero["T"] == T) & (aero["conf"] == "high")]
        if len(hi):
            summarise(hi, f"aero_cl T={T} high-confidence stake only")

    # ---- trailing-APR selection, T=7, PIT (previous epoch known at entry)
    print("\n" + "=" * 108)
    print("EX-ANTE RULE: at each entry day, take aero_cl pools in the top quartile of")
    print("previous-epoch emission weekly return (known before entry). T=7.")
    print("=" * 108)
    t7 = aero[aero["T"] == 7].copy()
    keys = t7[["entry", "pool", "wr_lag"]].drop_duplicates()
    keys = keys[keys["wr_lag"].notna()]
    keys["pct"] = keys.groupby("entry")["wr_lag"].rank(pct=True, method="average")
    top = keys[keys["pct"] >= 0.75]
    sel = t7.merge(top[["entry", "pool"]], on=["entry", "pool"])
    rest = t7.merge(keys[keys["pct"] < 0.75][["entry", "pool"]], on=["entry", "pool"])
    print(f"selected n={len(sel)}  rest n={len(rest)}  "
          f"entry days={sel['entry'].nunique()}  symbols={sel['symbol'].nunique()}")
    summarise(sel, "SELECTED top-quartile trailing emission APR, T=7")
    summarise(rest, "NOT selected, T=7")

    # half-year
    for name, mask in (("h1", sel["entry"] < SPLIT), ("h2", sel["entry"] >= SPLIT)):
        g = sel[mask]
        if len(g):
            print(f"  {name}: n={len(g)} net={g['net'].mean():.1f}  "
                  f"net+em={g['net_em'].mean():.1f}  em_med={g['em_bps'].median():.1f}")

    # whose IL vs subsidy
    print("\nselected book decomposition:")
    print(f"  fee_bps={sel['fee_bps'].mean():.1f}  il_bps={sel['il_bps'].mean():.1f}  "
          f"cost={sel['cost_strict_bps'].mean():.1f}  em_bps={sel['em_bps'].mean():.1f}")
    print(f"  rest   fee_bps={rest['fee_bps'].mean():.1f}  il_bps={rest['il_bps'].mean():.1f}  "
          f"cost={rest['cost_strict_bps'].mean():.1f}  em_bps={rest['em_bps'].mean():.1f}")

    by = sel.groupby("symbol")["net_em"].agg(["mean", "count", "sum"]).sort_values("sum", ascending=False)
    tot = by["sum"].sum()
    print("\ntop symbols in selected book by total net+em:")
    print(by.head(10).to_string(float_format=lambda x: f"{x:10.1f}"))
    if tot:
        print(f"top symbol share of selected net+em: {by.iloc[0]['sum']/tot:.1%} ({by.index[0]})")

    out = {
        "aero_t7_mean_net": float(t7["net"].mean()) if len(t7) else None,
        "aero_t7_mean_net_em": float(t7["net_em"].mean()) if len(t7) else None,
        "aero_t7_median_em": float(t7["em_bps"].median()) if len(t7) else None,
        "selected_n": int(len(sel)),
        "selected_mean_net": float(sel["net"].mean()) if len(sel) else None,
        "selected_mean_net_em": float(sel["net_em"].mean()) if len(sel) else None,
        "selected_mean_il": float(sel["il_bps"].mean()) if len(sel) else None,
        "selected_mean_em": float(sel["em_bps"].mean()) if len(sel) else None,
        "rest_mean_net_em": float(rest["net_em"].mean()) if len(rest) else None,
    }
    json.dump(out, open(OUT / "overlay_summary.json", "w"), indent=1)
    print(f"\nwrote {OUT / 'overlay_summary.json'}")


def main():
    calendar()
    apr = load_apr()
    n = sum(len(v) for v in apr.values())
    print(f"\nloaded {n} (pool, epoch) APR rows, {len(apr)} pools")
    overlay(apr)


if __name__ == "__main__":
    main()
