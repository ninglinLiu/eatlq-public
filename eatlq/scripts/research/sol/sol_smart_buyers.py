#!/usr/bin/env python3
"""Direction B collector — extract EARLY BUYER wallets (smart-money candidates) and
a price grid for each coin, from on-chain pump TradeEvent logs.

TradeEvent layout: disc8 | mint32 | solAmt u64 | tokAmt u64 | isBuy u8 | user32 |
ts i64 | vSol u64 | vTok u64.  buyer = user (offset 57). price = vSol/vTok.

For each coin we record:
  - early_buys: first K successful trades -> [{t_rel, buyer, is_buy, sol, price}]
  - grid:       curve price at created+{60,180,600,1800,3600,7200}s (entry/exit)

Universe: all winners (ath>=--win-thr) + a random control of others, so we have
enough winner early-buyers to discover recurring smart wallets, while keeping the
control to measure how often the same wallets appear in non-winners. Train/test
split is done later by creation time. Output: smart_buyers.jsonl
"""
from __future__ import annotations
import argparse
import base64
import json
import random
import struct
import importlib.util
from datetime import datetime, timezone

_spec = importlib.util.spec_from_file_location("samp", "/opt/EATLQ/scripts/research/sol/sol_sample_creates.py")
_s = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_s)
_rev = importlib.util.spec_from_file_location("ev", "/opt/EATLQ/scripts/research/sol/sol_realized_ev.py")
_e = importlib.util.module_from_spec(_rev)
_rev.loader.exec_module(_e)

EP = "/opt/EATLQ/mvp/diagnostics/sol/creates_earlypath.jsonl"
OUT = "/opt/EATLQ/mvp/diagnostics/sol/smart_buyers.jsonl"
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58enc(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n > 0:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = 0
    for c in b:
        if c == 0:
            pad += 1
        else:
            break
    return "1" * pad + out


def decode_event(raw, mint_b):
    NEED = 8 + 32 + 8 + 8 + 1 + 32 + 8 + 8 + 8
    if len(raw) < NEED or raw[8:40] != mint_b:
        return None
    sol = struct.unpack_from("<Q", raw, 40)[0] / 1e9
    is_buy = raw[56]
    buyer = b58enc(raw[57:89])
    off = 8 + 32 + 8 + 8 + 1 + 32 + 8
    vSol = struct.unpack_from("<Q", raw, off)[0] / 1e9
    vTok = struct.unpack_from("<Q", raw, off + 8)[0] / 1e6
    price = vSol / vTok if (1.0 <= vSol <= 5000.0 and 1e5 <= vTok <= 2e9) else None
    return {"sol": sol, "is_buy": int(is_buy), "buyer": buyer, "price": price}


def tx_event(rpc, sig, mint_b, cache):
    if sig in cache:
        return cache[sig]
    tx = rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                      "commitment": "confirmed"}])
    ev = None
    if tx and not tx["meta"].get("err"):
        for L in tx["meta"].get("logMessages", []):
            if not L.startswith("Program data:"):
                continue
            try:
                raw = base64.b64decode(L.split("Program data: ")[1])
            except Exception:
                continue
            ev = decode_event(raw, mint_b)
            if ev:
                break
    cache[sig] = ev
    return ev


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--win-thr", type=float, default=8000)
    ap.add_argument("--control", type=int, default=320)
    ap.add_argument("--k-early", type=int, default=12)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--days", type=str, default="", help="comma days e.g. 06-14,06-15 -> take ALL coins in these days")
    ap.add_argument("--skip-file", type=str, default="", help="jsonl whose mints to skip (already collected)")
    ap.add_argument("--out", type=str, default=OUT)
    ap.add_argument("--in", dest="infile", type=str, default=EP,
                    help="source jsonl; EP(earlypath) or the labeled file both work "
                         "(needs mint, created_ms, bonding_curve, ath_mc, time_to_ath_s)")
    ap.add_argument("--append", action="store_true")
    args = ap.parse_args()
    out_path = args.out

    rows = [json.loads(l) for l in open(args.infile) if l.strip()]
    rows = [r for r in rows if r.get("bonding_curve") and r.get("created_ms")]
    skip = set()
    if args.skip_file:
        try:
            skip = {json.loads(l)["mint"] for l in open(args.skip_file)}
        except FileNotFoundError:
            pass
    if args.days:
        days = set(args.days.split(","))
        def dayof(r):
            return datetime.fromtimestamp(r["created_ms"]/1000, tz=timezone.utc).strftime("%m-%d")
        universe = [r for r in rows if dayof(r) in days and r["mint"] not in skip]
        rng = random.Random(args.seed); rng.shuffle(universe)
        print(f"[B] day-mode {sorted(days)}: {len(universe)} coins (skipped {len(skip)} already-collected)")
    else:
        winners = [r for r in rows if (r.get("ath_mc") or 0) >= args.win_thr]
        others = [r for r in rows if (r.get("ath_mc") or 0) < args.win_thr]
        rng = random.Random(args.seed)
        rng.shuffle(others)
        universe = winners + others[:args.control]
        rng.shuffle(universe)
        print(f"[B] universe={len(universe)} (winners>={int(args.win_thr)}: {len(winners)}, control: {min(args.control,len(others))})")

    GRID = [60, 180, 600, 1800, 3600, 7200]
    rpc = _e.RPC(_e.rpc_url(), min_interval=0.18)
    n = 0
    with open(out_path, "a" if args.append else "w") as f:
        for i, r in enumerate(universe):
            mint_b = _s.b58dec(r["mint"])
            created = r["created_ms"] / 1000.0
            bc = r["bonding_curve"]
            trades, reached = _e.collect_trades(rpc, bc, created - 5)
            cache = {}
            succ = [(t, sig) for (t, sig, err) in trades if not err]
            # early buys (first K successful)
            early = []
            for t, sig in succ[:args.k_early]:
                ev = tx_event(rpc, sig, mint_b, cache)
                if ev:
                    early.append({"t_rel": round(t - created, 1), "buyer": ev["buyer"],
                                  "is_buy": ev["is_buy"], "sol": round(ev["sol"], 3),
                                  "price": ev["price"]})
            # price grid (reuse cache via pick_price's own getTransaction; cache by sig)
            grid = {}
            for g in GRID:
                p = _e.pick_price(rpc, trades, created + g, True, mint_b)
                grid[str(g)] = p
            row = {"mint": r["mint"], "created_ms": r["created_ms"],
                   "day": datetime.fromtimestamp(r["created_ms"]/1000, tz=timezone.utc).strftime("%m-%d"),
                   "ath_mc": r.get("ath_mc"), "time_to_ath_s": r.get("time_to_ath_s"),
                   "n180": r.get("n_trades_180s"), "graduated": r.get("graduated"),
                   "early_buys": early, "grid": grid}
            f.write(json.dumps(row) + "\n")
            n += 1
            if (i + 1) % 40 == 0:
                f.flush()
                print(f"  [B] {i+1}/{len(universe)} rpc={rpc.calls}", flush=True)
    print(f"[done] wrote {n} -> {out_path}  rpc_calls={rpc.calls}")


if __name__ == "__main__":
    main()
