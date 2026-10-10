#!/usr/bin/env python3
"""Capture a PRICE-PATH GRID (curve price at a set of relative times) for the
signal universe, in ONE RPC pass, so entry/exit timing can be swept OFFLINE
(no further RPC). Answers: is ANY realistic (entry, hold) config +EV, or is the
right tail simply uncapturable after costs?

price = pump TradeEvent virtualSol/virtualToken for THIS mint (validated).
Output: mvp/diagnostics/sol/price_paths.jsonl  {mint, day, ath_mc, t2ath, n180, path:{t:price}}
"""
from __future__ import annotations
import json
import importlib.util
from datetime import datetime, timezone

_spec = importlib.util.spec_from_file_location("ev", "/opt/EATLQ/scripts/research/sol/sol_realized_ev.py")
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)

EP = "/opt/EATLQ/mvp/diagnostics/sol/creates_earlypath.jsonl"
OUT = "/opt/EATLQ/mvp/diagnostics/sol/price_paths.jsonl"
GRID = [0, 30, 45, 60, 90, 120, 180, 240, 300, 420, 600, 900, 1800, 3600, 5400, 7200]


def main():
    rows = [json.loads(l) for l in open(EP)]
    uni = [r for r in rows if (r.get("n_trades_180s") or 0) >= 40 and r.get("alive_at_180s")]
    print(f"[path] universe {len(uni)} coins, grid {len(GRID)} points")
    rpc = ev.RPC(ev.rpc_url(), min_interval=0.18)
    done = set()
    import os
    mode = "w"
    if os.path.exists(OUT):
        for l in open(OUT):
            try: done.add(json.loads(l)["mint"])
            except Exception: pass
        mode = "a"
    with open(OUT, mode) as f:
        for i, r in enumerate(uni):
            if r["mint"] in done:
                continue
            created = r["created_ms"] / 1000.0
            mint_b = ev._s.b58dec(r["mint"])
            trades, reached = ev.collect_trades(rpc, r["bonding_curve"], created - 5)
            succ = [(t, sig) for (t, sig, e) in trades if not e]
            path = {}
            # cache decoded prices by sig to avoid duplicate getTransaction across grid
            sig_price = {}
            for gt in GRID:
                tgt = created + gt
                cand = [x for x in succ if x[0] >= tgt][:4] or [x for x in succ if x[0] <= tgt][-4:][::-1]
                price = None
                for t, sig in cand:
                    if abs(t - tgt) > 6 * 3600:
                        continue
                    if sig not in sig_price:
                        sig_price[sig] = ev.price_at(rpc, sig, mint_b)
                    if sig_price[sig]:
                        price = sig_price[sig]; break
                if price:
                    path[str(gt)] = price
            out = {"mint": r["mint"],
                   "day": datetime.fromtimestamp(created, tz=timezone.utc).strftime("%m-%d"),
                   "ath_mc": r.get("ath_mc"), "t2ath": r.get("time_to_ath_s"),
                   "n180": r.get("n_trades_180s"), "reached": reached, "path": path}
            f.write(json.dumps(out) + "\n")
            if (i + 1) % 40 == 0:
                f.flush()
                print(f"  [path] {i+1}/{len(uni)} rpc={rpc.calls}", flush=True)
    print(f"[done] -> {OUT} rpc={rpc.calls}")


if __name__ == "__main__":
    main()
