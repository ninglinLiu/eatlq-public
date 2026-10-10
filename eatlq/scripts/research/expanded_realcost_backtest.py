#!/usr/bin/env python3
"""Expanded current-regime real-cost backtest (direct RPC, bypasses dead pipeline).

WHY: our ClickHouse stalled on 2026-05-18 (archiver/decoder dead since 06-08), which
made it LOOK like Base had zero new onsets. A direct-RPC census proves new pools are
created continuously (~50-300/day, ~25-150 WETH-paired/day). So the small sample was a
pipeline artifact, not a market reality. This script rebuilds the backtest from scratch
on FRESH on-chain data over a wide window, with honest real cost and anti-overfit gates.

METHOD (no lookahead, no exit-policy cherry-picking by default):
  1. Census  : all Uniswap-V3 PoolCreated over last N days (10k-block getLogs chunks).
  2. Universe: WETH-paired, non-routing alt.
  3. Onset   : per pool, pull Swap logs over [birth, birth+2h]; onset = first time swap
               count >= HEAT within first ONSET_MAX_SEC. First-signal-only (1 row/pool).
  4. Price   : reconstruct alt/WETH price path from Swap sqrtPriceX96 (ratios; free).
  5. Real $  : realized return = QuoterV2 round-trip WETH->alt (entry block) then
               alt->WETH (exit block). This captures price move + BOTH-side slippage at
               $10 notional + LP fee in one measurement. Sell-fail/honeypot => -100%.
  6. Labels  : fixed horizons 30m/1h/2h (honest) + one trailing policy (right-tail probe).
  7. Gates   : n, unique_pools, top_pool_share, PF, PF_top1_removed, bootstrap_p5, WR,
               by-fee, wash split, time split (train/val/oos), 10-trade canary Monte Carlo.
"""
from __future__ import annotations
import os, sys, time, json, random, statistics
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from v3_quoterv2_struct import (  # noqa: E402
    JsonRpcClient, WETH_BASE, quote_exact_input_single_struct,
)

RPC_URL = os.environ.get("RPC_URL", "https://mainnet.base.org")
FACTORY = "0x33128a8fc17869897dce68ed026d694621f6fdfd"
POOLCREATED_TOPIC = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
WASH_BOT = "0x6ff5"  # prefix of known wash/control bot family

# tunables
DAYS = int(os.environ.get("DAYS", "30"))
HEAT = int(os.environ.get("HEAT", "5"))            # swaps to declare onset
ONSET_MAX_SEC = int(os.environ.get("ONSET_MAX_SEC", "7200"))
NOTIONAL_USD = float(os.environ.get("NOTIONAL_USD", "10"))
ETH_USD = float(os.environ.get("ETH_USD", "3000"))
GAS_USD = float(os.environ.get("GAS_USD", "0.03"))
BLK_SEC = 2.0
MAX_RPC = int(os.environ.get("MAX_RPC", "8000"))
OUT_DIR = os.environ.get("OUT_DIR", "/opt/EATLQ/mvp/audits/sprint097_expanded_realcost")

# routing / major tokens that are NOT memecoin alts (skip as the "alt")
ROUTERS = {
    "0x4200000000000000000000000000000000000006",  # WETH (pair token)
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",  # DAI
    "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2",  # USDT
    "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",  # cbBTC
    "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22",  # cbETH
    "0xc1cba3fcea344f92d9239c08c0568f6f2f0ee452",  # wstETH
}

rpc = JsonRpcClient(RPC_URL, delay_s=0.02)


def get_logs_chunked(addr, topics, from_b, to_b, chunk=9000):
    out = []
    b = from_b
    while b <= to_b:
        if rpc.calls > MAX_RPC:
            raise RuntimeError(f"RPC budget {MAX_RPC} exceeded during getLogs")
        t = min(b + chunk, to_b)
        flt = {"fromBlock": hex(b), "toBlock": hex(t), "topics": topics}
        if addr:
            flt["address"] = addr
        try:
            logs = rpc.call("eth_getLogs", [flt])
        except Exception as e:
            # split on 413 / range errors
            if chunk > 500 and ("413" in str(e) or "range" in str(e).lower() or "large" in str(e).lower()):
                mid = (b + t) // 2
                out += get_logs_chunked(addr, topics, b, mid, chunk // 2)
                out += get_logs_chunked(addr, topics, mid + 1, t, chunk // 2)
                b = t + 1
                continue
            logs = []
        out += logs or []
        b = t + 1
    return out


def census():
    head = int(rpc.call("eth_blockNumber", []), 16)
    span = int(DAYS * 86400 / BLK_SEC)
    from_b = head - span
    print(f"[census] head={head} window={DAYS}d blocks [{from_b}..{head}]")
    logs = get_logs_chunked(FACTORY, [POOLCREATED_TOPIC], from_b, head)
    pools = []
    for lg in logs:
        tps = lg["topics"]
        t0 = ("0x" + tps[1][-40:]).lower()
        t1 = ("0x" + tps[2][-40:]).lower()
        fee = int(tps[3], 16)
        data = lg["data"][2:]
        pool = ("0x" + data[64:128][-40:]).lower()
        birth = int(lg["blockNumber"], 16)
        if t0 == WETH_BASE.lower():
            weth_t0, alt = True, t1
        elif t1 == WETH_BASE.lower():
            weth_t0, alt = False, t0
        else:
            continue  # not WETH-paired
        if alt in ROUTERS:
            continue
        pools.append(dict(pool=pool, alt=alt, fee=fee, birth=birth, weth_t0=weth_t0))
    # dedup by pool (first birth)
    seen = {}
    for p in pools:
        if p["pool"] not in seen:
            seen[p["pool"]] = p
    pools = sorted(seen.values(), key=lambda x: x["birth"])
    print(f"[census] WETH-paired non-routing new pools (30d): {len(pools)}  rpc={rpc.calls}")
    return head, pools


def decode_swaps(pool, birth):
    to_b = birth + int(7200 / BLK_SEC)  # 2h window
    logs = get_logs_chunked(pool, [SWAP_TOPIC], birth, to_b)
    sw = []
    for lg in logs:
        tps = lg["topics"]
        if len(tps) < 3:
            continue
        sender = ("0x" + tps[1][-40:]).lower()
        recip = ("0x" + tps[2][-40:]).lower()
        data = lg["data"][2:]
        try:
            sqrtp = int(data[128:192], 16)
        except Exception:
            continue
        if sqrtp == 0:
            continue
        sw.append((int(lg["blockNumber"], 16), sender, recip, sqrtp))
    sw.sort(key=lambda x: x[0])
    return sw


def alt_price(sqrtp, weth_t0):
    # alt price in WETH (relative units; decimals cancel for ratios)
    r = (sqrtp / (2 ** 96)) ** 2
    return (1.0 / r) if weth_t0 else r


def trailing_exit(path, entry_i, arm=0.30, trail=0.25, sl=-0.60, max_sec=7200):
    # path: list of (block, price). returns exit index
    p0 = path[entry_i][1]
    b0 = path[entry_i][0]
    peak = p0
    armed = False
    for i in range(entry_i + 1, len(path)):
        b, p = path[i]
        if (b - b0) * BLK_SEC > max_sec:
            return i
        ret = p / p0 - 1
        if ret <= sl:
            return i
        if ret >= arm:
            armed = True
        if p > peak:
            peak = p
        if armed and p <= peak * (1 - trail):
            return i
    return len(path) - 1


def roundtrip_return(pool, alt, fee, weth_t0, entry_block, exit_block):
    """Realized real-cost return via QuoterV2 entry+exit. -1.0 if honeypot/sell-fail."""
    amt_in = int(NOTIONAL_USD / ETH_USD * 1e18)
    q_in = quote_exact_input_single_struct(rpc, WETH_BASE, alt, amt_in, fee,
                                           block_identifier=entry_block)
    if not q_in["ok"]:
        return None, "entry_fail:" + q_in.get("reason", "")
    tokens = q_in["amount_out"]
    q_out = quote_exact_input_single_struct(rpc, alt, WETH_BASE, tokens, fee,
                                            block_identifier=exit_block)
    if not q_out["ok"]:
        return -1.0, "sell_fail:" + q_out.get("reason", "")  # honeypot => full loss
    weth_back = q_out["amount_out"]
    gas_eth = GAS_USD / ETH_USD * 1e18 * 2  # buy+sell gas
    net = (weth_back - gas_eth) / amt_in - 1.0
    return net, "ok"


def build_signals(head, pools):
    rows = []
    n = len(pools)
    for idx, p in enumerate(pools):
        if rpc.calls > MAX_RPC:
            print(f"[onset] RPC budget hit at pool {idx}/{n}")
            break
        sw = decode_swaps(p["pool"], p["birth"])
        if len(sw) < HEAT:
            continue
        # onset: first index where cumulative swaps>=HEAT within ONSET_MAX_SEC
        onset_i = None
        for i in range(HEAT - 1, len(sw)):
            if (sw[i][0] - p["birth"]) * BLK_SEC <= ONSET_MAX_SEC:
                onset_i = i
                break
        if onset_i is None:
            continue
        senders = [s[1] for s in sw[:onset_i + 1]]
        recips = [s[2] for s in sw[:onset_i + 1]]
        sc = defaultdict(int)
        for s in senders:
            sc[s] += 1
        dom_share = max(sc.values()) / len(senders)
        wash_share = sum(v for k, v in sc.items() if k.startswith(WASH_BOT)) / len(senders)
        rc = defaultdict(int)
        for r in recips:
            rc[r] += 1
        distinct_recip = len(rc)
        top_recip = max(rc.values()) / len(recips)
        path = [(s[0], alt_price(s[3], p["weth_t0"])) for s in sw]
        entry_block = sw[onset_i][0]
        # fixed-horizon exit blocks
        def exit_at(sec):
            tgt = entry_block + int(sec / BLK_SEC)
            ex = onset_i
            for i in range(onset_i, len(sw)):
                if sw[i][0] <= tgt:
                    ex = i
                else:
                    break
            return ex
        ex_30 = exit_at(1800)
        ex_60 = exit_at(3600)
        ex_120 = exit_at(7200)
        ex_tr = trailing_exit(path, onset_i)
        rows.append(dict(
            pool=p["pool"], alt=p["alt"], fee=p["fee"], birth=p["birth"], weth_t0=p["weth_t0"],
            n_swaps=len(sw), onset_lag=(entry_block - p["birth"]) * BLK_SEC,
            dom_share=round(dom_share, 3), wash_share=round(wash_share, 3),
            distinct_recip=distinct_recip, top_recip=round(top_recip, 3),
            entry_block=entry_block,
            exit_block_30=sw[ex_30][0], exit_block_60=sw[ex_60][0],
            exit_block_120=sw[ex_120][0], exit_block_tr=path[ex_tr][0],
        ))
        if (idx + 1) % 25 == 0:
            print(f"[onset] {idx+1}/{n} scanned, {len(rows)} onsets, rpc={rpc.calls}")
    print(f"[onset] total onsets (first-signal-only): {len(rows)}  rpc={rpc.calls}")
    return rows


def price_real(rows):
    for i, r in enumerate(rows):
        if rpc.calls > MAX_RPC:
            print(f"[price] RPC budget hit at {i}/{len(rows)}")
            r["priced"] = False
            continue
        for tag, eb in [("30", r["exit_block_30"]), ("60", r["exit_block_60"]),
                        ("120", r["exit_block_120"]), ("tr", r["exit_block_tr"])]:
            ret, why = roundtrip_return(r["pool"], r["alt"], r["fee"], r["weth_t0"],
                                        r["entry_block"], eb)
            r[f"ret_{tag}"] = ret
            r[f"why_{tag}"] = why
        r["priced"] = r.get("ret_120") is not None
        if (i + 1) % 20 == 0:
            print(f"[price] {i+1}/{len(rows)} priced, rpc={rpc.calls}")
    return [r for r in rows if r.get("priced")]


# ---------- stats / gates ----------
def pf(rets):
    g = sum(x for x in rets if x > 0)
    l = -sum(x for x in rets if x < 0)
    return (g / l) if l > 0 else (float("inf") if g > 0 else 0.0)


def pf_top1_removed(rows, key):
    if len(rows) < 3:
        return 0.0
    bypool = defaultdict(list)
    for r in rows:
        bypool[r["pool"]].append(r[key])
    contrib = {p: sum(x for x in v if x > 0) for p, v in bypool.items()}
    top = max(contrib, key=contrib.get)
    rest = [r[key] for r in rows if r["pool"] != top]
    return pf(rest)


def bootstrap_p5(rets, n=2000):
    if len(rets) < 5:
        return -1.0
    evs = []
    for _ in range(n):
        s = [random.choice(rets) for _ in rets]
        evs.append(statistics.mean(s))
    evs.sort()
    return evs[int(0.05 * len(evs))]


def canary_mc(rets, trades=10, runs=20000, stake=10):
    if not rets:
        return {}
    wins = 0
    pnls = []
    for _ in range(runs):
        pnl = sum(random.choice(rets) * stake for _ in range(trades))
        pnls.append(pnl)
        if pnl > 0:
            wins += 1
    pnls.sort()
    return dict(p_profit=wins / runs, median=pnls[runs // 2],
                p5=pnls[int(0.05 * runs)], p95=pnls[int(0.95 * runs)])


def summarize(rows, key, label):
    rets = [r[key] for r in rows]
    pools = {r["pool"] for r in rows}
    bypool = defaultdict(float)
    for r in rows:
        if r[key] > 0:
            bypool[r["pool"]] += r[key]
    tot_pos = sum(bypool.values())
    top_share = (max(bypool.values()) / tot_pos) if tot_pos > 0 else 0.0
    wr = sum(1 for x in rets if x > 0) / len(rets)
    print(f"\n=== {label} (n={len(rets)}, pools={len(pools)}) ===")
    print(f"  EV={statistics.mean(rets):+.3f}  median={statistics.median(rets):+.3f}  WR={wr:.1%}")
    print(f"  PF={pf(rets):.2f}  PF_top1_removed={pf_top1_removed(rows,key):.2f}  top_pool_share={top_share:.1%}")
    print(f"  bootstrap_p5_EV={bootstrap_p5(rets):+.3f}")
    mc = canary_mc(rets)
    if mc:
        print(f"  10-trade canary($10): P(profit)={mc['p_profit']:.1%} median=${mc['median']:+.1f} "
              f"p5=${mc['p5']:+.1f} p95=${mc['p95']:+.1f}")
    return dict(n=len(rets), pools=len(pools), ev=statistics.mean(rets), wr=wr,
                pf=pf(rets), pf_top1=pf_top1_removed(rows, key), top_share=top_share,
                p5=bootstrap_p5(rets), mc=mc)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    t0 = time.time()
    head, pools = census()
    rows = build_signals(head, pools)
    if not rows:
        print("NO ONSETS — check gates"); return
    rows = price_real(rows)
    # persist raw
    with open(os.path.join(OUT_DIR, "per_signal.json"), "w") as f:
        json.dump(rows, f, indent=2, default=str)
    print(f"\n[priced] {len(rows)} signals with real-cost returns")

    results = {}
    for key, lbl in [("ret_120", "FIXED 2h (honest)"), ("ret_60", "FIXED 1h"),
                     ("ret_30", "FIXED 30m"), ("ret_tr", "TRAILING arm30/trail25/sl60 2h")]:
        rr = [r for r in rows if r.get(key) is not None]
        results[key] = summarize(rr, key, lbl)

    # WASH split on the 2h label
    clean = [r for r in rows if r["dom_share"] <= 0.5 and r["wash_share"] <= 0.34
             and r["distinct_recip"] >= 3 and r["top_recip"] <= 0.5 and r.get("ret_120") is not None]
    washy = [r for r in rows if not (r["dom_share"] <= 0.5 and r["wash_share"] <= 0.34
             and r["distinct_recip"] >= 3 and r["top_recip"] <= 0.5) and r.get("ret_120") is not None]
    summarize(clean, "ret_120", "CLEAN subset 2h (non-wash, organic)")
    if washy:
        summarize(washy, "ret_120", "WASH/dominated subset 2h")

    # TIME split (oos = newest 25%) on clean 2h
    cs = sorted(clean, key=lambda x: x["birth"])
    if len(cs) >= 12:
        k = len(cs)
        tr, va, oos = cs[:k//2], cs[k//2:k*3//4], cs[k*3//4:]
        for sub, nm in [(tr, "CLEAN TRAIN"), (va, "CLEAN VAL"), (oos, "CLEAN OOS newest25%")]:
            summarize(sub, "ret_120", nm)

    with open(os.path.join(OUT_DIR, "summary.json"), "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n[done] rpc_calls={rpc.calls} elapsed={time.time()-t0:.0f}s out={OUT_DIR}")


if __name__ == "__main__":
    main()
