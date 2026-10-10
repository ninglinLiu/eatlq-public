#!/usr/bin/env python3
"""FRONT A (redefined) — does GENUINE OPERATOR NET-ACCUMULATION predict a
positive REALIZED (sellable) return, in a no-short bonding-curve market?

WHY THIS EXISTS / WHAT WAS WRONG BEFORE
---------------------------------------
Prior work (creation_fingerprint.py, deployer_reputation.py) tested two DIFFERENT
things and conflated them with "operator buying":
  (1) deployer LIFETIME REPUTATION  -> falsified (swapped-split flip = noise)
  (2) first ~1s sniper structure    -> only "passing" feature was the whale's own
                                        footprint = adverse-selection trap
Neither measured the actual hypothesis: "the operator is GENUINELY net-accumulating
THIS coin (real SOL in, NOT dumping the float back)". And BOTH labeled winners by
ath_mc = PAPER market-cap peak, not what you could withdraw.

This script fixes both:
  * SIGNAL  : per-wallet net SOL flow from early_buys; separate genuine accumulation
              (a wallet net-buys and does NOT sell in the window) from wash/dump.
  * OUTCOME : REALIZED, sellable round-trip return from the bonding-curve price grid
              (entry T+ENTRY_S, exit T+EXIT_S, real round-trip cost). NOT paper ATH.

NO-LOOKAHEAD: every signal feature uses only trades with t_rel <= SIGNAL_MAX_S
(default 10s). The entry decision is taken at T+ENTRY_S (default 180s). So the
signal is fully knowable before entry.

BIGGEST ENEMY (unchanged): adverse selection. Even if genuine accumulation
separates, the operator can flip to dumping on followers. A PASS here = candidate
for a tiny live canary ONLY, never scaled on backtest.

GATES (the project gold standard; bidirectional swapped split is the killer):
  lift>=1.3 AND bootstrap_p5(lift)>1 AND n_sel>=30 AND placebo~1
  AND consistent in BOTH directions of the time split (else = artifact).
Plus a realized-EV readout per signal group (mean/median realized ret, not lift).
"""
from __future__ import annotations
import argparse
import json
import glob
import random
import statistics as st
import datetime as dt
from collections import defaultdict

SB_GLOB = "/opt/EATLQ/mvp/diagnostics/sol/smart_buyers*.jsonl"
SIGNAL_MAX_S = 10.0      # features only from trades within first 10s (knowable early)
ENTRY_S = "180"          # grid key: enter at T+180s (overridable via --entry)
EXIT_S = "1800"          # grid key: exit at T+1800s (30m) (overridable via --exit)
COST = 0.03              # round-trip cost (pump 1%/side + slippage/priority buffer)
WHALE_NET_SOL = 5.0      # "whale genuine accumulation" threshold
BIG_DUMP_SOL = 3.0       # a wallet net-selling >= this early = early distribution
TRAIN_DAYS = {"06-11", "06-12", "06-13"}
TEST_DAYS = {"06-14", "06-15"}


def day_of(ms):
    return dt.datetime.fromtimestamp(int(ms) / 1000, dt.timezone.utc).strftime("%m-%d")


def load_coins():
    """union across smart_buyers* files, dedup by mint (first wins)."""
    seen = {}
    for f in sorted(glob.glob(SB_GLOB)):
        for line in open(f):
            try:
                d = json.loads(line)
            except Exception:
                continue
            m = d.get("mint")
            if m and m not in seen and d.get("early_buys") is not None:
                seen[m] = d
    return list(seen.values())


def grid_price(grid, key):
    """price at horizon `key`; if null, fall back to last earlier non-null
    (frozen curve = a dead coin you can still sell back into per pump mechanics)."""
    order = ["60", "180", "600", "1800", "3600", "7200"]
    if key not in order:
        return grid.get(key)
    idx = order.index(key)
    for k in order[idx::-1]:
        v = grid.get(k)
        if v:
            return v
    return None


def realized_ret(coin):
    """REALIZED sellable round-trip from the bonding-curve price grid. None if
    unpriceable at entry."""
    grid = coin.get("grid") or {}
    pe = grid_price(grid, ENTRY_S)
    px = grid_price(grid, EXIT_S)
    if not pe or not px:
        return None
    return (px / pe) * (1 - COST) - 1


def accumulation_feats(coin):
    """per-wallet net SOL within the early signal window; separate genuine
    accumulation from wash/dump. All from t_rel <= SIGNAL_MAX_S (no lookahead)."""
    eb = [e for e in (coin.get("early_buys") or []) if (e.get("t_rel") or 9e9) <= SIGNAL_MAX_S]
    if not eb:
        return None
    buy = defaultdict(float)
    sell = defaultdict(float)
    for e in eb:
        w = e.get("buyer")
        s = float(e.get("sol") or 0.0)
        if e.get("is_buy"):
            buy[w] += s
        else:
            sell[w] += s
    wallets = set(buy) | set(sell)
    net = {w: buy[w] - sell[w] for w in wallets}
    tot_buy = sum(buy.values())
    tot_sell = sum(sell.values())
    # the dominant net accumulator
    top_w = max(net, key=net.get) if net else None
    top_net = net.get(top_w, 0.0)
    top_sold = sell.get(top_w, 0.0)
    # biggest early distributor (net seller)
    max_net_sell = max((-v for v in net.values()), default=0.0)
    return {
        "n_wallets": len(wallets),
        "tot_buy": tot_buy,
        "tot_sell": tot_sell,
        "net_sol": tot_buy - tot_sell,
        "top_net": top_net,
        "top_sold": top_sold,
        # genuine accumulation: a whale net-bought >= threshold AND did NOT sell early
        "genuine_acc": 1 if (top_net >= WHALE_NET_SOL and top_sold == 0.0) else 0,
        # whale bought big but ALSO dumped some back early (wash / partial distribution)
        "whale_washy": 1 if (top_net >= WHALE_NET_SOL and top_sold > 0.0) else 0,
        "big_early_dump": 1 if max_net_sell >= BIG_DUMP_SOL else 0,
    }


def lift_ci(coins, pred, n_boot=1500, seed=1):
    wins = [1 if c["_win"] else 0 for c in coins]
    flags = [1 if pred(c) else 0 for c in coins]
    n = len(coins)
    nsel = sum(flags)
    base = sum(wins) / n if n else 0
    if nsel == 0 or base == 0:
        return None
    nwin_sel = sum(w for w, f in zip(wins, flags) if f)
    p_sel = nwin_sel / nsel
    lift = p_sel / base
    rng = random.Random(seed)
    lifts = []
    for _ in range(n_boot):
        sw = ssel = swsel = 0
        for _ in range(n):
            i = rng.randrange(n)
            sw += wins[i]
            if flags[i]:
                ssel += 1
                swsel += wins[i]
        if ssel and sw:
            lifts.append((swsel / ssel) / (sw / n))
    lifts.sort()
    p5 = lifts[int(0.05 * len(lifts))] if lifts else 0
    return dict(n_sel=nsel, n_win=nwin_sel, p_sel=p_sel, base=base, lift=lift, p5=p5)


def placebo(coins, pred, n=200, seed=7):
    rng = random.Random(seed)
    labels = [1 if c["_win"] else 0 for c in coins]
    flags = [1 if pred(c) else 0 for c in coins]
    nsel = sum(flags)
    if nsel == 0:
        return 0
    out = []
    for _ in range(n):
        rng.shuffle(labels)
        base = sum(labels) / len(labels)
        if base > 0:
            out.append((sum(l for l, f in zip(labels, flags) if f) / nsel) / base)
    return st.mean(out) if out else 0


def ev_readout(coins, pred):
    sel = [c["_ret"] for c in coins if pred(c)]
    if not sel:
        return "n=0"
    return (f"n={len(sel)} meanRet={100*sum(sel)/len(sel):+.1f}% "
            f"medRet={100*st.median(sel):+.1f}% winRet={100*sum(1 for x in sel if x>0)/len(sel):.0f}%")


def scan(train, test, name, pred):
    tr = lift_ci(train, pred)
    te = lift_ci(test, pred)
    if not tr or not te:
        print(f"  {name:26s} (insufficient sample)")
        return
    pl = placebo(test, pred)
    both = (tr["lift"] >= 1.3 and tr["p5"] > 1.0 and te["lift"] >= 1.3 and te["p5"] > 1.0
            and te["n_sel"] >= 30 and abs(pl - 1) < 0.2)
    verdict = "PASS(both)" if both else (
        "test-only(ARTIFACT)" if (te["lift"] >= 1.3 and te["p5"] > 1 and tr["lift"] < 1) else "fail")
    print(f"  {name:26s} TRAIN lift={tr['lift']:.2f}(p5={tr['p5']:.2f},n{tr['n_sel']}) | "
          f"TEST lift={te['lift']:.2f}(p5={te['p5']:.2f},n{te['n_sel']},win{te['n_win']}) "
          f"placebo={pl:.2f} -> {verdict}")
    print(f"      realized EV  TRAIN[{ev_readout(train, pred)}]  TEST[{ev_readout(test, pred)}]")


def main():
    global ENTRY_S, EXIT_S, SIGNAL_MAX_S, WHALE_NET_SOL
    ap = argparse.ArgumentParser()
    ap.add_argument("--entry", default=ENTRY_S, help="grid key for entry (60/180/600/1800)")
    ap.add_argument("--exit", default=EXIT_S, help="grid key for exit (600/1800/3600/7200)")
    ap.add_argument("--signal-max-s", type=float, default=SIGNAL_MAX_S)
    ap.add_argument("--whale-sol", type=float, default=WHALE_NET_SOL)
    a = ap.parse_args()
    ENTRY_S, EXIT_S, SIGNAL_MAX_S, WHALE_NET_SOL = a.entry, a.exit, a.signal_max_s, a.whale_sol
    print(f"[cfg] entry=T+{ENTRY_S}s exit=T+{EXIT_S}s signal_window={SIGNAL_MAX_S}s "
          f"whale_net>={WHALE_NET_SOL}SOL cost={int(100*COST)}%\n")
    coins = load_coins()
    kept = []
    for c in coins:
        c["_day"] = day_of(c["created_ms"])
        r = realized_ret(c)
        f = accumulation_feats(c)
        if r is None or f is None:
            continue
        c["_ret"] = r
        c["_win"] = 1 if r > 0 else 0   # REALIZED sellable win, not paper ATH
        c["_f"] = f
        kept.append(c)

    train = [c for c in kept if c["_day"] in TRAIN_DAYS]
    test = [c for c in kept if c["_day"] in TEST_DAYS]
    print("#### FRONT A: genuine operator net-accumulation -> REALIZED sellable return ####")
    print(f"#### biggest enemy: adverse selection. PASS = tiny canary only, never scale ####\n")
    print(f"coins with early_buys+grid+priced: {len(kept)}  "
          f"(train {len(train)} / test {len(test)})")
    if kept:
        base_all = sum(c["_win"] for c in kept) / len(kept)
        print(f"realized-win base rate (ret>0 after {int(100*COST)}% cost, "
              f"entry T+{ENTRY_S}s exit T+{EXIT_S}s): {100*base_all:.1f}%")
        print(f"all-coin realized EV: mean={100*sum(c['_ret'] for c in kept)/len(kept):+.1f}%  "
              f"median={100*st.median([c['_ret'] for c in kept]):+.1f}%\n")
    if len(train) < 30 or len(test) < 30:
        print("WARNING: sample too small for a trustworthy verdict; treat as directional only.\n")

    print("=== CASE-CONTROL: signal -> P(realized win) (gates: lift>=1.3 & p5>1 & n>=30 & placebo~1 & both-sides) ===\n")
    feats = [
        ("net buying (net_sol>0)", lambda c: c["_f"]["net_sol"] > 0),
        ("whale net-buy>=5 SOL", lambda c: c["_f"]["top_net"] >= WHALE_NET_SOL),
        ("GENUINE acc (whale, no dump)", lambda c: c["_f"]["genuine_acc"] == 1),
        ("NO big early dump", lambda c: c["_f"]["big_early_dump"] == 0),
        ("genuine acc & no big dump",
         lambda c: c["_f"]["genuine_acc"] == 1 and c["_f"]["big_early_dump"] == 0),
        ("whale washy (buy+dump)", lambda c: c["_f"]["whale_washy"] == 1),
    ]
    for nm, fn in feats:
        scan(train, test, nm, fn)

    print("\n=== SWAPPED SPLIT (discover<->verify) — a real edge survives both; an artifact flips ===")
    for nm, fn in feats:
        a = lift_ci(test, fn)
        b = lift_ci(train, fn)
        if a and b:
            ok = a["lift"] >= 1.3 and b["lift"] >= 1.3
            print(f"  {nm:26s} discover(test) lift={a['lift']:.2f} | verify(train) lift={b['lift']:.2f} "
                  f"-> {'consistent' if ok else 'FLIPS (not real)'}")


if __name__ == "__main__":
    main()
