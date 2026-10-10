"""
Sprint 072 — A10: 5,431-Pool Universe Momentum Breakout Screener
================================================================
Scans the full pool_features_5m universe (5,431 pools tracked since 2025-10)
to surface pools exhibiting sustained buy pressure + positive price velocity
(momentum breakout pattern) that are candidates for paper shadow expansion.

Two-phase scan:
  Phase A — Feature-signal screen (pools with pool_features_5m data in last 24h)
             Computes Momentum Breakout Score (MBS) per pool.
  Phase B — Long-tail new-pool scan (pools with sudden swap activity in last 2h
             but NO pool_features_5m data → brand-new breakouts not yet indexed)

Output
------
  sprint072_alpha_waterfall/sprint072_a10_momentum_watchlist.json
  sprint072_alpha_waterfall/sprint072_a10_momentum_screener_readout.md

Usage
-----
  python3 scripts/sprint072_a10_momentum_screener.py
"""
from __future__ import annotations
import json, math, subprocess, time, datetime, sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO      = Path("/opt/EATLQ")
OUT_DIR   = REPO / "mvp/audits/sprint072_alpha_waterfall"
OUT_JSON  = OUT_DIR / "sprint072_a10_momentum_watchlist.json"
OUT_MD    = OUT_DIR / "sprint072_a10_momentum_screener_readout.md"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Known paper shadow pools (exclude from watchlist — already tracked)
PAPER_SHADOW_POOLS = {
    "0x0a423a23c97ce0e4296d13c2b846f8b8b66085d6",
    "0x369adee33d0d5c5c4cbaabf75965976c60909451",
    "0xa5a98447c7e10dbf35da4a4c4ba151539c200344",
    "0xa68c78d969e51ac10d488c6d1da97541151f599c",
    "0xb4cb800910b228ed3d0834cf79d697127bbb00e5",  # known 0%WR — excluded
}

# Known large LP / infrastructure pools (not meme candidates)
EXCLUDE_INFRA = {
    "0xd0b53d9277642d899df5c87a3966a349a798f224",  # WETH/USDC primary LP (A4)
}

# MBS scoring parameters
MBS_WIN_BULLISH_PCT_WEIGHT = 0.40
MBS_AVG_BPR_WEIGHT         = 0.20
MBS_PEAK_HEAT_LOG_WEIGHT   = 0.25
MBS_RECENCY_WEIGHT         = 0.15

# Thresholds
MIN_WINDOWS_24H   = 5    # minimum feature windows to trust signal
MIN_WIN_BULLISH   = 2    # minimum bullish windows required
MIN_AVG_BPR       = 0.35 # minimum average buy pressure ratio

# Phase B new-pool breakout params
PHASE_B_LOOKBACK_H   = 2
PHASE_B_MIN_SWAPS    = 5   # minimum swaps in last 2h to flag
PHASE_B_MAX_AGE_H    = 48  # only consider pools < 48h old

# ---------------------------------------------------------------------------
# ClickHouse helper
# ---------------------------------------------------------------------------
def ch(sql: str, *, timeout: int = 90, mem_gb: int = 12) -> tuple[str, str]:
    args = [
        "docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
        "--database", "eatlq", "--format", "TSV",
        f"--max_memory_usage={mem_gb}000000000",
        "--query", sql,
    ]
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip(), r.stderr.strip()


def log(msg: str) -> None:
    ts = datetime.datetime.now(tz=datetime.timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------
def utc_str(ts_epoch: int) -> str:
    return datetime.datetime.fromtimestamp(
        ts_epoch, tz=datetime.timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Momentum Breakout Score (MBS)
# ---------------------------------------------------------------------------
def compute_mbs(
    total_windows: int,
    win_bullish: int,
    avg_bpr: float,
    avg_heat: float,
    peak_heat: float,
    seconds_since_last_window: float,
    pool_age_sec: float,
) -> float:
    """
    MBS is a composite momentum score [0,1] indicating breakout conviction.

    Components:
      - bullish_pct    : fraction of windows that are bullish (BPR≥0.55 AND pv>0)
      - avg_bpr        : sustained buy-side pressure
      - peak_heat_log  : intensity ceiling normalised to [0,1] via log scale
      - recency        : penalises pools whose last feature window was >6h ago
      - age_bonus      : fresh pools (<48h) receive extra weight per paper shadow SOP
    """
    if total_windows == 0:
        return 0.0

    bullish_pct   = win_bullish / total_windows           # [0,1]
    bpr_score     = min(avg_bpr, 1.0)                     # [0,1]
    heat_log      = math.log1p(peak_heat) / math.log1p(350.0)  # normalise to max ~350
    heat_log      = min(heat_log, 1.0)

    # Recency: full weight if <1h, decay to 0.1 at 12h
    if seconds_since_last_window <= 3600:
        recency = 1.0
    elif seconds_since_last_window <= 10800:   # <3h
        recency = 0.80
    elif seconds_since_last_window <= 21600:   # <6h
        recency = 0.55
    elif seconds_since_last_window <= 43200:   # <12h
        recency = 0.30
    else:
        recency = 0.10

    base = (
        MBS_WIN_BULLISH_PCT_WEIGHT * bullish_pct
        + MBS_AVG_BPR_WEIGHT       * bpr_score
        + MBS_PEAK_HEAT_LOG_WEIGHT * heat_log
        + MBS_RECENCY_WEIGHT       * recency
    )

    # Age bonus: brand-new pools have higher breakout potential
    if 0 < pool_age_sec <= 48 * 3600:
        age_bonus = 1.5
    elif 0 < pool_age_sec <= 7 * 86400:
        age_bonus = 1.15
    else:
        age_bonus = 1.0

    return round(base * age_bonus, 4)


# ---------------------------------------------------------------------------
# Phase A: Feature-signal screen (pool_features_5m, last 24h)
# ---------------------------------------------------------------------------
def run_phase_a(now_ts: int) -> list[dict]:
    log("Phase A: querying pool_features_5m (last 24h)…")
    dt_24h = utc_str(now_ts - 86400)

    sql = f"""
    SELECT
        f.pool_address,
        f.total_windows,
        f.win_high_bpr,
        f.win_bullish,
        f.avg_bpr,
        f.avg_pv,
        f.avg_heat,
        f.peak_heat,
        f.last_window_epoch,
        d.token0_symbol,
        d.token1_symbol,
        d.fee_bps,
        d.has_weth_leg,
        d.pool_type,
        toUnixTimestamp(d.discovered_at) AS discovered_ts
    FROM (
        SELECT
            pool_address,
            count()                                                           AS total_windows,
            countIf(buy_pressure_ratio >= 0.55)                               AS win_high_bpr,
            countIf(buy_pressure_ratio >= 0.55 AND price_velocity_5m > 0)    AS win_bullish,
            avg(buy_pressure_ratio)                                            AS avg_bpr,
            avg(price_velocity_5m)                                             AS avg_pv,
            avg(activity_heat)                                                 AS avg_heat,
            max(activity_heat)                                                 AS peak_heat,
            toUnixTimestamp(max(window_start))                                 AS last_window_epoch
        FROM eatlq.pool_features_5m
        WHERE window_start >= toDateTime('{dt_24h}', 'UTC')
        GROUP BY pool_address
        HAVING total_windows >= {MIN_WINDOWS_24H}
    ) f
    LEFT JOIN eatlq.discovered_pools d ON f.pool_address = d.pool_address
    ORDER BY win_bullish DESC, avg_heat DESC
    """

    out, err = ch(sql)
    if not out:
        log(f"Phase A error: {err}")
        return []

    rows = []
    for line in out.split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 15:
            continue
        try:
            pool         = parts[0]
            total_w      = int(parts[1])
            win_hbpr     = int(parts[2])
            win_bull     = int(parts[3])
            avg_bpr      = float(parts[4])
            avg_pv       = float(parts[5])
            avg_heat     = float(parts[6])
            peak_heat    = float(parts[7])
            last_w_epoch = int(parts[8])
            tok0         = parts[9] or "?"
            tok1         = parts[10] or "?"
            fee_bps      = int(parts[11]) if parts[11] else 0
            has_weth     = int(parts[12]) if parts[12] else 0
            pool_type    = parts[13] or "?"
            disc_ts      = int(parts[14]) if parts[14] else 0
        except (ValueError, IndexError):
            continue

        # Skip excluded pools
        if pool in PAPER_SHADOW_POOLS or pool in EXCLUDE_INFRA:
            continue

        age_sec   = (now_ts - disc_ts) if disc_ts > 1000000 else 0
        secs_ago  = now_ts - last_w_epoch

        mbs = compute_mbs(
            total_windows=total_w,
            win_bullish=win_bull,
            avg_bpr=avg_bpr,
            avg_heat=avg_heat,
            peak_heat=peak_heat,
            seconds_since_last_window=secs_ago,
            pool_age_sec=age_sec,
        )

        pair = f"{tok0}/{tok1}"

        rows.append({
            "pool_address":    pool,
            "phase":           "A",
            "pair":            pair,
            "fee_bps":         fee_bps,
            "has_weth_leg":    has_weth,
            "pool_type":       pool_type,
            "pool_age_h":      round(age_sec / 3600, 1) if age_sec > 0 else None,
            "total_windows":   total_w,
            "win_bullish":     win_bull,
            "win_bullish_pct": round(win_bull / total_w, 3) if total_w > 0 else 0,
            "win_high_bpr":    win_hbpr,
            "avg_bpr":         round(avg_bpr, 4),
            "avg_pv":          round(avg_pv, 6),
            "avg_heat":        round(avg_heat, 2),
            "peak_heat":       int(peak_heat),
            "last_window_ago_min": round(secs_ago / 60, 1),
            "mbs":             mbs,
            # Eligibility flags
            "qualifies_weth":  has_weth == 1,
            "qualifies_fee":   fee_bps <= 100,
            "qualifies_momentum": win_bull >= MIN_WIN_BULLISH and avg_bpr >= MIN_AVG_BPR,
        })

    rows.sort(key=lambda r: r["mbs"], reverse=True)
    log(f"Phase A: {len(rows)} candidate pools scored (excludes known paper shadow + infra).")
    return rows


# ---------------------------------------------------------------------------
# Phase B: Long-tail new-pool breakout scan (uniswap_v3_swaps, last 2h)
# ---------------------------------------------------------------------------
def run_phase_b(now_ts: int, phase_a_pools: set[str]) -> list[dict]:
    log("Phase B: long-tail new-pool scan (last 2h swap activity)…")
    dt_2h  = utc_str(now_ts - PHASE_B_LOOKBACK_H * 3600)
    dt_48h = utc_str(now_ts - PHASE_B_MAX_AGE_H * 3600)

    # Pools with new burst activity in last 2h that have NOT appeared in Phase A
    sql = f"""
    SELECT
        s.pool_address,
        count()                                    AS swap_count_2h,
        countIf(toFloat64OrZero(amount1_raw) > 0)  AS buy_count,
        toUnixTimestamp(d.discovered_at)            AS discovered_ts,
        d.token0_symbol,
        d.token1_symbol,
        d.fee_bps,
        d.has_weth_leg,
        d.pool_type
    FROM eatlq.uniswap_v3_swaps s
    INNER JOIN eatlq.discovered_pools d ON s.pool_address = d.pool_address
    WHERE s.received_at_ms >= toDateTime('{dt_2h}', 'UTC')
      AND d.discovered_at  >= toDateTime('{dt_48h}', 'UTC')
      AND d.has_weth_leg    = 1
    GROUP BY s.pool_address, d.discovered_at, d.token0_symbol, d.token1_symbol,
             d.fee_bps, d.has_weth_leg, d.pool_type
    HAVING swap_count_2h >= {PHASE_B_MIN_SWAPS}
    ORDER BY swap_count_2h DESC
    LIMIT 30
    """

    out, err = ch(sql)
    if not out:
        log(f"Phase B warning (may be empty if no new pools): {err[:200]}")
        return []

    rows = []
    for line in out.split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 9:
            continue
        try:
            pool        = parts[0]
            swap_cnt    = int(parts[1])
            buy_cnt     = int(parts[2])
            disc_ts     = int(parts[3]) if parts[3] else 0
            tok0        = parts[4] or "?"
            tok1        = parts[5] or "?"
            fee_bps     = int(parts[6]) if parts[6] else 0
            has_weth    = int(parts[7]) if parts[7] else 0
            pool_type   = parts[8] or "?"
        except (ValueError, IndexError):
            continue

        if pool in PAPER_SHADOW_POOLS or pool in EXCLUDE_INFRA:
            continue
        if pool in phase_a_pools:
            continue  # already in Phase A

        age_sec = (now_ts - disc_ts) if disc_ts > 1000000 else 0
        bpr_est = buy_cnt / swap_cnt if swap_cnt > 0 else 0.5

        rows.append({
            "pool_address":    pool,
            "phase":           "B",
            "pair":            f"{tok0}/{tok1}",
            "fee_bps":         fee_bps,
            "has_weth_leg":    has_weth,
            "pool_type":       pool_type,
            "pool_age_h":      round(age_sec / 3600, 1) if age_sec > 0 else None,
            "swap_count_2h":   swap_cnt,
            "buy_count_2h":    buy_cnt,
            "bpr_est_2h":      round(bpr_est, 3),
            "mbs":             round(bpr_est * math.log1p(swap_cnt) / math.log1p(100) * 1.5, 4),
            "qualifies_weth":  has_weth == 1,
            "qualifies_fee":   fee_bps <= 100,
            "qualifies_momentum": swap_cnt >= PHASE_B_MIN_SWAPS and bpr_est >= 0.55,
            "note":            "Phase B: no pool_features_5m data — raw swap burst only",
        })

    rows.sort(key=lambda r: r["mbs"], reverse=True)
    log(f"Phase B: {len(rows)} new breakout pools found.")
    return rows


# ---------------------------------------------------------------------------
# Additional context: total pool_features_5m universe size
# ---------------------------------------------------------------------------
def get_universe_stats() -> dict:
    log("Fetching universe stats…")
    stats: dict = {}

    out1, _ = ch("SELECT uniqExact(pool_address) FROM eatlq.pool_features_5m")
    stats["total_unique_pools_ever"] = int(out1) if out1.isdigit() else None

    out2, _ = ch("SELECT count() FROM eatlq.discovered_pools WHERE has_weth_leg=1")
    stats["discovered_weth_pools"] = int(out2) if out2.isdigit() else None

    out3, _ = ch("SELECT count() FROM eatlq.discovered_pools")
    stats["discovered_pools_total"] = int(out3) if out3.isdigit() else None

    now_ts = int(time.time())
    dt_24h = utc_str(now_ts - 86400)
    out4, _ = ch(f"""
    SELECT uniqExact(pool_address) FROM eatlq.pool_features_5m
    WHERE window_start >= toDateTime('{dt_24h}', 'UTC')
    """, mem_gb=12)
    stats["pools_active_24h"] = int(out4) if out4.isdigit() else None

    dt_6h = utc_str(now_ts - 6 * 3600)
    out5, _ = ch(f"""
    SELECT uniqExact(pool_address) FROM eatlq.pool_features_5m
    WHERE window_start >= toDateTime('{dt_6h}', 'UTC')
    """, mem_gb=12)
    stats["pools_active_6h"] = int(out5) if out5.isdigit() else None

    return stats


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
def build_watchlist_json(
    phase_a: list[dict],
    phase_b: list[dict],
    universe_stats: dict,
    run_ts: int,
) -> dict:
    all_rows = phase_a + phase_b
    all_rows.sort(key=lambda r: r["mbs"], reverse=True)

    # Top-20 overall
    top20 = all_rows[:20]

    # Fully qualified top list (WETH + fee ≤100 + momentum)
    qualified = [
        r for r in all_rows
        if r.get("qualifies_weth") and r.get("qualifies_fee") and r.get("qualifies_momentum")
    ]

    return {
        "run_at_utc":          datetime.datetime.fromtimestamp(run_ts, tz=datetime.timezone.utc).isoformat(),
        "universe_stats":      universe_stats,
        "phase_a_count":       len(phase_a),
        "phase_b_count":       len(phase_b),
        "qualified_count":     len(qualified),
        "top20_by_mbs":        top20,
        "fully_qualified":     qualified,
        "all_candidates":      all_rows,
        "methodology": {
            "mbs_formula": "base_score × age_bonus",
            "base_score":  "0.40×(win_bullish/total_windows) + 0.20×avg_bpr + 0.25×log(peak_heat+1)/log(351) + 0.15×recency",
            "recency":     "1.0 if <1h, 0.80 if <3h, 0.55 if <6h, 0.30 if <12h, 0.10 else",
            "age_bonus":   "1.50 if <48h, 1.15 if <7d, 1.00 else",
            "qualifications": {
                "weth":     "has_weth_leg=1",
                "fee":      "fee_bps ≤ 100 (i.e. ≤0.01% Uniswap tier)",
                "momentum": f"win_bullish ≥ {MIN_WIN_BULLISH} AND avg_bpr ≥ {MIN_AVG_BPR}",
            },
        },
    }


def format_row_md(i: int, r: dict) -> str:
    phase = r.get("phase", "?")
    pair  = r.get("pair", "?/?")
    fee   = r.get("fee_bps", 0)
    age   = f"{r.get('pool_age_h', '?')}h" if r.get("pool_age_h") is not None else "?"
    heat  = r.get("avg_heat", 0)
    peak  = r.get("peak_heat", 0)
    bpr   = r.get("avg_bpr", r.get("bpr_est_2h", 0))
    bull  = r.get("win_bullish", r.get("buy_count_2h", "?"))
    total = r.get("total_windows", r.get("swap_count_2h", "?"))
    mbs   = r.get("mbs", 0)
    pool  = r.get("pool_address", "")[:10]
    weth  = "✓" if r.get("qualifies_weth") else "✗"
    fee_q = "✓" if r.get("qualifies_fee") else "✗"
    mom   = "✓" if r.get("qualifies_momentum") else "✗"

    return (
        f"| {i:2d} | {pool}… | {pair:<18} | {fee:>4}bps | {age:>6} | "
        f"{heat:>5.1f} | {peak:>4} | {bpr:.3f} | {bull}/{total} | "
        f"{mbs:.4f} | {phase} | {weth} {fee_q} {mom} |"
    )


def write_readout_md(
    output: dict,
    run_ts: int,
    out_path: Path,
) -> None:
    ts_str = datetime.datetime.fromtimestamp(run_ts, tz=datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )
    us = output["universe_stats"]
    top20 = output["top20_by_mbs"]
    qual  = output["fully_qualified"]

    lines = [
        f"# Sprint 072 A10 — 5,431-Pool Momentum Breakout Screener",
        f"",
        f"**Run:** {ts_str}  ",
        f"**Phase A pools scored:** {output['phase_a_count']}  ",
        f"**Phase B new pools found:** {output['phase_b_count']}  ",
        f"**Fully qualified candidates:** {output['qualified_count']}  ",
        f"",
        f"## Universe Statistics",
        f"",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total pools ever tracked (pool_features_5m) | {us.get('total_unique_pools_ever', '?')} |",
        f"| Discovered WETH-legged pools | {us.get('discovered_weth_pools', '?')} |",
        f"| Total discovered pools | {us.get('discovered_pools_total', '?')} |",
        f"| Pools with feature data last 24h | {us.get('pools_active_24h', '?')} |",
        f"| Pools with feature data last 6h | {us.get('pools_active_6h', '?')} |",
        f"",
        f"## Methodology — Momentum Breakout Score (MBS)",
        f"",
        f"```",
        f"MBS = base_score × age_bonus",
        f"",
        f"base_score = 0.40×(win_bullish/total_windows)   [sustained bullish windows]",
        f"           + 0.20×avg_bpr                        [buy pressure intensity]",
        f"           + 0.25×log(peak_heat+1)/log(351)      [peak activity spike]",
        f"           + 0.15×recency                        [how recently active]",
        f"",
        f"recency  = 1.00 if last_window < 1h ago",
        f"         = 0.80 if last_window < 3h ago",
        f"         = 0.55 if last_window < 6h ago",
        f"         = 0.30 if last_window < 12h ago",
        f"         = 0.10 otherwise",
        f"",
        f"age_bonus = 1.50 if pool_age < 48h (fresh meme candidate)",
        f"          = 1.15 if pool_age < 7d",
        f"          = 1.00 otherwise",
        f"",
        f"Qualification gates:",
        f"  WETH:     has_weth_leg = 1",
        f"  Fee:      fee_bps ≤ 100 (Uniswap 0.01% tier)",
        f"  Momentum: win_bullish ≥ 2 AND avg_bpr ≥ 0.35",
        f"```",
        f"",
        f"## Top-20 Candidates by MBS",
        f"",
        f"Columns: WETH ✓/✗ | Fee ✓/✗ | Momentum ✓/✗",
        f"",
        f"| Rank | Pool (prefix) | Pair | Fee | Age | Avg Heat | Peak | BPR | Bull/Total | MBS | Ph | Qualify |",
        f"|------|--------------|------|-----|-----|----------|------|-----|-----------|-----|----|----|",
    ]

    for i, r in enumerate(top20, 1):
        lines.append(format_row_md(i, r))

    lines += [
        f"",
        f"## Fully Qualified Candidates",
        f"(has_weth_leg=1 AND fee_bps≤100 AND win_bullish≥2 AND avg_bpr≥0.35)",
        f"",
    ]

    if qual:
        lines += [
            f"| Rank | Pool | Pair | Fee | Age | MBS | Notes |",
            f"|------|------|------|-----|-----|-----|-------|",
        ]
        for i, r in enumerate(qual, 1):
            pair = r.get("pair", "?/?")
            age  = f"{r.get('pool_age_h', '?')}h"
            mbs  = r.get("mbs", 0)
            fee  = r.get("fee_bps", 0)
            pool = r.get("pool_address", "")
            note = r.get("note", "")
            lines.append(
                f"| {i} | `{pool}` | {pair} | {fee}bps | {age} | {mbs:.4f} | {note} |"
            )
    else:
        lines.append("*No pools passed all qualification gates simultaneously.*")

    lines += [
        f"",
        f"## Alpha Assessment",
        f"",
        f"### Key Findings",
        f"",
    ]

    if qual:
        lines.append(f"- **{len(qual)} pools** pass all three qualification gates.")
        for r in qual[:3]:
            pair = r.get("pair", "?/?")
            age  = r.get("pool_age_h", "?")
            pool = r.get("pool_address", "")
            mbs  = r.get("mbs", 0)
            bull = r.get("win_bullish", "?")
            tot  = r.get("total_windows", "?")
            bpr  = r.get("avg_bpr", 0)
            lines.append(
                f"- `{pool}` ({pair}, {age}h old): MBS={mbs:.4f}, "
                f"bull={bull}/{tot}, avg_bpr={bpr:.3f}"
            )
    else:
        lines += [
            "- **0 pools** pass all three gates simultaneously.",
            "- Current paper shadow pools use fee=1bps (Uniswap 0.01% tier).",
            "- All new active pools with has_weth_leg=1 are using fee=30bps (0.3% tier).",
            "- Gate relaxation: consider accepting fee_bps ≤ 30 for watchlist.",
        ]

    # Fee-relaxed candidates (WETH + fee ≤ 30)
    relaxed = [
        r for r in output["all_candidates"]
        if r.get("qualifies_weth") and r.get("fee_bps", 9999) <= 30 and r.get("qualifies_momentum")
    ]

    if relaxed:
        lines += [
            f"",
            f"### Fee-Relaxed Candidates (WETH + fee ≤ 30bps + momentum)",
            f"",
            f"| # | Pool | Pair | Fee | Age | MBS | Bull/Total | avg_bpr |",
            f"|---|------|------|-----|-----|-----|-----------|---------|",
        ]
        for i, r in enumerate(relaxed[:10], 1):
            pair = r.get("pair", "?/?")
            age  = f"{r.get('pool_age_h', '?')}h"
            mbs  = r.get("mbs", 0)
            fee  = r.get("fee_bps", 0)
            pool = r.get("pool_address", "")
            bull = r.get("win_bullish", "?")
            tot  = r.get("total_windows", "?")
            bpr  = r.get("avg_bpr", r.get("bpr_est_2h", 0))
            lines.append(
                f"| {i} | `{pool}` | {pair} | {fee}bps | {age} | {mbs:.4f} | {bull}/{tot} | {bpr:.3f} |"
            )

    lines += [
        f"",
        f"### Recommendation",
        f"",
        f"| Action | Rationale |",
        f"|--------|-----------|",
    ]

    if qual:
        lines.append("| Promote to paper shadow watchlist | Qualified pools meet all gates |")
    if relaxed:
        lines.append("| Expand fee gate to 30bps and add top-3 relaxed candidates | All new meme pools are on 0.3% tier |")

    lines += [
        "| Re-run screener every 6h | Momentum windows shift — fresh pools appear at any time |",
        "| Track 0xdd746b5a (discovery OOM candidate) | Already seen in sprint072 diagnostics, sustained activity |",
        f"",
        f"---",
        f"*Generated by sprint072_a10_momentum_screener.py at {ts_str}*",
    ]

    out_path.write_text("\n".join(lines))
    log(f"Readout written → {out_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    run_ts = int(time.time())
    log("=" * 60)
    log("Sprint 072 A10 — Momentum Breakout Screener")
    log("=" * 60)

    # Universe stats
    universe_stats = get_universe_stats()
    log(f"Universe: {universe_stats}")

    # Phase A
    phase_a = run_phase_a(run_ts)
    phase_a_pool_set = {r["pool_address"] for r in phase_a}

    # Phase B
    phase_b = run_phase_b(run_ts, phase_a_pool_set)

    # Build output
    output = build_watchlist_json(phase_a, phase_b, universe_stats, run_ts)

    # Write JSON
    OUT_JSON.write_text(json.dumps(output, indent=2, default=str))
    log(f"Watchlist JSON → {OUT_JSON}")

    # Write markdown readout
    write_readout_md(output, run_ts, OUT_MD)

    # Summary to stdout
    log("")
    log("=" * 60)
    log(f"RESULTS SUMMARY")
    log(f"  Universe (pool_features_5m total): {universe_stats.get('total_unique_pools_ever')}")
    log(f"  Active last 24h: {universe_stats.get('pools_active_24h')}")
    log(f"  Phase A scored: {len(phase_a)}")
    log(f"  Phase B new pools: {len(phase_b)}")
    log(f"  Fully qualified: {output['qualified_count']}")
    log("")

    top5 = output["top20_by_mbs"][:5]
    log("Top 5 by MBS:")
    for i, r in enumerate(top5, 1):
        log(
            f"  {i}. {r['pool_address'][:12]}… {r['pair']:<18} "
            f"fee={r.get('fee_bps','?')}bps age={r.get('pool_age_h','?')}h "
            f"MBS={r['mbs']:.4f} bull={r.get('win_bullish','?')}/{r.get('total_windows','?')} "
            f"bpr={r.get('avg_bpr',0):.3f}"
        )
    log("=" * 60)


if __name__ == "__main__":
    main()
