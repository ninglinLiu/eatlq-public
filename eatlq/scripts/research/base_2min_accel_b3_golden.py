#!/usr/bin/env python3
"""Independent archive-RPC golden checks for B3 (research-only)."""
from __future__ import annotations

import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
from base_2min_accel_b3 import (  # noqa: E402
    CHAIN_ID, DEAD, LOOKBACK, TRANSFER_TOPIC, WETH, ZERO, Rpc, ch_query,
    decode_log, exclusion_set, load_universe, rpc_url, topic_address,
)
from base_2min_accel_b3_core import net_balances, reserve_net  # noqa: E402

FIXTURE = REPO / "tests/fixtures/base_2min_accel_b3_golden.json"
FOT_TOKEN = "0xfd860436e10f899bee052e7b5578205f29e396b9"
FOT_POOL = "0xa11c4daa19a632d220ec443c2e28c07b07803f78"
FOT_CREATION_TX = "0x8cf2ad85ef33eba5ef2bf2f5c8fa5aec4e121cbfde1360e83b8c91e9babd90d7"


def balance_of(rpc: Rpc, token: str, account: str, block: int) -> int:
    data = "0x70a08231" + "0" * 24 + account.lower()[2:]
    return int(rpc.call("eth_call", [{"to": token, "data": data}, hex(block)]) or "0x0", 16)


def fetched_events(kind: str, address: str, pool: str, begin: int, end: int,
                   topics: list, rpc: Rpc) -> list[dict]:
    rows = []
    for a in range(begin, end + 1, 10_000):
        logs = rpc.call("eth_getLogs", [{"fromBlock": hex(a), "toBlock": hex(min(end, a+9_999)),
            "address": address, "topics": topics}]) or []
        rows.extend(x for x in (decode_log(log, kind, address, pool) for log in logs) if x)
    return rows


def event_side(pool: str, pool_type: str, target_index: int, snapshot: int) -> tuple[int, int]:
    idx, other = target_index, 1-target_index
    if pool_type == "v2":
        row = ch_query(f"""
          SELECT
            toString(sum(mw)-sum(bw)+sum(sw_in)-sum(sw_out)) weth,
            toString(sum(mt)-sum(bt)+sum(st_in)-sum(st_out)) target
          FROM
          (SELECT
             toInt256(if({other}=0,amount0,amount1)) mw,toInt256(if({idx}=0,amount0,amount1)) mt,
             toInt256(0) bw,toInt256(0) bt,toInt256(0) sw_in,toInt256(0) sw_out,toInt256(0) st_in,toInt256(0) st_out
           FROM eatlq.uniswap_v2_mints_decoded WHERE chain_id={CHAIN_ID} AND lower(pair_address)='{pool}' AND block_number<={snapshot}
           UNION ALL SELECT 0,0,toInt256(if({other}=0,amount0,amount1)),toInt256(if({idx}=0,amount0,amount1)),0,0,0,0
           FROM eatlq.uniswap_v2_burns_decoded WHERE chain_id={CHAIN_ID} AND lower(pair_address)='{pool}' AND block_number<={snapshot}
           UNION ALL SELECT 0,0,0,0,toInt256(if({other}=0,amount0_in,amount1_in)),toInt256(if({other}=0,amount0_out,amount1_out)),toInt256(if({idx}=0,amount0_in,amount1_in)),toInt256(if({idx}=0,amount0_out,amount1_out))
           FROM eatlq.uniswap_v2_swaps_decoded WHERE chain_id={CHAIN_ID} AND lower(pair_address)='{pool}' AND block_number<={snapshot})
        """)[0]
        return int(row["weth"]), int(row["target"])
    row = ch_query(f"""
      SELECT toString(sum(weth)) weth,toString(sum(target)) target FROM
      (SELECT toInt256(if({other}=0,amount0,amount1)) weth,toInt256(if({idx}=0,amount0,amount1)) target
       FROM eatlq.uniswap_v3_mints_decoded WHERE chain_id={CHAIN_ID} AND lower(pool_address)='{pool}' AND block_number<={snapshot}
       UNION ALL SELECT -toInt256(if({other}=0,amount0,amount1)),-toInt256(if({idx}=0,amount0,amount1))
       FROM eatlq.uniswap_v3_burns_decoded WHERE chain_id={CHAIN_ID} AND lower(pool_address)='{pool}' AND block_number<={snapshot}
       UNION ALL SELECT toInt256(if({other}=0,amount0,amount1)),toInt256(if({idx}=0,amount0,amount1))
       FROM eatlq.uniswap_v3_swaps_decoded WHERE chain_id={CHAIN_ID} AND lower(pool_address)='{pool}' AND block_number<={snapshot})
    """)[0]
    return int(row["weth"]), int(row["target"])


def main() -> None:
    rpc = Rpc(rpc_url())
    universe = {p["pool_address"]: p for p in load_universe()}
    candidates = ch_query("""
      SELECT u.pool_address,u.pool_type,u.target_token,u.target_index,u.created_block,u.snapshot_120,
             f.window AS window,f.real_signal AS real_signal,f.proxy_signal AS proxy_signal,
             f.new_holders_1+f.new_holders_2 AS holder_hint
      FROM eatlq.base_pool_universe_b3 AS u FINAL
      INNER JOIN eatlq.base_pool_2min_snapshot_b3 AS f FINAL USING(chain_id,pool_address)
      WHERE f.real_signal=1 OR f.proxy_signal=1
      ORDER BY (u.pool_type='v2') DESC,holder_hint,pool_address
    """)
    target_rows = ch_query("SELECT request_address,block_number,sender,recipient,toString(amount) amount FROM eatlq.base_pool_transfers_b3 FINAL WHERE request_kind='target'")
    weth_rows = ch_query("SELECT pool_address,block_number,tx_hash,log_index,sender,recipient,toString(amount) amount FROM eatlq.base_pool_transfers_b3 FINAL WHERE request_kind IN ('weth_in','weth_out')")
    targets, weths = defaultdict(list), defaultdict(dict)
    for r in target_rows: targets[r["request_address"]].append(r)
    for r in weth_rows: weths[r["pool_address"]][(r["tx_hash"], int(r["log_index"]))] = r
    selected, used_types = [], set()
    for c in candidates:
        if len(selected) >= 3: break
        if c["pool_type"] in used_types and len(selected) < 2: continue
        pool, token, snap = c["pool_address"], c["target_token"], int(c["snapshot_120"])
        tev, wev = targets[token], list(weths[pool].values())
        wr, tr = reserve_net(wev, pool, snap), reserve_net(tev, pool, snap)
        wb, tb = balance_of(rpc, WETH, pool, snap), balance_of(rpc, token, pool, snap)
        excluded = exclusion_set() | {pool, token}
        nets = net_balances(tev, snap)
        holders = sorted(a for a, n in nets.items() if n > 0 and a not in excluded)
        checks = [{"address": a, "reconstructed": str(nets[a]),
                   "balance_of": str(balance_of(rpc, token, a, snap))} for a in holders]
        ew, et = event_side(pool, c["pool_type"], int(c["target_index"]), snap)
        case = {"kind": "locked_standard", "pool": pool, "pool_type": c["pool_type"],
            "target_token": token, "snapshot_block": snap, "window": c["window"],
            "real_signal": int(c["real_signal"]), "proxy_signal": int(c["proxy_signal"]),
            "reserve": {"weth_reconstructed": str(wr), "weth_balance_of": str(wb),
                "target_reconstructed": str(tr), "target_balance_of": str(tb),
                "event_weth": str(ew), "event_target": str(et)},
            "holder_reconstructed_count": len(holders),
            "holder_balance_of_positive_count": sum(int(x["balance_of"]) > 0 for x in checks),
            "holder_checks": checks,
            "event_side_equal": {"weth": ew == wr, "target": et == tr},
            "event_side_note": "local mint/burn/swap event series misses initial reserve-changing events when false; transfer-net remains primary",
            "standard_equalities": {"weth": wr == wb, "target": tr == tb,
                "holders": all(int(x["reconstructed"]) == int(x["balance_of"]) for x in checks)}}
        if case["standard_equalities"]["weth"] and case["standard_equalities"]["target"] and case["standard_equalities"]["holders"]:
            selected.append(case); used_types.add(c["pool_type"])

    receipt = rpc.call("eth_getTransactionReceipt", [FOT_CREATION_TX])
    created = int(receipt["blockNumber"], 16); snap = created + 60
    tev = fetched_events("target", FOT_TOKEN, FOT_POOL, max(0, created-LOOKBACK), snap, [TRANSFER_TOPIC], rpc)
    wi = fetched_events("weth_in", WETH, FOT_POOL, created, snap, [TRANSFER_TOPIC,None,topic_address(FOT_POOL)], rpc)
    wo = fetched_events("weth_out", WETH, FOT_POOL, created, snap, [TRANSFER_TOPIC,topic_address(FOT_POOL)], rpc)
    wev = list({(e["tx_hash"],e["log_index"]):e for e in wi+wo}.values())
    wr, tr = reserve_net(wev,FOT_POOL,snap), reserve_net(tev,FOT_POOL,snap)
    wb, tb = balance_of(rpc,WETH,FOT_POOL,snap), balance_of(rpc,FOT_TOKEN,FOT_POOL,snap)
    nets = net_balances(tev,snap); excluded={ZERO,DEAD,WETH,FOT_POOL,FOT_TOKEN}
    holders=sorted(a for a,n in nets.items() if n>0 and a not in excluded)
    checks=[{"address":a,"reconstructed":str(nets[a]),"balance_of":str(balance_of(rpc,FOT_TOKEN,a,snap))} for a in holders]
    selected.append({"kind":"fee_on_sell_sentinel_outside_locked_universe","pool":FOT_POOL,
        "target_token":FOT_TOKEN,"snapshot_block":snap,"cached_honeypot_sell_tax_pct":100,
        "risk_cache_sha256":hashlib.sha256((REPO/"scripts/research/base_lefttail/_external_risk_current.json").read_bytes()).hexdigest(),
        "reserve":{"weth_reconstructed":str(wr),"weth_balance_of":str(wb),"target_reconstructed":str(tr),"target_balance_of":str(tb)},
        "holder_checks":checks,"mismatch":{"weth":str(wb-wr),"target":str(tb-tr)},
        "note":"nonstandard golden sentinel only; excluded from locked universe and all return results"})
    out={"chain_id":CHAIN_ID,"cases":selected,"rpc_calls_this_run":rpc.calls,
         "all_locked_standard_integer_equalities":all(all(x["standard_equalities"].values()) for x in selected if x["kind"]=="locked_standard")}
    FIXTURE.write_text(json.dumps(out,indent=2,sort_keys=True)+"\n")
    print(json.dumps({"fixture":str(FIXTURE),"cases":len(selected),"rpc_calls":rpc.calls,
        "locked_equal":out["all_locked_standard_integer_equalities"]},indent=2))


if __name__ == "__main__": main()
