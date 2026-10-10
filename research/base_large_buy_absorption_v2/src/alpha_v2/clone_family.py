"""Leg N: bytecode clone-family reputation via one-time eth_getCode.

Family = keccak256(runtime bytecode). PIT outcomes of prior tokens in the same
family (birth < this token's birth) → survival / rug / max-ret features.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

from ..common import OUT, ROOT, get_rpc_url
from .common_v2 import ALPHA_V2_OUT, ensure_alpha_v2_out


def _rpc_call(url: str, method: str, params: list, *, timeout: int = 30) -> Any:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode())
    if "error" in body:
        raise RuntimeError(str(body["error"])[:300])
    return body.get("result")


def fetch_token_code_hashes(
    tokens: list[str],
    *,
    cache: Path | None = None,
    sleep_s: float = 0.02,
) -> pd.DataFrame:
    """One-time eth_getCode for each token → code_hash = sha256(runtime bytecode hex)."""
    ensure_alpha_v2_out()
    cache = cache or (ALPHA_V2_OUT / "token_code.parquet")
    done: dict[str, str] = {}
    if cache.exists():
        prev = pd.read_parquet(cache)
        done = dict(zip(prev["token"].astype(str).str.lower(), prev["code_hash"].astype(str)))
        print(f"[alpha_v2/N] loaded {len(done):,} cached code hashes", flush=True)

    todo = [t.lower() for t in tokens if t.lower() not in done]
    if not todo:
        return pd.DataFrame({"token": list(done.keys()), "code_hash": list(done.values()), "code_len": 0})

    url = get_rpc_url()
    print(f"[alpha_v2/N] eth_getCode for {len(todo):,} tokens…", flush=True)
    rows = [{"token": k, "code_hash": v, "code_len": -1} for k, v in done.items()]
    for i, tok in enumerate(todo):
        try:
            code = _rpc_call(url, "eth_getCode", [tok, "latest"])
            code = (code or "0x").lower()
            if code in ("0x", "0x0", ""):
                h = "EOA_OR_EMPTY"
                clen = 0
            else:
                raw = bytes.fromhex(code[2:] if code.startswith("0x") else code)
                h = hashlib.sha256(raw).hexdigest()
                clen = len(raw)
            rows.append({"token": tok, "code_hash": h, "code_len": clen})
        except Exception as e:
            print(f"[alpha_v2/N] getCode fail {tok}: {e}", flush=True)
            rows.append({"token": tok, "code_hash": "FETCH_FAIL", "code_len": -1})
        if (i + 1) % 50 == 0:
            print(f"[alpha_v2/N] progress {i+1}/{len(todo)}", flush=True)
            pd.DataFrame(rows).to_parquet(cache, index=False)
        if sleep_s:
            time.sleep(sleep_s)
    out = pd.DataFrame(rows)
    out.to_parquet(cache, index=False)
    print(
        f"[alpha_v2/N] code hashes={len(out):,} distinct_families="
        f"{out.loc[~out['code_hash'].isin(['EOA_OR_EMPTY','FETCH_FAIL']), 'code_hash'].nunique()}",
        flush=True,
    )
    return out


def _token_pool_outcomes(panel_swaps: pd.DataFrame) -> pd.DataFrame:
    """Per-token outcome stats from resolved_swaps (pool lifetime in our universe)."""
    s = panel_swaps.copy()
    s["token"] = s["token"].astype(str).str.lower()
    s["pool"] = s["pool"].astype(str).str.lower()
    s["ts"] = s["ts"].astype(np.int64)
    s["price_weth"] = pd.to_numeric(s["price_weth"], errors="coerce")
    birth = s.groupby("token")["ts"].min()
    last = s.groupby("token")["ts"].max()
    survival = ((last - birth) >= 48 * 3600).astype(int)
    first_px = s.sort_values("ts").groupby("token")["price_weth"].first()
    max_px = s.groupby("token")["price_weth"].max()
    last_px = s.sort_values("ts").groupby("token")["price_weth"].last()
    with np.errstate(divide="ignore", invalid="ignore"):
        max_ret = (max_px / first_px - 1.0).replace([np.inf, -np.inf], np.nan).fillna(0.0).clip(-1, 100)
    rug = ((last_px < 0.1 * max_px) & (survival == 0)).astype(int)
    out = pd.DataFrame({
        "token": birth.index.astype(str),
        "token_birth_ts": birth.values.astype(np.int64),
        "survived_48h": survival.reindex(birth.index).fillna(0).astype(int).values,
        "max_ret": max_ret.reindex(birth.index).fillna(0.0).values,
        "rug_flag": rug.reindex(birth.index).fillna(0).astype(int).values,
    })
    return out


def _expanding_family_stats(outcomes: pd.DataFrame, codes: pd.DataFrame) -> pd.DataFrame:
    o = outcomes.merge(codes[["token", "code_hash"]], on="token", how="left")
    o["code_hash"] = o["code_hash"].fillna("UNKNOWN")
    # exclude non-families
    o = o[~o["code_hash"].isin(["EOA_OR_EMPTY", "FETCH_FAIL", "UNKNOWN"])].copy()
    o = o.sort_values(["code_hash", "token_birth_ts"])
    if o.empty:
        return pd.DataFrame(columns=[
            "code_hash", "token_birth_ts", "token", "n_prior",
            "survival_rate", "max_ret_mean", "rug_rate", "family_size_so_far",
        ])
    g = o.groupby("code_hash", sort=False)
    o["n_prior"] = g.cumcount()  # 0 for first in family
    o["surv_cum"] = g["survived_48h"].cumsum().shift(1)
    o["ret_cum"] = g["max_ret"].cumsum().shift(1)
    o["rug_cum"] = g["rug_flag"].cumsum().shift(1)
    o["family_size_so_far"] = o["n_prior"]  # priors only
    o["survival_rate"] = np.where(o["n_prior"] > 0, o["surv_cum"] / o["n_prior"], np.nan)
    o["max_ret_mean"] = np.where(o["n_prior"] > 0, o["ret_cum"] / o["n_prior"], np.nan)
    o["rug_rate"] = np.where(o["n_prior"] > 0, o["rug_cum"] / o["n_prior"], np.nan)
    return o[[
        "code_hash", "token_birth_ts", "token", "n_prior",
        "survival_rate", "max_ret_mean", "rug_rate", "family_size_so_far",
    ]].reset_index(drop=True)


def attach_clone_family_features(
    panel: pd.DataFrame,
    *,
    swaps: pd.DataFrame | None = None,
) -> pd.DataFrame:
    ensure_alpha_v2_out()
    d = panel.copy()
    d["_row"] = np.arange(len(d))
    d["token"] = d["token"].astype(str).str.lower()
    d["decision_time"] = d["decision_time"].astype(np.int64)

    tokens = sorted(d["token"].unique().tolist())
    codes = fetch_token_code_hashes(tokens)

    if swaps is None:
        swaps = pd.read_parquet(
            ROOT / "data" / "resolved_swaps.parquet",
            columns=["token", "pool", "ts", "price_weth", "side"],
        )
    outcomes = _token_pool_outcomes(swaps)
    fam_stats = _expanding_family_stats(outcomes, codes)

    # attach code_hash + birth to each signal's token
    d = d.merge(codes[["token", "code_hash"]], on="token", how="left")
    d = d.merge(outcomes[["token", "token_birth_ts"]], on="token", how="left")
    d["code_hash"] = d["code_hash"].fillna("UNKNOWN")
    d["token_birth_ts"] = pd.to_numeric(d["token_birth_ts"], errors="coerce").fillna(0).astype(np.int64)

    # exact match on (code_hash, token_birth_ts) — stats already exclude self via shift
    left = d[["_row", "code_hash", "token_birth_ts", "token"]].copy()
    m = left.merge(
        fam_stats,
        on=["code_hash", "token_birth_ts", "token"],
        how="left",
    )
    # fallback asof for missed rows
    miss = m["n_prior"].isna() & ~m["code_hash"].isin(["EOA_OR_EMPTY", "FETCH_FAIL", "UNKNOWN"])
    if miss.any() and not fam_stats.empty:
        sub = m.loc[miss, ["_row", "code_hash", "token_birth_ts"]].copy()
        sub["_t"] = sub["token_birth_ts"]
        right = fam_stats.rename(columns={"token_birth_ts": "_t"}).sort_values(["code_hash", "_t"])
        # per-family searchsorted
        cols = ["n_prior", "survival_rate", "max_ret_mean", "rug_rate", "family_size_so_far"]
        for c in cols:
            sub[c] = np.nan
        left_g = {k: idx for k, idx in sub.groupby("code_hash", sort=False).groups.items()}
        for key, g in right.groupby("code_hash", sort=False):
            idxs = left_g.get(key)
            if idxs is None:
                continue
            keys = g["_t"].to_numpy(dtype=np.int64)
            vals = g[cols].to_numpy(dtype=float)
            times = sub.loc[idxs, "_t"].to_numpy(dtype=np.int64)
            pos = np.searchsorted(keys, times, side="left") - 1
            valid = pos >= 0
            dest = np.asarray(idxs)
            sub.loc[dest[valid], cols] = vals[pos[valid]]
        m = m.set_index("_row")
        sub = sub.set_index("_row")
        m.loc[sub.index, cols] = sub[cols]
        m = m.reset_index()

    feat = d[["_row"]].merge(
        m[["_row", "n_prior", "survival_rate", "max_ret_mean", "rug_rate", "family_size_so_far"]],
        on="_row",
        how="left",
    )
    feat = feat.rename(columns={
        "n_prior": "family_n_prior_tokens",
        "survival_rate": "family_prior_survival_rate",
        "max_ret_mean": "family_prior_max_ret_mean",
        "rug_rate": "family_prior_rug_rate",
        "family_size_so_far": "family_size",
    })
    feat["family_cold_start"] = (
        feat["family_n_prior_tokens"].isna() | (feat["family_n_prior_tokens"] <= 0)
    ).astype(float)
    for c in [
        "family_n_prior_tokens", "family_prior_survival_rate",
        "family_prior_max_ret_mean", "family_prior_rug_rate", "family_size",
    ]:
        feat[c] = pd.to_numeric(feat[c], errors="coerce").fillna(0.0)

    feat["family_score"] = (
        (1.0 - feat["family_cold_start"])
        * (
            feat["family_prior_survival_rate"]
            - feat["family_prior_rug_rate"]
            + 0.1 * feat["family_prior_max_ret_mean"].clip(0, 10)
            + 0.05 * np.log1p(feat["family_n_prior_tokens"])
        )
    )

    out_cols = [
        "family_n_prior_tokens", "family_prior_survival_rate", "family_prior_max_ret_mean",
        "family_prior_rug_rate", "family_cold_start", "family_size", "family_score",
    ]
    result = panel.copy()
    for c in out_cols:
        result[c] = feat[c].to_numpy()
    # also keep code_hash for audit
    result["code_hash"] = d["code_hash"].to_numpy()
    print(
        f"[alpha_v2/N] family_cold={result['family_cold_start'].mean():.1%} "
        f"score>0={(result['family_score']>0).mean():.1%} "
        f"n_families={result.loc[result['family_cold_start']==0, 'code_hash'].nunique() if 'code_hash' in result.columns else 0}",
        flush=True,
    )
    return result


if __name__ == "__main__":
    lab = pd.read_parquet(OUT / "panel" / "labeled_wait_absorption.parquet")
    elig = lab[(lab["scorable_primary"] == True) & (lab["buy_status"].eq("SUCCESS"))]  # noqa: E712
    attach_clone_family_features(elig.head(5000))
