"""Sprint057 — fee_100 Horizon Sweep + Exit Policy Map.

Fixed entry gate (fee_100 Sprint037G canonical). Variable exit horizons:
  5m / 10m / 15m / 30m  — from q4 bundle QuoterV2 labels (real)
  1h / 2h / 4h / 6h / 12h / 24h — from CH sqrtPriceX96 proxy (directional)

Analyses:
  1. Per-horizon stats for 6 segments (fee_100_all, heat_20_50, heat_20p,
     weth_is_token1, BPR<0.55, composite)
  2. MFE / MAE proxy from waypoints
  3. Exit policy simulation: fixed, TP, SL, TP+SL, trailing stop
  4. Long-horizon concentration check (pool / week)
  5. Optimal hold period identification

ClickHouse query design per schema-pk-filter-on-orderby rule:
  - Always filter chain_id = 8453 AND block_time range first (ORDER BY prefix)
  - pool_address IN (...) as secondary filter
  - Process per week to bound memory usage

Outputs:
  - mvp/diagnostics/sprint057/horizon_sweep_matrix.csv
  - mvp/diagnostics/sprint057/trade_path_mfe_mae.csv
  - mvp/audits/sprint057/horizon_sweep_readout.md
  - mvp/audits/sprint057/exit_policy_map.md
  - mvp/runs/run-057-fee100-horizon-sweep-readout.md
"""

from __future__ import annotations

import bisect
import csv
import glob
import io
import subprocess
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO      = Path(__file__).resolve().parents[1]
Q4_DIR    = REPO / "mvp/audits/sprint039a/q4_bundle_combined"
OUT_DIR   = REPO / "mvp/diagnostics/sprint057"
AUD_DIR   = REPO / "mvp/audits/sprint057"
RUN_DIR   = REPO / "mvp/runs"
OUT_DIR.mkdir(parents=True, exist_ok=True)
AUD_DIR.mkdir(parents=True, exist_ok=True)

CH_CONTAINER = "eatlq-clickhouse-1"
CHAIN_ID     = 8453
GAS_PROXY    = 0.002

# Horizons: seconds from entry_time_sec
HORIZONS_SHORT = {"5m": 300, "10m": 600, "15m": 900, "30m": 1800}
HORIZONS_LONG  = {"1h": 3600, "2h": 7200, "4h": 14400,
                   "6h": 21600, "12h": 43200, "24h": 86400}
ALL_HORIZONS = {**HORIZONS_SHORT, **HORIZONS_LONG}

# Sanity caps for sqrtPriceX96 proxy
SQRT_RATIO_MIN = 0.001
SQRT_RATIO_MAX = 1000
PROXY_RET_CAP  = 2.0   # cap extreme returns at ±200%

# ── ClickHouse helper ─────────────────────────────────────────────────────────

def _ch(sql: str, timeout: int = 120) -> list[list[str]]:
    """Per query-join-filter-before: filters applied in WHERE, not after."""
    r = subprocess.run(
        ["docker", "exec", CH_CONTAINER, "clickhouse-client",
         "--format", "CSV", "--query", sql],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"CH error: {r.stderr.strip()[:300]}")
    return [row for row in csv.reader(io.StringIO(r.stdout)) if row]

# ── data load ─────────────────────────────────────────────────────────────────

def load_q4_fee100() -> list[dict]:
    rows = []
    for fn in sorted(glob.glob(str(Q4_DIR / "*.csv"))):
        for r in csv.DictReader(open(fn)):
            if r.get("fee_bps") != "100" or r.get("entry_quote_ok") != "True":
                continue
            def _nr(col, ok_col):
                v = r.get(col)
                ok = r.get(ok_col) == "True"
                if not v or not ok or v in ("", "None"):
                    return None
                try:
                    return float(v) - GAS_PROXY
                except ValueError:
                    return None

            nr5  = _nr("net_return_5m",  "exit_ok_5m")
            nr10 = _nr("net_return_10m", "exit_ok_10m")
            nr15 = _nr("net_return_15m", "exit_ok_15m")
            nr30 = _nr("net_return_30m", "exit_ok_30m")

            rows.append({
                "pool":            r["pool_address"],
                "regime_week":     r["regime_week"],
                "entry_time_sec":  int(float(r["entry_time_sec"])),
                "weth_is_token0":  r.get("weth_is_token0") in ("1", "True", "true"),
                "activity_heat":   float(r.get("activity_heat") or 0),
                "buy_pressure_ratio": float(r.get("buy_pressure_ratio") or 0),
                "source":          "q4_bundle",
                # short-horizon real labels
                "nr_5m":   nr5,
                "nr_10m":  nr10,
                "nr_15m":  nr15,
                "nr_30m":  nr30,
                # long-horizon proxy (to be filled by CH)
                "nr_1h":   None,
                "nr_2h":   None,
                "nr_4h":   None,
                "nr_6h":   None,
                "nr_12h":  None,
                "nr_24h":  None,
            })
    return rows

# ── ClickHouse sqrtPriceX96 queries per week ──────────────────────────────────

def _proxy_return(sqrt_entry: float, sqrt_exit: float, weth_is_token0: bool) -> float | None:
    """Direction-aware proxy return from sqrtPriceX96 ratio.

    Per Sprint053 derivation:
    - weth_is_token0=True:  return ≈ (sqrt_entry/sqrt_exit)^2 - 1
    - weth_is_token0=False: return ≈ (sqrt_exit/sqrt_entry)^2 - 1
    """
    if not sqrt_entry or not sqrt_exit:
        return None
    ratio = sqrt_exit / sqrt_entry
    if not (SQRT_RATIO_MIN < ratio < SQRT_RATIO_MAX):
        return None
    if weth_is_token0:
        ret = (sqrt_entry / sqrt_exit) ** 2 - 1.0
    else:
        ret = (sqrt_exit / sqrt_entry) ** 2 - 1.0
    if abs(ret) > PROXY_RET_CAP:
        return None
    return ret


def fill_long_horizons_for_week(rows_week: list[dict]) -> None:
    """Fill nr_1h..nr_24h for all rows in a single week via CH sqrtPriceX96."""
    if not rows_week:
        return

    pools = sorted(set(r["pool"] for r in rows_week))
    min_ts = min(r["entry_time_sec"] for r in rows_week) - 300
    max_ts = max(r["entry_time_sec"] for r in rows_week) + 86400 + 300

    from_dt = datetime.fromtimestamp(min_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    to_dt   = datetime.fromtimestamp(max_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    pool_list = ",".join(f"'{p}'" for p in pools)

    # Per schema-pk-filter-on-orderby: chain_id + block_time first in WHERE
    # block_time and pool_address are ORDER BY prefix columns in this table
    sql = f"""
SELECT pool_address,
       toUnixTimestamp(block_time) AS ts,
       toFloat64(sqrt_price_x96)  AS sqrt_px
FROM eatlq.uniswap_v3_swaps_decoded
WHERE chain_id    = {CHAIN_ID}
  AND block_time >= toDateTime('{from_dt}')
  AND block_time <= toDateTime('{to_dt}')
  AND pool_address IN ({pool_list})
  AND sqrt_price_x96 > 0
ORDER BY pool_address, block_time
LIMIT 5000000
"""
    try:
        raw = _ch(sql, timeout=180)
    except Exception as e:
        print(f"  [CH warn] {e}", flush=True)
        return

    # Build per-pool time series
    pool_series: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for row in raw:
        if len(row) < 3:
            continue
        try:
            pool_series[row[0]].append((int(row[1]), float(row[2])))
        except ValueError:
            pass

    def get_sqrt_at(series: list[tuple[int, float]], target_ts: int) -> float | None:
        if not series:
            return None
        tss = [t for t, _ in series]
        idx = bisect.bisect_right(tss, target_ts) - 1
        if idx < 0:
            return None
        return series[idx][1]

    # Fill long horizon proxy returns
    for r in rows_week:
        pool    = r["pool"]
        series  = pool_series.get(pool, [])
        wt0     = r["weth_is_token0"]
        entry_ts = r["entry_time_sec"]

        sqrt_entry = get_sqrt_at(series, entry_ts)
        if sqrt_entry is None:
            continue  # no swap data at entry

        for label, delta in HORIZONS_LONG.items():
            sqrt_exit = get_sqrt_at(series, entry_ts + delta)
            r[f"nr_{label}"] = _proxy_return(sqrt_entry, sqrt_exit, wt0)


def fill_all_long_horizons(rows: list[dict]) -> None:
    """Process week by week to bound CH memory usage."""
    by_week: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_week[r["regime_week"]].append(r)

    for wk, wk_rows in sorted(by_week.items()):
        pools = set(r["pool"] for r in wk_rows)
        print(f"  [CH] week={wk} rows={len(wk_rows)} pools={len(pools)}", flush=True)
        fill_long_horizons_for_week(wk_rows)
        time.sleep(0.5)  # be gentle with CH

# ── segment definitions ───────────────────────────────────────────────────────

def _segments(rows: list[dict]) -> dict[str, list[dict]]:
    return {
        "fee_100_all":    rows,
        "heat_20_50":     [r for r in rows if 20 <= r["activity_heat"] < 50],
        "heat_20p":       [r for r in rows if r["activity_heat"] >= 20],
        "weth_is_token1": [r for r in rows if not r["weth_is_token0"]],
        "bpr_lt55":       [r for r in rows if r["buy_pressure_ratio"] < 0.55],
        "composite":      [r for r in rows
                           if not r["weth_is_token0"]
                           and r["activity_heat"] >= 20
                           and r["buy_pressure_ratio"] < 0.55],
    }

# ── stats helpers ──────────────────────────────────────────────────────────────

def _pf(pos: list[float], neg: list[float]) -> float | None:
    if not neg:
        return None
    return round(sum(pos) / sum(neg), 3)

def _horizon_stats(rows: list[dict], col: str) -> dict:
    vals = [r[col] for r in rows if r.get(col) is not None]
    if not vals:
        return {"n": 0}
    pos  = [v for v in vals if v > 0]
    neg  = [-v for v in vals if v <= 0]
    return {
        "n":      len(vals),
        "hit":    round(len(pos) / len(vals), 4),
        "pf":     _pf(pos, neg),
        "mean":   round(statistics.mean(vals), 6),
        "median": round(statistics.median(vals), 6),
        "max":    round(max(vals), 6),
        "min":    round(min(vals), 6),
        "loss_tail_p10": round(sorted(vals)[int(0.1 * len(vals))], 6) if len(vals) >= 10 else None,
    }

def _max_drawdown_seq(rets: list[float]) -> float:
    eq = peak = dd = 0.0
    for r in rets:
        eq += r; peak = max(peak, eq); dd = max(dd, peak - eq)
    return round(dd, 6)

def _portfolio_replay(rows: list[dict], col: str,
                      cooldown_s: int = 900, max_c: int = 3,
                      notional: float = 10.0) -> dict:
    rrows = [r for r in rows if r.get(col) is not None]
    rrows = sorted(rrows, key=lambda r: r["entry_time_sec"])
    pool_last: dict[str, float] = {}
    active: list[float] = []
    pnl_list: list[float] = []

    for r in rrows:
        t  = float(r["entry_time_sec"])
        nr = r[col]
        active = [x for x in active if x > t]
        if pool_last.get(r["pool"], 0) + cooldown_s > t:
            continue
        if len(active) >= max_c:
            continue
        pool_last[r["pool"]] = t
        active.append(t + 300)
        pnl_list.append(nr * notional)

    if not pnl_list:
        return {"trades": 0}
    wins   = [p for p in pnl_list if p > 0]
    losses = [-p for p in pnl_list if p <= 0]
    rets_r = [p / notional for p in pnl_list]
    total_inv = len(pnl_list) * notional
    return {
        "trades":    len(pnl_list),
        "win_rate":  round(len(wins) / len(pnl_list), 4),
        "pf":        round(sum(wins) / sum(losses), 3) if losses else None,
        "roi_pct":   round(100 * sum(pnl_list) / total_inv, 3),
        "max_dd_pct": round(100 * _max_drawdown_seq(rets_r), 3),
    }

# ── MFE / MAE ─────────────────────────────────────────────────────────────────

HORIZON_ORDER = ["nr_5m","nr_10m","nr_15m","nr_30m","nr_1h","nr_2h","nr_4h","nr_6h","nr_12h","nr_24h"]
HORIZON_LABELS= ["5m","10m","15m","30m","1h","2h","4h","6h","12h","24h"]

def compute_mfe_mae(rows: list[dict]) -> list[dict]:
    """Per-trade MFE/MAE proxy from horizon waypoints."""
    result = []
    for r in rows:
        waypoints = [(lbl, r.get(col)) for col, lbl in zip(HORIZON_ORDER, HORIZON_LABELS)]
        valid     = [(lbl, v) for lbl, v in waypoints if v is not None]
        if not valid:
            continue
        vals  = [v for _, v in valid]
        mfe   = max(vals)
        mae   = min(vals)
        # Time to MFE: first horizon where max is achieved
        mfe_label = next((lbl for lbl, v in valid if v == mfe), None)
        # Peak before final: max over all but last waypoint
        peak_early = max((v for _, v in valid[:-1]), default=None)
        final_ret  = valid[-1][1]
        recovered  = (peak_early is not None
                      and mfe > 0.01        # had meaningful MFE
                      and final_ret >= mfe * 0.5  # recovered at least 50% of MFE
                      )
        result.append({
            "pool":          r["pool"],
            "regime_week":   r["regime_week"],
            "activity_heat": r["activity_heat"],
            "bpr":           r["buy_pressure_ratio"],
            "wt0":           int(r["weth_is_token0"]),
            "nr_5m":         r.get("nr_5m"),
            "nr_30m":        r.get("nr_30m"),
            "nr_1h":         r.get("nr_1h"),
            "nr_24h":        r.get("nr_24h"),
            "mfe":           round(mfe, 6),
            "mae":           round(mae, 6),
            "mfe_label":     mfe_label,
            "final_ret":     round(final_ret, 6),
            "recovered":     recovered,
            "n_waypoints":   len(valid),
        })
    return result

# ── exit policy simulation ────────────────────────────────────────────────────

def simulate_policy(rows: list[dict], tp: float | None, sl: float | None,
                    trailing_pct: float | None,
                    notional: float = 10.0, cooldown_s: int = 900) -> dict:
    """
    Simulate a single exit policy across the horizon waypoints.
    Policy fires at the first waypoint where:
      - TP: return > tp
      - SL: return < -sl
      - Trailing: return < (peak_so_far - trailing_pct)
    If no policy fires, exit at last available horizon.
    """
    rrows = sorted(rows, key=lambda r: r["entry_time_sec"])
    pool_last: dict[str, float] = {}
    active: list[float] = []
    pnl_list: list[float] = []

    for r in rrows:
        t = float(r["entry_time_sec"])
        active = [x for x in active if x > t]
        if pool_last.get(r["pool"], 0) + cooldown_s > t:
            continue
        if len(active) >= 3:
            continue

        # Walk through waypoints in order, apply policy
        waypoints = [r.get(c) for c in HORIZON_ORDER]
        exit_ret  = None
        peak      = 0.0

        for v in waypoints:
            if v is None:
                continue
            peak = max(peak, v)
            # TP
            if tp is not None and v >= tp:
                exit_ret = v; break
            # SL
            if sl is not None and v <= -sl:
                exit_ret = v; break
            # Trailing
            if trailing_pct is not None and peak > 0 and v <= peak - trailing_pct:
                exit_ret = v; break
            exit_ret = v  # keep updating until policy fires or last waypoint

        if exit_ret is None:
            continue

        pool_last[r["pool"]] = t
        active.append(t + 300)
        nr_net = exit_ret - GAS_PROXY
        pnl_list.append(nr_net * notional)

    if not pnl_list:
        return {"trades": 0}
    wins   = [p for p in pnl_list if p > 0]
    losses = [-p for p in pnl_list if p <= 0]
    total_inv = len(pnl_list) * notional
    rets_r = [p / notional for p in pnl_list]
    return {
        "trades":    len(pnl_list),
        "win_rate":  round(len(wins) / len(pnl_list), 4),
        "pf":        round(sum(wins) / sum(losses), 3) if losses else None,
        "roi_pct":   round(100 * sum(pnl_list) / total_inv, 3),
        "max_dd_pct":round(100 * _max_drawdown_seq(rets_r), 3),
    }

# ── concentration checks ──────────────────────────────────────────────────────

def concentration_check(rows: list[dict], col: str) -> dict:
    """Check if long-horizon returns are driven by few pools or weeks."""
    valid = [r for r in rows if r.get(col) is not None]
    if not valid:
        return {}
    pool_cnt = Counter(r["pool"] for r in valid)
    week_cnt = Counter(r["regime_week"] for r in valid)

    # Pool-level PF
    pool_pf: dict[str, float | None] = {}
    for pool, cnt in pool_cnt.most_common(5):
        pr = [r[col] for r in valid if r["pool"] == pool]
        pos = [v for v in pr if v > 0]; neg = [-v for v in pr if v <= 0]
        pool_pf[pool[:14]] = round(sum(pos)/sum(neg), 3) if neg else None

    hhi = round(sum((c/len(valid))**2 for c in pool_cnt.values()), 4)

    return {
        "n":           len(valid),
        "unique_pools": len(pool_cnt),
        "unique_weeks": len(week_cnt),
        "hhi":         hhi,
        "top1_pool_share": round(pool_cnt.most_common(1)[0][1] / len(valid), 3),
        "top3_pool_share": round(sum(c for _, c in pool_cnt.most_common(3)) / len(valid), 3),
        "top5_pool_pf":    pool_pf,
    }

# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=== Sprint057 — Horizon Sweep ===", flush=True)

    # Load data
    print("\n[1/8] Loading q4 bundle fee_100 rows...", flush=True)
    rows = load_q4_fee100()
    print(f"  Loaded {len(rows)} fee_100 rows", flush=True)

    # Fill long horizons from CH
    print("\n[2/8] Fetching long-horizon sqrtPriceX96 from ClickHouse...", flush=True)
    fill_all_long_horizons(rows)

    # Count coverage for each horizon
    for col in HORIZON_ORDER:
        n = sum(1 for r in rows if r.get(col) is not None)
        print(f"  {col}: {n}/{len(rows)} labeled", flush=True)

    # Segments
    print("\n[3/8] Building segments...", flush=True)
    segs = _segments(rows)
    for seg, sr in segs.items():
        print(f"  {seg}: {len(sr)} rows", flush=True)

    # ── Horizon sweep matrix ──────────────────────────────────────────────────
    print("\n[4/8] Computing horizon sweep matrix...", flush=True)
    matrix_rows: list[dict] = []

    for seg, sr in segs.items():
        for col, lbl in zip(HORIZON_ORDER, HORIZON_LABELS):
            s = _horizon_stats(sr, col)
            rp = _portfolio_replay(sr, col)
            matrix_rows.append({
                "segment":   seg,
                "horizon":   lbl,
                "is_proxy":  lbl not in HORIZONS_SHORT,
                **{f"raw_{k}": v for k, v in s.items()},
                **{f"rp_{k}": v for k, v in rp.items()},
            })
            if s["n"] > 0:
                print(f"  {seg:20s} {lbl:4s}: n={s['n']:5d} hit={s['hit']:.3f} "
                      f"pf={s.get('pf','?'):6} mean={s.get('mean',0):.4f} "
                      f"trades={rp.get('trades',0)}", flush=True)

    # Save matrix
    cols = (["segment","horizon","is_proxy"] +
            [f"raw_{k}" for k in ["n","hit","pf","mean","median","max","min","loss_tail_p10"]] +
            [f"rp_{k}"  for k in ["trades","win_rate","pf","roi_pct","max_dd_pct"]])
    with open(OUT_DIR / "horizon_sweep_matrix.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader(); w.writerows(matrix_rows)
    print(f"  Wrote horizon_sweep_matrix.csv ({len(matrix_rows)} rows)", flush=True)

    # ── MFE / MAE ─────────────────────────────────────────────────────────────
    print("\n[5/8] Computing MFE / MAE...", flush=True)
    mfe_rows = compute_mfe_mae(rows)
    print(f"  {len(mfe_rows)} trades with ≥1 waypoint", flush=True)
    if mfe_rows:
        mfes = [r["mfe"] for r in mfe_rows]
        maes = [r["mae"] for r in mfe_rows]
        recov = sum(1 for r in mfe_rows if r.get("recovered", False))
        mfe_dist = Counter(r["mfe_label"] for r in mfe_rows if r.get("mfe_label"))
        print(f"  Avg MFE={statistics.mean(mfes):.4f}  Avg MAE={statistics.mean(maes):.4f}", flush=True)
        print(f"  Recovered {recov}/{len(mfe_rows)} ({100*recov/len(mfe_rows):.1f}%)", flush=True)
        print(f"  Time-to-MFE distribution: {dict(mfe_dist.most_common(5))}", flush=True)

    mfe_cols = ["pool","regime_week","activity_heat","bpr","wt0",
                "nr_5m","nr_30m","nr_1h","nr_24h","mfe","mae","mfe_label","final_ret","recovered","n_waypoints"]
    with open(OUT_DIR / "trade_path_mfe_mae.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=mfe_cols, extrasaction="ignore")
        w.writeheader(); w.writerows(mfe_rows)
    print(f"  Wrote trade_path_mfe_mae.csv", flush=True)

    # ── Exit policy simulation ────────────────────────────────────────────────
    print("\n[6/8] Exit policy simulation...", flush=True)
    policies = [
        ("fixed_5m",   None,  None,  None),
        ("fixed_15m",  None,  None,  None),  # special: use col nr_15m
        ("fixed_30m",  None,  None,  None),  # special: use col nr_30m
        ("fixed_1h",   None,  None,  None),  # special: use col nr_1h
        ("fixed_24h",  None,  None,  None),  # special: use col nr_24h
        ("tp10",       0.10,  None,  None),
        ("tp20",       0.20,  None,  None),
        ("tp30",       0.30,  None,  None),
        ("sl5",        None,  0.05,  None),
        ("sl10",       None,  0.10,  None),
        ("tp10_sl5",   0.10,  0.05,  None),
        ("tp20_sl10",  0.20,  0.10,  None),
        ("trail10",    None,  None,  0.10),
        ("trail20",    None,  None,  0.20),
        ("trail10_tp30", 0.30, None, 0.10),
    ]

    # Fixed-horizon policies use _portfolio_replay directly (cleaner)
    policy_results: list[dict] = []
    fixed_horizons = {"fixed_5m":"nr_5m","fixed_15m":"nr_15m","fixed_30m":"nr_30m",
                       "fixed_1h":"nr_1h","fixed_24h":"nr_24h"}

    for seg, sr in segs.items():
        # Fixed exits
        for pol_name, col in fixed_horizons.items():
            rp = _portfolio_replay(sr, col)
            policy_results.append({
                "segment": seg, "policy": pol_name,
                "tp": None, "sl": None, "trail": None,
                **rp,
            })
            if rp.get("trades", 0) > 0:
                print(f"  {seg:20s} {pol_name:15s}: trades={rp['trades']:4d} "
                      f"win={rp['win_rate']:.3f} pf={rp.get('pf','?'):6} "
                      f"roi={rp.get('roi_pct',0):.2f}%", flush=True)

        # Active policies
        for pol_name, tp, sl, trail in policies:
            if pol_name.startswith("fixed_"):
                continue
            rp = simulate_policy(sr, tp, sl, trail)
            policy_results.append({
                "segment": seg, "policy": pol_name,
                "tp": tp, "sl": sl, "trail": trail,
                **rp,
            })
            if rp.get("trades", 0) > 0:
                print(f"  {seg:20s} {pol_name:15s}: trades={rp['trades']:4d} "
                      f"win={rp['win_rate']:.3f} pf={rp.get('pf','?'):6} "
                      f"roi={rp.get('roi_pct',0):.2f}%", flush=True)

    # ── Long-horizon concentration ─────────────────────────────────────────────
    print("\n[7/8] Concentration checks for long horizons...", flush=True)
    conc_results: dict[str, dict] = {}
    for seg, sr in segs.items():
        for col, lbl in [("nr_1h","1h"),("nr_4h","4h"),("nr_24h","24h")]:
            conc = concentration_check(sr, col)
            conc_results[f"{seg}@{lbl}"] = conc
            if conc.get("n", 0) > 0:
                print(f"  {seg:20s} @{lbl}: n={conc['n']} pools={conc['unique_pools']} "
                      f"hhi={conc.get('hhi','?')} top1={conc.get('top1_pool_share',0):.2f}", flush=True)

    # ── Write readouts ─────────────────────────────────────────────────────────
    print("\n[8/8] Writing readouts...", flush=True)
    _write_horizon_readout(matrix_rows, mfe_rows, conc_results, segs)
    _write_exit_policy_map(policy_results, segs)
    _write_run_readout(matrix_rows, mfe_rows, policy_results, segs)
    print("Done.", flush=True)


# ── readout generators ────────────────────────────────────────────────────────

def _best_horizon(matrix_rows: list[dict], seg: str) -> tuple[str, float]:
    """Return (horizon, pf) with the best replay PF for a segment."""
    seg_rows = [r for r in matrix_rows if r["segment"] == seg and r.get("rp_trades",0) >= 20]
    if not seg_rows:
        return ("?", 0.0)
    best = max(seg_rows, key=lambda r: r.get("rp_pf") or 0)
    return (best["horizon"], best.get("rp_pf") or 0)


def _write_horizon_readout(matrix_rows, mfe_rows, conc_results, segs) -> None:
    lines = [
        "# Sprint057 — Horizon Sweep Readout",
        "",
        "> Short horizons (5m/10m/15m/30m): real QuoterV2 labels from Sprint039G q4 bundle.",
        "> Long horizons (1h/2h/4h/6h/12h/24h): sqrtPriceX96 proxy from ClickHouse (directional).",
        "",
        "## Per-Segment Horizon Summary",
        "",
    ]

    segs_order = ["fee_100_all","heat_20_50","heat_20p","weth_is_token1","bpr_lt55","composite"]
    for seg in segs_order:
        best_h, best_pf = _best_horizon(matrix_rows, seg)
        seg_rows = [r for r in matrix_rows if r["segment"] == seg]
        lines += [f"### {seg}", "", "| Horizon | N | Hit | PF | Mean | Trades | W% | RPL PF | ROI% | Max DD% |",
                  "|---------|---|-----|-----|------|--------|-----|--------|------|---------|"]
        for r in seg_rows:
            if not r.get("raw_n"):
                continue
            marker = " ★" if r["horizon"] == best_h and best_pf > 1.5 else ""
            proxy_note = " †" if r.get("is_proxy") else ""
            lines.append(
                f"| {r['horizon']}{proxy_note}{marker} "
                f"| {r.get('raw_n',0)} | {r.get('raw_hit',0):.3f} | {r.get('raw_pf','?')} "
                f"| {r.get('raw_mean',0):.4f} | {r.get('rp_trades',0)} | {r.get('rp_win_rate',0):.3f} "
                f"| {r.get('rp_pf','?')} | {r.get('rp_roi_pct',0):.2f}% | {r.get('rp_max_dd_pct',0):.1f}% |"
            )
        lines += ["", f"> Best horizon: **{best_h}** (replay PF={best_pf})", ""]

    # MFE / MAE summary
    lines += ["## MFE / MAE Analysis", ""]
    if mfe_rows:
        mfes = [r["mfe"] for r in mfe_rows]
        maes = [r["mae"] for r in mfe_rows]
        recov = sum(1 for r in mfe_rows if r.get("recovered"))
        mfe_dist = Counter(r["mfe_label"] for r in mfe_rows if r.get("mfe_label"))
        lines += [
            f"| Metric | Value |",
            f"|--------|-------|",
            f"| Avg proxy MFE | {statistics.mean(mfes):.4f} |",
            f"| Avg proxy MAE | {statistics.mean(maes):.4f} |",
            f"| MFE/MAE ratio | {statistics.mean(mfes)/abs(statistics.mean(maes)):.2f} |",
            f"| Recovery rate | {100*recov/len(mfe_rows):.1f}% |",
            "",
            "**Time to MFE distribution:**",
        ]
        for lbl, cnt in mfe_dist.most_common():
            lines.append(f"- `{lbl}`: {cnt} trades ({100*cnt/len(mfe_rows):.1f}%)")
    lines.append("")

    # Concentration
    lines += ["## Long-Horizon Concentration", "", "† proxy data subject to survivorship bias (pools with no swaps excluded).", ""]
    for key, conc in sorted(conc_results.items()):
        if not conc.get("n"):
            continue
        lines.append(f"**{key}:** n={conc['n']} pools={conc['unique_pools']} "
                     f"HHI={conc.get('hhi','?')} top1={conc.get('top1_pool_share',0):.2f}")

    lines += ["", "† proxy = sqrtPriceX96 directional proxy (not QuoterV2)"]
    (AUD_DIR / "horizon_sweep_readout.md").write_text("\n".join(lines))
    print("  Wrote horizon_sweep_readout.md", flush=True)


def _write_exit_policy_map(policy_results, segs) -> None:
    lines = [
        "# Sprint057 — Exit Policy Map",
        "",
        "Policy simulated using horizon waypoints as trigger points.",
        "NOT a continuous price path — waypoint resolution only.",
        "",
        "## fee_100_all — All Policies",
        "",
        "| Policy | Trades | Win | PF | ROI% | Max DD% |",
        "|--------|--------|-----|-----|------|---------|",
    ]
    for r in sorted(policy_results, key=lambda x: -(x.get("pf") or 0)):
        if r["segment"] != "fee_100_all" or not r.get("trades"):
            continue
        lines.append(
            f"| {r['policy']} | {r['trades']} | {r.get('win_rate',0):.3f} "
            f"| {r.get('pf','?')} | {r.get('roi_pct',0):.2f}% | {r.get('max_dd_pct',0):.1f}% |"
        )
    lines += ["", "## composite — All Policies", "",
              "| Policy | Trades | Win | PF | ROI% | Max DD% |",
              "|--------|--------|-----|-----|------|---------|"]
    for r in sorted(policy_results, key=lambda x: -(x.get("pf") or 0)):
        if r["segment"] != "composite" or not r.get("trades"):
            continue
        lines.append(
            f"| {r['policy']} | {r['trades']} | {r.get('win_rate',0):.3f} "
            f"| {r.get('pf','?')} | {r.get('roi_pct',0):.2f}% | {r.get('max_dd_pct',0):.1f}% |"
        )
    lines += ["", "## Verdict", "",
              "- Best fixed exit: see horizon_sweep_readout.md",
              "- Active policies: TP/SL/trailing may improve absolute ROI but reduce trade count"]
    (AUD_DIR / "exit_policy_map.md").write_text("\n".join(lines))
    print("  Wrote exit_policy_map.md", flush=True)


def _write_run_readout(matrix_rows, mfe_rows, policy_results, segs) -> None:
    # Find best horizon per segment
    best = {}
    for seg in segs:
        h, pf = _best_horizon(matrix_rows, seg)
        best[seg] = (h, pf)

    # Best active policy for fee_100_all
    active = [r for r in policy_results
              if r["segment"] == "fee_100_all" and not r["policy"].startswith("fixed")
              and r.get("trades", 0) >= 10]
    best_pol = max(active, key=lambda r: r.get("pf") or 0) if active else {}

    lines = [
        "# Sprint057 — fee_100 Horizon Sweep Run Readout",
        "",
        f"**Date:** {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "",
        "## 1. Best Hold Period by Segment",
        "",
        "| Segment | Best Horizon | Replay PF |",
        "|---------|--------------|-----------|",
    ]
    for seg, (h, pf) in best.items():
        lines.append(f"| {seg} | {h} | {pf} |")

    if mfe_rows:
        mfes = [r["mfe"] for r in mfe_rows]
        maes = [r["mae"] for r in mfe_rows]
        mfe_dist = Counter(r["mfe_label"] for r in mfe_rows if r.get("mfe_label"))
        dom_mfe_h = mfe_dist.most_common(1)[0][0] if mfe_dist else "?"
        recov = 100 * sum(1 for r in mfe_rows if r.get("recovered")) / len(mfe_rows)
        lines += [
            "",
            "## 2. MFE / MAE Summary",
            "",
            f"- Avg MFE (proxy): **{statistics.mean(mfes):.4f}** — dominant at **{dom_mfe_h}**",
            f"- Avg MAE (proxy): **{statistics.mean(maes):.4f}**",
            f"- MFE/|MAE| ratio: **{statistics.mean(mfes)/abs(statistics.mean(maes)):.2f}**",
            f"- Recovery rate: **{recov:.1f}%** of trades recover to ≥50% of their MFE by 24h",
        ]

    lines += [
        "",
        "## 3. Exit Policy Verdict",
        "",
        f"Best active policy (fee_100_all): **{best_pol.get('policy','?')}** "
        f"PF={best_pol.get('pf','?')} ROI={best_pol.get('roi_pct',0):.2f}%",
        "",
        "## 4. Composite Signal Best Horizon",
        "",
    ]
    comp_rows = [r for r in matrix_rows if r["segment"] == "composite" and r.get("raw_n",0) >= 20]
    for r in sorted(comp_rows, key=lambda x: -(x.get("rp_pf") or 0))[:3]:
        lines.append(f"- **{r['horizon']}**: n={r['raw_n']} hit={r['raw_hit']:.3f} "
                     f"PF={r['raw_pf']} replay_PF={r['rp_pf']}")

    lines += ["", "## 5. Paper Shadow Update",
              "Update EXIT_HORIZONS to include 1h/2h/4h/6h/12h/24h.",
              "",
              "## 6. Next Steps",
              "- If PF improves past 30m: update live pilot hold to best horizon",
              "- If TP policy improves Calmar: add TP gate to paper shadow",
              "- If 24h concentration > 50% single pool: add per-pool exposure cap"]
    (RUN_DIR / "run-057-fee100-horizon-sweep-readout.md").write_text("\n".join(lines))
    print("  Wrote run-057-fee100-horizon-sweep-readout.md", flush=True)


if __name__ == "__main__":
    main()
