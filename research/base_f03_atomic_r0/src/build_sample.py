"""Build executable F03 sample and fetch entry/exit blocks. No fills."""
from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd

from ch_readonly import query_tsv
from safety import research_entry_guard

ROOT = Path(__file__).resolve().parents[1]
WILDCAT = Path("/root/base_wildcat_alpha")
OUT = ROOT / "out"
WETH = "0x4200000000000000000000000000000000000006"
SEED = 8453


def _hour_str(ts) -> str:
    return pd.Timestamp(ts).tz_convert("UTC").strftime("%Y-%m-%d %H:%M:%S")


def _in_tuples(rows: pd.DataFrame, hour_col: str) -> str:
    parts = []
    for r in rows.itertuples(index=False):
        parts.append(f"(lower('{r.pool_address}'), toDateTime('{_hour_str(getattr(r, hour_col))}', 'UTC'))")
    return ",\n    ".join(parts)


def fetch_hour_blocks(keys: pd.DataFrame, hour_col: str) -> pd.DataFrame:
    if keys.empty:
        return pd.DataFrame(columns=["pool_address", "block_hour", "last_block", "first_block", "swap_count"])
    sql = f"""
SELECT
    lower(pool_address) AS pool_address,
    toStartOfHour(block_time) AS block_hour,
    max(block_number) AS last_block,
    min(block_number) AS first_block,
    uniqExact(tuple(tx_hash, log_index)) AS swap_count
FROM eatlq.uniswap_v3_swaps
WHERE chain_id = 8453 AND removed = 0
  AND (lower(pool_address), toStartOfHour(block_time)) IN (
    {_in_tuples(keys, hour_col)}
  )
GROUP BY pool_address, block_hour
"""
    raw = query_tsv(sql)
    df = pd.read_csv(StringIO(raw), sep="\t")
    df["block_hour"] = pd.to_datetime(df.block_hour, utc=True)
    df["pool_address"] = df.pool_address.str.lower()
    return df


def main() -> None:
    research_entry_guard()
    OUT.mkdir(parents=True, exist_ok=True)
    x = pd.read_parquet(WILDCAT / "data/feature_panel.parquet")
    x["block_hour"] = pd.to_datetime(x.block_hour, utc=True)
    x["t0"] = x.token0_address.str.lower()
    x["t1"] = x.token1_address.str.lower()
    d = x.dropna(subset=["direction_persistence", "fwd_ret_4"]).copy()
    d = d[d.fwd_ret_4.abs() <= np.log(100)]
    cutoff = d.block_hour.quantile(0.40)
    cal, rep = d[d.block_hour <= cutoff], d[d.block_hour > cutoff]
    low, high = cal.direction_persistence.quantile([0.2, 0.8]).tolist()

    def classify(row) -> str:
        side = "HIGH" if row.direction_persistence >= high else ("LOW" if row.direction_persistence <= low else "MID")
        if side == "HIGH" and row.t1 == WETH:
            return "LONG_TICK"
        if side == "LOW" and row.t0 == WETH:
            return "SHORT_TICK_VIA_ALT"
        if side == "MID":
            return "MID"
        return "NOT_EXECUTABLE"

    rep = rep.copy()
    rep["exec_class"] = [classify(r) for r in rep.itertuples(index=False)]
    rep["f03_side"] = np.select(
        [rep.direction_persistence >= high, rep.direction_persistence <= low],
        ["HIGH", "LOW"],
        default="MID",
    )
    long_tick = rep[rep.exec_class == "LONG_TICK"].copy()
    short_via = rep[rep.exec_class == "SHORT_TICK_VIA_ALT"].copy()
    short_via["month"] = short_via.block_hour.dt.tz_convert("UTC").dt.strftime("%Y-%m")
    parts = []
    rng = np.random.default_rng(SEED)
    for month, g in short_via.groupby("month"):
        take = min(20, len(g))
        idx = rng.choice(g.index.to_numpy(), size=take, replace=False)
        parts.append(g.loc[idx])
    short_sample = pd.concat(parts) if parts else short_via.head(0)

    def pack(q: pd.DataFrame, role: str) -> pd.DataFrame:
        out = q[
            [
                "pool_address",
                "block_hour",
                "direction_persistence",
                "fwd_ret_4",
                "ret_0",
                "flow_imbalance",
                "swap_count",
                "token0_address",
                "token1_address",
                "token0_symbol",
                "token1_symbol",
                "f03_side",
                "exec_class",
            ]
        ].copy()
        out["role"] = role
        out["buy_token"] = np.where(out.exec_class == "LONG_TICK", out.token0_address, out.token1_address)
        out["buy_symbol"] = np.where(out.exec_class == "LONG_TICK", out.token0_symbol, out.token1_symbol)
        return out

    sample = pd.concat([pack(long_tick, "PRIMARY_LONG_TICK"), pack(short_sample, "SECONDARY_SHORT_VIA")], ignore_index=True)

    # shadows
    shadows = pd.read_csv(WILDCAT / "SHADOW/signals.csv")
    meta = pd.read_csv(WILDCAT / "data/pool_meta.tsv", sep="\t").drop_duplicates("pool_address", keep="last")
    sh_rows = []
    t0 = pd.Timestamp("2026-08-14T13:00:00+00:00")
    for _, s in shadows.iterrows():
        p = str(s.pool_address).lower()
        m = meta[meta.pool_address.str.lower() == p]
        t0a = str(m.iloc[0].token0_address).lower() if len(m) else ""
        t1a = str(m.iloc[0].token1_address).lower() if len(m) else ""
        side = s.signal_side
        if side == "HIGH" and t1a == WETH:
            klass = "LONG_TICK"
            buy = t0a
            buy_sym = m.iloc[0].token0_symbol
        elif side == "LOW" and t0a == WETH:
            klass = "SHORT_TICK_VIA_ALT"
            buy = t1a
            buy_sym = m.iloc[0].token1_symbol
        else:
            klass = "NOT_EXECUTABLE"
            buy = ""
            buy_sym = ""
        sh_rows.append(
            {
                "pool_address": p,
                "block_hour": t0,
                "direction_persistence": float(s.direction_persistence),
                "fwd_ret_4": np.nan,
                "ret_0": np.nan,
                "flow_imbalance": np.nan,
                "swap_count": int(s.swap_count),
                "token0_address": t0a,
                "token1_address": t1a,
                "token0_symbol": m.iloc[0].token0_symbol if len(m) else "",
                "token1_symbol": m.iloc[0].token1_symbol if len(m) else "",
                "f03_side": side,
                "exec_class": klass,
                "role": "SHADOW",
                "buy_token": buy,
                "buy_symbol": buy_sym,
            }
        )
    sh = pd.DataFrame(sh_rows)
    sample = pd.concat([sample, sh], ignore_index=True)
    sample["pool_address"] = sample.pool_address.str.lower()
    sample["exit_hour"] = sample.block_hour + pd.Timedelta(hours=4)
    sample["signal_id"] = [
        f"{r.role}_{r.pool_address[:10]}_{pd.Timestamp(r.block_hour).strftime('%Y%m%d%H')}"
        for r in sample.itertuples(index=False)
    ]

    entry_b = fetch_hour_blocks(sample, "block_hour")
    exit_b = fetch_hour_blocks(sample, "exit_hour")
    sample = sample.merge(
        entry_b.rename(columns={"last_block": "signal_last_block", "first_block": "signal_first_block", "swap_count": "ch_swaps_signal", "block_hour": "block_hour"}),
        on=["pool_address", "block_hour"],
        how="left",
    )
    sample = sample.merge(
        exit_b.rename(
            columns={
                "last_block": "exit_last_block",
                "first_block": "exit_first_block",
                "swap_count": "ch_swaps_exit",
                "block_hour": "exit_hour",
            }
        ),
        on=["pool_address", "exit_hour"],
        how="left",
    )
    sample["entry_block"] = sample.signal_last_block + 1
    sample["exit_block"] = np.where(sample.exit_last_block.notna(), sample.exit_last_block, sample.entry_block + 7200)
    sample.to_csv(OUT / "sample.csv", index=False)
    summary = {
        "cutoff": str(cutoff),
        "thresholds": {"low": low, "high": high},
        "replay_long_tick": int((sample.role == "PRIMARY_LONG_TICK").sum()),
        "replay_short_via": int((sample.role == "SECONDARY_SHORT_VIA").sum()),
        "shadow": int((sample.role == "SHADOW").sum()),
        "shadow_executable": int(((sample.role == "SHADOW") & (sample.exec_class != "NOT_EXECUTABLE")).sum()),
        "missing_entry_block": int(sample.entry_block.isna().sum()),
        "missing_exit_hour_swaps": int(sample.exit_last_block.isna().sum()),
        "not_executable_shadows": sh.loc[sh.exec_class == "NOT_EXECUTABLE", ["pool_address", "token0_symbol", "token1_symbol", "f03_side"]].to_dict("records"),
    }
    (OUT / "sample_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
