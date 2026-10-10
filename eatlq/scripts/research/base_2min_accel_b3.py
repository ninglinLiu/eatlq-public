#!/usr/bin/env python3
"""B3 bounded Base two-minute acceleration falsification (research-only)."""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts/wallet_profile_v0"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ch_helper import DEFAULT_CH_CONTAINER, ch_query  # noqa: E402
from base_2min_accel_b3_core import (  # noqa: E402
    WETH, accelerated, classify_verdict, execute_path, monte_carlo,
    newly_positive, reserve_net, summarize,
)

CHAIN_ID = 8453
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
LOOKBACK = 43_200
OLD_CUTOFF = 10_800
START = "2026-02-27 05:41:15"
END = "2026-06-08 18:29:40"
DDL = REPO / "infra/clickhouse/init/043_base_2min_accel_b3.sql"
OUT = REPO / "mvp/diagnostics/base_2min_accel_b3_v1_20260714.json"
ETH_FIXTURE = REPO / "mvp/diagnostics/base_2min_accel_b3_eth_daily_20260714.json"
EXCLUSIONS = REPO / "configs/research/base_attribution_exclusions.json"
ZERO = "0x" + "0" * 40
DEAD = "0x" + "0" * 36 + "dead"


def load_env() -> None:
    for path in (REPO / ".env", REPO / "config/.env", REPO / "infra/compose/.env"):
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def rpc_url() -> str:
    load_env()
    for key in ("POOL_DISCOVERY_RPC_URL", "EXECUTOR_READ_RPC_URL", "LABEL_RPC_URL"):
        if os.environ.get(key):
            return os.environ[key]
    raise RuntimeError("no archive research RPC configured")


def client(sql: str, rows: list[dict] | None = None, multiquery: bool = False,
           timeout: int = 600) -> None:
    cmd = ["docker", "exec", "-i", DEFAULT_CH_CONTAINER, "clickhouse-client"]
    if multiquery:
        cmd.append("--multiquery")
    payload = sql
    if rows:
        payload += "\n" + "\n".join(json.dumps(r, separators=(",", ":")) for r in rows)
    r = subprocess.run(cmd, input=payload, text=True, capture_output=True,
                       timeout=timeout, check=False)
    if r.returncode:
        raise RuntimeError(r.stderr[:1500])


def install() -> None:
    client(DDL.read_text(), multiquery=True)


def build_universe() -> list[dict]:
    pools = ch_query(f"""
      SELECT lower(p.pool_address) pool_address,p.pool_type,
        if(lower(p.token0_address)='{WETH}',lower(p.token1_address),lower(p.token0_address)) target_token,
        if(lower(p.token0_address)='{WETH}',1,0) target_index,
        if(lower(p.token0_address)='{WETH}',p.token1_decimals,p.token0_decimals) target_decimals,
        p.created_block,toString(m.block_time) created_time
      FROM eatlq.discovered_pools AS p FINAL
      INNER JOIN eatlq.block_ts_map AS m FINAL
        ON p.chain_id=m.chain_id AND p.created_block=m.block_number
      WHERE p.chain_id={CHAIN_ID} AND p.has_weth_leg=1
        AND m.block_time>=toDateTime64('{START}',3,'UTC')
        AND m.block_time<toDateTime64('{END}',3,'UTC')
      ORDER BY p.created_block,p.pool_address
    """, timeout=600)
    maps = ch_query(f"""
      SELECT block_number,toUnixTimestamp64Milli(block_time) ms
      FROM eatlq.block_ts_map FINAL WHERE chain_id={CHAIN_ID}
        AND block_time>=toDateTime64('{START}',3,'UTC')
        AND block_time<toDateTime64('{END}',3,'UTC')+INTERVAL 301 SECOND
      ORDER BY ms,block_number
    """, timeout=600)
    times = [int(x["ms"]) for x in maps]
    blocks = [int(x["block_number"]) for x in maps]
    rows = []
    for p in pools:
        dt = datetime.fromisoformat(str(p["created_time"]).replace(" ", "T")).replace(tzinfo=timezone.utc)
        ms0 = int(dt.timestamp() * 1000)
        snaps = []
        for sec in (60, 120, 150, 300):
            i = bisect.bisect_right(times, ms0 + sec * 1000) - 1
            snaps.append(blocks[i] if i >= 0 else 0)
        window = "W1" if p["created_time"] < "2026-04-01" else "W2" if p["created_time"] < "2026-05-01" else "W3"
        rows.append({"chain_id": CHAIN_ID, **p, "snapshot_60": snaps[0],
                     "snapshot_120": snaps[1], "snapshot_150": snaps[2],
                     "snapshot_300": snaps[3], "window": window})
    if len(rows) != 3331 or len({r["target_token"] for r in rows}) != 3330:
        raise AssertionError("locked universe drift")
    client("TRUNCATE TABLE eatlq.base_pool_universe_b3")
    client("INSERT INTO eatlq.base_pool_universe_b3 "
           "(chain_id,pool_address,pool_type,target_token,target_index,target_decimals,created_block,created_time,snapshot_60,snapshot_120,snapshot_150,snapshot_300,window) FORMAT JSONEachRow", rows)
    return rows


class Rpc:
    def __init__(self, url: str):
        self.url, self.calls = url, 0
        self.lock = threading.Lock()

    def call(self, method: str, params: list, timeout: int = 240):
        with self.lock:
            self.calls += 1
            ident = self.calls
        body = json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params}).encode()
        req = Request(self.url, data=body, headers={"Content-Type": "application/json",
                      "User-Agent": "EATLQ-B3-research/1.0"}, method="POST")
        with urlopen(req, timeout=timeout) as response:
            obj = json.loads(response.read())
        if obj.get("error"):
            raise RuntimeError(str(obj["error"]))
        return obj.get("result")


def topic_address(a: str) -> str:
    return "0x" + "0" * 24 + a.lower()[2:]


def decode_log(log: dict, kind: str, request_address: str, pool: str) -> dict | None:
    topics = log.get("topics") or []
    data = str(log.get("data", ""))
    if len(topics) != 3 or len(data) != 66:
        return None
    return {"chain_id": CHAIN_ID, "request_kind": kind, "request_address": request_address,
            "pool_address": pool, "block_number": int(log["blockNumber"], 16),
            "tx_hash": str(log["transactionHash"]).lower(), "log_index": int(log["logIndex"], 16),
            "token_address": str(log["address"]).lower(),
            "sender": "0x" + str(topics[1])[-40:].lower(),
            "recipient": "0x" + str(topics[2])[-40:].lower(), "amount": str(int(data, 16))}


def transfer_requests(universe: list[dict]) -> list[dict]:
    by_token: dict[str, list[dict]] = defaultdict(list)
    for p in universe:
        by_token[p["target_token"]].append(p)
    reqs = []
    for token, ps in sorted(by_token.items()):
        reqs.append({"kind": "target", "address": token, "pool": "",
                     "from": max(0, min(int(p["created_block"]) for p in ps) - LOOKBACK),
                     "to": max(int(p["snapshot_300"]) for p in ps),
                     "topics": [TRANSFER_TOPIC]})
    for p in universe:
        pool = p["pool_address"]
        common = {"address": WETH, "pool": pool, "from": int(p["created_block"]),
                  "to": int(p["snapshot_300"])}
        reqs.append({**common, "kind": "weth_in", "topics": [TRANSFER_TOPIC, None, topic_address(pool)]})
        reqs.append({**common, "kind": "weth_out", "topics": [TRANSFER_TOPIC, topic_address(pool)]})
    if len(reqs) != 9992:
        raise AssertionError(f"RPC budget drift: {len(reqs)}")
    return reqs


def fetch_transfers(universe: list[dict], workers: int = 12) -> dict:
    rpc = Rpc(rpc_url())
    reqs = transfer_requests(universe)
    client("TRUNCATE TABLE eatlq.base_pool_transfers_b3")
    client("TRUNCATE TABLE eatlq.base_transfer_fetch_status_b3")

    def fetch(req: dict) -> tuple[list[dict], dict]:
        attempts, err = 0, ""
        logs = []
        # The configured archive endpoint has a measured 10,000-block getLogs
        # ceiling (43,200-block requests return HTTP 413). These are provider-
        # mandated subranges of one frozen logical token request, not universe
        # expansion. WETH pool requests remain one call each.
        ranges = ([(a, min(req["to"], a + 9_999))
                   for a in range(req["from"], req["to"] + 1, 10_000)]
                  if req["kind"] == "target" else [(req["from"], req["to"])])
        for begin, finish in ranges:
            part = None
            for attempt in range(1, 6):
                attempts += 1
                try:
                    part = rpc.call("eth_getLogs", [{"fromBlock": hex(begin),
                        "toBlock": hex(finish), "address": req["address"], "topics": req["topics"]}]) or []
                    break
                except Exception as exc:
                    err = str(exc)[:500]
                    if attempt < 5:
                        time.sleep(attempt * .75)
            if part is None:
                break
            logs.extend(part)
        else:
            err = ""
        rows = [x for x in (decode_log(log, req["kind"], req["address"], req["pool"]) for log in logs) if x]
        status = {"chain_id": CHAIN_ID, "request_kind": req["kind"],
                  "request_address": req["address"], "pool_address": req["pool"],
                  "from_block": req["from"], "to_block": req["to"],
                  "completed": int(not err), "log_count": len(rows),
                  "rpc_attempts": attempts, "error": err}
        return rows, status

    inserted = completed = 0
    pending_rows: list[dict] = []
    pending_status: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, (rows, status) in enumerate(ex.map(fetch, reqs), 1):
            pending_rows.extend(rows)
            pending_status.append(status)
            inserted += len(rows)
            completed += status["completed"]
            if i % 100 == 0 or i == len(reqs):
                if pending_rows:
                    client("INSERT INTO eatlq.base_pool_transfers_b3 "
                           "(chain_id,request_kind,request_address,pool_address,block_number,tx_hash,log_index,token_address,sender,recipient,amount) FORMAT JSONEachRow", pending_rows)
                client("INSERT INTO eatlq.base_transfer_fetch_status_b3 "
                       "(chain_id,request_kind,request_address,pool_address,from_block,to_block,completed,log_count,rpc_attempts,error) FORMAT JSONEachRow", pending_status)
                pending_rows, pending_status = [], []
            if i % 250 == 0 or i == len(reqs):
                print(f"[B3 transfer] logical={i}/{len(reqs)} actual_rpc={rpc.calls} rows={inserted} ok={completed}", flush=True)
    return {"logical_requests": len(reqs), "actual_rpc_calls": rpc.calls,
            "completed": completed, "failed": len(reqs)-completed, "rows": inserted}


def retry_failed(universe: list[dict], workers: int = 6) -> dict:
    """Retry only incomplete frozen logical ranges, without truncating good data."""
    wanted = {(r["kind"], r["address"], r["pool"]): r for r in transfer_requests(universe)}
    failed = ch_query("SELECT request_kind,request_address,pool_address FROM eatlq.base_transfer_fetch_status_b3 FINAL WHERE completed=0")
    reqs = [wanted[(r["request_kind"], r["request_address"], r["pool_address"])] for r in failed]
    rpc = Rpc(rpc_url())

    def one(req: dict):
        ranges = ([(a, min(req["to"], a+9_999)) for a in range(req["from"], req["to"]+1, 10_000)]
                  if req["kind"] == "target" else [(req["from"], req["to"])])
        logs, attempts, err = [], 0, ""
        for begin, finish in ranges:
            part = None
            for attempt in range(1, 6):
                attempts += 1
                try:
                    part = rpc.call("eth_getLogs", [{"fromBlock": hex(begin), "toBlock": hex(finish),
                        "address": req["address"], "topics": req["topics"]}]) or []
                    break
                except Exception as exc:
                    err = str(exc)[:500]; time.sleep(attempt)
            if part is None: break
            logs.extend(part)
        else: err = ""
        rows = [x for x in (decode_log(log, req["kind"], req["address"], req["pool"]) for log in logs) if x]
        status = {"chain_id": CHAIN_ID, "request_kind": req["kind"], "request_address": req["address"],
            "pool_address": req["pool"], "from_block": req["from"], "to_block": req["to"],
            "completed": int(not err), "log_count": len(rows), "rpc_attempts": attempts, "error": err}
        return rows, status

    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(one, reqs))
    rows = sum((r for r, _ in results), [])
    statuses = [s for _, s in results]
    if rows:
        client("INSERT INTO eatlq.base_pool_transfers_b3 (chain_id,request_kind,request_address,pool_address,block_number,tx_hash,log_index,token_address,sender,recipient,amount) FORMAT JSONEachRow", rows)
    if statuses:
        client("INSERT INTO eatlq.base_transfer_fetch_status_b3 (chain_id,request_kind,request_address,pool_address,from_block,to_block,completed,log_count,rpc_attempts,error) FORMAT JSONEachRow", statuses)
    return {"retried": len(reqs), "completed": sum(s["completed"] for s in statuses),
            "remaining": sum(1-s["completed"] for s in statuses), "rpc_calls": rpc.calls, "rows": len(rows)}


def load_universe() -> list[dict]:
    return ch_query("SELECT * EXCEPT built_at FROM eatlq.base_pool_universe_b3 FINAL ORDER BY created_block,pool_address", timeout=600)


def load_swaps(universe: list[dict]) -> dict[str, list[dict]]:
    v3 = ch_query(f"""
      SELECT lower(s.pool_address) pool_address,s.block_number,
        toUnixTimestamp64Milli(u.created_time)/1000+2*(toInt64(s.block_number)-toInt64(u.created_block)) ts,
        lower(s.tx_hash) tx_hash,s.log_index,toString(s.amount0) amount0,toString(s.amount1) amount1,
        toString(s.sqrt_price_x96) sqrt_price_x96
      FROM eatlq.uniswap_v3_swaps_decoded s
      INNER JOIN eatlq.base_pool_universe_b3 AS u FINAL ON lower(s.pool_address)=u.pool_address
      WHERE s.chain_id={CHAIN_ID}
        AND s.block_number BETWEEN u.created_block AND u.created_block+4650
    """, timeout=1200)
    v2 = ch_query(f"""
      SELECT lower(s.pair_address) pool_address,s.block_number,
        toUnixTimestamp64Milli(u.created_time)/1000+2*(toInt64(s.block_number)-toInt64(u.created_block)) ts,
        lower(s.tx_hash) tx_hash,s.log_index,toString(s.amount0_in) amount0_in,toString(s.amount0_out) amount0_out,
        toString(s.amount1_in) amount1_in,toString(s.amount1_out) amount1_out
      FROM eatlq.uniswap_v2_swaps_decoded s
      INNER JOIN eatlq.base_pool_universe_b3 AS u FINAL ON lower(s.pair_address)=u.pool_address
      WHERE s.chain_id={CHAIN_ID}
        AND s.block_number BETWEEN u.created_block AND u.created_block+4650
    """, timeout=1200)
    meta = {p["pool_address"]: p for p in universe}
    out: dict[str, list[dict]] = defaultdict(list)
    for r in v3:
        p, idx = meta[r["pool_address"]], int(meta[r["pool_address"]]["target_index"])
        vals = [int(r["amount0"]), int(r["amount1"])]
        target, weth = vals[idx], vals[1-idx]
        if not target or not weth:
            continue
        target_norm = abs(target) / 10 ** int(p["target_decimals"])
        weth_norm = abs(weth) / 1e18
        price = weth_norm / target_norm
        raw_ratio = (int(r["sqrt_price_x96"]) / 2**96) ** 2
        marginal = (raw_ratio * 10**int(p["target_decimals"]) / 1e18 if idx == 0
                    else 10**int(p["target_decimals"]) / (raw_ratio * 1e18))
        out[r["pool_address"]].append({**r, "side": "buy" if target < 0 else "sell",
            "weth": weth_norm, "price": price, "protocol": "v3",
            "execution_price_deviation": abs(price / marginal - 1) if marginal else None})
    for r in v2:
        p, idx = meta[r["pool_address"]], int(meta[r["pool_address"]]["target_index"])
        ins = [int(r["amount0_in"]), int(r["amount1_in"])]
        outs = [int(r["amount0_out"]), int(r["amount1_out"])]
        if outs[idx] > 0:
            side, target, weth = "buy", outs[idx], ins[1-idx]
        elif ins[idx] > 0:
            side, target, weth = "sell", ins[idx], outs[1-idx]
        else:
            continue
        if not target or not weth:
            continue
        target_norm = target / 10 ** int(p["target_decimals"])
        weth_norm = weth / 1e18
        out[r["pool_address"]].append({**r, "side": side, "weth": weth_norm,
            "price": weth_norm / target_norm, "protocol": "v2"})
    for rows in out.values():
        rows.sort(key=lambda x: (x["ts"], int(x["log_index"])))
        prior = None
        for row in rows:
            if row["protocol"] == "v2":
                row["execution_price_deviation"] = abs(row["price"] / prior - 1) if prior else None
            prior = row["price"]
    return out


def exclusion_set() -> set[str]:
    obj = json.loads(EXCLUSIONS.read_text())
    rows = obj.get("addresses", obj.get("exclusions", obj if isinstance(obj, list) else []))
    vals = set()
    for x in rows:
        a = x if isinstance(x, str) else x.get("address", "")
        if a:
            vals.add(a.lower())
    vals.update({ZERO, DEAD, WETH, "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                 "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca"})
    return vals


def build_features(universe: list[dict], swaps_by_pool: dict[str, list[dict]]) -> dict:
    target_rows = ch_query("SELECT request_address,block_number,tx_hash,log_index,sender,recipient,toString(amount) amount FROM eatlq.base_pool_transfers_b3 FINAL WHERE request_kind='target'", timeout=1200)
    weth_rows = ch_query("SELECT pool_address,block_number,tx_hash,log_index,sender,recipient,toString(amount) amount FROM eatlq.base_pool_transfers_b3 FINAL WHERE request_kind IN ('weth_in','weth_out')", timeout=1200)
    status = ch_query("SELECT request_kind,request_address,pool_address,completed FROM eatlq.base_transfer_fetch_status_b3 FINAL")
    ok = {(r["request_kind"], r["request_address"], r["pool_address"]): int(r["completed"]) for r in status}
    targets: dict[str, list[dict]] = defaultdict(list)
    for r in target_rows: targets[r["request_address"]].append(r)
    weths: dict[str, dict[tuple, dict]] = defaultdict(dict)
    for r in weth_rows: weths[r["pool_address"]][(r["tx_hash"], int(r["log_index"]))] = r
    earlier = {r["token"]: int(r["first_block"]) for r in ch_query(f"""
      SELECT if(lower(token0_address)='{WETH}',lower(token1_address),lower(token0_address)) token,min(created_block) first_block
      FROM eatlq.discovered_pools FINAL WHERE chain_id={CHAIN_ID} AND has_weth_leg=1 GROUP BY token
    """)}
    excluded_base = exclusion_set()
    features, counts = [], defaultdict(int)
    for p in universe:
        pool, token = p["pool_address"], p["target_token"]
        tev, wev = targets[token], list(weths[pool].values())
        complete = ok.get(("target", token, ""), 0) and ok.get(("weth_in", WETH, pool), 0) and ok.get(("weth_out", WETH, pool), 0)
        local_old = earlier.get(token, int(p["created_block"])) < int(p["created_block"]) - OLD_CUTOFF
        rpc_old = any(int(e["block_number"]) < int(p["created_block"]) - OLD_CUTOFF for e in tev)
        fresh_class = "fresh" if complete and not local_old and not rpc_old else "old_token_new_pool" if complete else "freshness_unknown"
        premint = [e for e in tev if int(p["created_block"])-OLD_CUTOFF <= int(e["block_number"]) < int(p["created_block"])]
        pexcluded = excluded_base | {pool, token}
        h1, h2 = newly_positive(tev, int(p["created_block"])-1, int(p["snapshot_60"]), int(p["snapshot_120"]), pexcluded)
        t0 = datetime.fromisoformat(str(p["created_time"]).replace(" ", "T")).replace(tzinfo=timezone.utc).timestamp()
        early = [s for s in swaps_by_pool.get(pool, []) if int(s["block_number"]) <= int(p["snapshot_120"])]
        v1 = sum(s["weth"] for s in early if int(s["block_number"]) <= int(p["snapshot_60"]))
        v2 = sum(s["weth"] for s in early if int(p["snapshot_60"]) < int(s["block_number"]) <= int(p["snapshot_120"]))
        r60, r120 = reserve_net(wev, pool, int(p["snapshot_60"])), reserve_net(wev, pool, int(p["snapshot_120"]))
        rec1 = {e["recipient"] for e in tev if int(p["created_block"]) <= int(e["block_number"]) <= int(p["snapshot_60"]) and e["recipient"] not in pexcluded}
        rec2 = {e["recipient"] for e in tev if int(p["snapshot_60"]) < int(e["block_number"]) <= int(p["snapshot_120"]) and e["recipient"] not in pexcluded}
        volume_pass = v1+v2 >= .01 and accelerated(v2, v1)
        real = fresh_class == "fresh" and volume_pass and accelerated(r120, r60) and accelerated(len(h2), len(h1))
        # RH proxy: cumulative volume/2 stock growth and distinct Transfer recipients.
        proxy = fresh_class == "fresh" and volume_pass and accelerated((v1+v2)/2, v1/2) and accelerated(len(rec2), len(rec1))
        max_block = max([int(e["block_number"]) for e in tev+wev if int(e["block_number"]) <= int(p["snapshot_120"])] or [int(p["created_block"])])
        row = {"chain_id": CHAIN_ID, "pool_address": pool, "window": p["window"], "fresh_class": fresh_class,
            "premint_address_count": len({e["recipient"] for e in premint}), "premint_amount_raw": str(sum(int(e["amount"]) for e in premint)),
            "volume_1": v1, "volume_2": v2, "reserve_60_raw": str(r60), "reserve_120_raw": str(r120),
            "new_holders_1": len(h1), "new_holders_2": len(h2), "new_holders_eoa_1": len(h1), "new_holders_eoa_2": len(h2),
            "proxy_recipients_1": len(rec1), "proxy_recipients_2": len(rec2), "real_signal": int(real), "real_signal_eoa": int(real),
            "proxy_signal": int(proxy), "max_feature_block": max_block, "snapshot_block": int(p["snapshot_120"])}
        if max_block > int(p["snapshot_120"]): raise AssertionError("feature lookahead")
        features.append(row); counts[fresh_class] += 1
    client("TRUNCATE TABLE eatlq.base_pool_2min_snapshot_b3")
    client("INSERT INTO eatlq.base_pool_2min_snapshot_b3 (chain_id,pool_address,window,fresh_class,premint_address_count,premint_amount_raw,volume_1,volume_2,reserve_60_raw,reserve_120_raw,new_holders_1,new_holders_2,new_holders_eoa_1,new_holders_eoa_2,proxy_recipients_1,proxy_recipients_2,real_signal,real_signal_eoa,proxy_signal,max_feature_block,snapshot_block) FORMAT JSONEachRow", features)
    return {"classification": dict(counts), "features": features}


def enrich_eoa(universe: list[dict], workers: int = 12) -> dict:
    """EOA sensitivity only for pools that can pass volume+reserve gates."""
    feats = {r["pool_address"]: r for r in ch_query("SELECT * EXCEPT built_at FROM eatlq.base_pool_2min_snapshot_b3 FINAL")}
    candidates = {p for p, f in feats.items() if f["fresh_class"] == "fresh"
                  and float(f["volume_1"])+float(f["volume_2"]) >= .01
                  and accelerated(float(f["volume_2"]), float(f["volume_1"]))
                  and accelerated(int(f["reserve_120_raw"]), int(f["reserve_60_raw"]))}
    meta = {p["pool_address"]: p for p in universe}
    token_rows = ch_query("SELECT request_address,block_number,sender,recipient,toString(amount) amount FROM eatlq.base_pool_transfers_b3 FINAL WHERE request_kind='target'")
    by_token: dict[str, list[dict]] = defaultdict(list)
    for r in token_rows: by_token[r["request_address"]].append(r)
    excluded = exclusion_set()
    holder_sets = {}
    calls = set()
    for pool in candidates:
        p = meta[pool]
        h1, h2 = newly_positive(by_token[p["target_token"]], int(p["created_block"])-1,
            int(p["snapshot_60"]), int(p["snapshot_120"]), excluded | {pool, p["target_token"]})
        holder_sets[pool] = (h1, h2)
        calls.update((a, int(p["snapshot_120"])) for a in h1 | h2)
    rpc = Rpc(rpc_url())
    def get_code(item):
        a, block = item
        code = None
        for attempt in range(1, 6):
            try:
                code = rpc.call("eth_getCode", [a, hex(block)], timeout=90) or "0x"
                break
            except Exception:
                if attempt == 5: raise
                time.sleep(attempt)
        return {"chain_id": CHAIN_ID, "address": a, "at_block": block,
                "account_code_class": "eoa" if code in ("0x", "0x0") else "smart_account",
                "code_hash": hashlib.sha256(code.encode()).hexdigest()}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        codes = list(ex.map(get_code, sorted(calls)))
    client("TRUNCATE TABLE eatlq.base_account_code_b3")
    if codes:
        client("INSERT INTO eatlq.base_account_code_b3 (chain_id,address,at_block,account_code_class,code_hash) FORMAT JSONEachRow", codes)
    cls = {(r["address"], int(r["at_block"])): r["account_code_class"] for r in codes}
    rows = []
    for pool, f in feats.items():
        p = meta[pool]
        h1, h2 = holder_sets.get(pool, (set(), set()))
        e1 = {a for a in h1 if cls.get((a, int(p["snapshot_120"])), "eoa") == "eoa"}
        e2 = {a for a in h2 if cls.get((a, int(p["snapshot_120"])), "eoa") == "eoa"}
        f["new_holders_eoa_1"], f["new_holders_eoa_2"] = len(e1), len(e2)
        f["real_signal_eoa"] = int(bool(f["real_signal"]) and accelerated(len(e2), len(e1)))
        rows.append(f)
    client("INSERT INTO eatlq.base_pool_2min_snapshot_b3 (chain_id,pool_address,window,fresh_class,premint_address_count,premint_amount_raw,volume_1,volume_2,reserve_60_raw,reserve_120_raw,new_holders_1,new_holders_2,new_holders_eoa_1,new_holders_eoa_2,proxy_recipients_1,proxy_recipients_2,real_signal,real_signal_eoa,proxy_signal,max_feature_block,snapshot_block) FORMAT JSONEachRow", rows)
    return {"candidate_pools": len(candidates), "addresses": len(calls), "rpc_calls": rpc.calls,
            "smart_accounts": sum(r["account_code_class"] == "smart_account" for r in codes)}


def eth_daily() -> dict[str, float]:
    url = "https://api.exchange.coinbase.com/products/ETH-USD/candles?start=2026-02-27T00%3A00%3A00Z&end=2026-06-09T00%3A00%3A00Z&granularity=86400"
    req = Request(url, headers={"User-Agent": "EATLQ-B3-research/1.0"})
    rows = json.loads(urlopen(req, timeout=60).read())
    obj = {datetime.fromtimestamp(int(r[0]), timezone.utc).date().isoformat(): float(r[4]) for r in rows}
    payload = json.dumps({"source": "Coinbase Exchange ETH-USD daily candles", "closes": obj}, indent=2, sort_keys=True) + "\n"
    ETH_FIXTURE.write_text(payload)
    return obj


def control_5min(universe: list[dict], swaps_by_pool: dict[str, list[dict]],
                 features: list[dict], prices: dict[str, float]) -> dict:
    """Frozen five-minute control; it cannot replace the two-minute primary."""
    by_f = {f["pool_address"]: f for f in features}
    target_rows = ch_query("SELECT request_address,block_number,sender,recipient,toString(amount) amount FROM eatlq.base_pool_transfers_b3 FINAL WHERE request_kind='target'")
    weth_rows = ch_query("SELECT pool_address,block_number,tx_hash,log_index,sender,recipient,toString(amount) amount FROM eatlq.base_pool_transfers_b3 FINAL WHERE request_kind IN ('weth_in','weth_out')")
    targets: dict[str, list[dict]] = defaultdict(list)
    weths: dict[str, dict[tuple, dict]] = defaultdict(dict)
    for r in target_rows: targets[r["request_address"]].append(r)
    for r in weth_rows: weths[r["pool_address"]][(r["tx_hash"], int(r["log_index"]))] = r
    excluded = exclusion_set()
    returns = {"real": defaultdict(list), "proxy": defaultdict(list)}
    hits = {"real": defaultdict(int), "proxy": defaultdict(int)}
    for p in universe:
        if by_f[p["pool_address"]]["fresh_class"] != "fresh": continue
        pool, token = p["pool_address"], p["target_token"]
        tev, wev = targets[token], list(weths[pool].values())
        swaps = swaps_by_pool.get(pool, [])
        early = [s for s in swaps if int(s["block_number"]) <= int(p["snapshot_300"])]
        v1 = sum(s["weth"] for s in early if int(s["block_number"]) <= int(p["snapshot_150"]))
        v2 = sum(s["weth"] for s in early if int(p["snapshot_150"]) < int(s["block_number"]) <= int(p["snapshot_300"]))
        r1, r2 = reserve_net(wev, pool, int(p["snapshot_150"])), reserve_net(wev, pool, int(p["snapshot_300"]))
        h1, h2 = newly_positive(tev, int(p["created_block"])-1, int(p["snapshot_150"]), int(p["snapshot_300"]), excluded | {pool, token})
        rec1 = {e["recipient"] for e in tev if int(p["created_block"]) <= int(e["block_number"]) <= int(p["snapshot_150"]) and e["recipient"] not in excluded | {pool, token}}
        rec2 = {e["recipient"] for e in tev if int(p["snapshot_150"]) < int(e["block_number"]) <= int(p["snapshot_300"]) and e["recipient"] not in excluded | {pool, token}}
        volume = v1+v2 >= .01 and accelerated(v2, v1)
        real = volume and accelerated(r2, r1) and accelerated(len(h2), len(h1))
        proxy = volume and accelerated((v1+v2)/2, v1/2) and accelerated(len(rec2), len(rec1))
        t0 = datetime.fromisoformat(str(p["created_time"]).replace(" ", "T")).replace(tzinfo=timezone.utc)
        outcome = execute_path(swaps, t0.timestamp()+420, prices[t0.date().isoformat()], True)
        for label, passed in (("real", real), ("proxy", proxy)):
            if passed:
                hits[label][p["window"]] += 1
                returns[label][p["window"]].append(outcome["net_return"])
    out = {}
    for label in ("real", "proxy"):
        out[label] = {w: summarize(returns[label].get(w, [])) for w in ("W1","W2","W3")}
        out[label]["pooled"] = summarize(sum((returns[label].get(w, []) for w in ("W1","W2","W3")), []))
    out["role"] = "control_only_not_primary"
    return out


def run_backtest(universe: list[dict], swaps: dict[str, list[dict]], features: list[dict], prices: dict[str, float]) -> dict:
    meta, feat = {p["pool_address"]: p for p in universe}, {f["pool_address"]: f for f in features}
    groups = ("real_signal", "proxy_signal", "volume_gate", "unconditional")
    returns = {g: defaultdict(list) for g in groups}
    gross_returns = {g: defaultdict(list) for g in groups}
    gas_usd = {g: [] for g in groups}
    details = []
    for pool, p in meta.items():
        f = feat[pool]
        if f["fresh_class"] != "fresh": continue
        t0 = datetime.fromisoformat(str(p["created_time"]).replace(" ", "T")).replace(tzinfo=timezone.utc)
        date = t0.date().isoformat(); price = prices.get(date)
        if not price: raise AssertionError(f"missing ETH close {date}")
        ladder = execute_path(swaps.get(pool, []), t0.timestamp()+240, price, True)
        uncapped = execute_path(swaps.get(pool, []), t0.timestamp()+240, price, False)
        passed = {"real_signal": bool(f["real_signal"]), "proxy_signal": bool(f["proxy_signal"]),
                  "volume_gate": f["volume_1"]+f["volume_2"] >= .01,
                  "unconditional": True}
        for g in groups:
            if passed[g]:
                returns[g][p["window"]].append(ladder["net_return"])
                gross_returns[g][p["window"]].append(ladder["gross_return"])
                gas_usd[g].append(ladder.get("gas_usd", 0.0))
        if passed["real_signal"] or passed["proxy_signal"]:
            details.append({"pool": pool, "window": p["window"], "real": passed["real_signal"],
                "proxy": passed["proxy_signal"], "ladder": ladder, "uncapped": uncapped})
    result = {"groups": {}, "opportunities": details}
    for g in groups:
        result["groups"][g] = {w: summarize(returns[g].get(w, [])) for w in ("W1","W2","W3")}
        pooled = sum((returns[g].get(w, []) for w in ("W1","W2","W3")), [])
        result["groups"][g]["pooled"] = summarize(pooled)
        gp = sum((gross_returns[g].get(w, []) for w in ("W1","W2","W3")), [])
        result["groups"][g]["pooled_gross_mean"] = sum(gp)/len(gp) if gp else None
        result["groups"][g]["mean_gas_usd_per_opportunity"] = sum(gas_usd[g])/len(gas_usd[g]) if gas_usd[g] else None
    for w in ("W1","W2","W3"):
        rr, base = result["groups"]["real_signal"][w], result["groups"]["unconditional"][w]
        rr["baseline_mean"] = base["mean"]
        rr["alpha_delta"] = None if rr["mean"] is None or base["mean"] is None else rr["mean"]-base["mean"]
        rr["top1_removed_alpha"] = None if rr["top1_removed_mean"] is None or base["mean"] is None else rr["top1_removed_mean"]-base["mean"]
    verdict_rows = {w: result["groups"]["real_signal"][w] for w in ("W1","W2","W3")}
    result["verdict"] = classify_verdict(verdict_rows)
    real_sets = {d["pool"] for d in details if d["real"]}; proxy_sets = {d["pool"] for d in details if d["proxy"]}
    result["real_proxy_bias"] = {"real_hits": len(real_sets), "proxy_hits": len(proxy_sets),
        "intersection": len(real_sets&proxy_sets), "union": len(real_sets|proxy_sets),
        "jaccard": len(real_sets&proxy_sets)/len(real_sets|proxy_sets) if real_sets|proxy_sets else None,
        "proxy_only": len(proxy_sets-real_sets), "real_only": len(real_sets-proxy_sets)}
    powered = {w: vals for w, vals in returns["real_signal"].items() if len(vals) >= 20}
    result["monte_carlo"] = monte_carlo(powered) if powered else None
    result["uncapped"] = {}
    for label, predicate in (("real", lambda d: d["real"]), ("proxy", lambda d: d["proxy"])):
        result["uncapped"][label] = {}
        for w in ("W1", "W2", "W3"):
            vals = [d["uncapped"]["net_return"] for d in details if predicate(d) and d["window"] == w]
            result["uncapped"][label][w] = summarize(vals)
        vals = [d["uncapped"]["net_return"] for d in details if predicate(d)]
        result["uncapped"][label]["pooled"] = summarize(vals)
    for label, predicate in (("real", lambda d: d["real"]), ("proxy", lambda d: d["proxy"])):
        selected = []
        for d in details:
            if not predicate(d) or not d["ladder"]["filled"]: continue
            selected.append(d["ladder"]["entry"])
            selected.extend(d["ladder"]["exits"])
        vals = sorted(float(s["execution_price_deviation"]) for s in selected
                      if s.get("execution_price_deviation") is not None)
        result.setdefault("observed_execution_deviation", {})[label] = {
            "selected_swaps": len(selected), "measured_swaps": len(vals),
            "mean_bps": 10_000*sum(vals)/len(vals) if vals else None,
            "median_bps": 10_000*vals[len(vals)//2] if vals else None,
            "p95_bps": 10_000*vals[min(len(vals)-1, int(.95*len(vals)))] if vals else None}
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=("install","universe","fetch","retry","features","eoa","backtest","all"))
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args()
    install()
    if args.stage == "install": return
    universe = build_universe() if args.stage in ("universe","all") else load_universe()
    if args.stage == "universe": return
    audit = {}
    if args.stage == "retry":
        print(json.dumps(retry_failed(universe, args.workers), indent=2)); return
    if args.stage in ("fetch","all"):
        audit["rpc"] = fetch_transfers(universe, args.workers)
        if args.stage == "fetch":
            print(json.dumps(audit, indent=2)); return
    swaps = load_swaps(universe)
    built = build_features(universe, swaps) if args.stage in ("features","all") else {
        "features": ch_query("SELECT * EXCEPT built_at FROM eatlq.base_pool_2min_snapshot_b3 FINAL")}
    if args.stage == "features":
        print(json.dumps({"classification": built.get("classification")}, indent=2)); return
    if args.stage == "eoa":
        print(json.dumps(enrich_eoa(universe, args.workers), indent=2)); return
    prices = eth_daily()
    audit["result"] = run_backtest(universe, swaps, built["features"], prices)
    audit["result"]["control_5min"] = control_5min(universe, swaps, built["features"], prices)
    raw = json.dumps(audit, indent=2, sort_keys=True, default=str) + "\n"
    OUT.write_text(raw)
    print(json.dumps({"output": str(OUT), "sha256": hashlib.sha256(raw.encode()).hexdigest(),
                      "verdict": audit["result"]["verdict"]}, indent=2))


if __name__ == "__main__":
    main()
