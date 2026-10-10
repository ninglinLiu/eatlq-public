#!/usr/bin/env python3
"""Stage 4b — REALIZED, real-cost EV of the stage-4a signal, reconstructed from
on-chain bonding-curve prices (pump TradeEvent virtual reserves).

Strategy simulated (faithful to a 30-300s poller):
  - universe: coins passing the OOS-validated early signal (n_trades_180s>=40 AND
    alive_at_180s) — i.e. tradeable decision made by T+180s.
  - ENTRY at created+ENTRY_S using the curve price of the first successful trade
    at/after entry (or the frozen curve price if none) .
  - EXIT at entry+HOLD_S using the curve price of the last successful trade <= exit
    (bonding curve always lets you sell back; a dead coin's curve is frozen, not
    auto -100%, but other sellers pushing price down ARE captured).
  - cost: pump 1%/side + slippage/priority buffer => --cost round-trip (default 3%).

Price = virtualSolReserves/virtualTokenReserves decoded from the pump TradeEvent
log (disc8|mint32|solAmt u64|tokAmt u64|isBuy1|user32|ts i64|vSol u64|vTok u64).
Returns are price ratios so decimal units cancel.

Gates (anti self-deception): EV>0, PF, PF_top1_removed>=1.3, bootstrap p5 of mean
return >0, OOS by day, unique coins, top-coin pnl share.
"""
from __future__ import annotations
import argparse
import base64
import json
import random
import struct
from collections import defaultdict
from datetime import datetime, timezone
import importlib.util

_spec = importlib.util.spec_from_file_location("samp", "/opt/EATLQ/scripts/research/sol/sol_sample_creates.py")
_s = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_s)
RPC = _s.RPC
rpc_url = _s.rpc_url

EP = "/opt/EATLQ/mvp/diagnostics/sol/creates_earlypath.jsonl"
OUT = "/opt/EATLQ/mvp/diagnostics/sol/realized_ev.jsonl"
MAX_PAGES = 6


def day_of(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%m-%d")


def collect_trades(rpc, bc, until_t):
    """page newest-first until oldest collected <= until_t (or pages exhausted).
    returns sorted [(t, sig, err)] and reached flag."""
    rows = []
    before = None
    reached = False
    for _ in range(MAX_PAGES):
        params = [bc, {"limit": 1000, **({"before": before} if before else {})}]
        sigs = rpc("getSignaturesForAddress", params) or []
        if not sigs:
            reached = True
            break
        before = sigs[-1]["signature"]
        for s in sigs:
            if s.get("blockTime") is not None:
                rows.append((s["blockTime"], s["signature"], bool(s.get("err"))))
        if (sigs[-1].get("blockTime") or 1e18) <= until_t:
            reached = True
            break
        if len(sigs) < 1000:
            reached = True
            break
    rows.sort()
    return rows, reached


def price_at(rpc, sig, mint_b):
    """Decode the pump TradeEvent for THIS mint only. A tx can carry several
    'Program data:' logs (multi-hop routers, other tokens); we require the event's
    embedded mint (raw[8:40]) to equal mint_b and the reserves to be in plausible
    pump ranges, otherwise the price is garbage."""
    tx = rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                      "commitment": "confirmed"}])
    if not tx or tx["meta"].get("err"):
        return None
    NEED = 8 + 32 + 8 + 8 + 1 + 32 + 8 + 8 + 8
    for L in tx["meta"].get("logMessages", []):
        if not L.startswith("Program data:"):
            continue
        try:
            raw = base64.b64decode(L.split("Program data: ")[1])
        except Exception:
            continue
        if len(raw) < NEED or raw[8:40] != mint_b:
            continue
        try:
            off = 8 + 32 + 8 + 8 + 1 + 32 + 8  # disc, mint, solAmt, tokAmt, isBuy, user, ts
            vSol = struct.unpack_from("<Q", raw, off)[0] / 1e9
            vTok = struct.unpack_from("<Q", raw, off + 8)[0] / 1e6
            # plausible pump curve: vSol in [1, 5000] SOL, vTok in [1e5, 2e9] tokens
            if 1.0 <= vSol <= 5000.0 and 1e5 <= vTok <= 2e9:
                return vSol / vTok
        except Exception:
            continue
    return None


def pick_price(rpc, trades, target_t, prefer_after, mint_b):
    """find a successful trade near target_t and return its curve price."""
    succ = [(t, sig) for (t, sig, err) in trades if not err]
    if not succ:
        return None
    if prefer_after:
        cand = [x for x in succ if x[0] >= target_t][:8]
        if not cand:
            cand = [x for x in succ if x[0] <= target_t][-8:][::-1]
    else:
        cand = [x for x in succ if x[0] <= target_t][-8:][::-1]
        if not cand:
            cand = [x for x in succ if x[0] >= target_t][:8]
    for t, sig in cand:
        if abs(t - target_t) > 6 * 3600:
            continue
        p = price_at(rpc, sig, mint_b)
        if p:
            return p
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--entry-s", type=float, default=180)
    ap.add_argument("--hold-s", type=float, default=7200)
    ap.add_argument("--cost", type=float, default=0.03, help="round-trip cost fraction")
    ap.add_argument("--n180", type=int, default=40)
    ap.add_argument("--control", action="store_true", help="also sim a random non-signal control set")
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(EP)]

    def sig_pass(r):
        return (r.get("n_trades_180s") or 0) >= args.n180 and r.get("alive_at_180s")
    universe = [r for r in rows if sig_pass(r)]
    print(f"[ev] signal universe: {len(universe)} coins  (entry+{args.entry_s}s hold {args.hold_s/3600:.1f}h cost {100*args.cost:.1f}%)")

    rpc = RPC(rpc_url(), min_interval=0.18)
    results = []
    with open(OUT, "w") as f:
        for i, r in enumerate(universe):
            created = r["created_ms"] / 1000.0
            entry_t = created + args.entry_s
            exit_t = entry_t + args.hold_s
            mint_b = _s.b58dec(r["mint"])
            trades, reached = collect_trades(rpc, r["bonding_curve"], created - 5)
            pe = pick_price(rpc, trades, entry_t, True, mint_b)
            px = pick_price(rpc, trades, exit_t, False, mint_b)
            ret = None
            if pe and px:
                ret = (px / pe) * (1 - args.cost) - 1
            row = {"mint": r["mint"], "day": day_of(r["created_ms"]), "ath_mc": r.get("ath_mc"),
                   "time_to_ath_s": r.get("time_to_ath_s"), "n180": r.get("n_trades_180s"),
                   "entry_price": pe, "exit_price": px, "ret": ret, "reached": reached}
            f.write(json.dumps(row) + "\n")
            if ret is not None:
                results.append(row)
            if (i + 1) % 50 == 0:
                f.flush()
                print(f"  [ev] {i+1}/{len(universe)} priced={len(results)} rpc={rpc.calls}", flush=True)

    report(results, args)


def report(results, args):
    rets = [r["ret"] for r in results]
    n = len(rets)
    if n == 0:
        print("no priced trades"); return
    import statistics as st
    wins = [x for x in rets if x > 0]
    gains = sum(x for x in rets if x > 0)
    losses = -sum(x for x in rets if x < 0)
    pf = gains / losses if losses else float("inf")
    # PF top1 removed (remove single best trade)
    srt = sorted(rets, reverse=True)
    g2 = sum(x for x in srt[1:] if x > 0)
    pf_top1 = g2 / losses if losses else float("inf")
    # bootstrap p5 of mean
    rng = random.Random(1)
    means = sorted((sum(rng.choice(rets) for _ in range(n)) / n) for _ in range(3000))
    p5 = means[int(0.05 * len(means))]
    print(f"\n=== Stage 4b REALIZED EV (priced n={n}) ===")
    print(f"  mean ret   : {100*sum(rets)/n:+.1f}%   median {100*st.median(rets):+.1f}%")
    print(f"  win rate   : {100*len(wins)/n:.1f}%")
    print(f"  PF         : {pf:.2f}    PF_top1_removed: {pf_top1:.2f}")
    print(f"  bootstrap mean p5: {100*p5:+.1f}%")
    print(f"  best trade : {100*max(rets):+.0f}%   worst {100*min(rets):+.0f}%")
    # top coin pnl share
    tot_abs = sum(abs(x) for x in rets)
    print(f"  top-trade |pnl| share: {100*max(abs(x) for x in rets)/tot_abs:.0f}%")
    # OOS by day
    byday = defaultdict(list)
    for r in results:
        byday[r["day"]].append(r["ret"])
    print("  by day (n, mean%):")
    for d in sorted(byday):
        v = byday[d]
        print(f"    {d}: n={len(v):3d} mean={100*sum(v)/len(v):+.1f}% win={100*sum(1 for x in v if x>0)/len(v):.0f}%")
    gate = (sum(rets)/n > 0 and pf_top1 >= 1.3 and p5 > 0 and n >= 30)
    print(f"  >>> GATE: {'PASS' if gate else 'FAIL'} (EV>0 & PF_top1>=1.3 & boot_p5>0 & n>=30)")


if __name__ == "__main__":
    main()
