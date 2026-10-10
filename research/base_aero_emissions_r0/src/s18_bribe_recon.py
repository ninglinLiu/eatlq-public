#!/usr/bin/env python3
"""Q3 cheapest kill: how big is the Aerodrome bribe market vs the cost of locking veAERO?

Bribes are paid by protocols to veAERO voters, not to LPs. Completely different
counterparty from C-family emissions. We only need the order of magnitude:

    weekly_bribe_usd / ve_locked_usd   vs   AERO funding cost

If the ratio is tiny, the whole book is dead before any lock-duration backtest.
Bounded: 157 gaugeToBribe calls + NotifyReward logs for the top-20 gauges over 8 weeks.
"""
from __future__ import annotations

import sys
from collections import defaultdict

sys.path.insert(0, "/root/base_aero_emissions_r0/src")
import common as C
import rpc as R

SEL_BRIBE = "0x929c8dcd"  # gaugeToBribe(address)
# NotifyReward(address indexed from, address indexed reward, uint256 indexed epoch, uint256 amount)
TOPIC_NOTIFY = "0x" + __import__("eth_hash.auto", fromlist=["keccak"]).keccak(
    b"NotifyReward(address,address,uint256,uint256)"
).hex()

USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
WETH = C.WETH.lower()
AERO = C.AERO.lower()
STABLES = {USDC, "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca"}  # USDbC

WEEK = 604800
N_WEEKS = 4
TOP_N = 15


def enc_addr(a):
    return a.lower().replace("0x", "").rjust(64, "0")


def dec_addr(word):
    return "0x" + word[-40:].lower()


def main():
    print(f"NotifyReward topic0 = {TOPIC_NOTIFY}")
    gauges = C.ch_rows("""
        SELECT e.pool AS pool, e.gauge AS gauge, u.symbol AS symbol,
               sum(e.emission_usd) AS em_usd
        FROM census.aero_emission_apr e
        LEFT JOIN edge.bars_universe_50 u ON u.pool = e.pool
        WHERE e.epoch_start >= now() - INTERVAL 56 DAY
        GROUP BY pool, gauge, symbol
        ORDER BY em_usd DESC
    """)
    print(f"gauges with emissions in last 8w: {len(gauges)}")

    reqs = [("eth_call", [{"to": C.VOTER, "data": SEL_BRIBE + enc_addr(g["gauge"])}, "latest"])
            for g in gauges]
    res = R.rpc_batch(reqs)
    for g, (out, err) in zip(gauges, res):
        g["bribe"] = None if err or not out or out == "0x" else dec_addr(out)
    mapped = [g for g in gauges if g["bribe"] and int(g["bribe"], 16) != 0]
    print(f"gaugeToBribe resolved: {len(mapped)}/{len(gauges)}")
    C.save_json("bribe_map.json", [{"pool": g["pool"], "gauge": g["gauge"],
                                    "symbol": g["symbol"], "em_usd": float(g["em_usd"] or 0),
                                    "bribe": g["bribe"]} for g in mapped])

    top = mapped[:TOP_N]
    print("top gauges:")
    for g in top:
        print(f"  {str(g['symbol'])[:10]:10s} em=${float(g['em_usd'] or 0):10,.0f}  {g['bribe']}")

    panel = C.load_json("panel.json")
    head = panel["head"]
    # 8 weeks of 2s blocks
    span = int(N_WEEKS * WEEK / 2)
    lo = max(1, head - span)
    print(f"scanning NotifyReward logs blocks {lo}..{head} on {len(top)} bribe contracts")

    logs_all = []
    failed_all = []
    for i, g in enumerate(top):
        logs, failed = R.scan_logs(g["bribe"], [TOPIC_NOTIFY], lo, head,
                                   workers=8, progress=0)
        logs_all.extend((g, lg) for lg in logs)
        failed_all.extend(failed)
        print(f"  {i+1:2d}/{len(top)} {str(g['symbol'])[:10]:10s} logs={len(logs)} failed={len(failed)}")
    print(f"total NotifyReward logs: {len(logs_all)}  failed chunks: {len(failed_all)}")
    print(f"rpc stats: {R.STATS}")

    # decode: topics[2]=reward token, topics[3]=epoch, data=amount
    rows = []
    by_token = defaultdict(float)  # raw; priced below for known tokens
    for g, lg in logs_all:
        topics = lg.get("topics") or []
        if len(topics) < 4:
            continue
        token = "0x" + topics[2][-40:].lower()
        epoch = int(topics[3], 16)
        amt = int(lg["data"], 16) if lg.get("data") and lg["data"] != "0x" else 0
        rows.append({"symbol": g["symbol"], "bribe": g["bribe"], "token": token,
                     "epoch": epoch, "amount_raw": amt, "block": int(lg["blockNumber"], 16)})
        by_token[token] += amt

    # price AERO and WETH from existing tables; USDC = 1e-6 USD per raw unit
    aero_px = C.ch_rows("SELECT avg(aero_usd) AS p FROM census.aero_price_hourly "
                        "WHERE bar >= now() - INTERVAL 56 DAY")
    eth_px = C.ch_rows("SELECT avg(eth_usd) AS p FROM edge.eth_usd_1h "
                       "WHERE bar >= now() - INTERVAL 56 DAY")
    aero_p = float(aero_px[0]["p"]) if aero_px and aero_px[0]["p"] else None
    eth_p = float(eth_px[0]["p"]) if eth_px and eth_px[0]["p"] else None
    print(f"avg AERO usd={aero_p}  ETH usd={eth_p}")

    def usd(token, raw):
        if token in STABLES:
            return raw / 1e6
        if token == WETH and eth_p:
            return raw / 1e18 * eth_p
        if token == AERO and aero_p:
            return raw / 1e18 * aero_p
        return None

    priced = 0.0
    unpriced_events = 0
    by_sym = defaultdict(lambda: {"usd": 0.0, "unpriced": 0, "n": 0})
    token_usd = defaultdict(float)
    for r in rows:
        v = usd(r["token"], r["amount_raw"])
        by_sym[r["symbol"]]["n"] += 1
        if v is None:
            unpriced_events += 1
            by_sym[r["symbol"]]["unpriced"] += 1
        else:
            priced += v
            by_sym[r["symbol"]]["usd"] += v
            token_usd[r["token"]] += v

    weeks = N_WEEKS
    print("\n" + "=" * 88)
    print(f"BRI BE MARKET, top-{TOP_N} gauges, {weeks} weeks")
    print("=" * 88)
    print(f"NotifyReward events: {len(rows)}")
    print(f"priced USD (USDC/WETH/AERO only): ${priced:,.0f}  "
          f"(${priced/weeks:,.0f}/week)")
    print(f"unpriced events (other tokens): {unpriced_events}/{len(rows)}")
    print("by token (priced):")
    for t, v in sorted(token_usd.items(), key=lambda x: -x[1])[:10]:
        tag = {USDC: "USDC", WETH: "WETH", AERO: "AERO"}.get(t, t[:10])
        print(f"  {tag:8s} ${v:12,.0f}")
    print("by pool symbol:")
    for s, d in sorted(by_sym.items(), key=lambda x: -x[1]["usd"])[:15]:
        print(f"  {str(s)[:10]:10s} ${d['usd']:10,.0f}  events={d['n']:4d}  "
              f"unpriced={d['unpriced']}")

    # ve supply: VotingEscrow.totalSupply() is veAERO units, 1 ve ≈ 1 AERO if max-locked
    ts_hex = R.rpc("eth_call", [{"to": C.VOTING_ESCROW, "data": C.SEL["totalSupply()"]}, "latest"])
    ve_raw = R.dec_uint(ts_hex)
    ve = ve_raw / 1e18 if ve_raw else None
    ve_usd = ve * aero_p if (ve and aero_p) else None
    print(f"\nveAERO totalSupply ≈ {ve:,.0f}  (${ve_usd:,.0f} at avg AERO)" if ve_usd else "\nve supply missing")
    if ve_usd and ve_usd > 0:
        # these top-20 are not the whole market; scale crudely by emission share
        top_em = sum(float(g["em_usd"] or 0) for g in top)
        all_em = sum(float(g["em_usd"] or 0) for g in mapped)
        share = top_em / all_em if all_em else 1
        # priced bribes are a LOWER bound (other tokens unpriced)
        weekly_lb = priced / weeks
        implied_apr_lb = 52 * weekly_lb / ve_usd
        # if top-20 are `share` of emissions, bribes may concentrate even more; do not scale up
        print(f"top-20 emission share of mapped gauges: {share:.1%}")
        print(f"bribe APR lower bound if ALL ve sat on these top-20: {implied_apr_lb*100:.2f}%")
        print(f"  (this OVERSTATES voter APR — ve is spread across all gauges.")
        print(f"   useful as: if even this is << AERO funding cost, the book is dead)")
        print(f"naive protocol-wide scale (lb / share): {implied_apr_lb/share*100:.2f}% APR")

    C.save_json("bribe_recon.json", {
        "n_events": len(rows), "priced_usd": priced, "unpriced_events": unpriced_events,
        "weeks": weeks, "ve": ve, "ve_usd": ve_usd, "aero_p": aero_p,
        "rpc": dict(R.STATS),
    })
    print(f"\nrpc stats: {R.STATS}")


if __name__ == "__main__":
    main()
