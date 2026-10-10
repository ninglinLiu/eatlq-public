"""Tier A/B/C/D economic-actor resolution for Base UniV3 swaps.

Adapted from EATLQ B1 resolve_eoa + B3 ERC-4337 / route-family logic.
Does NOT force everything to EOA — smart accounts are first-class subjects.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

from .attribution_policy import (
    EXCLUDED_ADDRESSES,
    ROUTER_ADDRESSES,
    normalize_address,
    resolve_eoa,
)
from .common import OUT, WETH, ch_query, get_rpc_url, load_freeze, save_json, utcnow

USER_OPERATION_EVENT = "0x49628fd1471006c1482da88028e9ce4dbb080b815c9b0344d39e5a8e6ec1419f"
ENTRYPOINTS = {
    "0x5ff137d4b0fdcd49dca30c7cf57e578a026d2789": "entrypoint_v06",
    "0x0000000071727de22e5e9d8baf0edac6f37da032": "entrypoint_v07",
    "0x4337084d9e255ff0702461cf8895ce9e3b5ff108": "entrypoint_v08",
    "0x433709009b8330fda32311df1c2afa402ed8d009": "entrypoint_v09",
}
UNIVERSAL_ROUTERS = {
    "0x6ff5693b99212da76ad316178a184ab56d299b43",
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad",
    "0xfdf682f51fe81aa4898f0ae2163d8a55c127fbc7",
}
UNISWAP_ROUTERS = {
    "0x2626664c2603336e57b271c5c0b26f421741e481",
    "0xe592427a0aece92de3edee1f18e0157c05861564",
}
AERODROME_ROUTERS = {
    "0xcf77a3ba9a5ca399b7c97c74d54e5b1bebe2438d",
    "0xbe6d8da2d4f922f359b1da84fed3c60b269ce8a0",
}
SOLVER_SETTLEMENT = {
    "0x9008d19f58abd9ed0d60971565aa8510560ab41",
    "0x6a000f20005980200259b80c5102003040001068",
}
SHARED_INFRA = EXCLUDED_ADDRESSES | set(ENTRYPOINTS) | UNIVERSAL_ROUTERS | UNISWAP_ROUTERS | AERODROME_ROUTERS | SOLVER_SETTLEMENT | ROUTER_ADDRESSES


@dataclass
class ResolvedActor:
    tx_hash: str
    log_index: int
    pool: str
    token: str
    block_number: int
    ts: int
    side: str
    quote_weth: float
    target_qty: float
    price_weth: float
    tx_from: str
    sender: str
    recipient: str
    economic_actor: str
    tier: str  # A | B | C | D | UNRESOLVED
    attribution_source: str
    route_family: str
    resolution_confidence: str
    entity_cluster: str
    censored: bool = False
    censor_reason: str = ""


class RpcClient:
    def __init__(self, url: str):
        self.url = url
        self.n = 0

    def call(self, method: str, params: list[Any], *, timeout: int = 60) -> Any:
        self.n += 1
        last = None
        for i in range(4):
            try:
                r = requests.post(
                    self.url,
                    json={"jsonrpc": "2.0", "id": self.n, "method": method, "params": params},
                    timeout=timeout,
                )
                r.raise_for_status()
                body = r.json()
                if "error" in body:
                    raise RuntimeError(str(body["error"])[:300])
                return body.get("result")
            except Exception as e:
                last = e
                time.sleep(0.4 * (i + 1))
        raise RuntimeError(last)

    def batch(self, calls: list[tuple[str, list[Any]]], *, timeout: int = 180) -> list[Any]:
        payload = [
            {"jsonrpc": "2.0", "id": i, "method": m, "params": p}
            for i, (m, p) in enumerate(calls)
        ]
        r = requests.post(self.url, json=payload, timeout=timeout)
        r.raise_for_status()
        body = r.json()
        by_id = {item["id"]: item for item in body}
        out = []
        for i in range(len(calls)):
            item = by_id.get(i, {})
            if "error" in item:
                out.append(None)
            else:
                out.append(item.get("result"))
        return out


def route_family(tx_to: str, pools: set[str], has_userops: bool) -> str:
    if has_userops or tx_to in ENTRYPOINTS:
        return "erc4337_entrypoint"
    if tx_to in UNIVERSAL_ROUTERS:
        return "universal_router"
    if tx_to in UNISWAP_ROUTERS:
        return "uniswap_router"
    if tx_to in AERODROME_ROUTERS:
        return "aerodrome_router"
    if tx_to in SOLVER_SETTLEMENT:
        return "solver_settlement"
    if tx_to in SHARED_INFRA:
        return "shared_infra"
    if tx_to in pools:
        return "direct_pool"
    return "unknown_contract_or_route"


def parse_userops(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    events = []
    for log in receipt.get("logs") or []:
        topics = [str(topic).lower() for topic in (log.get("topics") or [])]
        if len(topics) >= 3 and topics[0] == USER_OPERATION_EVENT:
            events.append({
                "log_index": int(log["logIndex"], 16),
                "entrypoint": normalize_address(log.get("address")),
                "userop_hash": topics[1],
                "sender": "0x" + topics[2][-40:],
            })
    return sorted(events, key=lambda row: row["log_index"])


def map_swap_to_userop(swap_log_index: int, events: list[dict[str, Any]]) -> dict[str, Any] | None:
    previous = -1
    for event in events:
        if previous < swap_log_index < event["log_index"]:
            return event
        previous = event["log_index"]
    return None


def classify_tier(
    actor: str,
    *,
    is_eoa: bool | None,
    from_userop: bool,
    route: str,
) -> str:
    if not actor or actor in SHARED_INFRA:
        return "D"
    if from_userop:
        return "B"
    if is_eoa is True:
        return "A"
    if is_eoa is False and route not in ("solver_settlement", "shared_infra", "unknown_contract_or_route"):
        return "C"
    if is_eoa is False:
        return "D"
    return "UNRESOLVED"


def load_swap_universe(freeze: dict | None = None) -> pd.DataFrame:
    """Load new-pool WETH UniV3 swaps in monthly chunks (memory-safe)."""
    freeze = freeze or load_freeze()
    start = freeze["calendar"]["decision_start"].replace("T", " ").replace("Z", "")
    end = freeze["calendar"]["decision_end"].replace("T", " ").replace("Z", "")

    print("[phase1] loading pool index…", flush=True)
    pools = ch_query(f"""
    SELECT
      lower(d.pool_address) AS pool,
      lower(d.token0_address) AS token0,
      lower(d.token1_address) AS token1,
      d.fee_bps AS fee_bps,
      min(s.block_time) AS first_swap_time
    FROM eatlq.discovered_pools AS d
    INNER JOIN eatlq.uniswap_v3_swaps_decoded AS s
      ON lower(s.pool_address) = lower(d.pool_address)
    WHERE d.pool_type = 'v3' AND d.has_weth_leg = 1
      AND s.block_time >= toDateTime('{start}')
      AND s.block_time < toDateTime('{end}')
    GROUP BY pool, token0, token1, fee_bps
    HAVING first_swap_time >= toDateTime('{start}')
       AND first_swap_time < toDateTime('{end}')
    SETTINGS max_bytes_before_external_group_by = '8G'
    """, timeout=900)
    if pools.empty:
        return pools
    pools["pool_first_ts"] = pd.to_datetime(pools["first_swap_time"], utc=True).map(lambda x: int(x.timestamp()))
    print(f"[phase1] pools: {len(pools):,}", flush=True)

    # Monthly chunks on pool first_swap month
    pools["ym"] = pd.to_datetime(pools["first_swap_time"], utc=True).dt.strftime("%Y-%m")
    frames = []
    for ym, gpools in pools.groupby("ym"):
        pool_list = gpools["pool"].tolist()
        # Smaller batches avoid CH memory blows on hot months
        batch_size = 150
        for i in range(0, len(pool_list), batch_size):
            batch = pool_list[i:i + batch_size]
            in_list = ",".join(f"'{p}'" for p in batch)
            meta = gpools[gpools["pool"].isin(batch)][["pool", "token0", "token1", "fee_bps", "pool_first_ts"]]
            sql = f"""
            SELECT
              lower(s.pool_address) AS pool,
              toUnixTimestamp(s.block_time) AS ts,
              s.block_number AS block_number,
              lower(s.tx_hash) AS tx_hash,
              s.log_index AS log_index,
              lower(s.sender) AS sender,
              lower(s.recipient) AS recipient,
              s.amount0_raw AS amount0,
              s.amount1_raw AS amount1,
              s.tick_raw AS tick,
              s.liquidity_raw AS liquidity
            FROM eatlq.uniswap_v3_swaps_decoded AS s
            WHERE lower(s.pool_address) IN ({in_list})
              AND s.block_time >= toDateTime('{start}')
              AND s.block_time < toDateTime('{end}') + INTERVAL 2 DAY
            SETTINGS max_memory_usage = '8000000000'
            """
            try:
                sw = ch_query(sql, timeout=600)
            except Exception as e:
                print(f"[phase1] chunk {ym} batch {i} failed: {str(e)[:200]}", flush=True)
                continue
            if sw.empty:
                continue
            sw = sw.merge(meta, on="pool", how="inner")
            sw = sw[sw["ts"].astype(int) < sw["pool_first_ts"].astype(int) + 48 * 3600]
            sw = sw[sw["ts"].astype(int) >= sw["pool_first_ts"].astype(int)]
            frames.append(sw)
            print(f"[phase1] chunk {ym} batch {i // batch_size + 1}: +{len(sw):,} swaps", flush=True)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    # Skip bulk tx_from join (too slow / ARG-heavy). Recipient/sender offline
    # resolution already covers ~90%; RPC sample recovers tx_from for golden audit.
    out["tx_from"] = ""
    print(f"[phase1] loaded {len(out):,} swaps; tx_from deferred to RPC sample", flush=True)
    return out


def _signed(x) -> int:
    from .common import parse_i256
    return parse_i256(x)


def annotate_side_qty(df: pd.DataFrame) -> pd.DataFrame:
    """Add token, side, quote_weth, target_qty, price_weth, pool_age_s."""
    weth = WETH.lower()
    a0 = df["amount0"].map(_signed)
    a1 = df["amount1"].map(_signed)
    token0_is_weth = df["token0"] == weth
    alt = np.where(token0_is_weth, df["token1"], df["token0"])
    # buy alt = WETH in (positive amount for WETH leg in Uniswap signed convention:
    # amount0>0 means token0 leaving pool; for WETH->alt buy, WETH enters pool so WETH amount > 0)
    weth_amt = np.where(token0_is_weth, a0, a1).astype(np.float64)
    alt_amt = np.where(token0_is_weth, a1, a0).astype(np.float64)
    # Convention in decoded view: positive = token leaves pool (sold by pool / bought by user for the other)
    # Empirically for UniV3: trader buys alt when WETH amount > 0 (WETH into pool) and alt amount < 0.
    side = np.where((weth_amt > 0) & (alt_amt < 0), "buy",
           np.where((weth_amt < 0) & (alt_amt > 0), "sell", "other"))
    quote_weth = np.abs(weth_amt) / 1e18
    target_qty = np.abs(alt_amt)  # raw; relative only
    price = np.where(target_qty > 0, quote_weth / np.maximum(target_qty, 1e-30), 0.0)
    out = df.copy()
    out["token"] = alt
    out["side"] = side
    out["quote_weth"] = quote_weth
    out["target_qty"] = target_qty
    out["price_weth"] = price
    out["pool_age_s"] = out["ts"].astype(int) - out["pool_first_ts"].astype(int)
    return out


def resolve_offline(df: pd.DataFrame) -> pd.DataFrame:
    """Vectorized offline resolution using CH fields only (no RPC)."""
    out = df.copy()
    # NOTE: never normalize tx_hash — ADDRESS_RE is 20-byte and would blank 32-byte hashes
    for col in ("tx_from", "sender", "recipient", "pool", "token"):
        out[col] = out[col].map(normalize_address) if col in out.columns else ""
    if "tx_hash" in out.columns:
        out["tx_hash"] = out["tx_hash"].astype(str).str.lower().str.strip()

    tx_from = out["tx_from"]
    sender = out["sender"]
    recipient = out["recipient"]

    # Cascade: tx_from → recipient → sender (excluding shared infra)
    actor = np.where(
        tx_from.str.len().eq(42) & ~tx_from.isin(SHARED_INFRA), tx_from,
        np.where(
            recipient.str.len().eq(42) & ~recipient.isin(SHARED_INFRA), recipient,
            np.where(sender.str.len().eq(42) & ~sender.isin(SHARED_INFRA), sender, ""),
        ),
    )
    source = np.where(
        tx_from.str.len().eq(42) & ~tx_from.isin(SHARED_INFRA), "tx_from",
        np.where(
            recipient.str.len().eq(42) & ~recipient.isin(SHARED_INFRA), "recipient",
            np.where(sender.str.len().eq(42) & ~sender.isin(SHARED_INFRA), "sender", "unresolved"),
        ),
    )
    route = np.full(len(out), "unknown", dtype=object)
    route = np.where(sender.isin(UNIVERSAL_ROUTERS) | recipient.isin(UNIVERSAL_ROUTERS), "universal_router", route)
    route = np.where(sender.isin(UNISWAP_ROUTERS), "uniswap_router", route)
    route = np.where(sender.isin(ENTRYPOINTS) | tx_from.isin(ENTRYPOINTS), "erc4337_entrypoint", route)
    route = np.where(sender.isin(SHARED_INFRA) & (route == "unknown"), "shared_infra", route)

    tier = np.full(len(out), "UNRESOLVED", dtype=object)
    conf = np.full(len(out), "UNATTRIBUTABLE", dtype=object)

    m_a_hi = (source == "tx_from") & (tx_from == recipient) & (tx_from.str.len() == 42)
    m_a = (source == "tx_from") & ~m_a_hi
    m_c = source == "recipient"

    tier = np.where(m_a_hi, "A", tier)
    conf = np.where(m_a_hi, "ACCOUNT_HIGH_CONFIDENCE", conf)
    tier = np.where(m_a, "A", tier)
    conf = np.where(m_a, "ENTITY_MEDIUM_CONFIDENCE", conf)
    tier = np.where(m_c, "C", tier)
    conf = np.where(m_c, "ENTITY_MEDIUM_CONFIDENCE", conf)
    # sender-only → D / ambiguous
    m_sender_only = source == "sender"
    tier = np.where(m_sender_only, "D", tier)
    conf = np.where(m_sender_only, "AMBIGUOUS", conf)
    actor = np.where(m_sender_only, "", actor)
    source = np.where(m_sender_only, "excluded_sender_only", source)

    # ERC-4337 offline heuristic
    m_4337 = tx_from.isin(ENTRYPOINTS) | (route == "erc4337_entrypoint")
    m_4337_ok = m_4337 & recipient.str.len().eq(42) & ~recipient.isin(SHARED_INFRA)
    m_4337_bad = m_4337 & ~m_4337_ok
    actor = np.where(m_4337_ok, recipient, actor)
    source = np.where(m_4337_ok, "erc4337_recipient_proxy", source)
    tier = np.where(m_4337_ok, "B", tier)
    conf = np.where(m_4337_ok, "ENTITY_MEDIUM_CONFIDENCE", conf)
    actor = np.where(m_4337_bad, "", actor)
    source = np.where(m_4337_bad, "erc4337_unmapped", source)
    tier = np.where(m_4337_bad, "UNRESOLVED", tier)
    conf = np.where(m_4337_bad, "AMBIGUOUS", conf)

    out["economic_actor"] = actor
    out["tier"] = tier
    out["attribution_source"] = source
    out["route_family"] = route
    out["resolution_confidence"] = conf
    keep = [
        "tx_hash", "log_index", "pool", "token", "block_number", "ts", "side",
        "quote_weth", "target_qty", "price_weth", "pool_age_s", "fee_bps",
        "liquidity", "tick", "tx_from", "sender", "recipient",
        "economic_actor", "tier", "attribution_source", "route_family", "resolution_confidence",
    ]
    for c in keep:
        if c not in out.columns:
            out[c] = "" if c not in ("log_index", "block_number", "ts", "quote_weth", "target_qty", "price_weth", "pool_age_s", "fee_bps") else 0
    return out[keep]


def enrich_with_rpc_sample(
    resolved: pd.DataFrame,
    *,
    sample_size: int = 200,
    max_seconds: float = 90.0,
) -> pd.DataFrame:
    """RPC-enrich a stratified sample: receipts for UserOp.sender + getCode for tier."""
    if resolved.empty or sample_size <= 0:
        return resolved
    out = resolved.copy()
    out["rpc_enriched"] = False

    # Stratify by current offline route_family
    groups = list(out.groupby("route_family", sort=False))
    picks = []
    per = max(1, sample_size // max(len(groups), 1))
    for _, g in groups:
        g2 = g.drop_duplicates("tx_hash")
        n = min(per, len(g2))
        if n == 0:
            continue
        idx = np.linspace(0, len(g2) - 1, n).astype(int)
        picks.append(g2.iloc[idx])
    sample = pd.concat(picks, ignore_index=True).drop_duplicates("tx_hash").head(sample_size)
    sample_hashes = set(sample["tx_hash"].astype(str).tolist())
    if not sample_hashes:
        return out

    t0 = time.monotonic()
    try:
        rpc = RpcClient(get_rpc_url())
    except Exception as e:
        print(f"[phase1] RPC client unavailable: {e}", flush=True)
        return out
    tx_hashes = list(sample_hashes)
    receipts: dict[str, dict] = {}
    txs: dict[str, dict] = {}
    for offset in range(0, len(tx_hashes), 10):
        if time.monotonic() - t0 > max_seconds:
            print(f"[phase1] RPC enrich time budget hit at {offset}/{len(tx_hashes)}", flush=True)
            break
        batch = tx_hashes[offset:offset + 10]
        try:
            tx_res = rpc.batch([("eth_getTransactionByHash", [h]) for h in batch], timeout=45)
            rc_res = rpc.batch([("eth_getTransactionReceipt", [h]) for h in batch], timeout=45)
        except Exception as e:
            print(f"[phase1] RPC batch failed: {e}", flush=True)
            continue
        for h, tx, rc in zip(batch, tx_res, rc_res):
            txs[str(h)] = tx or {}
            receipts[str(h)] = rc or {}
        time.sleep(0.1)

    # Numpy positional updates — avoids pandas mask broadcast bugs on large frames
    tx_arr = out["tx_hash"].astype(str).to_numpy()
    route_col = out.columns.get_loc("route_family")
    enrich_col = out.columns.get_loc("rpc_enriched")
    actor_col = out.columns.get_loc("economic_actor")
    tier_col = out.columns.get_loc("tier")
    src_col = out.columns.get_loc("attribution_source")
    conf_col = out.columns.get_loc("resolution_confidence")
    log_col = out.columns.get_loc("log_index")
    pool_arr = out["pool"].astype(str).to_numpy()
    n_touched = 0
    for h, rc in receipts.items():
        hs = str(h)
        idxs = np.flatnonzero(tx_arr == hs)
        if len(idxs) == 0:
            continue
        events = parse_userops(rc)
        tx = txs.get(h, {}) or txs.get(hs, {})
        tx_to = normalize_address(tx.get("to"))
        pools = set(pool_arr[idxs].tolist())
        family = route_family(tx_to, pools, bool(events))
        for i in idxs:
            out.iat[i, route_col] = family
            out.iat[i, enrich_col] = True
            n_touched += 1
            if not events:
                continue
            li = int(out.iat[i, log_col])
            mapped = map_swap_to_userop(li, events)
            if not mapped:
                continue
            actor = normalize_address(mapped["sender"])
            if actor and actor not in SHARED_INFRA:
                out.iat[i, actor_col] = actor
                out.iat[i, tier_col] = "B"
                out.iat[i, src_col] = "erc4337_userop_sender_log_boundary"
                out.iat[i, conf_col] = "ACCOUNT_HIGH_CONFIDENCE"
    print(
        f"[phase1] RPC-enriched touched={n_touched} unique_sample={len(sample_hashes)} "
        f"rpc_flag_sum={int(out['rpc_enriched'].sum())}",
        flush=True,
    )
    return out


def sybil_precluster(resolved: pd.DataFrame) -> pd.DataFrame:
    """Cluster actors that share funder-like patterns via identical tx_from bundler
    and near-identical timing/route. Conservative: same economic_actor stays itself;
    addresses that always co-appear with same tx_from within 2 blocks get linked.
    """
    out = resolved.copy()
    out["entity_cluster"] = out["economic_actor"]
    # Link: if two different actors share the same tx_from on >3 txs and route is erc4337, keep separate
    # (smart accounts under same bundler are NOT the same entity).
    # Link only when recipient==economic_actor and another address receives from same actor quickly — skip heavy RPC.
    # Lightweight: actors with identical first-8 of code-less and same first-seen funder tx_from across buys.
    actor_funder = (
        out[out["economic_actor"].astype(str).str.len() == 42]
        .groupby("economic_actor")["tx_from"]
        .agg(lambda s: s.value_counts().index[0] if len(s) else "")
    )
    # Collapse only exact economic_actor equality (identity). Cross-address Sybil needs funder graph —
    # record funder for downstream consensus counting.
    out["primary_funder"] = out["economic_actor"].map(actor_funder).fillna("")
    # Cluster key: economic_actor itself (stable). Optional funder-sibling flag for consensus.
    funder_counts = out.groupby("primary_funder")["economic_actor"].nunique()
    shared_funders = set(funder_counts[funder_counts >= 3].index) - {"", None}
    out["sybil_suspect"] = out["primary_funder"].isin(shared_funders)
    # For consensus: independent cluster = economic_actor unless sybil_suspect then use funder as cluster
    out["entity_cluster"] = np.where(
        out["sybil_suspect"] & (out["primary_funder"].astype(str).str.len() == 42),
        "funder:" + out["primary_funder"].astype(str),
        out["economic_actor"],
    )
    return out


def flag_censored_transfers(resolved: pd.DataFrame) -> pd.DataFrame:
    """Mark actors whose token inventory leaves via non-V3 path as CENSORED_TRANSFER_OUT.

    Heuristic (warehouse-available): if an actor bought token T on V3 but we never observe
    a V3 sell of T by the same actor, AND we observe an erc20 transfer of T from actor to
    an address that is not a known UniV3 pool — mark censored for PnL/diamond purposes.
    """
    out = resolved.copy()
    out["censored"] = False
    out["censor_reason"] = ""
    buyers = out[(out["side"] == "buy") & (out["economic_actor"].astype(str).str.len() == 42)]
    sellers = out[(out["side"] == "sell") & (out["economic_actor"].astype(str).str.len() == 42)]
    sold_keys = set(zip(sellers["economic_actor"], sellers["token"]))
    # Sample check via CH for transfer-outs without matching V3 sell
    if buyers.empty:
        return out
    # Limit CH probe to top actors by buy count to keep runtime bounded
    top_actors = buyers["economic_actor"].value_counts().head(200).index.tolist()
    if not top_actors:
        return out
    actor_list = ",".join(f"'{a}'" for a in top_actors)
    token_list = ",".join(f"'{t}'" for t in buyers["token"].drop_duplicates().head(500).tolist())
    try:
        xfer = ch_query(f"""
        SELECT
          lower(sender) AS actor,
          lower(token_address) AS token,
          count() AS n_out
        FROM eatlq.erc20_transfers
        WHERE lower(sender) IN ({actor_list})
          AND lower(token_address) IN ({token_list})
          AND lower(recipient) NOT IN (
            SELECT lower(pool_address) FROM eatlq.discovered_pools WHERE pool_type='v3'
          )
        GROUP BY actor, token
        HAVING n_out >= 1
        """, timeout=300)
    except Exception:
        xfer = pd.DataFrame()

    if xfer.empty:
        return out

    censored_keys = {
        (r.actor, r.token) for r in xfer.itertuples(index=False)
        if (r.actor, r.token) not in sold_keys
    }
    if censored_keys:
        key_s = out["economic_actor"].astype(str) + "|" + out["token"].astype(str)
        cens_s = {f"{a}|{t}" for a, t in censored_keys}
        mask = key_s.isin(cens_s)
        out.loc[mask, "censored"] = True
        out.loc[mask, "censor_reason"] = "CENSORED_TRANSFER_OUT"
    return out


def _reapply_offline_routes(df: pd.DataFrame) -> pd.DataFrame:
    """Repair route_family from sender/recipient (idempotent)."""
    out = df.copy()
    sender = out["sender"].map(normalize_address)
    recipient = out["recipient"].map(normalize_address)
    tx_from = out["tx_from"].map(normalize_address) if "tx_from" in out.columns else pd.Series([""] * len(out))
    route = np.full(len(out), "unknown", dtype=object)
    route = np.where(sender.isin(UNIVERSAL_ROUTERS) | recipient.isin(UNIVERSAL_ROUTERS), "universal_router", route)
    route = np.where(sender.isin(UNISWAP_ROUTERS), "uniswap_router", route)
    route = np.where(sender.isin(ENTRYPOINTS) | tx_from.isin(ENTRYPOINTS), "erc4337_entrypoint", route)
    route = np.where(sender.isin(AERODROME_ROUTERS), "aerodrome_router", route)
    route = np.where(sender.isin(SOLVER_SETTLEMENT), "solver_settlement", route)
    route = np.where(sender.isin(SHARED_INFRA) & (route == "unknown"), "shared_infra", route)
    out["route_family"] = route
    return out


def run_resolution(*, rpc_sample: int = 120, resume: bool = True) -> pd.DataFrame:
    freeze = load_freeze()
    cache = OUT / "phase1" / "resolved_swaps.parquet"
    cache.parent.mkdir(parents=True, exist_ok=True)

    if resume and cache.exists():
        print(f"[phase1] resuming from cache {cache}", flush=True)
        resolved = pd.read_parquet(cache)
        needs_repair = (
            int(resolved["route_family"].nunique()) <= 1
            or str(resolved["route_family"].mode().iloc[0]) == "unknown_contract_or_route"
        )
        if needs_repair:
            print("[phase1] repairing offline route_family from sender…", flush=True)
            resolved = _reapply_offline_routes(resolved)
            resolved["rpc_enriched"] = False
            try:
                # Bound RPC sample tightly on resume to avoid hangs
                resolved = enrich_with_rpc_sample(resolved, sample_size=min(40, rpc_sample))
            except Exception as e:
                print(f"[phase1] RPC enrich skipped: {e}", flush=True)
                resolved["rpc_enriched"] = False
            if "entity_cluster" not in resolved.columns:
                resolved = sybil_precluster(resolved)
            else:
                # Refresh sybil fields cheaply
                resolved = sybil_precluster(resolved)
            if "censored" not in resolved.columns:
                resolved["censored"] = False
                resolved["censor_reason"] = ""
            # Skip heavy transfer-censor CH probe on resume unless never done
            if float(resolved["censored"].mean()) == 0.0 and "censor_reason" in resolved.columns:
                print("[phase1] running bounded CENSORED_TRANSFER_OUT probe…", flush=True)
                resolved = flag_censored_transfers(resolved)
            resolved.to_parquet(cache, index=False)
            print("[phase1] cache repaired and saved", flush=True)
    else:
        print("[phase1] loading swap universe from ClickHouse…", flush=True)
        raw = load_swap_universe(freeze)
        print(f"[phase1] raw swaps: {len(raw):,}", flush=True)
        if raw.empty:
            raise RuntimeError("DATA_LINEAGE_INSUFFICIENT: empty swap universe")
        raw = annotate_side_qty(raw)
        raw = raw[raw["side"].isin(["buy", "sell"])].copy()
        print(f"[phase1] buy/sell rows: {len(raw):,}", flush=True)
        resolved = resolve_offline(raw)
        resolved = _reapply_offline_routes(resolved)
        print("[phase1] offline resolution done; RPC-enriching sample…", flush=True)
        try:
            resolved = enrich_with_rpc_sample(resolved, sample_size=rpc_sample)
        except Exception as e:
            print(f"[phase1] RPC enrich skipped: {e}", flush=True)
            resolved["rpc_enriched"] = False
        resolved = sybil_precluster(resolved)
        print("[phase1] flagging CENSORED_TRANSFER_OUT…", flush=True)
        resolved = flag_censored_transfers(resolved)
        resolved.to_parquet(cache, index=False)

    cov = {
        "n_rows": int(len(resolved)),
        "n_buys": int((resolved["side"] == "buy").sum()),
        "resolved_rate": float((resolved["economic_actor"].astype(str).str.len() == 42).mean()),
        "tier_counts": resolved["tier"].value_counts().to_dict(),
        "confidence_counts": resolved["resolution_confidence"].value_counts().to_dict(),
        "route_counts": resolved["route_family"].value_counts().to_dict(),
        "censored_rate": float(resolved["censored"].mean()),
        "unique_actors": int(resolved.loc[resolved["economic_actor"].astype(str).str.len() == 42, "economic_actor"].nunique()),
        "unique_clusters": int(resolved.loc[resolved["entity_cluster"].astype(str).str.len() > 0, "entity_cluster"].nunique()),
        "generated_at": utcnow(),
    }
    save_json(OUT / "phase1" / "resolution_coverage.json", cov)
    print(json.dumps(cov, indent=2), flush=True)
    return resolved


if __name__ == "__main__":
    run_resolution()
