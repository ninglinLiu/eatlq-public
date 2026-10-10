#!/usr/bin/env python3
"""
修正渔法 (corrected fishing method) — self-contained live onset detector.

Bypasses the broken redis->archiver->decoder pipeline: pulls fresh data DIRECTLY
from QuickNode via eth_getLogs, applies the FULL corrected gate, and emits only
structurally-clean + sellable live candidates. No trade here (detect-only); the
canary executor consumes the candidates ledger.

GATE (sprint097 validated; no lowering of standards):
  1. universe   : Uniswap-V3, WETH-paired, non-routing alt
  2. fee==100   : 0.01% tier ONLY (the single structurally +EV venue; all other tiers were
                  decisively negative on 326 fresh onsets, e.g. fee=10000: EV -60% WR 6%)
  3. onset      : heat >= 5 swaps within 30min of birth (backtest: 100% of fee=100 winners
                  onset <=30min); ENTRY only within 15min of onset so we buy ~onset price and
                  never chase a pool that already ran up (faithful to backtest entry@onset)
  4. 0x6ff5 tier: dom_share flipped from exclusion -> signal.
                  Rule A (0x6ff5 present @ fee=100, backtest 94% WR): require >=1 external buyer.
                  Rule B (no 0x6ff5 @ fee=100, organic): require distinct recipients>=3, top<=0.5.
  5. sellable   : QuoterV2 WETH->alt entry AND alt->WETH exit both succeed (honeypot-aware)
  6. full cost  : round-trip QuoterV2 net (incl slippage@notional + LP fee) - gas; recorded

Outputs: mvp/diagnostics/live/candidates_ledger.csv  (one row per evaluated onset pool)
Only rows with verdict=CLEAN are eligible for the canary.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from v3_quoterv2_struct import JsonRpcClient, quote_exact_input_single_struct, WETH_BASE  # noqa: E402
from canary_ledger import append_rows  # noqa: E402

FACTORY = "0x33128a8fc17869897dce68ed026d694621f6fdfd"
POOLCREATED_TOPIC = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
ROUTING = {
    "0x4200000000000000000000000000000000000006",  # WETH
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",  # cbBTC
}
FEE_BY_SPACING = {1: 100, 10: 500, 60: 3000, 200: 10000, 100: 500}
LEDGER = "/opt/EATLQ/mvp/diagnostics/live/candidates_ledger.csv"
GAS_USD_ROUNDTRIP = 0.10
BLOCK_SEC = 2.0


def topic_addr(t):
    return "0x" + t[-40:]


def s256(h):
    v = int(h, 16)
    return v - (1 << 256) if v >= (1 << 255) else v


def now():
    return datetime.now(timezone.utc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default=os.environ.get("BASE_RPC_URL", "https://mainnet.base.org"))
    ap.add_argument("--lookback-min", type=int, default=180, help="scan pools created in last N minutes")
    ap.add_argument("--max-age-min", type=int, default=90, help="outer safety bound on pool age")
    ap.add_argument("--onset-window-min", type=int, default=30,
                    help="how long after birth onset(heat>=5) may occur (backtest: 100%% of fee=100 winners onset <=30min)")
    ap.add_argument("--entry-fresh-min", type=int, default=15,
                    help="only buy within N min of onset, so entry~onset price (faithful to backtest entry@onset; never chase a pool that already ran up)")
    ap.add_argument("--size-usd", type=float, default=10.0)
    ap.add_argument("--eth-usd", type=float, default=3000.0)
    ap.add_argument("--max-rpc", type=int, default=1500, help="hard RPC call budget")
    args = ap.parse_args()

    rpc = JsonRpcClient(args.rpc, delay_s=0.03)
    head = int(rpc.call("eth_blockNumber", []), 16)
    span = int(args.lookback_min * 60 / BLOCK_SEC)
    frm = head - span
    print(f"[渔] head={head} scan PoolCreated [{frm},{head}] (~{args.lookback_min}min)", flush=True)

    # 1) PoolCreated in window (chunked)
    created = []
    step = 1500
    b = frm
    while b <= head and rpc.calls < args.max_rpc:
        to = min(b + step - 1, head)
        try:
            logs = rpc.call("eth_getLogs", [{"fromBlock": hex(b), "toBlock": hex(to),
                                             "address": FACTORY, "topics": [POOLCREATED_TOPIC]}])
        except Exception as e:
            print(f"  getLogs PoolCreated {b}-{to} err {e}", flush=True)
            logs = []
        for lg in logs or []:
            tps = lg["topics"]
            t0 = topic_addr(tps[1]).lower(); t1 = topic_addr(tps[2]).lower()
            fee = int(tps[3], 16)
            data = lg["data"][2:]
            pool = ("0x" + data[64:128][-40:]).lower()
            created.append({"pool": pool, "t0": t0, "t1": t1, "fee": fee,
                            "block": int(lg["blockNumber"], 16)})
        b = to + 1
    # WETH-paired non-routing
    cands = []
    for c in created:
        weth_t1 = c["t1"] == WETH_BASE.lower()
        weth_t0 = c["t0"] == WETH_BASE.lower()
        if not (weth_t0 or weth_t1):
            continue
        alt = c["t0"] if weth_t1 else c["t1"]
        if alt in ROUTING:
            continue
        c["alt"] = alt; c["weth_t1"] = weth_t1
        cands.append(c)
    print(f"[渔] PoolCreated total={len(created)} WETH-paired non-routing={len(cands)}", flush=True)

    rows = []
    for c in cands:
        if rpc.calls > args.max_rpc:
            print("[渔] RPC budget reached, stop", flush=True)
            break
        pool = c["pool"]; birth_block = c["block"]
        age_min = (head - birth_block) * BLOCK_SEC / 60.0
        rec = {"ts": now().isoformat(), "pool": pool, "alt": c["alt"], "fee": c["fee"],
               "birth_block": birth_block, "age_min": round(age_min, 1),
               "rule": "", "verdict": "", "reason": ""}
        # sprint097 GATE: 0.01% fee tier (fee==100) is the only structurally +EV venue.
        # All other tiers were decisively negative (fee=10000: EV -60% WR 6%).
        if c["fee"] != 100:
            rec["verdict"] = "REJECT"; rec["reason"] = f"fee={c['fee']}!=100(dead_venue)"; rows.append(rec); continue
        if age_min < 2:
            rec["verdict"] = "REJECT"; rec["reason"] = "too_young(<2min)"; rows.append(rec); continue
        if age_min > args.max_age_min:
            rec["verdict"] = "REJECT"; rec["reason"] = f"too_old(>{args.max_age_min}min)"; rows.append(rec); continue
        # onset detection window: scan swaps from birth up to onset_window_min (capped at head).
        # backtest: 100% of fee=100 winners reached onset(heat>=5) within 30min of birth.
        onset_window_blocks = int(args.onset_window_min * 60 / BLOCK_SEC)
        end_block = min(head, birth_block + onset_window_blocks)
        try:
            slogs = rpc.call("eth_getLogs", [{"fromBlock": hex(birth_block), "toBlock": hex(end_block),
                                             "address": pool, "topics": [SWAP_TOPIC]}])
        except Exception as e:
            rec["verdict"] = "REJECT"; rec["reason"] = f"swap_logs_err"; rows.append(rec); continue
        slogs = slogs or []
        slogs.sort(key=lambda lg: (int(lg["blockNumber"], 16), int(lg.get("logIndex", "0x0"), 16)))
        n = len(slogs)
        HEAT = 5
        if n < HEAT:
            rec["verdict"] = "REJECT"; rec["reason"] = f"heat={n}(<{HEAT},no_onset)"; rows.append(rec); continue
        # onset = block of the HEAT-th swap; entry must be near onset (faithful to backtest)
        onset_block = int(slogs[HEAT - 1]["blockNumber"], 16)
        onset_lag_min = (onset_block - birth_block) * BLOCK_SEC / 60.0
        onset_age_min = (head - onset_block) * BLOCK_SEC / 60.0
        rec.update({"early_swaps": n, "onset_lag_min": round(onset_lag_min, 1),
                    "onset_age_min": round(onset_age_min, 1)})
        if onset_age_min > args.entry_fresh_min:
            rec["verdict"] = "REJECT"
            rec["reason"] = f"onset_too_old({onset_age_min:.0f}min>{args.entry_fresh_min},missed_entry)"
            rows.append(rec); continue
        senders = [topic_addr(lg["topics"][1]).lower() for lg in slogs]
        recipients = [topic_addr(lg["topics"][2]).lower() for lg in slogs]
        # 0x6ff5 tiered logic (sprint097): at fee=100, 0x6ff5 PUMPS (94% WR) -> signal, not exclusion.
        share_6ff5 = sum(1 for s in senders if s.startswith("0x6ff5")) / max(n, 1)
        n_non6ff5_buyers = len({r for r in recipients if not r.startswith("0x6ff5")})
        rcount = Counter(recipients)
        distinct_rec = len(rcount)
        top_rec_share = max(rcount.values()) / max(n, 1)
        rec.update({"share_0x6ff5": round(share_6ff5, 3), "distinct_recipients": distinct_rec,
                    "top_recipient_share": round(top_rec_share, 3),
                    "non_0x6ff5_buyers": n_non6ff5_buyers})
        if share_6ff5 > 0:
            # Rule A: 0x6ff5-controlled fee=100 pool. Require >=1 external (non-0x6ff5) buyer
            # so we are not the SOLE counterparty (user's possibility A vs pure-wash B).
            rec["rule"] = "A_0x6ff5_fee100"
            if n_non6ff5_buyers < 1:
                rec["verdict"] = "REJECT"; rec["reason"] = "0x6ff5_only_no_external_buyer"; rows.append(rec); continue
        else:
            # Rule B: non-0x6ff5 fee=100 pool. Require organic multi-buyer flow.
            rec["rule"] = "B_organic_fee100"
            if distinct_rec < 3 or top_rec_share > 0.5:
                rec["verdict"] = "REJECT"; rec["reason"] = f"not_organic(distinct={distinct_rec},top={top_rec_share:.2f})"; rows.append(rec); continue
        # sellable + full cost via QuoterV2 both sides
        size_wei = int(args.size_usd / args.eth_usd * 1e18)
        qin = quote_exact_input_single_struct(rpc, WETH_BASE, c["alt"], size_wei, c["fee"])
        if not qin.get("ok"):
            rec["verdict"] = "REJECT"; rec["reason"] = f"entry_quote_fail({qin.get('reason')})"; rows.append(rec); continue
        alt_out = int(qin["amount_out"])
        qout = quote_exact_input_single_struct(rpc, c["alt"], WETH_BASE, alt_out, c["fee"])
        if not qout.get("ok") or int(qout["amount_out"]) <= 0:
            rec["verdict"] = "REJECT"; rec["reason"] = f"NOT_SELLABLE/honeypot({qout.get('reason')})"; rows.append(rec); continue
        weth_back = int(qout["amount_out"])
        roundtrip = weth_back / size_wei - 1.0
        gas_pct = GAS_USD_ROUNDTRIP / args.size_usd
        rt_net = roundtrip - gas_pct
        rec.update({"roundtrip_quoter": round(roundtrip, 4), "gas_pct": round(gas_pct, 4),
                    "roundtrip_net_after_cost": round(rt_net, 4), "sellable": True})
        rec["verdict"] = "CLEAN"
        rec["reason"] = f"passes all gates ({rec['rule']}+fee100+onset+sellable)"
        rows.append(rec)

    # Single source of truth for schema + self-healing header (canary_ledger).
    # This permanently prevents the header/row drift that once made every CLEAN
    # row unreadable by the executor.
    append_rows(rows, LEDGER)

    clean = [r for r in rows if r["verdict"] == "CLEAN"]
    vc = Counter(r["verdict"] for r in rows)
    print(f"\n[渔] evaluated onset pools={len(rows)}  verdicts={dict(vc)}  rpc_calls={rpc.calls}")
    print(f"[渔] reject reasons: {dict(Counter(r['reason'].split('(')[0] for r in rows if r['verdict']=='REJECT'))}")
    if clean:
        print(f"\n[渔] *** {len(clean)} CLEAN live candidate(s) ***")
        for r in clean:
            print(f"  [{r['rule']}] pool={r['pool']} alt={r['alt']} fee={r['fee']} age={r['age_min']}min "
                  f"heat={r['early_swaps']} 0x6ff5={r['share_0x6ff5']} ext_buyers={r['non_0x6ff5_buyers']} "
                  f"recipients={r['distinct_recipients']} sellable_roundtrip_net={r['roundtrip_net_after_cost']}")
    else:
        print("\n[渔] NO CLEAN candidate this scan. Per rule: NO trade. Standards NOT lowered.")
    print(f"[渔] ledger -> {LEDGER}")


if __name__ == "__main__":
    main()
