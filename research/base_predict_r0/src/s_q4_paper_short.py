#!/usr/bin/env python3
"""Q4 paper reversal of A3: selling into strength, without building a borrow stack.

A3 long = buy after z_L > k, hold h hours. Round 41: confirmed loss, wrong sign.
The other side is a short. You cannot flip the sign of *net* PnL (that would credit
you the round-trip). Correct arithmetic, from run_a5_year.py:

    edge      = cond_return − uncond_return          # already in a5_year_50.json
    long_net  = edge − hurdle
    short_net = −edge − hurdle_short − borrow        # one round-trip to short, plus interest

CI: negate and swap the edge bounds, then subtract hurdle and borrow.

Borrow is not the binding constraint on a 4h hold (50% APR ≈ 2.3 bps). The binding
constraint is (1) the hurdle is ~110 bps and the edge lower bound is often smaller,
and (2) Morpho/Aave on Base almost only let you borrow USDC/WETH against majors —
the 179 A3 names are not loan tokens.

Zero RPC. Reads only.
"""
from __future__ import annotations

import json
from pathlib import Path

A3 = json.loads(Path("/root/base_predict_r0/out/a5_year_50.json").read_text())[
    "A3 multi-bar momentum"
]
MARKETS = json.loads(Path("/root/recon_out/morpho_markets.json").read_text())
COLLAT = json.loads(Path("/root/recon_out/collateral_table.json").read_text())
UNIVERSE = Path("/root/base_condmm_r0/out/universe50.jsonl")
OUT = Path("/root/base_predict_r0/out")

# conservative alt-borrow APR if a market even exists
BORROW_APR = 0.50
HOURS_YEAR = 365.25 * 24

MAJORS = {
    "WETH", "ETH", "USDC", "USDT", "DAI", "USD", "EURC", "EUR",
    "cbBTC", "WBTC", "BTC", "cbETH", "wstETH", "weETH", "AERO",
}


def borrow_bps(h):
    return BORROW_APR * (h / HOURS_YEAR) * 1e4


def main():
    tok_sym = {r["token"].lower(): r["symbol"] for r in COLLAT}
    loan_syms, coll_syms = set(), set()
    for m in MARKETS.values():
        lt, ct = m["loanToken"].lower(), m["collateralToken"].lower()
        loan_syms.add(tok_sym.get(lt, lt[:10]))
        coll_syms.add(tok_sym.get(ct, ct[:10]))
    print("Morpho Blue Base (from recon sample):")
    print(f"  distinct loan tokens (what you can actually borrow): {sorted(loan_syms)}")
    print(f"  distinct collateral tokens: {sorted(coll_syms)[:20]} ... n={len(coll_syms)}")

    a3_syms = set()
    # cheap_symbols isn't the A3 roster; universe50 is the C panel. A3 used 179
    # symbols from the $50 year run. We only need the COUNT overlap later if we
    # had a roster; the cell metadata already says n_syms=179.
    print(f"  A3 cells run on 179 symbols (year/$50). Majors we could borrow: "
          f"{sorted(loan_syms & MAJORS)}")

    print("\n" + "=" * 118)
    print("A3 LONG vs PAPER SHORT")
    print("short_net = -edge - hurdle - borrow(50% APR). Naive -long_net is shown only to")
    print("flag the sign-of-cost trap (it credits you the round-trip).")
    print("=" * 118)
    hdr = (f"{'cell':14s} {'split':5s} {'n':>7s} {'edge':>8s} {'e_lo':>8s} {'e_hi':>8s} "
           f"{'hurdle':>7s} {'long':>8s} {'[lo,hi]':>22s} "
           f"{'short':>8s} {'[lo,hi]':>22s} {'naive-net':>9s} {'clear?':>7s}")
    print(hdr)

    rows = []
    n_clear = 0
    for cell, splits in A3.items():
        L, k, h = cell.split("_")
        h = int(h)
        bps_b = borrow_bps(h)
        for split, r in splits.items():
            if not r or r.get("n") is None:
                continue
            edge, elo, ehi = r["edge_bps"], r["edge_lo"], r["edge_hi"]
            hurdle = r["hurdle_bps"]
            long_m, llo, lhi = r["mean"], r["lo"], r["hi"]
            short_m = -edge - hurdle - bps_b
            # flip edge CI
            slo = -ehi - hurdle - bps_b
            shi = -elo - hurdle - bps_b
            naive = -long_m
            clear = slo is not None and slo > 0
            if clear:
                n_clear += 1
            def fmtci(lo, hi):
                if lo is None:
                    return f"{'n/a':>22s}"
                return f"[{lo:8.1f},{hi:8.1f}]"
            print(f"{cell:14s} {split:5s} {r['n']:7d} {edge:8.1f} {elo:8.1f} {ehi:8.1f} "
                  f"{hurdle:7.1f} {long_m:8.1f} {fmtci(llo,lhi)} "
                  f"{short_m:8.1f} {fmtci(slo,shi)} {naive:9.1f} "
                  f"{'YES' if clear else 'no':>7s}")
            rows.append({
                "cell": cell, "split": split, "n": r["n"],
                "edge": edge, "edge_lo": elo, "edge_hi": ehi, "hurdle": hurdle,
                "borrow_bps": bps_b, "long_net": long_m, "long_lo": llo, "long_hi": lhi,
                "short_net": short_m, "short_lo": slo, "short_hi": shi,
                "naive_flip": naive, "clears": clear, "top_sym": r.get("top_sym"),
            })

    print(f"\ncells with short lower bound > 0: {n_clear}/{len(rows)}")
    print(f"borrow at {BORROW_APR:.0%} APR: {borrow_bps(4):.2f} bps / 4h, "
          f"{borrow_bps(24):.2f} bps / 24h  (not the binding term)")

    # headline cell
    h = [x for x in rows if x["cell"] == "4_1.5_4"]
    print("\nHEADLINE (4h 1.5σ up, hold 4h) — the cell RESEARCH_PLAN quotes:")
    for x in h:
        print(f"  {x['split']}: long {x['long_net']:.1f} {x['long_lo']:.1f}…{x['long_hi']:.1f}   "
              f"edge {x['edge']:.1f} [{x['edge_lo']:.1f}, {x['edge_hi']:.1f}]  hurdle {x['hurdle']:.1f}")
        print(f"         short {x['short_net']:.1f} [{x['short_lo']:.1f}, {x['short_hi']:.1f}]  "
              f"naive -long_net = {x['naive_flip']:.1f}  (trap)")

    print("\nWHY THE OTHER SIDE DOES NOT PAY THIS ACCOUNT")
    print("1. Cost sign: flipping net PnL fabricates a profit equal to 2×hurdle.")
    print("   The real short still pays ~110 bps to enter and exit.")
    print("2. Edge vs hurdle: even where edge is significantly negative OOS, the")
    print("   lower bound of −edge is often smaller than the hurdle, so short CI")
    print("   still crosses zero.")
    print("3. Inventory: Morpho loan tokens on Base in the recon sample are")
    print(f"   {sorted(loan_syms)}. A3's 179 names are micro-caps. You cannot borrow")
    print("   YGG/ATTN/AUDD (the cells' top symbols) to sell them.")
    print("4. The names you CAN borrow (WETH, USDC, cbBTC) are exactly the ones")
    print("   A3 does not fire on — they do not make 1.5σ 4-hour moves.")

    json.dump({"rows": rows, "n_clear": n_clear, "loan_tokens": sorted(loan_syms),
               "borrow_apr": BORROW_APR},
              open(OUT / "a3_paper_short.json", "w"), indent=1)
    print(f"\nwrote {OUT / 'a3_paper_short.json'}")


if __name__ == "__main__":
    main()
