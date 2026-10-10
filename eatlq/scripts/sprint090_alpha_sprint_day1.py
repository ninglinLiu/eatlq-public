#!/usr/bin/env python3
"""Sprint090 Day-1 — universe report + robustness on 30d backtest + forward kickoff marker.

Usage:
  python3 scripts/sprint090_alpha_sprint_day1.py
  python3 scripts/sprint090_alpha_sprint_day1.py --per-signal mvp/audits/sprint088_historical_backtest_30d/per_signal.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "mvp/audits/sprint090_alpha_search"
CH = "eatlq-clickhouse-1"
SENDER_0X6FF5 = "0x6ff5693b99212da76ad316178a184ab56d299b43"


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--per-signal", default="mvp/audits/sprint088_historical_backtest_30d/per_signal.csv")
    p.add_argument("--bootstrap-n", type=int, default=2000)
    return p.parse_args()


def _ch(sql: str) -> str:
    r = subprocess.run(
        ["docker", "exec", CH, "clickhouse-client", "--query", sql],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:400])
    return r.stdout.strip()


def pf(vals: list[float]) -> float | None:
    pos = sum(v for v in vals if v > 0)
    neg = abs(sum(v for v in vals if v < 0))
    if neg > 0:
        return round(pos / neg, 4)
    if pos > 0:
        return float("inf")
    return None


def load_signals(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def metrics(rows: list[dict]) -> dict:
    dec = [r for r in rows if r.get("rpc_error", "").lower() not in ("true", "1")]
    nets = []
    for r in dec:
        v = r.get("trailing_2h_net", "")
        if v in ("", "None", None):
            continue
        try:
            nets.append(float(v))
        except ValueError:
            pass
    pool_cnt = Counter(r["pool"] for r in dec)
    top_pool_share = (max(pool_cnt.values()) / len(dec)) if dec else 0.0
    return {
        "n": len(nets),
        "unique_pools": len(pool_cnt),
        "top_pool_share": round(top_pool_share, 4),
        "mean": round(sum(nets) / len(nets), 6) if nets else None,
        "win_rate": round(sum(1 for v in nets if v > 0) / len(nets), 4) if nets else None,
        "pf": pf(nets),
        "sell_fail_rate": round(sum(1 for r in dec if str(r.get("sell_path_ok")).lower() == "false") / max(len(dec), 1), 4),
        "honeypot_rate": round(sum(1 for r in dec if str(r.get("honeypot_flag")).lower() == "true") / max(len(dec), 1), 4),
        "_nets": nets,
        "_dec": dec,
    }


def bootstrap_pf(nets: list[float], n: int = 2000) -> dict:
    if not nets:
        return {}
    pfs = []
    for _ in range(n):
        sample = [nets[random.randrange(len(nets))] for _ in range(len(nets))]
        p = pf(sample)
        if p is not None and p != float("inf"):
            pfs.append(p)
    pfs.sort()
    if not pfs:
        return {}
    return {
        "n": n,
        "pf_p5": round(pfs[int(0.05 * len(pfs))], 4),
        "pf_median": round(pfs[len(pfs) // 2], 4),
        "pf_p95": round(pfs[int(0.95 * len(pfs)) - 1], 4),
        "pct_pf_below_1_3": round(sum(1 for p in pfs if p < 1.3) / len(pfs), 4),
    }


def universe_ch() -> dict:
    daily = _ch("""
SELECT toDate(block_time) AS d, count() AS swaps, uniqExact(pool_address) AS pools
FROM eatlq.uniswap_v3_swaps
WHERE chain_id = 8453 AND block_time >= now() - INTERVAL 30 DAY AND block_time IS NOT NULL
GROUP BY d ORDER BY d
FORMAT TabSeparated
""")
    rows_daily = []
    for line in daily.splitlines():
        p = line.split("\t")
        if len(p) >= 3:
            rows_daily.append({"date": p[0], "swaps": int(p[1]), "pools": int(p[2])})

    u30 = _ch("""
SELECT uniqExact(pool_address) FROM eatlq.uniswap_v3_swaps
WHERE chain_id=8453 AND block_time >= now()-INTERVAL 30 DAY FORMAT TabSeparated
""")
    u7 = _ch("""
SELECT uniqExact(pool_address) FROM eatlq.uniswap_v3_swaps
WHERE chain_id=8453 AND block_time >= now()-INTERVAL 7 DAY FORMAT TabSeparated
""")
    u1h = _ch("""
SELECT uniqExact(pool_address) FROM eatlq.uniswap_v3_swaps
WHERE chain_id=8453 AND block_time >= now()-INTERVAL 1 HOUR FORMAT TabSeparated
""")

    fee = _ch("""
SELECT fee_bps, count() AS pools, sum(swaps) AS swaps
FROM (
  SELECT lower(d.pool_address) AS pool, d.fee_bps, count() AS swaps
  FROM eatlq.uniswap_v3_swaps s
  INNER JOIN eatlq.discovered_pools d ON lower(s.pool_address) = lower(d.pool_address)
  WHERE s.chain_id = 8453 AND s.block_time >= now() - INTERVAL 30 DAY
  GROUP BY pool, d.fee_bps
) GROUP BY fee_bps ORDER BY swaps DESC
FORMAT TabSeparated
""")
    fee_rows = []
    for line in fee.splitlines():
        p = line.split("\t")
        if len(p) >= 3:
            fee_rows.append({"fee_bps": p[0], "pools": int(p[1]), "swaps": int(p[2])})

    gap_src = _ch("""
SELECT count(), uniqExact(pool_address) FROM eatlq.uniswap_v3_swaps
WHERE source='sprint089_gap_backfill' FORMAT TabSeparated
""")

    return {
        "daily": rows_daily,
        "unique_pools_30d": int(u30 or 0),
        "unique_pools_7d": int(u7 or 0),
        "unique_pools_1h": int(u1h or 0),
        "fee_tiers_30d": fee_rows,
        "gap_backfill_swaps": gap_src,
        "avg_pools_per_day_7d": round(
            sum(r["pools"] for r in rows_daily[-7:]) / min(7, len(rows_daily[-7:])), 1
        ) if rows_daily else 0,
    }


def main() -> int:
    args = parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    per_path = REPO / args.per_signal
    if not per_path.exists():
        print(f"ERROR: missing {per_path}", flush=True)
        return 2

    uni = universe_ch()
    rows = load_signals(per_path)
    base = metrics(rows)

    nets = base.pop("_nets")
    dec = base.pop("_dec")

    # top1 trade removed
    if nets:
        idx = max(range(len(nets)), key=lambda i: nets[i])
        nets_no_top1 = nets[:idx] + nets[idx + 1 :]
        top1_net = nets[idx]
    else:
        nets_no_top1 = []
        top1_net = None
    base_no_top1_trade = {
        "n": len(nets_no_top1),
        "pf": pf(nets_no_top1),
        "mean": round(sum(nets_no_top1) / len(nets_no_top1), 6) if nets_no_top1 else None,
        "win_rate": round(sum(1 for v in nets_no_top1 if v > 0) / len(nets_no_top1), 4) if nets_no_top1 else None,
        "removed_top1_net": top1_net,
    }

    # top1 pool removed
    pool_cnt = Counter(r["pool"] for r in dec)
    if pool_cnt:
        top_pool = pool_cnt.most_common(1)[0][0]
        dec_no_pool = [r for r in dec if r["pool"] != top_pool]
    else:
        top_pool = ""
        dec_no_pool = dec
    nets_no_pool = []
    for r in dec_no_pool:
        try:
            nets_no_pool.append(float(r["trailing_2h_net"]))
        except (ValueError, TypeError):
            pass
    base_no_top1_pool = {"n": len(nets_no_pool), "pf": pf(nets_no_pool), "excluded_pool": top_pool}

    # no honeypot
    dec_no_hp = [r for r in dec if str(r.get("honeypot_flag")).lower() != "true"]
    nets_no_hp = [float(r["trailing_2h_net"]) for r in dec_no_hp if r.get("trailing_2h_net") not in ("", None)]
    base_no_hp = {"n": len(nets_no_hp), "pf": pf(nets_no_hp)}

    boot = bootstrap_pf(nets, args.bootstrap_n)

    # Verdict
    passes_robust = (
        base.get("pf") is not None
        and base_no_top1_trade.get("pf") is not None
        and base_no_top1_trade["pf"] >= 1.3
        and boot.get("pf_p5", 0) >= 1.0
        and base.get("unique_pools", 0) >= 30
    )
    universe_ok = uni["unique_pools_30d"] >= 50 or uni["avg_pools_per_day_7d"] >= 20

    verdict = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "sop_status": "PROVISIONAL — delete aggressive SOP clauses if this FAILs",
        "historical_30d_relaxed": base,
        "pf_top1_trade_removed": base_no_top1_trade,
        "pf_top1_pool_removed": base_no_top1_pool,
        "no_honeypot": base_no_hp,
        "bootstrap": boot,
        "universe": uni,
        "passes_robust_pf": passes_robust,
        "universe_breadth_ok": universe_ok,
        "verdict": "PASS_ROBUST" if passes_robust and universe_ok else "FAIL_ROBUST",
        "TRAINING_ALLOWED": False,
        "LIVE_ALLOWED": False,
        "next": "48h forward: sprint055_fee100_paper_shadow.py --once (SPRINT087)",
    }
    (OUT / "robustness_summary.json").write_text(json.dumps(verdict, indent=2) + "\n")

    # UNIVERSE_REPORT.md
    daily_tbl = "\n".join(
        f"| {r['date']} | {r['swaps']:,} | {r['pools']} |" for r in uni["daily"][-14:]
    )
    fee_tbl = "\n".join(
        f"| {r['fee_bps']} | {r['pools']} | {r['swaps']:,} |" for r in uni["fee_tiers_30d"]
    ) or "| (no join) | — | — |"

    (OUT / "UNIVERSE_REPORT.md").write_text(f"""# Sprint090 — Universe Report

Generated: {verdict['generated_at_utc']}

## Headline

| Metric | Value | Target |
|---|---:|---|
| unique pools (30d) | **{uni['unique_pools_30d']}** | ≥50 research / 200/day direction |
| unique pools (7d) | {uni['unique_pools_7d']} | |
| unique pools (1h) | {uni['unique_pools_1h']} | |
| avg pools/day (last 7d) | **{uni['avg_pools_per_day_7d']}** | ≥20 → 200 |
| gap_backfill in swaps | {uni['gap_backfill_swaps']} | |

**Universe breadth gate**: {'✅' if universe_ok else '❌'} `universe_breadth_ok`

## Last 14 days (swaps / unique pools)

| date | swaps | pools |
|---|---:|---:|
{daily_tbl}

## Fee tiers (30d, via discovered_pools join)

| fee_bps | pools | swaps |
|---|---:|---:|
{fee_tbl}

## Interpretation

- Pipeline post-089 widened data; **5/19–5/21** pool counts improved vs **5/18** still-low day.
- Fee metadata join may under-count tiers — treat **FEE_BLIND_SPOT** until `discovered_pools.fee_bps` coverage improves.
- Do **not** promote on universe until `avg_pools_per_day` trends toward **200+** or historical `unique_pools≥30` with **robust PF**.

""")

    (OUT / "ROBUSTNESS.md").write_text(f"""# Sprint090 — Robustness (30d relaxed backtest)

Source: `{args.per_signal}`

> SOP is **provisional**. If `FAIL_ROBUST`, strip aggressive clauses from `docs/alpha-search-sprint-sop.md`.

## Headline (trailing 2h, real cost)

| Slice | n | PF | WR | mean |
|---|---:|---:|---:|---:|
| All | {base.get('n')} | **{base.get('pf')}** | {base.get('win_rate')} | {base.get('mean')} |
| Top1 **trade** removed | {base_no_top1_trade.get('n')} | **{base_no_top1_trade.get('pf')}** | {base_no_top1_trade.get('win_rate')} | {base_no_top1_trade.get('mean')} |
| Top1 **pool** removed ({top_pool[:10]}…) | {base_no_top1_pool.get('n')} | **{base_no_top1_pool.get('pf')}** | | |
| No honeypot | {base_no_hp.get('n')} | {base_no_hp.get('pf')} | | |

## Bootstrap ({args.bootstrap_n} resamples)

- PF p5: **{boot.get('pf_p5', 'n/a')}** (need ≥1.0)
- PF median: {boot.get('pf_median', 'n/a')}
- PF p95: {boot.get('pf_p95', 'n/a')}
- % samples PF&lt;1.3: {boot.get('pct_pf_below_1_3', 'n/a')}

## Diversity

- unique_pools: {base.get('unique_pools')} (min 30)
- top_pool_share: {base.get('top_pool_share')}
- honeypot_rate: {base.get('honeypot_rate')}
- sell_fail_rate: {base.get('sell_fail_rate')}

## Verdict

**{verdict['verdict']}** — robust PF gate: top1-trade-removed PF≥1.3 AND bootstrap p5≥1.0

Original sprint088 readout said **PASS** on gross 30d; this file is the **honesty check**.
""")

    (OUT / "VERDICT.md").write_text(
        f"# Sprint090 Day-1 Verdict\n\n"
        f"**{verdict['verdict']}** at {verdict['generated_at_utc']}\n\n"
        f"- Historical relaxed PASS (see sprint088_30d) is **not sufficient** alone.\n"
        f"- Robust: top1-trade PF={base_no_top1_trade.get('pf')}, bootstrap p5={boot.get('pf_p5')}\n"
        f"- Universe 30d pools={uni['unique_pools_30d']}, avg/day(7d)={uni['avg_pools_per_day_7d']}\n\n"
        f"Next: 48h forward paper (`logs/sprint087_expanded_paper_shadow.csv`).\n"
    )

    print(json.dumps({"verdict": verdict["verdict"], "pf": base.get("pf"), "pf_top1_removed": base_no_top1_trade.get("pf")}, indent=2))
    return 0 if verdict["verdict"] == "PASS_ROBUST" else 1


if __name__ == "__main__":
    raise SystemExit(main())
