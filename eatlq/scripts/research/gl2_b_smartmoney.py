#!/usr/bin/env python3
"""GL2-B two-stage, research-only account FIFO and copyability replay.

Stage 1 is deliberately unable to inspect forward copy returns.  Stage 2
requires the hash of a frozen stage-1 distribution and a separate threshold
preregistration file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

REPO = Path(__file__).resolve().parents[2]
CH = "eatlq-clickhouse-1"
WETH = "0x4200000000000000000000000000000000000006"
START, END = 46_389_458, 46_417_105
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
sys.path.insert(0, str(REPO / "scripts" / "wallet_profile_v0"))
from base_transfer_net_attribution import RpcClient  # noqa: E402
from base_b3_entity_attribution import load_rpc_url, load_universe  # noqa: E402


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ch(sql: str, timeout: int = 300) -> list[dict[str, Any]]:
    p = subprocess.run(
        ["docker", "exec", CH, "clickhouse-client", "--max_threads", "2", "--query", sql],
        text=True, capture_output=True, timeout=timeout, check=False,
    )
    if p.returncode:
        raise RuntimeError(p.stderr or p.stdout)
    return [json.loads(x) for x in p.stdout.splitlines() if x.strip()]


def q(values: list[str]) -> str:
    return ",".join("'" + x.replace("'", "''") + "'" for x in values)


def quantiles(xs: list[float]) -> dict[str, float | int | None]:
    if not xs:
        return {"n": 0, "min": None, "p25": None, "median": None, "p75": None, "p90": None, "max": None}
    ys = sorted(xs)
    def at(frac: float) -> float:
        return ys[round((len(ys) - 1) * frac)]
    return {"n": len(ys), "min": ys[0], "p25": at(.25), "median": at(.5),
            "p75": at(.75), "p90": at(.9), "max": ys[-1]}


def rpc_batch(rpc: RpcClient, calls: list[tuple[str, list[Any]]], chunk: int = 20) -> list[Any]:
    out: list[Any] = []
    for offset in range(0, len(calls), chunk):
        for attempt in range(6):
            try:
                out.extend(rpc.batch(calls[offset:offset + chunk], timeout=180))
                time.sleep(1.05)
                break
            except RuntimeError:
                if attempt == 5:
                    raise
                time.sleep(1.5 * (attempt + 1))
    return out


def receipt_nets(rpc: RpcClient, txs: list[str], by_tx: dict[str, dict[str, Any]]) -> dict[tuple[str, str, str], int]:
    receipts = rpc_batch(rpc, [("eth_getTransactionReceipt", [tx]) for tx in txs])
    nets: dict[tuple[str, str, str], int] = defaultdict(int)
    for tx, receipt in zip(txs, receipts):
        account = by_tx[tx]["resolved_account_or_entity"].lower()
        for log in (receipt or {}).get("logs", []):
            topics = [str(x).lower() for x in (log.get("topics") or [])]
            if len(topics) < 3 or topics[0] != TRANSFER_TOPIC:
                continue
            token = str(log.get("address", "")).lower()
            sender, recipient = "0x" + topics[1][-40:], "0x" + topics[2][-40:]
            raw_data = str(log.get("data", "0x0"))
            amount = int(raw_data, 16) if raw_data not in ("0x", "") else 0
            if recipient == account:
                nets[(tx, token, account)] += amount
            if sender == account:
                nets[(tx, token, account)] -= amount
    return nets


def load_stage1_trades(sample: dict[str, Any], rpc: RpcClient) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    high = [x for x in sample["rows"] if x["resolution_confidence"] == "ACCOUNT_HIGH_CONFIDENCE"]
    by_tx = {x["tx_hash"].lower(): x for x in high}
    txs = sorted(by_tx)
    swaps = ch(f"""
      SELECT lower(s.tx_hash) tx_hash,s.block_number,toString(s.block_time) block_time,s.log_index,
             lower(s.pool_address) pool,lower(p.token0_address) token0,lower(p.token1_address) token1,
             p.token0_decimals,p.token1_decimals,toString(s.amount0) amount0,toString(s.amount1) amount1
      FROM eatlq.uniswap_v3_swaps_decoded s
      INNER JOIN
      (SELECT lower(pool_address) pool_address,argMax(lower(token0_address),discovered_at) token0_address,
              argMax(lower(token1_address),discovered_at) token1_address,
              argMax(token0_decimals,discovered_at) token0_decimals,
              argMax(token1_decimals,discovered_at) token1_decimals
       FROM eatlq.discovered_pools WHERE chain_id=8453 GROUP BY pool_address) p
      ON lower(s.pool_address)=p.pool_address
      WHERE s.chain_id=8453 AND s.removed=0 AND lower(s.tx_hash) IN ({q(txs)})
      ORDER BY s.block_number,s.log_index FORMAT JSONEachRow
    """)
    nets = receipt_nets(rpc, txs, by_tx)
    out: list[dict[str, Any]] = []
    drops = Counter()
    for s in swaps:
        a = by_tx[s["tx_hash"]]
        a0, a1 = int(s["amount0"]), int(s["amount1"])
        if s["token0"] == WETH:
            weth_raw, token_raw, token, dec = a0, a1, s["token1"], int(s["token1_decimals"])
        elif s["token1"] == WETH:
            weth_raw, token_raw, token, dec = a1, a0, s["token0"], int(s["token0_decimals"])
        else:
            drops["not_weth_pair"] += 1; continue
        side = "buy" if weth_raw > 0 and token_raw < 0 else "sell" if weth_raw < 0 and token_raw > 0 else "unknown"
        if side == "unknown":
            drops["unknown_side"] += 1; continue
        account = a["resolved_account_or_entity"].lower()
        net = nets[(s["tx_hash"], token, account)]
        weth_net = nets[(s["tx_hash"], WETH, account)]
        expected = abs(token_raw) if side == "buy" else -abs(token_raw)
        # Multi-pool tx can repeat the same account/token net.  Only unique tx/token rows enter FIFO.
        out.append({"tx_hash": s["tx_hash"], "block_number": int(s["block_number"]),
                    "block_time": s["block_time"], "log_index": int(s["log_index"]),
                    "pool": s["pool"], "account": account, "token": token,
                    "token_decimals": dec, "side": side, "token_raw": abs(token_raw),
                    "account_token_net_raw": net, "account_weth_net_raw": weth_net,
                    "weth_raw": abs(weth_net), "pool_weth_raw_reference_only": abs(weth_raw),
                    "inventory_direction_ok": net > 0 if side == "buy" else net < 0,
                    "weth_cashflow_direction_ok": weth_net < 0 if side == "buy" else weth_net > 0,
                    "pool_amount_equals_account_net": net == expected,
                    "route_family": a["route_family"], "account_code_class": a["tx_signer_code_class_at_swap_block"]})
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    duplicate = set()
    for row in out:
        key = (row["tx_hash"], row["account"], row["token"])
        if key in unique:
            duplicate.add(key)
        else:
            unique[key] = row
    for k, x in unique.items():
        if k in duplicate:
            x["weth_closure_method"] = "UNRESOLVED_MULTI_LEG"
        elif x["weth_cashflow_direction_ok"]:
            x["weth_closure_method"] = "ACCOUNT_WETH_RECEIPT_NET"
        elif x["inventory_direction_ok"] and x["pool_amount_equals_account_net"]:
            # Exactly one WETH/target pool leg plus account target-token receipt net is
            # the frozen route-level closure for native ETH wrap/unwrap transactions.
            x["weth_closure_method"] = "UNIQUE_SINGLE_WETH_POOL_ROUTE"
            x["weth_raw"] = x["pool_weth_raw_reference_only"]
        else:
            x["weth_closure_method"] = "UNRESOLVED_NO_CLOSED_CASHFLOW"
    fifo_rows = [x for k, x in unique.items() if k not in duplicate and x["inventory_direction_ok"]
                 and x["weth_closure_method"] in ("ACCOUNT_WETH_RECEIPT_NET","UNIQUE_SINGLE_WETH_POOL_ROUTE")]
    drops["multi_leg_duplicate_tx_account_token"] = len(duplicate)
    drops["inventory_direction_mismatch"] = sum(not x["inventory_direction_ok"] for x in unique.values())
    drops["weth_cashflow_unclosed"] = sum(x["weth_closure_method"].startswith("UNRESOLVED") for x in unique.values())
    return sorted(fifo_rows, key=lambda x: (x["block_number"], x["log_index"], x["tx_hash"])), dict(drops)


def fifo(rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    lots: dict[tuple[str, str], deque[list[int]]] = defaultdict(deque)  # qty,cost
    acc: dict[str, dict[str, Any]] = defaultdict(lambda: {"buys":0,"sells":0,"bought_raw":0,"sold_raw":0,
        "cost_raw":0,"proceeds_raw":0,"realized_weth_raw":0,"closed_pnls_raw":[],"oversell_raw":0,
        "first_block":None,"last_block":None})
    events = []
    for r in rows:
        a = acc[r["account"]]; key = (r["account"], r["token"])
        a["first_block"] = r["block_number"] if a["first_block"] is None else min(a["first_block"], r["block_number"])
        a["last_block"] = r["block_number"] if a["last_block"] is None else max(a["last_block"], r["block_number"])
        if r["side"] == "buy":
            qty = int(r["account_token_net_raw"])
            if qty <= 0: continue
            lots[key].append([qty, int(r["weth_raw"])])
            a["buys"] += 1; a["bought_raw"] += qty; a["cost_raw"] += int(r["weth_raw"])
            events.append({**r,"matched_raw":0,"realized_pnl_raw":0})
            continue
        sell_qty = -int(r["account_token_net_raw"])
        if sell_qty <= 0: continue
        original, remaining = sell_qty, sell_qty
        matched_cost = 0
        while remaining and lots[key]:
            qty, cost = lots[key][0]
            take = min(remaining, qty)
            take_cost = cost if take == qty else cost * take // qty
            matched_cost += take_cost; qty -= take; cost -= take_cost; remaining -= take
            if qty == 0: lots[key].popleft()
            else: lots[key][0] = [qty, cost]
        matched = original - remaining
        proceeds = int(r["weth_raw"]) * matched // original if original else 0
        pnl = proceeds - matched_cost
        a["sells"] += 1; a["sold_raw"] += matched; a["proceeds_raw"] += proceeds
        a["realized_weth_raw"] += pnl; a["oversell_raw"] += remaining
        if matched: a["closed_pnls_raw"].append(pnl)
        events.append({**r,"matched_raw":matched,"matched_cost_raw":matched_cost,
                       "matched_proceeds_raw":proceeds,"realized_pnl_raw":pnl,"oversell_raw":remaining})
    for (wallet, _), queue in lots.items():
        a = acc[wallet]
        a.setdefault("remaining_raw", 0)
        a["remaining_raw"] += sum(x[0] for x in queue)
    for wallet, a in acc.items():
        pnls = a.pop("closed_pnls_raw")
        gains = sum(x for x in pnls if x > 0); losses = -sum(x for x in pnls if x < 0)
        a["n_closed"] = len(pnls); a["winrate"] = sum(x > 0 for x in pnls)/len(pnls) if pnls else 0.0
        a["profit_factor"] = gains/losses if losses else (999.0 if gains else 0.0)
        a["median_pnl_weth"] = median(pnls)/1e18 if pnls else 0.0
        a["realized_weth"] = a["realized_weth_raw"]/1e18
        a["top1_profit_share"] = max([x for x in pnls if x>0], default=0)/gains if gains else 1.0
        a["sell_buy_ratio"] = a["sells"]/a["buys"] if a["buys"] else 999.0
        a["span_blocks"] = (a["last_block"]-a["first_block"]) if a["first_block"] is not None else 0
        a.setdefault("remaining_raw", 0)
    return dict(acc), events


def stage1(args: argparse.Namespace) -> None:
    sample = json.loads(args.sample.read_text())
    trace = json.loads(args.trace.read_text())
    rpc = RpcClient(load_rpc_url(args.rpc_env_file, args.rpc_env_key))
    eoa = [x for x in sample["rows"] if x["resolution_confidence"] == "ACCOUNT_HIGH_CONFIDENCE"
           and x["tx_signer_code_class_at_swap_block"] == "eoa" and x["resolution_method"].startswith("direct_transaction_signer")]
    mismatches = [x for x in eoa if x["resolved_account_or_entity"].lower() != x["tx_from"].lower()]
    trace_eoa = [x for x in trace["rows"] if x["resolution_method"].startswith("direct_transaction_signer")]
    trace_bad = [x for x in trace_eoa if not x["trace_match"] or x["resolved_account_or_entity"] != x["trace_root_from"]]
    rows, drops = load_stage1_trades(sample, rpc)
    accounts, events = fifo(rows)
    dist_fields = ["n_closed","realized_weth","winrate","profit_factor","median_pnl_weth",
                   "top1_profit_share","sell_buy_ratio","span_blocks","buys","sells"]
    distribution = {f: quantiles([float(x[f]) for x in accounts.values()]) for f in dist_fields}
    eligible_golden = sorted([w for w,a in accounts.items() if a["buys"]>=2 and a["sells"]>=1 and a["n_closed"]>=1],
                             key=lambda w: hashlib.sha256(w.encode()).hexdigest())[:5]
    golden = []
    for wallet in eligible_golden:
        ev = [x for x in events if x["account"] == wallet]
        # Independent integer conservation recalculation, not a tolerance comparison.
        bought = sum(int(x["account_token_net_raw"]) for x in ev if x["side"] == "buy")
        sold = sum(int(x.get("matched_raw",0)) for x in ev if x["side"] == "sell")
        cost = sum(int(x["weth_raw"]) for x in ev if x["side"] == "buy")
        proceeds = sum(int(x.get("matched_proceeds_raw",0)) for x in ev if x["side"] == "sell")
        expected = {"bought_raw":bought,"sold_raw":sold,"remaining_raw":bought-sold,
                    "cost_raw":cost,"proceeds_raw":proceeds}
        actual = {k:int(accounts[wallet][k]) for k in expected}
        calls=[]
        for x in ev:
            calldata="0x70a08231"+wallet[2:].rjust(64,"0")
            calls.extend([("eth_call",[{"to":x["token"],"data":calldata},hex(x["block_number"]-1)]),
                          ("eth_call",[{"to":x["token"],"data":calldata},hex(x["block_number"])])])
        raw_balances=rpc_batch(rpc,calls) if calls else []
        balance_checks=[]
        for i,x in enumerate(ev):
            before=int(raw_balances[2*i] or "0x0",16); after=int(raw_balances[2*i+1] or "0x0",16)
            balance_checks.append({"tx_hash":x["tx_hash"],"block_number":x["block_number"],"token":x["token"],
                "balance_before_raw":before,"balance_after_raw":after,"balance_delta_raw":after-before,
                "receipt_transfer_net_raw":int(x["account_token_net_raw"]),
                "integer_equal":after-before==int(x["account_token_net_raw"])})
        golden.append({"wallet":wallet,"transactions":len(ev),"expected":expected,"actual":actual,
                       "integer_equal":expected==actual,
                       "all_transfer_inventory_direction_ok":all(x["inventory_direction_ok"] for x in ev),
                       "balance_of_checks":balance_checks,
                       "all_balance_of_integer_equal":all(x["integer_equal"] for x in balance_checks)})
    result = {"schema_version":1,"alpha_id":"AR-GL2-B-SMARTMONEY","research_only":True,"no_go":True,
      "window":{"start_block":START,"end_block":END},
      "population":{"target_universe_transactions":sample["target_universe"]["unique_transactions"],
                    "deterministic_attribution_sample":len(sample["rows"]),
                    "high_confidence_sample":sum(x["resolution_confidence"]=="ACCOUNT_HIGH_CONFIDENCE" for x in sample["rows"]),
                    "fifo_accounts":len(accounts),"fifo_events":len(events),
                    "scope_warning":"qualification distribution is the preregistered deterministic B3 target-universe sample, not all Base swaps"},
      "attribution":{"eoa_subset_n":len(eoa),"eoa_mismatch_n":len(mismatches),
                     "eoa_mismatch_pct":100*len(mismatches)/len(eoa) if eoa else None,
                     "gate_le_2pct":bool(eoa) and len(mismatches)/len(eoa)<=.02,
                     "trace_eoa_golden_n":len(trace_eoa),"trace_eoa_mismatch_n":len(trace_bad),
                     "trace_eoa_integer_gate":len(trace_eoa)>0 and not trace_bad,
                     "smart_accounts_reference_only":True},
      "fifo":{"input_drops":drops,"golden_required_min":3,"golden_wallets":golden,
              "golden_n":len(golden),"golden_integer_equal_n":sum(x["integer_equal"] and x["all_balance_of_integer_equal"] for x in golden),
              "golden_gate":len(golden)>=3 and all(x["integer_equal"] and x["all_balance_of_integer_equal"] for x in golden),
              "oversell_accounts":sum(int(a["oversell_raw"])>0 for a in accounts.values())},
      "qualification_distribution":distribution,
      "wallet_metrics":accounts,
      "stage2_forward_fields_present":False,
      "external_cross_validation":{"nansen":"NOT_RUN_MISSING_AUTHORITY_AND_API_KEY"},
      "rpc_ledger":{"endpoint_alias":"prettiest-blue-aura","methods":{"eth_getTransactionReceipt":sum(x["resolution_confidence"]=="ACCOUNT_HIGH_CONFIDENCE" for x in sample["rows"]),"eth_call_balanceOf":sum(2*len(x["balance_of_checks"]) for x in golden)},"rpc_batch_calls":rpc.calls,"secrets_recorded":False},
      "decision_flags":{"training_allowed":False,"live_allowed":False,"self_go_allowed":False}}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n")
    print(json.dumps({"population":result["population"],"attribution":result["attribution"],
                      "fifo":{k:v for k,v in result["fifo"].items() if k!="golden_wallets"},
                      "qualification_distribution":distribution},indent=2))


def parse_time(value: str) -> float:
    value=value.replace(" ","T")
    dt=datetime.fromisoformat(value.replace("Z","+00:00"))
    if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def outcome_summary(rows: list[dict[str,Any]]) -> dict[str,Any]:
    vals=[x["return_frac"] for x in rows if x["status"]=="RESOLVED_TRUE_SWAP_REFERENCES"]
    unresolved=Counter(x["status"] for x in rows if x["status"]!="RESOLVED_TRUE_SWAP_REFERENCES")
    if not vals:
        stats={"mean":None,"median":None,"winrate":None,"p90":None,"max":None,"top1_removed_mean":None}
    else:
        ys=sorted(vals); trimmed=sorted(vals,reverse=True)[max(1,math.ceil(.01*len(vals))):]
        stats={"mean":mean(vals),"median":median(vals),"winrate":sum(x>0 for x in vals)/len(vals),
               "p90":ys[round(.9*(len(ys)-1))],"max":max(vals),
               "top1_removed_mean":mean(trimmed) if trimmed else None}
    return {"n_signals":len(rows),"resolved":len(vals),"unresolved":len(rows)-len(vals),
            "unresolved_reasons":dict(sorted(unresolved.items())),**stats}


def stage2(args: argparse.Namespace) -> None:
    if sha(args.stage1) != args.stage1_sha: raise ValueError("stage1 hash mismatch")
    if sha(args.prereg) != args.prereg_sha: raise ValueError("stage2 prereg hash mismatch")
    stage1=json.loads(args.stage1.read_text())
    eligible={w for w,a in stage1["wallet_metrics"].items() if a["n_closed"]>=2 and a["realized_weth"]>0
      and a["winrate"]>=.5 and a["profit_factor"]>=1.2 and a["top1_profit_share"]<=.75
      and a["sell_buy_ratio"]<=2 and a["span_blocks"]>=1000}
    sample=json.loads(args.sample.read_text())
    pools=sorted({x["venue_address"] for x in load_universe()})
    forward_start,forward_end=46_417_106,46_444_753
    raw=ch(f"""
      SELECT lower(s.tx_hash) tx_hash,s.block_number,toString(s.block_time) block_time,s.log_index,
             lower(s.pool_address) pool,
             lower(p.token0_address) token0,lower(p.token1_address) token1,
             p.token0_decimals,p.token1_decimals,toString(s.amount0) amount0,toString(s.amount1) amount1
      FROM eatlq.uniswap_v3_swaps_decoded s
      INNER JOIN (SELECT lower(pool_address) pool_address,argMax(lower(token0_address),discovered_at) token0_address,
          argMax(lower(token1_address),discovered_at) token1_address,argMax(token0_decimals,discovered_at) token0_decimals,
          argMax(token1_decimals,discovered_at) token1_decimals FROM eatlq.discovered_pools WHERE chain_id=8453 GROUP BY pool_address) p
        ON lower(s.pool_address)=p.pool_address
      WHERE s.chain_id=8453 AND s.removed=0 AND s.block_number BETWEEN {forward_start} AND {forward_end}
        AND lower(s.pool_address) IN ({q(pools)}) AND s.block_time IS NOT NULL
      ORDER BY s.block_number,s.log_index FORMAT JSONEachRow
    """)
    rpc=RpcClient(load_rpc_url(args.rpc_env_file,args.rpc_env_key))
    tx_hashes=sorted({x["tx_hash"] for x in raw})
    tx_payloads=rpc_batch(rpc,[("eth_getTransactionByHash",[x]) for x in tx_hashes])
    tx_from={h:str((x or {}).get("from","")).lower() for h,x in zip(tx_hashes,tx_payloads)}
    trades=[]
    for x in raw:
        a0,a1=int(x["amount0"]),int(x["amount1"])
        if x["token0"]==WETH: wr,tr,token=a0,a1,x["token1"]
        elif x["token1"]==WETH: wr,tr,token=a1,a0,x["token0"]
        else: continue
        side="buy" if wr>0 and tr<0 else "sell" if wr<0 and tr>0 else "unknown"
        if side=="unknown" or tr==0: continue
        trades.append({"tx_hash":x["tx_hash"],"block_number":int(x["block_number"]),"log_index":int(x["log_index"]),
          "ts":parse_time(x["block_time"]),"pool":x["pool"],"wallet":tx_from.get(x["tx_hash"],""),"token":token,
          "side":side,"price_weth_raw_per_token_raw":abs(wr)/abs(tr)})
    by_pool=defaultdict(list)
    for x in trades: by_pool[x["pool"]].append(x)
    smart_signals=[x for x in trades if x["side"]=="buy" and x["wallet"] in eligible]
    baseline_all=[x for x in trades if x["side"]=="buy" and x["wallet"] not in eligible]
    baseline_all.sort(key=lambda x:hashlib.sha256((x["tx_hash"]+str(x["log_index"])).encode()).hexdigest())
    baseline_signals=baseline_all[:len(smart_signals)]
    notional=.001; gas=.0000215
    def simulate(signal:dict[str,Any],delay:int,kind:str)->dict[str,Any]:
        path=by_pool[signal["pool"]]
        entry=next((x for x in path if x["tx_hash"]!=signal["tx_hash"] and x["ts"]>=signal["ts"]+delay),None)
        base={"kind":kind,"delay_sec":delay,"source_tx":signal["tx_hash"],"source_wallet":signal["wallet"],
              "source_ts":signal["ts"],"pool":signal["pool"],"token":signal["token"]}
        if entry is None: return {**base,"status":"UNRESOLVED_NO_TRUE_ENTRY_REFERENCE"}
        exit_=next((x for x in path if x["tx_hash"]!=entry["tx_hash"] and x["ts"]>=entry["ts"]+7200),None)
        if exit_ is None: return {**base,"status":"UNRESOLVED_NO_TRUE_EXIT","entry_tx":entry["tx_hash"],"entry_ts":entry["ts"]}
        entry_price=entry["price_weth_raw_per_token_raw"]*1.003
        exit_price=exit_["price_weth_raw_per_token_raw"]*.997
        ret=(notional*(exit_price/entry_price)-notional-2*gas)/notional
        return {**base,"status":"RESOLVED_TRUE_SWAP_REFERENCES","evidence_type":"TRUE_SWAP_MARKET_REFERENCE_SIMULATED_FILL_PLUS_FROZEN_30BPS_ADVERSE",
                "entry_tx":entry["tx_hash"],"entry_ts":entry["ts"],"exit_tx":exit_["tx_hash"],"exit_ts":exit_["ts"],
                "entry_after_source":entry["ts"]>=signal["ts"]+delay and entry["tx_hash"]!=signal["tx_hash"],"return_frac":ret}
    rows=[]; summaries={}
    for delay in (1,5,15):
        sr=[simulate(x,delay,"smart") for x in smart_signals]; br=[simulate(x,delay,"baseline") for x in baseline_signals]
        rows.extend(sr); rows.extend(br); summaries[str(delay)]={"smart":outcome_summary(sr),"baseline":outcome_summary(br)}
    result={"schema_version":1,"alpha_id":"AR-GL2-B-SMARTMONEY","research_only":True,"no_go":True,
      "stage1_sha256":args.stage1_sha,"stage2_prereg_sha256":args.prereg_sha,
      "forward_window":{"start_block":forward_start,"end_block":forward_end},"eligible_wallets":sorted(eligible),
      "population":{"pools":len(pools),"true_swaps":len(trades),"smart_buy_signals":len(smart_signals),"baseline_sample":len(baseline_signals)},
      "rpc_ledger":{"endpoint_alias":"prettiest-blue-aura","methods":{"eth_getTransactionByHash":len(tx_hashes)},"rpc_batch_calls":rpc.calls,"secrets_recorded":False},
      "execution_semantics":{"delays_sec":[1,5,15],"entry":"next different true swap market reference after source+delay; 30bps adverse simulated fill","hold_sec":7200,"exit":"next different true swap market reference; 30bps adverse","notional_weth":notional,"gas_weth_per_leg":gas,"real_orders":False},
      "by_delay":summaries,"rows":rows,"external_cross_validation":{"nansen":"NOT_RUN_MISSING_AUTHORITY_AND_API_KEY"},
      "decision_flags":{"training_allowed":False,"live_allowed":False,"self_go_allowed":False}}
    args.output.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n")
    print(json.dumps({k:result[k] for k in ("population","by_delay")},indent=2))


def supplement(args: argparse.Namespace) -> None:
    if sha(args.stage1)!=args.stage1_sha: raise ValueError("stage1 hash mismatch")
    frozen=json.loads(args.stage1.read_text())
    txs=sorted({c["tx_hash"] for g in frozen["fifo"]["golden_wallets"] for c in g["balance_of_checks"]})
    swaps=ch(f"""SELECT lower(s.tx_hash) tx_hash,lower(s.pool_address) pool,s.block_number,s.log_index,
      lower(p.token0_address) token0,lower(p.token1_address) token1,toString(s.amount0) amount0,toString(s.amount1) amount1
      FROM eatlq.uniswap_v3_swaps_decoded s INNER JOIN
      (SELECT lower(pool_address) pool_address,argMax(lower(token0_address),discovered_at) token0_address,
       argMax(lower(token1_address),discovered_at) token1_address FROM eatlq.discovered_pools WHERE chain_id=8453 GROUP BY pool_address) p
      ON lower(s.pool_address)=p.pool_address WHERE s.chain_id=8453 AND s.removed=0 AND lower(s.tx_hash) IN ({q(txs)})
      ORDER BY s.block_number,s.log_index FORMAT JSONEachRow""")
    rpc=RpcClient(load_rpc_url(args.rpc_env_file,args.rpc_env_key))
    receipts=rpc_batch(rpc,[("eth_getTransactionReceipt",[x]) for x in txs]); by_receipt=dict(zip(txs,receipts))
    checks=[]
    for s in swaps:
        expected=int(s["amount0"]) if s["token0"]==WETH else int(s["amount1"]) if s["token1"]==WETH else None
        if expected is None: continue
        pool=s["pool"]; net=0
        for log in (by_receipt[s["tx_hash"]] or {}).get("logs",[]):
            topics=[str(x).lower() for x in (log.get("topics") or [])]
            if str(log.get("address","")).lower()!=WETH or len(topics)<3 or topics[0]!=TRANSFER_TOPIC: continue
            data=str(log.get("data","0x0")); amount=int(data,16) if data not in ("0x","") else 0
            sender,recipient="0x"+topics[1][-40:],"0x"+topics[2][-40:]
            if recipient==pool: net+=amount
            if sender==pool: net-=amount
        checks.append({"tx_hash":s["tx_hash"],"pool":pool,"block_number":int(s["block_number"]),"log_index":int(s["log_index"]),
          "swap_weth_raw_signed":expected,"receipt_pool_weth_transfer_net_raw_signed":net,"integer_equal":net==expected})
    result={"schema_version":1,"alpha_id":"AR-GL2-B-SMARTMONEY","research_only":True,"no_go":True,
      "frozen_stage1_sha256":args.stage1_sha,"golden_wallets":len(frozen["fifo"]["golden_wallets"]),
      "target_balance_checks":sum(len(x["balance_of_checks"]) for x in frozen["fifo"]["golden_wallets"]),
      "weth_cash_leg_checks":len(checks),"weth_cash_leg_integer_equal_n":sum(x["integer_equal"] for x in checks),
      "weth_cash_leg_gate":bool(checks) and all(x["integer_equal"] for x in checks),"checks":checks,
      "rpc_ledger":{"endpoint_alias":"prettiest-blue-aura","methods":{"eth_getTransactionReceipt":len(txs)},"rpc_batch_calls":rpc.calls,"secrets_recorded":False}}
    args.output.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n"); print(json.dumps({k:result[k] for k in ("golden_wallets","target_balance_checks","weth_cash_leg_checks","weth_cash_leg_integer_equal_n","weth_cash_leg_gate")},indent=2))


def finalize(args: argparse.Namespace) -> None:
    s1=json.loads(args.stage1.read_text()); sup=json.loads(args.supplement.read_text()); f=json.loads(args.forward.read_text())
    contaminated=sorted(w for w in f["eligible_wallets"] if int(s1["wallet_metrics"][w]["oversell_raw"])>0)
    signal_wallets=sorted({x["source_wallet"] for x in f["rows"] if x["kind"]=="smart"})
    clean=sorted(set(f["eligible_wallets"])-set(contaminated)); clean_signals=sorted(set(signal_wallets)&set(clean))
    result={"schema_version":1,"alpha_id":"AR-GL2-B-SMARTMONEY","research_only":True,"no_go":True,
      "frozen_hashes":{"stage1":sha(args.stage1),"stage2_prereg":sha(args.prereg),"stage2_erratum":sha(args.erratum)},
      "attribution_gate":s1["attribution"],
      "fifo_golden":{"wallets":s1["fifo"]["golden_n"],"target_balance_delta_checks":sup["target_balance_checks"],
        "target_balance_all_integer_equal":s1["fifo"]["golden_integer_equal_n"]==s1["fifo"]["golden_n"],
        "weth_pool_cash_leg_checks":sup["weth_cash_leg_checks"],"weth_pool_cash_leg_integer_equal":sup["weth_cash_leg_integer_equal_n"],
        "gate":s1["fifo"]["golden_gate"] and sup["weth_cash_leg_gate"]},
      "qualification":{"eligible_wallets":f["eligible_wallets"],"eligible_n":len(f["eligible_wallets"]),
        "left_truncated_oversell_wallets":contaminated,"left_truncated_n":len(contaminated),
        "complete_inventory_wallets":clean,"complete_inventory_n":len(clean)},
      "forward":{"population":f["population"],"signal_wallets":signal_wallets,
        "all_signal_wallets_left_truncated":bool(signal_wallets) and set(signal_wallets)<=set(contaminated),
        "complete_inventory_signal_wallets":clean_signals,"complete_inventory_signal_n":sum(1 for x in f["rows"] if x["kind"]=="smart" and x["delay_sec"]==1 and x["source_wallet"] in clean),
        "descriptive_contaminated_group_by_delay":f["by_delay"],
        "comparison_warning":"smart resolved 4/4 versus baseline 2/4 and every smart signal came from left-truncated oversell wallets; means are not an identified edge comparison"},
      "external_cross_validation":{"nansen":"NOT_RUN_MISSING_AUTHORITY_AND_API_KEY"},
      "merit":"UNTESTED_ELIGIBILITY_CONTAMINATED","reviewer_grade":"PENDING_INDEPENDENT_REVIEW",
      "decision_flags":{"training_allowed":False,"live_allowed":False,"self_go_allowed":False}}
    args.output.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n"); print(json.dumps({"fifo_golden":result["fifo_golden"],"qualification":result["qualification"],"forward":{k:v for k,v in result["forward"].items() if k!="descriptive_contaminated_group_by_delay"},"merit":result["merit"]},indent=2))


def main() -> None:
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest="mode",required=True)
    s=sub.add_parser("stage1"); s.add_argument("--sample",type=Path,required=True); s.add_argument("--trace",type=Path,required=True); s.add_argument("--output",type=Path,required=True); s.add_argument("--rpc-env-file",type=Path,required=True); s.add_argument("--rpc-env-key",required=True)
    f=sub.add_parser("stage2"); f.add_argument("--stage1",type=Path,required=True); f.add_argument("--stage1-sha",required=True); f.add_argument("--prereg",type=Path,required=True); f.add_argument("--prereg-sha",required=True); f.add_argument("--sample",type=Path,required=True); f.add_argument("--output",type=Path,required=True); f.add_argument("--rpc-env-file",type=Path,required=True); f.add_argument("--rpc-env-key",required=True)
    g=sub.add_parser("supplement"); g.add_argument("--stage1",type=Path,required=True); g.add_argument("--stage1-sha",required=True); g.add_argument("--output",type=Path,required=True); g.add_argument("--rpc-env-file",type=Path,required=True); g.add_argument("--rpc-env-key",required=True)
    z=sub.add_parser("finalize"); z.add_argument("--stage1",type=Path,required=True); z.add_argument("--supplement",type=Path,required=True); z.add_argument("--forward",type=Path,required=True); z.add_argument("--prereg",type=Path,required=True); z.add_argument("--erratum",type=Path,required=True); z.add_argument("--output",type=Path,required=True)
    args=p.parse_args()
    if args.mode=="stage1": stage1(args)
    elif args.mode=="stage2": stage2(args)
    elif args.mode=="supplement": supplement(args)
    elif args.mode=="finalize": finalize(args)


if __name__=="__main__": main()
