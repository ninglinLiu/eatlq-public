"""Family B: PIT genesis-quality features (deployer / LP / holder growth).

STRICT RULE: only events with timestamp strictly before decision_time T.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from ..common import OUT, ROOT, ch_query
from .common_alpha import ALPHA_OUT, ensure_alpha_out


def load_pool_deployers(*, cache: Path | None = None) -> pd.DataFrame:
    """pool -> deployer via discovered_pools.created_tx_hash x tx_from_by_hash."""
    ensure_alpha_out()
    cache = cache or (ALPHA_OUT / "pool_deployers.parquet")
    if cache.exists():
        return pd.read_parquet(cache)
    sql = """
    SELECT
      lower(d.pool_address) AS pool,
      lower(d.token0_address) AS token0,
      lower(d.token1_address) AS token1,
      toUnixTimestamp(d.discovered_at) AS discovered_ts,
      d.created_block AS created_block,
      lower(d.created_tx_hash) AS created_tx_hash,
      lower(t.tx_from) AS deployer
    FROM eatlq.discovered_pools AS d
    LEFT JOIN eatlq.tx_from_by_hash AS t
      ON lower(t.tx_hash) = lower(d.created_tx_hash)
    WHERE d.pool_type = 'v3' AND d.has_weth_leg = 1
    """
    df = ch_query(sql, timeout=600)
    if df.empty:
        df = pd.DataFrame(columns=[
            "pool", "token0", "token1", "discovered_ts", "created_block",
            "created_tx_hash", "deployer",
        ])
    else:
        df["pool"] = df["pool"].astype(str).str.lower()
        df["deployer"] = df["deployer"].fillna("").astype(str).str.lower()
        df["discovered_ts"] = pd.to_numeric(df["discovered_ts"], errors="coerce").fillna(0).astype(np.int64)
    df.to_parquet(cache, index=False)
    print(f"[alpha/B] deployers={len(df):,} with_addr={(df['deployer']!='').sum():,}", flush=True)
    return df


def load_pool_first_swap_ts(swaps: pd.DataFrame | None = None) -> pd.DataFrame:
    if swaps is None:
        swaps = pd.read_parquet(ROOT / "data" / "resolved_swaps.parquet", columns=["pool", "ts"])
    g = swaps.groupby(swaps["pool"].astype(str).str.lower())["ts"].min().reset_index()
    g.columns = ["pool", "pool_birth_ts"]
    g["pool_birth_ts"] = g["pool_birth_ts"].astype(np.int64)
    return g


def build_deployer_pool_outcomes(
    deployers: pd.DataFrame,
    swaps: pd.DataFrame,
) -> pd.DataFrame:
    """One row per pool with outcome stats computed from that pool's own swap history.

    Outcomes are *pool-level* and only used for a *later* pool of the same deployer
    (PIT via birth_ts of the later pool).
    """
    ensure_alpha_out()
    cache = ALPHA_OUT / "deployer_pool_outcomes.parquet"
    if cache.exists():
        return pd.read_parquet(cache)

    s = swaps.copy()
    s["pool"] = s["pool"].astype(str).str.lower()
    s["ts"] = s["ts"].astype(np.int64)
    s["price_weth"] = pd.to_numeric(s["price_weth"], errors="coerce")
    birth = s.groupby("pool")["ts"].min()
    # survival: any swap after birth+48h
    last = s.groupby("pool")["ts"].max()
    survival = ((last - birth) >= 48 * 3600).astype(int)

    # max ret from first price
    first_px = s.sort_values("ts").groupby("pool")["price_weth"].first()
    max_px = s.groupby("pool")["price_weth"].max()
    with np.errstate(divide="ignore", invalid="ignore"):
        max_ret = (max_px / first_px - 1.0).replace([np.inf, -np.inf], np.nan).fillna(0.0).clip(-1, 100)

    # rug proxy: last price < 10% of max and survival==0 (died fast + crushed)
    last_px = s.sort_values("ts").groupby("pool")["price_weth"].last()
    rug = ((last_px < 0.1 * max_px) & (survival == 0)).astype(int)

    out = pd.DataFrame({
        "pool": birth.index.astype(str),
        "pool_birth_ts": birth.values.astype(np.int64),
        "survived_48h": survival.reindex(birth.index).fillna(0).astype(int).values,
        "max_ret": max_ret.reindex(birth.index).fillna(0.0).values,
        "rug_flag": rug.reindex(birth.index).fillna(0).astype(int).values,
    })
    out = out.merge(deployers[["pool", "deployer"]], on="pool", how="left")
    out["deployer"] = out["deployer"].fillna("").astype(str)
    out.to_parquet(cache, index=False)
    print(f"[alpha/B] deployer pool outcomes={len(out):,}", flush=True)
    return out


def _expanding_deployer_stats(outcomes: pd.DataFrame) -> pd.DataFrame:
    o = outcomes[outcomes["deployer"].ne("")].sort_values(["deployer", "pool_birth_ts"]).copy()
    if o.empty:
        return pd.DataFrame(columns=[
            "deployer", "pool_birth_ts", "n_prior", "survival_rate", "max_ret_mean", "rug_rate",
        ])
    g = o.groupby("deployer", sort=False)
    # shift so current pool is NOT included in its own deployer stats
    o["n_prior"] = g.cumcount()  # 0 for first pool
    o["surv_cum"] = g["survived_48h"].cumsum().shift(1)
    o["ret_cum"] = g["max_ret"].cumsum().shift(1)
    o["rug_cum"] = g["rug_flag"].cumsum().shift(1)
    o["survival_rate"] = np.where(o["n_prior"] > 0, o["surv_cum"] / o["n_prior"], np.nan)
    o["max_ret_mean"] = np.where(o["n_prior"] > 0, o["ret_cum"] / o["n_prior"], np.nan)
    o["rug_rate"] = np.where(o["n_prior"] > 0, o["rug_cum"] / o["n_prior"], np.nan)
    return o[[
        "deployer", "pool_birth_ts", "n_prior", "survival_rate", "max_ret_mean", "rug_rate",
    ]].reset_index(drop=True)


def load_lp_events(pools: list[str], *, cache: Path | None = None) -> pd.DataFrame:
    """Mint/burn events for given pools (unix ts)."""
    ensure_alpha_out()
    cache = cache or (ALPHA_OUT / "lp_events.parquet")
    if cache.exists():
        return pd.read_parquet(cache)
    if not pools:
        return pd.DataFrame(columns=["pool", "ts", "event", "owner", "liq"])
    frames = []
    batch = 200
    for i in range(0, len(pools), batch):
        chunk = pools[i : i + batch]
        in_list = ",".join(f"'{p}'" for p in chunk)
        sql_m = f"""
        SELECT
          lower(pool_address) AS pool,
          toUnixTimestamp(block_time) AS ts,
          'mint' AS event,
          lower(owner) AS owner,
          toFloat64OrZero(toString(liquidity)) AS liq
        FROM eatlq.uniswap_v3_mints_decoded
        WHERE lower(pool_address) IN ({in_list})
        """
        sql_b = f"""
        SELECT
          lower(pool_address) AS pool,
          toUnixTimestamp(block_time) AS ts,
          'burn' AS event,
          lower(owner) AS owner,
          toFloat64OrZero(toString(liquidity)) AS liq
        FROM eatlq.uniswap_v3_burns_decoded
        WHERE lower(pool_address) IN ({in_list})
        """
        try:
            m = ch_query(sql_m, timeout=600)
            b = ch_query(sql_b, timeout=600)
            if not m.empty:
                frames.append(m)
            if not b.empty:
                frames.append(b)
        except Exception as e:
            print(f"[alpha/B] LP batch {i} failed: {e}", flush=True)
            continue
        if (i // batch) % 5 == 0:
            print(f"[alpha/B] LP batches done {i + batch}/{len(pools)}", flush=True)
    if not frames:
        out = pd.DataFrame(columns=["pool", "ts", "event", "owner", "liq"])
    else:
        out = pd.concat(frames, ignore_index=True)
        out["pool"] = out["pool"].astype(str).str.lower()
        out["ts"] = pd.to_numeric(out["ts"], errors="coerce").fillna(0).astype(np.int64)
        out["liq"] = pd.to_numeric(out["liq"], errors="coerce").fillna(0.0)
        out["owner"] = out["owner"].astype(str).str.lower()
    out.to_parquet(cache, index=False)
    print(f"[alpha/B] lp events={len(out):,}", flush=True)
    return out


def load_holder_transfers(
    tokens: list[str],
    birth: pd.DataFrame,
    token_to_pool: pd.DataFrame,
    *,
    cache: Path | None = None,
    window_s: int = 48 * 3600,
) -> pd.DataFrame:
    """ERC20 transfers for target tokens restricted to first `window_s` after pool birth."""
    ensure_alpha_out()
    cache = cache or (ALPHA_OUT / "holder_transfers.parquet")
    if cache.exists():
        return pd.read_parquet(cache)
    if not tokens:
        return pd.DataFrame(columns=["token", "ts", "recipient", "block_number"])
    # build token -> birth_ts
    tp = token_to_pool.copy()
    tp["token"] = tp["token"].astype(str).str.lower()
    tp["pool"] = tp["pool"].astype(str).str.lower()
    tp = tp.drop_duplicates("token").merge(birth, on="pool", how="left")
    birth_map = dict(zip(tp["token"], tp["pool_birth_ts"].fillna(0).astype(np.int64)))

    frames = []
    batch = 80
    for i in range(0, len(tokens), batch):
        chunk = tokens[i : i + batch]
        in_list = ",".join(f"'{t}'" for t in chunk)
        # pull transfers; filter to birth window in pandas (CH may not know birth)
        sql = f"""
        SELECT
          lower(token_address) AS token,
          toUnixTimestamp(block_time) AS ts,
          lower(recipient) AS recipient,
          block_number
        FROM eatlq.erc20_transfers_decoded
        WHERE lower(token_address) IN ({in_list})
        SETTINGS max_memory_usage = '8000000000'
        """
        try:
            tr = ch_query(sql, timeout=900)
            if not tr.empty:
                tr["token"] = tr["token"].astype(str).str.lower()
                tr["ts"] = pd.to_numeric(tr["ts"], errors="coerce").fillna(0).astype(np.int64)
                bts = tr["token"].map(birth_map).fillna(0).astype(np.int64)
                tr = tr[(tr["ts"] >= bts) & (tr["ts"] < bts + window_s)]
                if not tr.empty:
                    frames.append(tr)
        except Exception as e:
            print(f"[alpha/B] transfer batch {i} failed: {e}", flush=True)
            continue
        if (i // batch) % 5 == 0:
            print(f"[alpha/B] transfer batches {i + batch}/{len(tokens)}", flush=True)
    if not frames:
        out = pd.DataFrame(columns=["token", "ts", "recipient", "block_number"])
    else:
        out = pd.concat(frames, ignore_index=True)
        out["block_number"] = pd.to_numeric(out["block_number"], errors="coerce").fillna(0).astype(np.int64)
        out["recipient"] = out["recipient"].astype(str).str.lower()
    out.to_parquet(cache, index=False)
    print(f"[alpha/B] transfers={len(out):,}", flush=True)
    return out


def _lp_expanding_panel(lp: pd.DataFrame, birth: pd.DataFrame) -> pd.DataFrame:
    """Per-pool expanding LP stats at each event timestamp (for asof join)."""
    if lp.empty:
        return pd.DataFrame(columns=[
            "pool", "ts", "lp_n_mints", "lp_n_burns", "lp_early_burn_frac",
            "lp_net_liq_proxy", "lp_owner_concentration",
        ])
    d = lp.merge(birth, on="pool", how="left")
    d = d.sort_values(["pool", "ts"]).copy()
    d["is_mint"] = (d["event"] == "mint").astype(int)
    d["is_burn"] = (d["event"] == "burn").astype(int)
    d["signed_liq"] = np.where(d["is_mint"] == 1, d["liq"], -d["liq"])
    d["early"] = d["ts"] < (d["pool_birth_ts"].fillna(0) + 1800)
    d["early_burn"] = ((d["is_burn"] == 1) & d["early"]).astype(int)
    d["early_evt"] = d["early"].astype(int)
    g = d.groupby("pool", sort=False)
    d["lp_n_mints"] = g["is_mint"].cumsum()
    d["lp_n_burns"] = g["is_burn"].cumsum()
    d["lp_net_liq_proxy"] = g["signed_liq"].cumsum()
    early_burn_c = g["early_burn"].cumsum()
    early_evt_c = g["early_evt"].cumsum().clip(lower=1)
    d["lp_early_burn_frac"] = early_burn_c / early_evt_c
    # owner concentration approx: running max mint-owner share is expensive; use
    # cumulative mint count concentration proxy = 1/n_distinct_mint_owners so far
    d["mint_owner"] = np.where(d["is_mint"] == 1, d["owner"], None)
    # approximate concentration via cumulative mint share of first owner seen
    first_owner = g["owner"].transform("first")
    d["from_first"] = ((d["is_mint"] == 1) & (d["owner"] == first_owner)).astype(float) * d["liq"]
    d["mint_liq"] = np.where(d["is_mint"] == 1, d["liq"], 0.0)
    d["mint_liq_c"] = g["mint_liq"].cumsum()
    d["first_liq_c"] = g["from_first"].cumsum()
    d["lp_owner_concentration"] = np.where(
        d["mint_liq_c"] > 0, d["first_liq_c"] / d["mint_liq_c"], 0.0
    )
    return d[[
        "pool", "ts", "lp_n_mints", "lp_n_burns", "lp_early_burn_frac",
        "lp_net_liq_proxy", "lp_owner_concentration",
    ]].reset_index(drop=True)


def _holder_expanding_panel(transfers: pd.DataFrame, birth: pd.DataFrame, token_pool: pd.DataFrame) -> pd.DataFrame:
    """Per-token expanding holder stats at each transfer ts."""
    if transfers.empty:
        return pd.DataFrame(columns=[
            "token", "ts", "holder_n_distinct", "holder_growth_rate",
            "holder_top_share", "holder_same_block_bundle_share",
        ])
    tp = token_pool.drop_duplicates("token")[["token", "pool"]].copy()
    tp["token"] = tp["token"].astype(str).str.lower()
    tp["pool"] = tp["pool"].astype(str).str.lower()
    d = transfers.merge(tp, on="token", how="left").merge(birth, on="pool", how="left")
    d = d.sort_values(["token", "ts"]).copy()
    # approximate distinct holders: first-seen flag per recipient within token
    d["first_seen"] = ~d.duplicated(["token", "recipient"])
    g = d.groupby("token", sort=False)
    d["holder_n_distinct"] = g["first_seen"].cumsum()
    age_h = ((d["ts"] - d["pool_birth_ts"].fillna(d["ts"])) / 3600.0).clip(lower=1e-6)
    d["holder_growth_rate"] = d["holder_n_distinct"] / age_h
    # top share approx: running count of modal early recipient / n
    d["n_evt"] = g.cumcount() + 1
    first_rec = g["recipient"].transform("first")
    d["is_first_rec"] = (d["recipient"] == first_rec).astype(int)
    d["holder_top_share"] = g["is_first_rec"].cumsum() / d["n_evt"]
    # same-block bundle approx: running count in first block / n
    first_blk = g["block_number"].transform("first")
    d["is_first_blk"] = (d["block_number"] == first_blk).astype(int)
    d["holder_same_block_bundle_share"] = g["is_first_blk"].cumsum() / d["n_evt"]
    return d[[
        "token", "ts", "holder_n_distinct", "holder_growth_rate",
        "holder_top_share", "holder_same_block_bundle_share",
    ]].reset_index(drop=True)


def _searchsorted_asof(
    left: pd.DataFrame,
    right: pd.DataFrame,
    *,
    by: str,
    left_on: str,
    right_on: str,
    cols: list[str],
) -> pd.DataFrame:
    """Strict asof (right_on < left_on) via per-group searchsorted."""
    out = left.copy()
    for c in cols:
        out[c] = 0.0
    if right.empty:
        return out
    r = right.sort_values([by, right_on], kind="mergesort").drop_duplicates([by, right_on], keep="last")
    left_g = {k: idx for k, idx in left.groupby(by, sort=False).groups.items()}
    for key, g in r.groupby(by, sort=False):
        idxs = left_g.get(key)
        if idxs is None:
            continue
        keys = g[right_on].to_numpy(dtype=np.int64)
        vals = g[cols].to_numpy(dtype=float)
        times = left.loc[idxs, left_on].to_numpy(dtype=np.int64)
        pos = np.searchsorted(keys, times, side="left") - 1
        valid = pos >= 0
        dest = np.asarray(idxs)
        out.loc[dest[valid], cols] = vals[pos[valid]]
    for c in cols:
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0)
    return out


def _asof_lp(signals: pd.DataFrame, lp_panel: pd.DataFrame) -> pd.DataFrame:
    left = signals[["_row", "pool", "decision_time"]].copy()
    left["pool"] = left["pool"].astype(str).str.lower()
    left["decision_time"] = pd.to_numeric(left["decision_time"], errors="coerce").fillna(0).astype(np.int64)
    cols = ["lp_n_mints", "lp_n_burns", "lp_early_burn_frac", "lp_net_liq_proxy", "lp_owner_concentration"]
    if lp_panel.empty:
        out = left[["_row"]].copy()
        for c in cols:
            out[c] = 0.0
        return out
    right = lp_panel.copy()
    right["pool"] = right["pool"].astype(str).str.lower()
    right["ts"] = pd.to_numeric(right["ts"], errors="coerce").fillna(0).astype(np.int64)
    m = _searchsorted_asof(left, right, by="pool", left_on="decision_time", right_on="ts", cols=cols)
    return m[["_row", *cols]]


def _asof_holders(signals: pd.DataFrame, h_panel: pd.DataFrame) -> pd.DataFrame:
    left = signals[["_row", "token", "decision_time"]].copy()
    left["token"] = left["token"].astype(str).str.lower()
    left["decision_time"] = pd.to_numeric(left["decision_time"], errors="coerce").fillna(0).astype(np.int64)
    cols = ["holder_n_distinct", "holder_growth_rate", "holder_top_share", "holder_same_block_bundle_share"]
    if h_panel.empty:
        out = left[["_row"]].copy()
        for c in cols:
            out[c] = 0.0
        return out
    right = h_panel.copy()
    right["token"] = right["token"].astype(str).str.lower()
    right["ts"] = pd.to_numeric(right["ts"], errors="coerce").fillna(0).astype(np.int64)
    m = _searchsorted_asof(left, right, by="token", left_on="decision_time", right_on="ts", cols=cols)
    return m[["_row", *cols]]


def attach_genesis_features(
    panel: pd.DataFrame,
    *,
    swaps: pd.DataFrame | None = None,
) -> pd.DataFrame:
    ensure_alpha_out()
    d = panel.copy()
    d["_row"] = np.arange(len(d))
    d["pool"] = d["pool"].astype(str).str.lower()
    d["token"] = d["token"].astype(str).str.lower()
    d["decision_time"] = d["decision_time"].astype(np.int64)

    if swaps is None:
        swaps = pd.read_parquet(
            ROOT / "data" / "resolved_swaps.parquet",
            columns=["pool", "token", "ts", "side", "price_weth", "quote_weth"],
        )
    deployers = load_pool_deployers()
    birth = load_pool_first_swap_ts(swaps)
    outcomes = build_deployer_pool_outcomes(deployers, swaps)
    dep_stats = _expanding_deployer_stats(outcomes)

    # attach deployer + birth to signals
    d = d.merge(deployers[["pool", "deployer"]], on="pool", how="left")
    d["deployer"] = d["deployer"].fillna("").astype(str)
    d = d.merge(birth, on="pool", how="left")

    # asof deployer stats by deployer using pool_birth_ts of CURRENT pool as T_dep
    # (only prior pools of deployer, already shifted in expanding stats)
    left = d[["_row", "deployer", "pool_birth_ts"]].copy()
    left["pool_birth_ts"] = pd.to_numeric(left["pool_birth_ts"], errors="coerce").fillna(0).astype(np.int64)
    left = left.sort_values(["deployer", "pool_birth_ts"])
    right = dep_stats.sort_values(["deployer", "pool_birth_ts"])
    if not right.empty and left["deployer"].ne("").any():
        # Prefer exact (deployer, pool_birth_ts) match — stats already exclude self via shift(1)
        m = left.merge(right, on=["deployer", "pool_birth_ts"], how="left")
        miss_mask = m["n_prior"].isna() & m["deployer"].ne("")
        if miss_mask.any():
            sub_l = m.loc[miss_mask, ["_row", "deployer", "pool_birth_ts"]].copy()
            sub_l["_t"] = sub_l["pool_birth_ts"]
            right_asof = right.rename(columns={"pool_birth_ts": "_t"}).sort_values(["deployer", "_t"])
            asof = pd.merge_asof(
                sub_l.sort_values(["deployer", "_t"]),
                right_asof,
                on="_t",
                by="deployer",
                direction="backward",
                allow_exact_matches=False,
            )
            cols = ["n_prior", "survival_rate", "max_ret_mean", "rug_rate"]
            m = m.set_index("_row")
            asof = asof.set_index("_row")
            m.loc[asof.index, cols] = asof[cols]
            m = m.reset_index()
    else:
        m = left.copy()
        m["n_prior"] = np.nan
        m["survival_rate"] = np.nan
        m["max_ret_mean"] = np.nan
        m["rug_rate"] = np.nan

    feat = d[["_row"]].merge(
        m[["_row", "n_prior", "survival_rate", "max_ret_mean", "rug_rate"]],
        on="_row",
        how="left",
    )
    feat = feat.rename(columns={
        "n_prior": "deployer_n_prior_pools",
        "survival_rate": "deployer_prior_survival_rate",
        "max_ret_mean": "deployer_prior_max_ret_mean",
        "rug_rate": "deployer_prior_rug_rate",
    })
    feat["deployer_cold_start"] = (
        feat["deployer_n_prior_pools"].isna() | (feat["deployer_n_prior_pools"] <= 0)
    ).astype(float)
    for c in [
        "deployer_n_prior_pools", "deployer_prior_survival_rate",
        "deployer_prior_max_ret_mean", "deployer_prior_rug_rate",
    ]:
        feat[c] = pd.to_numeric(feat[c], errors="coerce").fillna(0.0)

    pools = sorted(d["pool"].unique().tolist())
    tokens = sorted(d["token"].unique().tolist())
    token_pool = d[["token", "pool"]].drop_duplicates()

    print("[alpha/B] loading LP events from CH…", flush=True)
    lp = load_lp_events(pools)

    # Prefer fast swap-based holder proxy (distinct buy recipients in resolved_swaps).
    # CH erc20 transfers are optional enrichment; skip if cache missing and proxy suffices.
    transfer_cache = ALPHA_OUT / "holder_transfers.parquet"
    if transfer_cache.exists():
        transfers = pd.read_parquet(transfer_cache)
        print(f"[alpha/B] loaded cached transfers={len(transfers):,}", flush=True)
    else:
        print("[alpha/B] building swap-based holder proxy (no CH transfer pull)…", flush=True)
        sb = swaps.copy()
        sb["pool"] = sb["pool"].astype(str).str.lower()
        sb["token"] = sb["token"].astype(str).str.lower() if "token" in sb.columns else ""
        if "token" not in sb.columns or sb["token"].eq("").all():
            # map pool->token from panel
            sb = sb.drop(columns=["token"], errors="ignore").merge(
                token_pool, on="pool", how="left"
            )
        buys = sb[sb["side"].eq("buy")][["token", "ts", "economic_actor", "block_number"]].copy() if "block_number" in sb.columns else sb[sb["side"].eq("buy")][["token", "ts", "economic_actor"]].copy()
        buys = buys.rename(columns={"economic_actor": "recipient"})
        buys["recipient"] = buys["recipient"].astype(str).str.lower()
        buys["ts"] = buys["ts"].astype(np.int64)
        if "block_number" not in buys.columns:
            buys["block_number"] = buys["ts"]  # coarse proxy
        # restrict to first 48h via birth
        buys = buys.merge(token_pool, on="token", how="left").merge(birth, on="pool", how="left")
        buys = buys[
            buys["ts"].ge(buys["pool_birth_ts"].fillna(0))
            & buys["ts"].lt(buys["pool_birth_ts"].fillna(0) + 48 * 3600)
        ]
        transfers = buys[["token", "ts", "recipient", "block_number"]].copy()
        transfers.to_parquet(transfer_cache, index=False)
        print(f"[alpha/B] swap-holder proxy rows={len(transfers):,}", flush=True)

    print("[alpha/B] expanding LP + holder panels + asof…", flush=True)
    lp_panel = _lp_expanding_panel(lp, birth)
    h_panel = _holder_expanding_panel(transfers, birth, token_pool)
    lpf = _asof_lp(d, lp_panel)
    hf = _asof_holders(d, h_panel)

    feat = feat.merge(lpf, on="_row", how="left")
    feat = feat.merge(hf, on="_row", how="left")
    for c in [
        "lp_n_mints", "lp_n_burns", "lp_early_burn_frac", "lp_net_liq_proxy", "lp_owner_concentration",
        "holder_n_distinct", "holder_growth_rate", "holder_top_share", "holder_same_block_bundle_share",
    ]:
        feat[c] = pd.to_numeric(feat.get(c, 0), errors="coerce").fillna(0.0)

    # composite genesis score: high survival / low rug / organic holders / no early burn
    feat["genesis_score"] = (
        (1.0 - feat["deployer_cold_start"]) * (
            feat["deployer_prior_survival_rate"]
            - feat["deployer_prior_rug_rate"]
            + 0.1 * feat["deployer_prior_max_ret_mean"].clip(0, 10)
        )
        + 0.3 * np.log1p(feat["holder_n_distinct"])
        - 0.5 * feat["lp_early_burn_frac"]
        - 0.3 * feat["holder_same_block_bundle_share"]
        - 0.2 * feat["holder_top_share"]
        + 0.1 * np.log1p(feat["lp_n_mints"])
    )

    out_cols = [
        "deployer_n_prior_pools", "deployer_prior_survival_rate", "deployer_prior_max_ret_mean",
        "deployer_prior_rug_rate", "deployer_cold_start",
        "lp_n_mints", "lp_n_burns", "lp_early_burn_frac", "lp_net_liq_proxy", "lp_owner_concentration",
        "holder_n_distinct", "holder_growth_rate", "holder_top_share", "holder_same_block_bundle_share",
        "genesis_score",
    ]
    result = panel.copy()
    for c in out_cols:
        result[c] = feat[c].to_numpy()
    print(
        f"[alpha/B] deployer_cold={result['deployer_cold_start'].mean():.1%} "
        f"mean_holders={result['holder_n_distinct'].mean():.1f}",
        flush=True,
    )
    return result


if __name__ == "__main__":
    lab = pd.read_parquet(OUT / "panel" / "labeled_wait_absorption.parquet")
    elig = lab[(lab["scorable_primary"] == True) & (lab["buy_status"].eq("SUCCESS"))].head(5000)  # noqa: E712
    attach_genesis_features(elig)
