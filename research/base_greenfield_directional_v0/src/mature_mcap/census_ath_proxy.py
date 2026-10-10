#!/usr/bin/env python3
"""Census: Base UniV3 alts that look like they ever reached ~$1M scale.

Hybrid:
1) CH cumulative WETH volume filter (candidate universe)
2) DexScreener current marketCap/fdv (if >= $1M ⇒ ever reached)
3) On-chain peak FDV proxy = peak tick price * current totalSupply

This is NOT new-pool Rescue. deploy_allowed=false.
"""
from __future__ import annotations

import json
import math
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from web3 import Web3

ROOT = Path("/root/base_greenfield_directional_v0")
sys.path.insert(0, str(ROOT))

from src.common import ch_query, sha256_file, utcnow  # noqa: E402

OUT = ROOT / "out_mature_mcap"
CACHE = OUT / "cache"
FREEZE_PATH = ROOT / "configs" / "mature_mcap_census_freeze.json"
WETH = "0x4200000000000000000000000000000000000006"
LN1P = math.log(1.0001)
_SUPPLY = Web3.keccak(text="totalSupply()")[:4]


def load_freeze() -> dict:
    return json.loads(FREEZE_PATH.read_text())


def export_candidates(freeze: dict) -> pd.DataFrame:
    eth = float(freeze["eth_usd_freeze"])
    min_vol = float(freeze["thresholds_usd"]["candidate_cum_volume_usd"])
    sql = f"""
    WITH lower('{WETH}') AS weth,
    pools AS (
      SELECT
        lower(pool_address) AS pool,
        if(lower(token0_address)=weth, lower(token1_address), lower(token0_address)) AS alt,
        if(lower(token0_address)=weth, 1, 0) AS weth_is_token0,
        if(lower(token0_address)=weth, token1_decimals, token0_decimals) AS alt_decimals,
        if(lower(token0_address)=weth, token1_symbol, token0_symbol) AS alt_symbol
      FROM eatlq.discovered_pools
      WHERE pool_type='v3' AND has_weth_leg=1
    ),
    vol AS (
      SELECT
        p.alt AS token,
        any(p.alt_decimals) AS decimals,
        any(p.alt_symbol) AS symbol,
        uniqExact(p.pool) AS n_weth_pools,
        sum(if(p.weth_is_token0=1, abs(toFloat64(s.amount0)), abs(toFloat64(s.amount1)))) / 1e18 AS vol_weth,
        min(s.block_time) AS first_swap,
        max(s.block_time) AS last_swap,
        count() AS n_swaps
      FROM eatlq.uniswap_v3_swaps_decoded AS s
      INNER JOIN pools AS p ON lower(s.pool_address)=p.pool
      WHERE s.block_time IS NOT NULL
      GROUP BY p.alt
    )
    SELECT
      token, decimals, symbol, n_weth_pools, vol_weth,
      vol_weth * {eth} AS cum_volume_usd,
      toUnixTimestamp(first_swap) AS first_swap_ts,
      toUnixTimestamp(last_swap) AS last_swap_ts,
      n_swaps
    FROM vol
    WHERE vol_weth * {eth} >= {min_vol}
    ORDER BY cum_volume_usd DESC
    SETTINGS max_bytes_before_external_group_by='20G'
    """
    df = ch_query(sql, timeout=900)
    print(f"[census] volume candidates>={min_vol}: {len(df)}", flush=True)
    return df


def export_peak_prices(tokens: list[str], freeze: dict) -> pd.DataFrame:
    """Peak alt USD price observed in any WETH UniV3 pool (tick-based)."""
    if not tokens:
        return pd.DataFrame(columns=["token", "peak_price_usd", "n_price_obs"])
    eth = float(freeze["eth_usd_freeze"])
    # chunk IN lists
    rows = []
    chunk_size = 200
    for i in range(0, len(tokens), chunk_size):
        chunk = tokens[i : i + chunk_size]
        in_list = ",".join(f"'{t}'" for t in chunk)
        sql = f"""
        WITH lower('{WETH}') AS weth,
        pools AS (
          SELECT
            lower(pool_address) AS pool,
            if(lower(token0_address)=weth, lower(token1_address), lower(token0_address)) AS alt,
            if(lower(token0_address)=weth, 1, 0) AS weth_is_token0
          FROM eatlq.discovered_pools
          WHERE pool_type='v3' AND has_weth_leg=1
            AND if(lower(token0_address)=weth, lower(token1_address), lower(token0_address)) IN ({in_list})
        )
        SELECT
          p.alt AS token,
          max(
            if(
              p.weth_is_token0=1,
              {eth} / greatest(pow(1.0001, toFloat64(s.tick)), 1e-18),
              {eth} * pow(1.0001, toFloat64(s.tick))
            )
          ) AS peak_price_usd,
          count() AS n_price_obs
        FROM eatlq.uniswap_v3_swaps_decoded AS s
        INNER JOIN pools AS p ON lower(s.pool_address)=p.pool
        WHERE s.tick IS NOT NULL
        GROUP BY p.alt
        SETTINGS max_bytes_before_external_group_by='20G'
        """
        part = ch_query(sql, timeout=900)
        rows.append(part)
        print(f"[census] peak price chunk {i//chunk_size+1}: {len(part)}", flush=True)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def rpc() -> Web3:
    url = Path("/tmp/rpc_url.txt").read_text().strip().splitlines()[0].strip()
    return Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 60}))


def fetch_total_supply(w3: Web3, token: str) -> tuple[float | None, str]:
    try:
        checksum = Web3.to_checksum_address(token)
        raw = w3.eth.call({"to": checksum, "data": "0x" + _SUPPLY.hex()})
        if not raw or raw == b"" or raw == b"\x00" * 32:
            return None, "EMPTY"
        val = int.from_bytes(raw[:32], "big")
        return float(val), "OK"
    except Exception as e:
        return None, str(e)[:120]


def dexscreener_token(addr: str, retries: int = 3) -> dict:
    url = f"https://api.dexscreener.com/latest/dex/tokens/{addr.lower()}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 mature-mcap-census", "Accept": "application/json"})
    last_err = ""
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                d = json.loads(resp.read().decode())
            pairs = [p for p in (d.get("pairs") or []) if str(p.get("chainId", "")).lower() == "base"]
            if not pairs:
                return {"ok": False, "reason": "NO_BASE_PAIR", "marketCap": None, "fdv": None, "priceUsd": None, "liq_usd": None, "pair": None}
            best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0.0))
            return {
                "ok": True,
                "reason": "OK",
                "marketCap": float(best["marketCap"]) if best.get("marketCap") is not None else None,
                "fdv": float(best["fdv"]) if best.get("fdv") is not None else None,
                "priceUsd": float(best["priceUsd"]) if best.get("priceUsd") is not None else None,
                "liq_usd": float((best.get("liquidity") or {}).get("usd") or 0.0),
                "pair": best.get("pairAddress"),
                "dex_id": best.get("dexId"),
                "symbol_ds": (best.get("baseToken") or {}).get("symbol"),
            }
        except Exception as e:
            last_err = str(e)[:160]
            time.sleep(0.8 * (attempt + 1))
    return {"ok": False, "reason": last_err or "FAIL", "marketCap": None, "fdv": None, "priceUsd": None, "liq_usd": None, "pair": None}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    freeze = load_freeze()
    target = float(freeze["thresholds_usd"]["target_mcap_or_fdv_usd"])
    exclude = {x.lower() for x in freeze["core_exclusions"]}

    cand_path = CACHE / "volume_candidates.parquet"
    if cand_path.exists():
        cand = pd.read_parquet(cand_path)
        print(f"[census] loaded candidates cache n={len(cand)}", flush=True)
    else:
        cand = export_candidates(freeze)
        cand.to_parquet(cand_path, index=False)

    cand = cand[~cand["token"].astype(str).str.lower().isin(exclude)].copy()
    tokens = cand["token"].astype(str).str.lower().tolist()

    # peak prices
    peak_path = CACHE / "peak_prices.parquet"
    if peak_path.exists():
        peaks = pd.read_parquet(peak_path)
        print(f"[census] loaded peak prices n={len(peaks)}", flush=True)
    else:
        peaks = export_peak_prices(tokens, freeze)
        peaks.to_parquet(peak_path, index=False)

    d = cand.merge(peaks, on="token", how="left")

    # totalSupply via RPC
    supply_path = CACHE / "total_supply.parquet"
    if supply_path.exists():
        supplies = pd.read_parquet(supply_path)
        print(f"[census] loaded supplies n={len(supplies)}", flush=True)
    else:
        w3 = rpc()
        rows = []
        for i, tok in enumerate(tokens):
            raw, st = fetch_total_supply(w3, tok)
            dec = int(d.loc[d["token"] == tok, "decimals"].iloc[0]) if (d["token"] == tok).any() else 18
            human = (raw / (10 ** dec)) if raw is not None else None
            rows.append({"token": tok, "total_supply_raw": raw, "total_supply": human, "supply_status": st})
            if (i + 1) % 50 == 0:
                print(f"[census] totalSupply {i+1}/{len(tokens)}", flush=True)
                pd.DataFrame(rows).to_parquet(supply_path, index=False)
            time.sleep(0.05)
        supplies = pd.DataFrame(rows)
        supplies.to_parquet(supply_path, index=False)

    d = d.merge(supplies, on="token", how="left")
    d["onchain_peak_fdv_usd"] = d["peak_price_usd"] * d["total_supply"]
    # guard absurd outliers (bad tick / wrong supply)
    d["onchain_peak_fdv_usd_clipped"] = d["onchain_peak_fdv_usd"].clip(upper=1e14)

    # DexScreener current
    ds_path = CACHE / "dexscreener.parquet"
    if ds_path.exists():
        ds = pd.read_parquet(ds_path)
        print(f"[census] loaded dexscreener n={len(ds)}", flush=True)
    else:
        rows = []
        for i, tok in enumerate(tokens):
            info = dexscreener_token(tok)
            rows.append({"token": tok, **{f"ds_{k}": v for k, v in info.items()}})
            if (i + 1) % 25 == 0:
                print(f"[census] dexscreener {i+1}/{len(tokens)}", flush=True)
                pd.DataFrame(rows).to_parquet(ds_path, index=False)
            time.sleep(0.35)
        ds = pd.DataFrame(rows)
        ds.to_parquet(ds_path, index=False)

    d = d.merge(ds, on="token", how="left")

    d["hit_1m_dex_mcap"] = d["ds_marketCap"].fillna(0) >= target
    d["hit_1m_dex_fdv"] = d["ds_fdv"].fillna(0) >= target
    d["hit_1m_onchain_peak_fdv"] = d["onchain_peak_fdv_usd_clipped"].fillna(0) >= target
    d["hit_1m_any"] = d["hit_1m_dex_mcap"] | d["hit_1m_dex_fdv"] | d["hit_1m_onchain_peak_fdv"]
    # conservative: require either DS evidence OR (onchain peak AND cum vol >= 1M to reduce garbage)
    d["hit_1m_conservative"] = d["hit_1m_dex_mcap"] | d["hit_1m_dex_fdv"] | (
        d["hit_1m_onchain_peak_fdv"] & (d["cum_volume_usd"] >= target)
    )

    out_csv = OUT / "MATURE_MCAP_CANDIDATES.csv"
    d.sort_values("cum_volume_usd", ascending=False).to_csv(out_csv, index=False)
    hit = d[d["hit_1m_conservative"]].copy()
    hit_path = OUT / "HIT_1M_CONSERVATIVE.csv"
    hit.to_csv(hit_path, index=False)

    summary = {
        "created_utc": utcnow(),
        "phase": freeze["phase"],
        "eth_usd_freeze": freeze["eth_usd_freeze"],
        "target_usd": target,
        "n_volume_candidates_ge_100k": int(len(d)),
        "n_dexscreener_ok": int(d["ds_ok"].fillna(False).sum()) if "ds_ok" in d.columns else None,
        "n_hit_1m_dex_mcap": int(d["hit_1m_dex_mcap"].sum()),
        "n_hit_1m_dex_fdv": int(d["hit_1m_dex_fdv"].sum()),
        "n_hit_1m_onchain_peak_fdv": int(d["hit_1m_onchain_peak_fdv"].sum()),
        "n_hit_1m_any": int(d["hit_1m_any"].sum()),
        "n_hit_1m_conservative": int(d["hit_1m_conservative"].sum()),
        "n_cum_vol_ge_1m": int((d["cum_volume_usd"] >= target).sum()),
        "note": "DexScreener has current mcap/fdv only (not historical ATH). On-chain peak FDV uses current totalSupply × peak tick price.",
        "deploy_allowed": False,
        "freeze_sha256": sha256_file(FREEZE_PATH),
        "outputs": {
            "candidates": str(out_csv),
            "hit_1m_conservative": str(hit_path),
        },
    }
    (OUT / "CENSUS_SUMMARY.json").write_text(json.dumps(summary, indent=2) + "\n")

    md = f"""# Mature MCap Census V0

Generated: `{summary['created_utc']}`

## Question
Among Base UniV3 WETH-leg tokens (not new-pool sniping), how many look like they reached **~$1M** scale?

## Data limits
- Warehouse has **no** historical market-cap series.
- DexScreener: **current** `marketCap` / `fdv` only.
- On-chain proxy: peak tick price × **current** `totalSupply` (FDV-style; supply history not modeled).

## Candidate filter
Lifetime UniV3 WETH-leg cumulative volume ≥ **${freeze['thresholds_usd']['candidate_cum_volume_usd']:,.0f}** (ETH@${freeze['eth_usd_freeze']}).

## Counts
| Metric | N |
|---|---:|
| Volume candidates | {summary['n_volume_candidates_ge_100k']} |
| DexScreener OK | {summary['n_dexscreener_ok']} |
| Current DS marketCap ≥ $1M | {summary['n_hit_1m_dex_mcap']} |
| Current DS FDV ≥ $1M | {summary['n_hit_1m_dex_fdv']} |
| On-chain peak FDV proxy ≥ $1M | {summary['n_hit_1m_onchain_peak_fdv']} |
| **Conservative hit $1M** | **{summary['n_hit_1m_conservative']}** |
| Cum volume ≥ $1M (activity proxy only) | {summary['n_cum_vol_ge_1m']} |

Conservative = DS mcap/fdv ≥ $1M **or** (on-chain peak FDV ≥ $1M **and** cum volume ≥ $1M).

## Reproduce
```bash
cd /root/base_greenfield_directional_v0
.venv/bin/python -m src.mature_mcap.census_ath_proxy
```

`deploy_allowed=false`. Next: freeze PIT “first time ≥ $1M” then test mean-reversion on the conservative list.
"""
    (OUT / "README.md").write_text(md)
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
