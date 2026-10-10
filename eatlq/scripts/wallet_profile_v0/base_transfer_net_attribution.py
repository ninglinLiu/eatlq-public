#!/usr/bin/env python3
"""B2 research-only target-token transfer-net attribution for Base swaps."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from base_attribution import (
    ADDRESS_RE,
    CHAIN_ID,
    EXCLUDED_ADDRESSES,
    EXCLUSION_ROWS,
    MANIFEST,
    QUOTE_TOKENS,
    normalize_address,
)
from ch_helper import DEFAULT_CH_CONTAINER, ch_query

REPO = Path(__file__).resolve().parents[2]
DDL = REPO / "infra/clickhouse/init/042_base_cha_wallet_swaps.sql"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
WETH = "0x4200000000000000000000000000000000000006"
QUOTE_PRIORITY = tuple(
    row["address"] for row in EXCLUSION_ROWS if row["category"] == "quote_token"
)
# B2-v3 holder audit: 4/5 uniformly sampled multi-swap subgraph cases failed
# independent full-window balanceOf reconciliation.  Keep the implementation
# for diagnosis, but do not materialize its resolutions until a new golden
# protocol is explicitly approved.
TRANSFER_SUBGRAPH_GOLDEN_APPROVED = False


@dataclass(frozen=True)
class NetResolution:
    account: str
    source: str
    net_delta_raw: int
    candidate_count: int
    is_infra_only: bool
    residual_bucket: str


def _client(
    sql: str,
    *,
    multiquery: bool = False,
    json_rows: Iterable[dict[str, Any]] | None = None,
    timeout: int = 600,
) -> None:
    command = ["docker", "exec", "-i", DEFAULT_CH_CONTAINER, "clickhouse-client"]
    if multiquery:
        command.append("--multiquery")
    payload = sql
    if json_rows is not None:
        payload += "\n" + "\n".join(
            json.dumps(row, separators=(",", ":")) for row in json_rows
        )
    result = subprocess.run(
        command,
        input=payload,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[:1200])


def install() -> None:
    _client(DDL.read_text(encoding="utf-8"), multiquery=True)
    # B1 owns this research authority table; B2 re-syncs it after manifest changes.
    _client("TRUNCATE TABLE eatlq.base_wallet_attribution_exclusions")
    values = []
    for row in EXCLUSION_ROWS:
        label = row["label"].replace("'", "\\'")
        values.append(
            f"({CHAIN_ID},'{row['address']}','{row['category']}','{label}',"
            f"{int(MANIFEST['schema_version'])})"
        )
    _client(
        "INSERT INTO eatlq.base_wallet_attribution_exclusions "
        "(chain_id,address,category,label,schema_version) VALUES " + ",".join(values)
    )


def choose_quote_target(token0: object, token1: object) -> tuple[str, str, int]:
    """Return quote, target and target index under WETH-first frozen policy."""
    t0, t1 = normalize_address(token0), normalize_address(token1)
    if not t0 or not t1 or t0 == t1:
        return "", "", -1
    if t0 == WETH:
        return t0, t1, 1
    if t1 == WETH:
        return t1, t0, 0
    for quote in QUOTE_PRIORITY:
        if t0 == quote:
            return t0, t1, 1
        if t1 == quote:
            return t1, t0, 0
    return "", "", -1


def v3_side(amount0: int, amount1: int, target_index: int) -> str:
    amount = int(amount0) if target_index == 0 else int(amount1) if target_index == 1 else 0
    return "buy" if amount < 0 else "sell" if amount > 0 else "unknown"


def v2_side(row: dict[str, Any], target_index: int) -> str:
    if target_index == 0:
        amount_in, amount_out = int(row["amount0_in"]), int(row["amount0_out"])
    elif target_index == 1:
        amount_in, amount_out = int(row["amount1_in"]), int(row["amount1_out"])
    else:
        return "unknown"
    if amount_out and not amount_in:
        return "buy"
    if amount_in and not amount_out:
        return "sell"
    return "unknown"


def transfer_net(
    transfers: Iterable[dict[str, Any]],
) -> dict[str, int]:
    net: dict[str, int] = defaultdict(int)
    for transfer in transfers:
        amount = int(transfer["amount"])
        sender = normalize_address(transfer["sender"])
        recipient = normalize_address(transfer["recipient"])
        if sender:
            net[sender] -= amount
        if recipient:
            net[recipient] += amount
    return dict(net)


def resolve_transfer_net(
    *,
    side: str,
    transfers: list[dict[str, Any]],
    pool_address: str,
    tx_from: str = "",
    fallback_recipient: str = "",
) -> NetResolution:
    """Resolve the unique beneficial account; never pick among tied candidates."""
    pool = normalize_address(pool_address)
    infrastructure = EXCLUDED_ADDRESSES | ({pool} if pool else set())
    net = transfer_net(transfers)
    sign = 1 if side == "buy" else -1 if side == "sell" else 0
    noninfra = {
        address: delta
        for address, delta in net.items()
        if address not in infrastructure and delta != 0
    }
    candidates = {
        address: delta for address, delta in noninfra.items() if delta * sign > 0
    } if sign else {}
    if len(candidates) == 1:
        account, delta = next(iter(candidates.items()))
        source = "tx_from_confirmed" if account == normalize_address(tx_from) else "transfer_net"
        code_bucket = "resolved_pending_code"
        return NetResolution(account, source, delta, 1, False, code_bucket)
    if len(candidates) > 1:
        return NetResolution("", "ambiguous", 0, len(candidates), False, "ambiguous")
    if transfers:
        infra_changed = any(
            delta != 0 and address in infrastructure for address, delta in net.items()
        )
        recipient = normalize_address(fallback_recipient)
        fallback = recipient if recipient and recipient not in infrastructure else ""
        return NetResolution(
            fallback,
            "recipient_fallback" if fallback else "unresolved",
            0,
            0,
            infra_changed,
            "infra_only" if infra_changed else "unresolved",
        )
    recipient = normalize_address(fallback_recipient)
    fallback = recipient if recipient and recipient not in infrastructure else ""
    return NetResolution(
        fallback,
        "recipient_fallback" if fallback else "unresolved",
        0,
        0,
        False,
        "no_transfer",
    )


def resolve_swap_transfer_subgraph(
    *,
    side: str,
    transfers: list[dict[str, Any]],
    pool_address: str,
    swap_log_index: int,
    previous_swap_log_index: int | None = None,
    next_swap_log_index: int | None = None,
    tx_from: str = "",
) -> NetResolution:
    """Resolve one swap through its ordered, exact-amount transfer subgraph.

    This is deliberately stricter than choosing a recipient.  A pool-touching
    transfer must be uniquely anchored in this swap's log interval, every
    continuation must be a unique monotone-flow edge, and the terminal address
    must also be a same-direction transaction-net beneficiary.  Exact amounts
    are preferred but a single decreasing buy-path / increasing reverse sell-
    path is allowed for taxed tokens.  Any branch, merge, missing continuation,
    or interval collision stays ambiguous.
    """
    if side not in ("buy", "sell"):
        return NetResolution("", "ambiguous", 0, 0, False, "ambiguous")
    pool = normalize_address(pool_address)
    ordered = sorted(
        (
            {
                **row,
                "sender": normalize_address(row.get("sender")),
                "recipient": normalize_address(row.get("recipient")),
                "amount": int(row.get("amount", 0)),
                "log_index": int(row.get("log_index", -1)),
            }
            for row in transfers
        ),
        key=lambda row: row["log_index"],
    )
    lower = -1 if previous_swap_log_index is None else int(previous_swap_log_index)
    upper = (1 << 63) if next_swap_log_index is None else int(next_swap_log_index)
    if side == "buy":
        anchors = [
            row for row in ordered
            if lower < row["log_index"] < int(swap_log_index)
            and row["sender"] == pool and row["recipient"] != pool
        ]
    else:
        anchors = [
            row for row in ordered
            if lower < row["log_index"] < int(swap_log_index)
            and row["recipient"] == pool and row["sender"] != pool
        ]
    if not anchors:
        return NetResolution("", "ambiguous", 0, 0, False, "ambiguous")
    nearest_index = max(row["log_index"] for row in anchors)
    nearest = [row for row in anchors if row["log_index"] == nearest_index]
    if len(nearest) != 1:
        return NetResolution("", "ambiguous", 0, len(nearest), False, "ambiguous")
    anchor = nearest[0]
    amount = anchor["amount"]
    current = anchor["recipient"] if side == "buy" else anchor["sender"]
    cursor = anchor["log_index"]
    visited = {pool}
    while current and current not in visited:
        visited.add(current)
        if side == "buy":
            continuations = [
                row for row in ordered
                if cursor < row["log_index"] < upper
                and row["sender"] == current and 0 < row["amount"] <= amount
            ]
            if not continuations:
                break
            earliest = min(row["log_index"] for row in continuations)
            edges = [row for row in continuations if row["log_index"] == earliest]
            if len(edges) != 1:
                return NetResolution("", "ambiguous", 0, len(edges), False, "ambiguous")
            current, cursor, amount = (
                edges[0]["recipient"], edges[0]["log_index"], edges[0]["amount"]
            )
        else:
            continuations = [
                row for row in ordered
                if lower < row["log_index"] < cursor
                and row["recipient"] == current and row["amount"] >= amount
            ]
            if not continuations:
                break
            latest = max(row["log_index"] for row in continuations)
            edges = [row for row in continuations if row["log_index"] == latest]
            if len(edges) != 1:
                return NetResolution("", "ambiguous", 0, len(edges), False, "ambiguous")
            current, cursor, amount = (
                edges[0]["sender"], edges[0]["log_index"], edges[0]["amount"]
            )

    sign = 1 if side == "buy" else -1
    net = transfer_net(ordered)
    infrastructure = EXCLUDED_ADDRESSES | ({pool} if pool else set())
    net_candidates = {
        address: delta for address, delta in net.items()
        if address not in infrastructure and delta * sign > 0
    }
    if current in net_candidates:
        source = "tx_from_confirmed" if current == normalize_address(tx_from) else "transfer_subgraph"
        return NetResolution(
            current, source, net_candidates[current], len(net_candidates), False,
            "resolved_pending_code",
        )
    return NetResolution("", "ambiguous", 0, len(net_candidates), False, "ambiguous")


def load_swaps(start_block: int, end_block: int) -> list[dict[str, Any]]:
    pool_meta = """
      SELECT chain_id, lower(pool_address) AS venue,
             anyLast(lower(token0_address)) AS token0,
             anyLast(lower(token1_address)) AS token1,
             anyLast(lower(pool_type)) AS pool_type
      FROM eatlq.discovered_pools FINAL
      GROUP BY chain_id, venue
    """
    v3 = ch_query(
        f"""
        SELECT
          s.chain_id, multiIf(pm.pool_type='aero_cl','aero_cl','uniswap_v3') AS protocol,
          lower(s.pool_address) AS venue_address, s.block_number,
          toString(s.block_time) AS block_time, lower(s.tx_hash) AS tx_hash,
          s.log_index, lower(s.sender) AS swap_sender, lower(s.recipient) AS swap_recipient,
          pm.token0, pm.token1, toString(s.amount0) AS amount0, toString(s.amount1) AS amount1,
          '' AS amount0_in, '' AS amount0_out, '' AS amount1_in, '' AS amount1_out
        FROM eatlq.uniswap_v3_swaps_decoded s
        INNER JOIN ({pool_meta}) pm
          ON s.chain_id=pm.chain_id AND lower(s.pool_address)=pm.venue
        WHERE s.chain_id={CHAIN_ID} AND s.block_number BETWEEN {start_block} AND {end_block}
        """,
        timeout=600,
    )
    v2 = ch_query(
        f"""
        SELECT
          s.chain_id, multiIf(pm.pool_type='aero_vamm','aero_vamm','uniswap_v2') AS protocol,
          lower(s.pair_address) AS venue_address, s.block_number,
          toString(s.block_time) AS block_time, lower(s.tx_hash) AS tx_hash,
          s.log_index, lower(s.sender) AS swap_sender, lower(s.recipient) AS swap_recipient,
          pm.token0, pm.token1, '' AS amount0, '' AS amount1,
          toString(s.amount0_in) AS amount0_in, toString(s.amount0_out) AS amount0_out,
          toString(s.amount1_in) AS amount1_in, toString(s.amount1_out) AS amount1_out
        FROM eatlq.uniswap_v2_swaps_decoded s
        INNER JOIN ({pool_meta}) pm
          ON s.chain_id=pm.chain_id AND lower(s.pair_address)=pm.venue
        WHERE s.chain_id={CHAIN_ID} AND s.block_number BETWEEN {start_block} AND {end_block}
        """,
        timeout=600,
    )
    swaps = v3 + v2
    for row in swaps:
        quote, target, target_index = choose_quote_target(row["token0"], row["token1"])
        row["quote_token_address"] = quote
        row["target_token_address"] = target
        row["target_index"] = target_index
        row["side"] = (
            v3_side(int(row["amount0"]), int(row["amount1"]), target_index)
            if row["protocol"] in ("uniswap_v3", "aero_cl")
            else v2_side(row, target_index)
        )
    return swaps


class RpcClient:
    def __init__(self, url: str) -> None:
        self.url = url
        self.calls = 0

    def call(self, method: str, params: list[Any], timeout: int = 90) -> Any:
        self.calls += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self.calls, "method": method, "params": params})
        request = Request(
            self.url,
            data=body.encode(),
            headers={"Content-Type": "application/json", "User-Agent": "EATLQ-research/1.0"},
            method="POST",
        )
        with urlopen(request, timeout=timeout) as response:
            decoded = json.loads(response.read())
        if decoded.get("error"):
            raise RuntimeError(str(decoded["error"]))
        return decoded.get("result")

    def batch(self, calls: list[tuple[str, list[Any]]], timeout: int = 90) -> list[Any]:
        payload = [
            {"jsonrpc": "2.0", "id": index, "method": method, "params": params}
            for index, (method, params) in enumerate(calls)
        ]
        request = Request(
            self.url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "EATLQ-research/1.0"},
            method="POST",
        )
        with urlopen(request, timeout=timeout) as response:
            decoded = json.loads(response.read())
        by_id = {row["id"]: row for row in decoded}
        results = []
        for index in range(len(calls)):
            row = by_id[index]
            if row.get("error"):
                raise RuntimeError(str(row["error"]))
            results.append(row.get("result"))
        self.calls += 1
        return results


def _decode_transfer(log: dict[str, Any]) -> dict[str, Any] | None:
    topics = log.get("topics") or []
    data = str(log.get("data", ""))
    if len(topics) != 3 or len(data) != 66 or not data.startswith("0x"):
        return None
    return {
        "chain_id": CHAIN_ID,
        "block_number": int(log["blockNumber"], 16),
        "block_time": time.strftime(
            "%Y-%m-%d %H:%M:%S", time.gmtime(int(log.get("blockTimestamp", "0x0"), 16))
        ) if log.get("blockTimestamp") else None,
        "tx_hash": str(log["transactionHash"]).lower(),
        "log_index": int(log["logIndex"], 16),
        "token_address": str(log["address"]).lower(),
        "sender": "0x" + str(topics[1])[-40:].lower(),
        "recipient": "0x" + str(topics[2])[-40:].lower(),
        "amount": str(int(data, 16)),
    }


def fetch_target_transfers(
    swaps: list[dict[str, Any]],
    rpc: RpcClient,
    start_block: int,
    end_block: int,
    receipt_batch_size: int,
    receipt_workers: int,
    receipt_batch_delay: float,
) -> dict[str, int]:
    wanted_by_tx: dict[str, set[str]] = defaultdict(set)
    for row in swaps:
        if row["target_token_address"]:
            wanted_by_tx[row["tx_hash"]].add(row["target_token_address"])
    tx_hashes = sorted(wanted_by_tx)
    _client("TRUNCATE TABLE eatlq.base_target_transfers_b2")
    inserted = fetched = 0
    for offset in range(0, len(tx_hashes), receipt_batch_size):
        batch = tx_hashes[offset:offset + receipt_batch_size]

        def fetch_receipt(tx_hash: str) -> dict[str, Any]:
            for attempt in range(5):
                try:
                    receipt = rpc.call("eth_getTransactionReceipt", [tx_hash], timeout=60)
                    if receipt is None:
                        raise RuntimeError(f"archive RPC returned null receipt for {tx_hash}")
                    return receipt
                except Exception:
                    if attempt == 4:
                        raise
                    time.sleep(0.5 * (attempt + 1))
            raise AssertionError("unreachable")

        for attempt in range(4):
            try:
                receipts = rpc.batch(
                    [("eth_getTransactionReceipt", [tx_hash]) for tx_hash in batch],
                    timeout=180,
                )
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(1.5 * (attempt + 1))
        missing_indexes = [index for index, receipt in enumerate(receipts) if receipt is None]
        if missing_indexes:
            with ThreadPoolExecutor(max_workers=receipt_workers) as executor:
                recovered = list(executor.map(fetch_receipt, [batch[index] for index in missing_indexes]))
            for index, receipt in zip(missing_indexes, recovered):
                receipts[index] = receipt
        rows = []
        for tx_hash, receipt in zip(batch, receipts):
            for log in (receipt or {}).get("logs", []):
                fetched += 1
                topics = log.get("topics") or []
                if not topics or str(topics[0]).lower() != TRANSFER_TOPIC:
                    continue
                if str(log.get("address", "")).lower() not in wanted_by_tx[tx_hash]:
                    continue
                decoded = _decode_transfer(log)
                if decoded:
                    rows.append(decoded)
        if rows:
            _client(
                "INSERT INTO eatlq.base_target_transfers_b2 "
                "(chain_id,block_number,block_time,tx_hash,log_index,token_address,sender,recipient,amount) "
                "FORMAT JSONEachRow",
                json_rows=rows,
            )
            inserted += len(rows)
        print(
            f"[transfer] receipts={min(offset + len(batch), len(tx_hashes))}/{len(tx_hashes)} "
            f"logs_scanned={fetched} inserted={inserted}",
            flush=True,
        )
        if offset + len(batch) < len(tx_hashes):
            time.sleep(receipt_batch_delay)
    return {
        "rpc_calls": rpc.calls,
        "receipts_fetched": len(tx_hashes),
        "logs_scanned": fetched,
        "rows_inserted": inserted,
        "tokens": len({token for tokens in wanted_by_tx.values() for token in tokens}),
    }


def fetch_target_transfers_logs(
    swaps: list[dict[str, Any]],
    rpc: RpcClient,
    start_block: int,
    end_block: int,
    chunk_blocks: int,
    workers: int,
) -> dict[str, int]:
    """Backfill target-token transfers with block-range logs, then exact tx filtering."""
    wanted = {
        (row["tx_hash"], row["target_token_address"])
        for row in swaps
        if row["target_token_address"]
    }
    tokens_by_chunk: dict[int, set[str]] = defaultdict(set)
    for row in swaps:
        token = row["target_token_address"]
        if token:
            chunk_start = start_block + (
                (int(row["block_number"]) - start_block) // chunk_blocks
            ) * chunk_blocks
            tokens_by_chunk[chunk_start].add(token)
    chunks = [
        (begin, min(end_block, begin + chunk_blocks - 1), sorted(tokens_by_chunk[begin]))
        for begin in sorted(tokens_by_chunk)
    ]

    def fetch_chunk(item: tuple[int, int, list[str]]) -> tuple[int, int, list[dict[str, Any]]]:
        begin, finish, tokens = item
        params = [{
            "fromBlock": hex(begin),
            "toBlock": hex(finish),
            "address": tokens,
            "topics": [TRANSFER_TOPIC],
        }]
        for attempt in range(5):
            try:
                return begin, finish, rpc.call("eth_getLogs", params, timeout=240) or []
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(1.5 * (attempt + 1))
        raise AssertionError("unreachable")

    _client("TRUNCATE TABLE eatlq.base_target_transfers_b2")
    fetched = inserted = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for index, (begin, finish, logs) in enumerate(executor.map(fetch_chunk, chunks), 1):
            fetched += len(logs)
            rows = []
            for log in logs:
                decoded = _decode_transfer(log)
                if decoded and (decoded["tx_hash"], decoded["token_address"]) in wanted:
                    rows.append(decoded)
            if rows:
                _client(
                    "INSERT INTO eatlq.base_target_transfers_b2 "
                    "(chain_id,block_number,block_time,tx_hash,log_index,token_address,sender,recipient,amount) "
                    "FORMAT JSONEachRow",
                    json_rows=rows,
                )
                inserted += len(rows)
            print(
                f"[transfer-log] chunks={index}/{len(chunks)} blocks={begin}-{finish} "
                f"logs_scanned={fetched} inserted={inserted}",
                flush=True,
            )
    return {
        "rpc_calls": rpc.calls,
        "chunks": len(chunks),
        "logs_scanned": fetched,
        "rows_inserted": inserted,
        "tokens": len({token for _, token in wanted}),
    }


def load_transfers(start_block: int, end_block: int) -> dict[tuple[str, str], list[dict[str, Any]]]:
    rows = ch_query(
        f"""
        SELECT lower(tx_hash) AS tx_hash, lower(token_address) AS token_address,
               log_index, lower(sender) AS sender, lower(recipient) AS recipient,
               toString(amount) AS amount
        FROM eatlq.base_target_transfers_b2 FINAL
        WHERE chain_id={CHAIN_ID} AND block_number BETWEEN {start_block} AND {end_block}
        """,
        timeout=600,
    )
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["tx_hash"], row["token_address"])].append(row)
    return grouped


def load_tx_from() -> dict[str, str]:
    return {
        row["tx_hash"]: row["tx_from"]
        for row in ch_query(
            f"""
            SELECT lower(tx_hash) AS tx_hash, anyLast(lower(tx_from)) AS tx_from
            FROM eatlq.tx_from_by_hash WHERE chain_id={CHAIN_ID}
            GROUP BY tx_hash
            """
        )
    }


def classify_accounts(rpc: RpcClient, addresses: set[str], at_block: int) -> dict[str, str]:
    cached_rows = ch_query(f"""
        SELECT lower(address) AS address,argMax(account_code_class,fetched_at) AS account_code_class
        FROM eatlq.base_account_code_b2
        WHERE chain_id={CHAIN_ID} AND at_block={int(at_block)}
        GROUP BY address
    """, timeout=600)
    classifications: dict[str, str] = {
        row["address"]: row["account_code_class"] for row in cached_rows
        if row["address"] in addresses
        and row["account_code_class"] in ("eoa", "eip7702", "contract_wallet")
    }
    rows: list[dict[str, Any]] = []
    ordered = sorted(addresses - set(classifications))
    def fetch_code(address: str) -> str:
        for attempt in range(5):
            try:
                code = rpc.call("eth_getCode", [address, hex(at_block)], timeout=60)
                if code is None:
                    raise RuntimeError("archive RPC returned null eth_getCode result")
                return str(code)
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(0.75 * (attempt + 1))
        raise AssertionError("unreachable")

    batch_size = 200
    for offset in range(0, len(ordered), batch_size):
        batch = ordered[offset:offset + batch_size]
        with ThreadPoolExecutor(max_workers=20) as executor:
            results = list(executor.map(fetch_code, batch))
        for address, code in zip(batch, results):
            normalized_code = str(code or "0x").lower()
            classification = (
                "eoa" if normalized_code in ("0x", "0x0")
                else "eip7702" if normalized_code.startswith("0xef0100")
                else "contract_wallet"
            )
            classifications[address] = classification
            rows.append({
                "chain_id": CHAIN_ID,
                "address": address,
                "at_block": at_block,
                "account_code_class": classification,
                "code_hash": hashlib.sha256(normalized_code.encode()).hexdigest(),
            })
        print(f"[code] {min(offset + len(batch), len(ordered))}/{len(ordered)}", flush=True)
    if rows:
        _client(
            "INSERT INTO eatlq.base_account_code_b2 "
            "(chain_id,address,at_block,account_code_class,code_hash) FORMAT JSONEachRow",
            json_rows=rows,
        )
    return classifications


def build_attribution(
    swaps: list[dict[str, Any]], rpc: RpcClient, start_block: int, end_block: int
) -> dict[str, Any]:
    transfers = load_transfers(start_block, end_block)
    tx_from = load_tx_from()
    staged = []
    accounts = set()
    swap_intervals: dict[tuple[str, str, int], tuple[int | None, int | None]] = {}
    by_tx_token: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for swap in swaps:
        if swap["target_token_address"]:
            by_tx_token[(swap["tx_hash"], swap["target_token_address"])].append(swap)
    for tx_swaps in by_tx_token.values():
        tx_swaps.sort(key=lambda row: int(row["log_index"]))
        for index, row in enumerate(tx_swaps):
            swap_intervals[(row["tx_hash"], row["target_token_address"], int(row["log_index"]))] = (
                int(tx_swaps[index - 1]["log_index"]) if index else None,
                int(tx_swaps[index + 1]["log_index"]) if index + 1 < len(tx_swaps) else None,
            )
    for swap in swaps:
        key = (swap["tx_hash"], swap["target_token_address"])
        tx_transfers = transfers.get(key, []) if swap["target_token_address"] else []
        resolution = resolve_transfer_net(
            side=swap["side"],
            transfers=tx_transfers,
            pool_address=swap["venue_address"],
            tx_from=tx_from.get(swap["tx_hash"], ""),
            fallback_recipient=swap["swap_recipient"],
        )
        if resolution.source == "ambiguous" and TRANSFER_SUBGRAPH_GOLDEN_APPROVED:
            previous_log, next_log = swap_intervals.get(
                (swap["tx_hash"], swap["target_token_address"], int(swap["log_index"])),
                (None, None),
            )
            resolution = resolve_swap_transfer_subgraph(
                side=swap["side"],
                transfers=tx_transfers,
                pool_address=swap["venue_address"],
                swap_log_index=int(swap["log_index"]),
                previous_swap_log_index=previous_log,
                next_swap_log_index=next_log,
                tx_from=tx_from.get(swap["tx_hash"], ""),
            )
        if resolution.account:
            accounts.add(resolution.account)
        staged.append((swap, tx_transfers, resolution))
    code_classes = classify_accounts(rpc, accounts, end_block)
    output = []
    for swap, tx_transfers, resolution in staged:
        code_class = code_classes.get(resolution.account, "unknown") if resolution.account else "unknown"
        residual = resolution.residual_bucket
        if resolution.source in ("transfer_net", "transfer_subgraph", "tx_from_confirmed"):
            residual = {
                "eoa": "resolved_eoa",
                "eip7702": "resolved_eip7702",
                "contract_wallet": "resolved_contract_wallet",
            }.get(code_class, "resolved_unknown_code")
        known_from = normalize_address(tx_from.get(swap["tx_hash"], ""))
        output.append({
            "chain_id": CHAIN_ID,
            "protocol": swap["protocol"],
            "venue_address": swap["venue_address"],
            "block_number": swap["block_number"],
            "block_time": swap["block_time"] or None,
            "block_time_source": "archiver_blockTimestamp" if swap["block_time"] else "missing",
            "tx_hash": swap["tx_hash"],
            "log_index": swap["log_index"],
            "swap_sender": swap["swap_sender"],
            "swap_recipient": swap["swap_recipient"],
            "token0_address": swap["token0"],
            "token1_address": swap["token1"],
            "quote_token_address": swap["quote_token_address"],
            "target_token_address": swap["target_token_address"],
            "side": swap["side"],
            "resolved_account": resolution.account,
            "attribution_source": resolution.source,
            "net_delta_raw": str(resolution.net_delta_raw),
            "transfer_log_count": len(tx_transfers),
            "same_direction_candidate_count": resolution.candidate_count,
            "is_infra_only": int(resolution.is_infra_only),
            "account_code_class": code_class,
            "residual_bucket": residual,
            "tx_from": known_from,
            "tx_from_match": int(bool(known_from and resolution.account == known_from)),
        })
    _client("TRUNCATE TABLE eatlq.base_cha_wallet_swaps")
    for offset in range(0, len(output), 5000):
        _client(
            "INSERT INTO eatlq.base_cha_wallet_swaps "
            "(chain_id,protocol,venue_address,block_number,block_time,block_time_source,tx_hash,log_index,"
            "swap_sender,swap_recipient,token0_address,token1_address,quote_token_address,target_token_address,"
            "side,resolved_account,attribution_source,net_delta_raw,transfer_log_count,"
            "same_direction_candidate_count,is_infra_only,account_code_class,residual_bucket,tx_from,tx_from_match) "
            "FORMAT JSONEachRow",
            json_rows=output[offset:offset + 5000],
        )
    return {"rows": len(output), "accounts_classified": len(accounts), "rpc_calls": rpc.calls}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--fetch-transfers", action="store_true")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--start-block", type=int, required=True)
    parser.add_argument("--end-block", type=int, required=True)
    parser.add_argument("--receipt-batch-size", type=int, default=45)
    parser.add_argument("--receipt-workers", type=int, default=8)
    parser.add_argument("--receipt-batch-delay", type=float, default=1.05)
    parser.add_argument("--transfer-fetch-mode", choices=("logs", "receipts"), default="logs")
    parser.add_argument("--log-chunk-blocks", type=int, default=250)
    parser.add_argument("--log-workers", type=int, default=2)
    parser.add_argument("--rpc-url", default=os.environ.get("B2_RPC_URL", "https://mainnet.base.org"))
    parser.add_argument("--rpc-env-file", type=Path)
    parser.add_argument("--rpc-env-key")
    parser.add_argument("--meta-output", type=Path)
    args = parser.parse_args()
    if args.end_block < args.start_block:
        raise ValueError("end block precedes start block")
    if args.install:
        install()
    rpc_url = args.rpc_url
    if args.rpc_env_file or args.rpc_env_key:
        if not args.rpc_env_file or not args.rpc_env_key:
            raise ValueError("--rpc-env-file and --rpc-env-key must be supplied together")
        values = {}
        for line in args.rpc_env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
        rpc_url = values.get(args.rpc_env_key, "")
        if not rpc_url:
            raise ValueError(f"RPC key is empty: {args.rpc_env_key}")
    swaps = load_swaps(args.start_block, args.end_block)
    rpc = RpcClient(rpc_url)
    result: dict[str, Any] = {
        "start_block": args.start_block,
        "end_block": args.end_block,
        "swap_rows": len(swaps),
        "rpc_host": urlsplit(rpc_url).hostname or "unknown",
    }
    if args.fetch_transfers:
        if args.transfer_fetch_mode == "logs":
            result["transfer_backfill"] = fetch_target_transfers_logs(
                swaps,
                rpc,
                args.start_block,
                args.end_block,
                args.log_chunk_blocks,
                args.log_workers,
            )
        else:
            result["transfer_backfill"] = fetch_target_transfers(
                swaps,
                rpc,
                args.start_block,
                args.end_block,
                args.receipt_batch_size,
                args.receipt_workers,
                args.receipt_batch_delay,
            )
    if args.build:
        result["build"] = build_attribution(swaps, rpc, args.start_block, args.end_block)
    rendered = json.dumps(result, indent=2)
    if args.meta_output:
        args.meta_output.parent.mkdir(parents=True, exist_ok=True)
        args.meta_output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
