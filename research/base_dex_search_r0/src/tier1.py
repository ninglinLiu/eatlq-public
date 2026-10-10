"""Tier 1: paper forward return vs random, by (venue, size, horizon) family.

Keystone production features are not on the year panel → ks_as_is / p_fatal_* /
ks_complement are KS_INAPPLICABLE (recorded, not faked). Stand-in selection
rules (flow / path / liq / trailing volume) still run, plus random.

1m is DATA_GAP. 10m is proxied by one 15-minute bar from edge.rot_15m2.
univ4 is the confirmation cell from the existing V4 report, not a new backtest.

Paper returns with |ret| > MAX_ABS_RET are dropped (round-46 tick overflow),
not clipped and not counted as winners.
"""
import json
import subprocess
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/root/base_dex_search_r0/src")
from params import (
    COST_CAP_BPS, HORIZON_HOURS, MAX_ABS_RET, MAX_LINEAR_PX, MIN_PX, OUT, SIZES,
    slots,
)


def ch_parquet(query, timeout=1800):
    r = subprocess.run(
        ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
         "--max_memory_usage=30000000000", "--query", query + " FORMAT Parquet"],
        capture_output=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(r.stderr.decode()[-3000:])
    import io
    return pd.read_parquet(io.BytesIO(r.stdout))


def as_dt(s):
    if np.issubdtype(s.dtype, np.datetime64):
        return pd.to_datetime(s)
    if str(s.dtype) in ("uint32", "int32", "int64", "uint64"):
        return pd.to_datetime(s.astype("int64"), unit="s")
    return pd.to_datetime(s)


def as_day(s):
    """ClickHouse Date arrives as days-since-epoch (uint16/uint32)."""
    if np.issubdtype(s.dtype, np.datetime64):
        return pd.to_datetime(s).dt.normalize()
    if str(s.dtype) in ("uint16", "int16", "uint32", "int32", "uint64", "int64"):
        mx = int(pd.Series(s).max() or 0)
        unit = "D" if mx < 100000 else "s"
        return pd.to_datetime(s.astype("int64"), unit=unit).dt.normalize()
        return pd.to_datetime(s).dt.normalize()


def boot_mean(x, n=800, seed=3):
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    if len(x) < 20:
        return float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    draws = rng.choice(x, size=(n, len(x)), replace=True).mean(axis=1)
    lo, hi = np.quantile(draws, [0.025, 0.975])
    return float(x.mean()), float(lo), float(hi)


def cost_at(cost50, fee, size):
    """Scale $50 measured cost to $25: fee stays, slippage halves."""
    slip = np.maximum(cost50 - fee / 100.0, 0.0)
    return fee / 100.0 + slip * (size / 50.0)


def attach_fwd(sub, h_hours, price="linear"):
    """Add ret (time exit) and optional path-exit columns. Drops junk ticks."""
    h = max(int(round(h_hours)), 1)
    sub = sub.sort_values(["pool", "bar"]).copy()
    px = sub["close"].astype(float).to_numpy()
    g = sub.groupby("pool", sort=False)["close"]
    F = np.column_stack([g.shift(-i).to_numpy().astype(float) for i in range(1, h + 1)])
    if price == "sqrt":
        R = (F / px[:, None]) ** 2 - 1.0
        px_ok = np.isfinite(px) & (px > 0)
    else:
        R = F / px[:, None] - 1.0
        px_ok = np.isfinite(px) & (px >= MIN_PX) & (px <= MAX_LINEAR_PX)
        fwd_ok = np.isfinite(F) & (F >= MIN_PX) & (F <= MAX_LINEAR_PX)
        R = np.where(fwd_ok, R, np.nan)
    sane = np.isfinite(R) & (R >= -1.0) & (R <= MAX_ABS_RET)
    time_ok = px_ok & sane[:, -1]
    path_ok = px_ok & sane.all(axis=1)
    sub["ret"] = np.where(time_ok, R[:, -1], np.nan)
    # path exits (collapse to time when h==1). Missing intermediate bars
    # fall back to the time exit so a sparse pool is not silently dropped.
    hit_sl = (R <= -0.35) & sane
    hit_ks = (R <= -0.50) & sane
    hit_tp = (R >= 0.50) & sane
    idx = np.arange(len(R))

    def first_or_last(hit):
        any_hit = path_ok & hit.any(axis=1)
        j = hit.argmax(axis=1)
        return np.where(any_hit, R[idx, j], np.where(time_ok, R[:, -1], np.nan))

    sub["ret_time"] = sub["ret"]
    sub["ret_time_only"] = sub["ret"]
    sub["ret_sl_only"] = first_or_last(hit_sl)
    sub["ret_ks_50_4h"] = first_or_last(hit_ks)
    hit_tpsl = hit_tp | hit_sl
    sub["ret_tp_sl"] = first_or_last(hit_tpsl)
    return sub


def summarize(nets, bars, mid):
    nets = np.asarray(nets, float)
    ok = np.isfinite(nets)
    nets, bars = nets[ok], np.asarray(bars)[ok]
    if len(nets) < 30:
        return None
    m, lo, hi = boot_mean(nets)
    h1 = nets[bars < mid] if mid is not None else nets
    h2 = nets[bars >= mid] if mid is not None else nets
    h1m = float(np.mean(h1)) if len(h1) else float("nan")
    h2m = float(np.mean(h2)) if len(h2) else float("nan")
    return dict(
        n=int(len(nets)), mean=m, lo=lo, hi=hi,
        win=float((nets > 0).mean()),
        h1_mean=h1m, h2_mean=h2m,
        same_sign=bool(np.isfinite(h1m) and np.isfinite(h2m) and (h1m * h2m) > 0),
    )


def score_take(sub, size, rule, rng, ret_col="ret"):
    k = slots(size)
    if rule == "random":
        sc = rng.random(len(sub))
    elif rule == "vol":
        sc = sub["vol_weth"].to_numpy(dtype=float)
    elif rule == "flow_only":
        sc = np.abs(sub["net_weth"].to_numpy(dtype=float)) if "net_weth" in sub else np.abs(
            sub["vol_weth"].to_numpy(dtype=float))
    elif rule == "path_only":
        sc = sub["n_swaps"].to_numpy(dtype=float)
    elif rule == "liq_only":
        sc = -sub["cost_p50"].to_numpy(dtype=float)
    else:
        return "KS_INAPPLICABLE"
    rk = pd.Series(sc, index=sub.index).groupby(sub["bar"], sort=False).rank(
        ascending=False, method="first")
    take = sub[rk <= k]
    if "c" in take.columns:
        nets = take[ret_col].to_numpy(dtype=float) - 2.0 * take["c"].to_numpy(dtype=float) / 1e4
    else:
        nets = take[ret_col].to_numpy(dtype=float) - 2.0 * take["cost_p50"].to_numpy(dtype=float) / 1e4
    return take, nets


def family_block(prepared, size, rules, rng, mid, exits=("ret_time",)):
    out = {}
    for rule in rules:
        out[rule] = {}
        for ex in exits:
            got = score_take(prepared, size, rule, rng, ret_col=ex)
            if got == "KS_INAPPLICABLE":
                out[rule] = "KS_INAPPLICABLE"
                break
            take, nets = got
            st = summarize(nets, take["bar"].to_numpy(), mid)
            if st is None:
                out[rule][ex] = dict(status="UNDERPOWERED", n=int(np.isfinite(nets).sum()))
            else:
                out[rule][ex] = st
    return out


def flatten_rules(block):
    """Keep backward-compatible rules[name] = stats for the time exit."""
    flat = {}
    for name, st in block.items():
        if st == "KS_INAPPLICABLE" or not isinstance(st, dict):
            flat[name] = st
            continue
        time = st.get("ret_time") or st.get("ret") or next(iter(st.values()), None)
        flat[name] = time
    return flat


def collect_scored(flat, exits_block):
    scored = []
    for name, st in flat.items():
        if name == "random" or not isinstance(st, dict) or st.get("mean") is None:
            continue
        scored.append((name, "ret_time", st))
    if exits_block:
        for name, by_ex in exits_block.items():
            if name == "random" or not isinstance(by_ex, dict):
                continue
            for ex, st in by_ex.items():
                if isinstance(st, dict) and st.get("mean") is not None:
                    scored.append((name, ex, st))
    return scored


def verdict_from(flat, exits_block=None):
    rnd = flat.get("random") if isinstance(flat.get("random"), dict) else {}
    scored = collect_scored(flat, exits_block)
    if not scored and not rnd:
        return "FAMILY_DEAD", "no scored rule"
    if scored and all(st.get("hi", 1) < 0 for _, _, st in scored):
        return "FAMILY_DEAD", "all paper rules have hi<0"
    better = False
    any_cross = False
    for name, ex, st in scored:
        if st.get("lo") is not None and rnd.get("mean") is not None:
            if st["lo"] > 0 and st["mean"] > rnd.get("mean", -1e9) + 1e-9:
                better = True
        if st.get("lo", -1) <= 0 <= st.get("hi", 1):
            any_cross = True
    if better:
        return "SURVIVE_T1", "selection lo>0 and mean > random"
    if any_cross:
        return "UNDERPOWERED", "CI crosses zero; selection does not beat random with lo>0"
    return "FAMILY_DEAD", "no selection rule beats random with lo>0"


def load_cl():
    print("loading CL hourly panel", flush=True)
    bars = ch_parquet("""
        SELECT s.pool AS pool, s.bar AS bar, s.close AS close, s.vol_weth AS vol_weth,
               s.n_swaps AS n_swaps, u.venue AS venue, u.cost_p50 AS cost_p50, u.fee AS fee
        FROM edge.bars_1h_year_50 s
        INNER JOIN edge.bars_universe_50 u ON u.pool = s.pool
        WHERE s.close > 0 AND s.close <= 1e20
    """)
    flow = ch_parquet("""
        SELECT pool, bar, net_weth, ofi FROM edge.flow_1h_year_50
    """)
    bars["bar"] = as_dt(bars["bar"])
    flow["bar"] = as_dt(flow["bar"])
    df = bars.merge(flow, on=["pool", "bar"], how="left")
    df["net_weth"] = df["net_weth"].fillna(0)
    df["ofi"] = df["ofi"].fillna(0)
    print(f"  {len(df):,} rows  {df['pool'].nunique()} pools", flush=True)
    return df


def load_15m_stats(venue, size, rule, seed=7):
    """Rank and average inside ClickHouse. Returns a summarize()-shaped dict."""
    k = slots(size)
    slip = f"(fee/100.0 + greatest(cost_p50 - fee/100.0, 0) * ({float(size)}/50.0))"
    if rule == "random":
        order = f"cityHash64(pool, toString(bar), toUInt64({int(seed)}))"
    elif rule in ("vol", "flow_only"):
        order = "vol_weth DESC"
    elif rule == "path_only":
        order = "n_swaps DESC"
    elif rule == "liq_only":
        order = "cost_p50 ASC"
    else:
        return "KS_INAPPLICABLE"
    q = f"""
        WITH prep AS (
          SELECT r.pool AS pool, r.b AS bar,
            pow(            leadInFrame(r.sp_last, 1) OVER (
                PARTITION BY r.pool ORDER BY r.b
                ROWS BETWEEN CURRENT ROW AND 1 FOLLOWING)
                / r.sp_last, 2) - 1 AS ret,
            r.weth_vol AS vol_weth, r.n_swaps AS n_swaps,
            u.cost_p50 AS cost_p50, u.fee AS fee
          FROM edge.rot_15m2 r
          INNER JOIN edge.bars_universe_50 u ON u.pool = r.pool
          WHERE r.sp_last > 0 AND u.venue = '{venue}'
        ),
        ok AS (
          SELECT *, {slip} AS c FROM prep
          WHERE isFinite(ret) AND ret > -1 AND ret <= {MAX_ABS_RET}
            AND {slip} <= {COST_CAP_BPS}
        ),
        ranked AS (
          SELECT bar, ret - 2 * c / 1e4 AS net,
                 row_number() OVER (PARTITION BY bar ORDER BY {order}) AS rk
          FROM ok
        ),
        taken AS (SELECT bar, net FROM ranked WHERE rk <= {k})
        SELECT
          count() AS n,
          avg(net) AS mean,
          stddevSamp(net) / sqrt(count()) AS se,
          avg(net > 0) AS win,
          avgIf(net, bar < (SELECT min(bar) + (max(bar) - min(bar)) / 2 FROM taken)) AS h1,
          avgIf(net, bar >= (SELECT min(bar) + (max(bar) - min(bar)) / 2 FROM taken)) AS h2
        FROM taken
    """
    df = ch_parquet(q)
    if df.empty or int(df.iloc[0]["n"] or 0) < 30:
        return None
    n = int(df.iloc[0]["n"])
    m = float(df.iloc[0]["mean"])
    se = float(df.iloc[0]["se"] or 0.0)
    h1 = float(df.iloc[0]["h1"]) if df.iloc[0]["h1"] is not None else float("nan")
    h2 = float(df.iloc[0]["h2"]) if df.iloc[0]["h2"] is not None else float("nan")
    return dict(
        n=n, mean=m, lo=m - 1.96 * se, hi=m + 1.96 * se,
        win=float(df.iloc[0]["win"] or 0.0),
        h1_mean=h1, h2_mean=h2,
        same_sign=bool(np.isfinite(h1) and np.isfinite(h2) and (h1 * h2) > 0),
        ci="normal",
    )


def v4_confirm():
    return dict(
        venue="univ4", size_usd=None, horizon="init_rt",
        status="CONFIRM_ONLY", verdict="FAMILY_DEAD",
        reason="v4_alpha_candidate_no: Initialize clean 112/112 non-positive, median -2.6%",
        source="/opt/EATLQ/scripts/research/base_v4/_phase5_handoff_report.md",
    )


def load_v2():
    print("loading V2 hourly panel", flush=True)
    df = ch_parquet("""
        SELECT h.pool AS pool, h.h AS bar, h.px AS close, h.weth_vol AS vol_weth,
               h.n_swaps AS n_swaps, p.venue AS venue
        FROM census.v2_hourly h
        INNER JOIN census.v2_weth_pairs p ON p.pool = h.pool
        WHERE h.px > 0 AND h.px <= 1e20
    """)
    if df.empty:
        return df
    df["bar"] = as_dt(df["bar"])
    return df


def attach_v2_cost(df, size):
    cost = ch_parquet(f"""
        SELECT pool, d, cost_bps_med, feasible FROM census.v2_pit_cost_{int(size)}
    """)
    cost["d"] = as_day(cost["d"])
    out = df.copy()
    out["d"] = out["bar"].dt.normalize()
    out = out.merge(cost, on=["pool", "d"], how="left")
    out["cost_p50"] = out["cost_bps_med"].fillna(1e9)
    out["fee"] = 30.0
    out["c"] = out["cost_p50"]
    out = out[(out["feasible"] == 1) & (out["cost_p50"] <= COST_CAP_BPS)]
    return out


def decide(rec, flat):
    rec["rules"] = flat
    v, reason = verdict_from(flat, rec.get("exits"))
    rec["verdict"] = v
    rec["reason"] = reason
    return rec


def main():
    with open(f"{OUT}/venue_census.json") as f:
        census = json.load(f)
    ranked = census["ranked_for_recipes"]
    rng = np.random.default_rng(7)
    families = [v4_confirm()]

    cl_venues = [v for v in ranked if v in ("univ3", "aero_cl", "sushi_v3",
                                            "alienbase_cl", "unknown")]
    raw_map = {"univ3": "univ3", "aero_cl": "aero_cl", "sushi_v3": "unknown",
               "alienbase_cl": "unknown", "unknown": "unknown"}
    rules = ["random", "vol", "flow_only", "path_only", "liq_only"]
    ks_rules = ["ks_as_is", "p_fatal_loose", "p_fatal_tight", "ks_complement"]
    exits_1h = ("ret_time",)
    exits_4h = ("ret_time", "ret_tp_sl", "ret_ks_50_4h", "ret_sl_only", "ret_time_only")

    partial_path = f"{OUT}/tier1_partial.json"
    skip_hourly = False
    try:
        prev = json.load(open(partial_path))
        if prev.get("phase") == "hourly_done" and prev.get("families"):
            families = prev["families"]
            skip_hourly = True
            print("resuming from tier1_partial.json (hourly_done)", flush=True)
    except Exception:
        pass

    df = None
    mid = None
    if not skip_hourly:
        df = load_cl() if cl_venues else None
        if df is not None and len(df):
            mid = df["bar"].min() + (df["bar"].max() - df["bar"].min()) / 2

    # One venue × horizon at a time so 15m and path-exit copies do not sit in RAM.
    hourly_cache = {}
    for venue in (cl_venues if not skip_hourly else []):
        raw = raw_map.get(venue, venue)
        for hor, hh in HORIZON_HOURS.items():
            if hh is None:
                for size in SIZES:
                    families.append(dict(
                        venue=venue, size_usd=size, horizon=hor,
                        status="DATA_GAP", verdict="DATA_GAP",
                        reason="no 1-minute CL panel",
                    ))
                continue
            if hor == "10m":
                continue  # filled after hourly is released
            key = (raw, hor)
            if key not in hourly_cache:
                print(f"  prep {raw} {hor}", flush=True)
                hourly_cache[key] = attach_fwd(df[df["venue"] == raw], hh, price="linear")
            base = hourly_cache[key]
            n_drop = int(base["ret"].isna().sum())
            exits = exits_4h if hor in ("4h", "ks4h") else exits_1h
            for size in SIZES:
                rec = dict(venue=venue, size_usd=size, horizon=hor, rules={})
                prep = base.copy()
                prep["c"] = cost_at(prep["cost_p50"].to_numpy(),
                                    prep["fee"].to_numpy(), size)
                prep = prep[np.isfinite(prep["ret"]) & (prep["c"] <= COST_CAP_BPS)]
                block = family_block(prep, size, rules, rng, mid, exits)
                flat = flatten_rules(block)
                for kr in ks_rules:
                    flat[kr] = "KS_INAPPLICABLE"
                rec["exits"] = {k: v for k, v in block.items() if k != "random"}
                rec["n_sane"] = int(len(prep))
                rec["n_dropped_ret"] = n_drop
                decide(rec, flat)
                families.append(rec)
                print(f"  {venue} ${size} {hor} -> {rec.get('verdict')}", flush=True)
            # sushi/alienbase share raw=unknown; keep that prep until both done
        # drop this venue's hourly preps once both sizes/horizons for it are done
        # (unknown is shared — drop only after alienbase_cl, last in cl_venues)
        if venue != "sushi_v3":
            for k in list(hourly_cache):
                if k[0] == raw:
                    del hourly_cache[k]

    if df is not None:
        del df
    hourly_cache.clear()
    import gc
    gc.collect()
    json.dump(dict(phase="hourly_done", families=families),
              open(f"{OUT}/tier1_partial.json", "w"), indent=1, default=str)

    for venue in cl_venues:
        raw = raw_map.get(venue, venue)
        for size in SIZES:
            rec = dict(venue=venue, size_usd=size, horizon="10m",
                       rules={}, proxy="15m_sqrtPrice")
            try:
                probe = load_15m_stats(raw, size, "vol")
            except Exception as e:
                rec["status"] = "DATA_GAP"
                rec["verdict"] = "DATA_GAP"
                rec["reason"] = str(e)[:300]
                families.append(rec)
                print(f"  {venue} ${size} 10m -> DATA_GAP {e}", flush=True)
                continue
            if probe is None:
                rec["status"] = "DATA_GAP"
                rec["verdict"] = "DATA_GAP"
                rec["reason"] = "no 15m rows for venue"
                families.append(rec)
                continue
            flat = {}
            for rule in rules:
                print(f"    15m {venue} ${size} {rule}", flush=True)
                got = load_15m_stats(raw, size, rule) if rule != "vol" else probe
                if got == "KS_INAPPLICABLE":
                    flat[rule] = got
                elif got is None:
                    flat[rule] = dict(status="UNDERPOWERED", n=0)
                else:
                    flat[rule] = got
            for kr in ks_rules:
                flat[kr] = "KS_INAPPLICABLE"
            rec["n_sane"] = int((probe or {}).get("n") or 0)
            decide(rec, flat)
            families.append(rec)
            print(f"  {venue} ${size} 10m -> {rec.get('verdict')}", flush=True)

    for v in ranked:
        if v.startswith("other:"):
            for size in SIZES:
                for hor in HORIZON_HOURS:
                    families.append(dict(
                        venue=v, size_usd=size, horizon=hor,
                        status="DATA_GAP", verdict="DATA_GAP",
                        reason="factory has volume but no year-panel backfill",
                    ))

    # V2 / vAMM — load once
    try:
        v2 = load_v2()
    except Exception as e:
        v2 = None
        v2_err = str(e)[:300]
    else:
        v2_err = None

    for size in SIZES:
        if v2 is None:
            for hor in HORIZON_HOURS:
                families.append(dict(
                    venue="univ2+aero_vamm", size_usd=size, horizon=hor,
                    status="DATA_GAP", verdict="DATA_GAP", reason=v2_err,
                ))
            continue
        try:
            priced = attach_v2_cost(v2, size)
        except Exception as e:
            for hor in HORIZON_HOURS:
                families.append(dict(
                    venue="univ2+aero_vamm", size_usd=size, horizon=hor,
                    status="DATA_GAP", verdict="DATA_GAP", reason=str(e)[:300],
                ))
            continue
        for hor, hh in HORIZON_HOURS.items():
            rec = dict(venue="univ2+aero_vamm", size_usd=size, horizon=hor, rules={})
            if hh is None:
                rec["status"] = "DATA_GAP"
                rec["verdict"] = "DATA_GAP"
                rec["reason"] = "no 1-minute V2 panel"
                families.append(rec)
                continue
            if hor == "10m":
                rec["status"] = "DATA_GAP"
                rec["verdict"] = "DATA_GAP"
                rec["reason"] = "V2 15m bars not built"
                families.append(rec)
                continue
            flat = {}
            by_venue = {}
            for venue, sub in priced.groupby("venue"):
                prep = attach_fwd(sub, hh, price="linear")
                prep = prep[np.isfinite(prep["ret"])]
                if len(prep) < 200:
                    by_venue[venue] = dict(status="UNDERPOWERED", n=len(prep))
                    continue
                midv = prep["bar"].min() + (prep["bar"].max() - prep["bar"].min()) / 2
                block = family_block(prep, size, ["random", "vol", "path_only"], rng, midv)
                by_venue[venue] = flatten_rules(block)
            rec["by_venue"] = by_venue
            # lift univ2 (dominant) into rules for stamp/tier3
            lift = by_venue.get("univ2") or next(iter(by_venue.values()), {})
            if isinstance(lift, dict) and "random" in lift:
                flat = lift
            else:
                flat = dict(random=dict(status="UNDERPOWERED"))
            flat["ks_as_is"] = "KS_INAPPLICABLE"
            decide(rec, flat)
            families.append(rec)
            print(f"  v2 ${size} {hor} -> {rec['verdict']}", flush=True)

    n_surv = sum(1 for f in families if f.get("verdict") == "SURVIVE_T1")
    n_under = sum(1 for f in families if f.get("verdict") == "UNDERPOWERED")
    with open(f"{OUT}/tier1.json", "w") as f:
        json.dump(dict(n_families=len(families), n_survive=n_surv,
                       n_underpowered=n_under,
                       max_abs_ret=MAX_ABS_RET, families=families), f, indent=1,
                  default=str)
    print(f"tier1 families {len(families)} survive {n_surv} underpowered {n_under}")


if __name__ == "__main__":
    main()
