#!/usr/bin/env python3
"""CASE-CONTROL forensic: is there a CREATION-BLOCK-KNOWABLE fingerprint that
separates strong winners (强庄 proxy) from contemporaneous dead controls?
Like the fee=100 discovery — cheap data, hard gates.

#### BIGGEST ENEMY (read first) ####
ADVERSE SELECTION / EXIT LIQUIDITY. A strong manipulator's business IS dumping
the coin on whoever follows. Any fingerprint that "works" in backtest may only
mean "right now you happen to be pumping alongside him and exiting before his
distribution." That is extremely fragile to him changing tactics, and it is
unfalsifiable from historical data alone (the backtest cannot see the regime
where he flips to dumping on fingerprint-followers). THEREFORE: even a passing
fingerprint may ONLY be taken to a tiny live forward canary, NEVER scaled on the
backtest. Several "predictive" features below (whale first-buy, sniper
concentration) are themselves the manipulator's footprint = the thing that will
dump on you.

GATES: lift>=1.3, bootstrap_p5(lift)>1, n>=30, placebo collapses, time OOS.

CREATION-KNOWABLE FEATURES ONLY (no signal that needs elapsed time to form):
  - deployer reputation: prior creates / prior winners by this creator BEFORE
    this coin (strictly time-causal; knowable by querying deployer history)
  - initial sniper structure: distinct snipers in first ~1s, total/max SOL,
    top-1 concentration  (knowable within the creation block + next slot)
  - metadata: socials, name/sym length, nsfw
EXCLUDED (need time to form, already shown not same-block-usable):
  alive_180, n_trades_60/180, time_to_5th.
"""
from __future__ import annotations
import json, random, statistics as st
from collections import defaultdict

LAB = "/opt/EATLQ/mvp/diagnostics/sol/creates_labeled.jsonl"
SB_FILES = ["smart_buyers", "smart_buyers_extra", "smart_buyers_x2", "smart_buyers_x3", "smart_buyers_collected"]
WIN_THR, MIN_TTATH = 20000.0, 120.0
TRAIN_DAYS = {"06-11", "06-12", "06-13"}
TEST_DAYS = {"06-14", "06-15"}
import datetime as _dt


def day_of(bt):
    return _dt.datetime.fromtimestamp(int(bt), _dt.timezone.utc).strftime("%m-%d")


def is_winner(r):
    return float(r.get("ath_mc") or 0) >= WIN_THR and float(r.get("time_to_ath_s") or 0) >= MIN_TTATH


def load():
    lab = [json.loads(l) for l in open(LAB)]
    for r in lab:
        r["_day"] = day_of(r["block_time"])
        r["_bt"] = int(r["block_time"])
        r["_win"] = is_winner(r)
    # join early_buys
    eb = {}
    for f in SB_FILES:
        try:
            for l in open(f"/opt/EATLQ/mvp/diagnostics/sol/{f}.jsonl"):
                d = json.loads(l)
                if d["mint"] not in eb:
                    eb[d["mint"]] = d.get("early_buys", [])
        except FileNotFoundError:
            pass
    for r in lab:
        r["_eb"] = eb.get(r["mint"], None)
    return lab


def add_deployer_history(lab):
    """strict time-causal: for each coin, stats of its creator's EARLIER coins."""
    by_creator = defaultdict(list)
    for r in sorted(lab, key=lambda x: x["_bt"]):
        c = r["creator"]
        prior = by_creator[c]
        r["dep_prior_n"] = len(prior)
        r["dep_prior_wins"] = sum(1 for p in prior if p["_win"])
        r["dep_has_prior_win"] = 1 if r["dep_prior_wins"] > 0 else 0
        r["dep_repeat"] = 1 if len(prior) > 0 else 0
        by_creator[c].append(r)


def sniper_feats(r):
    eb = r.get("_eb")
    if not eb:
        return None
    first = [e for e in eb if e.get("is_buy") and (e.get("t_rel") or 9e9) <= 1.0]
    if not first:
        first = [e for e in eb if e.get("is_buy")][:5]
    sols = [e.get("sol", 0) or 0 for e in first]
    tot = sum(sols)
    return {
        "snipe_n": len(first),
        "snipe_tot_sol": tot,
        "snipe_max_sol": max(sols) if sols else 0,
        "snipe_top1_share": (max(sols) / tot) if tot > 0 else 0,
        "snipe_distinct": len({e["buyer"] for e in first}),
    }


def lift_with_ci(coins, pred, n_boot=1000, seed=1):
    """lift = P(win|pred) / P(win|all); bootstrap p5 of lift. Precompute arrays."""
    wins = [1 if r["_win"] else 0 for r in coins]
    flags = [1 if pred(r) else 0 for r in coins]
    n = len(coins)
    nsel = sum(flags)
    nwin_sel = sum(w for w, f in zip(wins, flags) if f)
    base = sum(wins) / n
    if nsel == 0 or base == 0:
        return None
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


def placebo(coins, pred, n=100, seed=7):
    """shuffle the winner labels; predictive feature -> lift collapses to ~1."""
    rng = random.Random(seed)
    labels = [1 if r["_win"] else 0 for r in coins]
    flags = [1 if pred(r) else 0 for r in coins]
    nsel = sum(flags)
    if nsel == 0:
        return 0
    lifts = []
    for _ in range(n):
        rng.shuffle(labels)
        base = sum(labels) / len(labels)
        swsel = sum(l for l, f in zip(labels, flags) if f)
        if base > 0:
            lifts.append((swsel / nsel) / base)
    return st.mean(lifts) if lifts else 0


def scan(train, test, name, pred):
    tr = lift_with_ci(train, pred)
    te = lift_with_ci(test, pred)
    if not tr or not te:
        print(f"  {name:28s} (insufficient)"); return
    pl = placebo(test, pred)
    passed = (te["lift"] >= 1.3 and te["p5"] > 1.0 and te["n_sel"] >= 30 and abs(pl - 1) < 0.2)
    print(f"  {name:28s} TRAIN lift={tr['lift']:.2f}(n={tr['n_sel']}) | "
          f"TEST lift={te['lift']:.2f} p5={te['p5']:.2f} n={te['n_sel']} win={te['n_win']} "
          f"P(win)={100*te['p_sel']:.1f}%(base {100*te['base']:.1f}%) placebo={pl:.2f} "
          f"-> {'PASS' if passed else 'fail'}")


def main():
    lab = load()
    add_deployer_history(lab)
    for r in lab:
        r["_sf"] = sniper_feats(r)
    train = [r for r in lab if r["_day"] in TRAIN_DAYS]
    test = [r for r in lab if r["_day"] in TEST_DAYS]
    print("#### BIGGEST ENEMY: adverse selection / exit liquidity (see header). ####")
    print(f"\ncase=winner(ath>={int(WIN_THR)} & tta>={int(MIN_TTATH)}s)  control=rest (contemporaneous)")
    print(f"train={len(train)} (win {sum(r['_win'] for r in train)}) | test={len(test)} (win {sum(r['_win'] for r in test)})")
    print(f"\n=== UNIVARIATE CREATION-KNOWABLE FINGERPRINT SCAN (gates: lift>=1.3, p5>1, n>=30, placebo~1) ===\n")

    print("-- deployer reputation --")
    scan(train, test, "deployer has prior winner", lambda r: r["dep_has_prior_win"] == 1)
    scan(train, test, "deployer repeat (>=1 prior)", lambda r: r["dep_repeat"] == 1)
    scan(train, test, "deployer prior_n>=3", lambda r: r["dep_prior_n"] >= 3)

    print("\n-- initial sniper structure (first ~1s) --")
    sub_tr = [r for r in train if r["_sf"]]
    sub_te = [r for r in test if r["_sf"]]
    print(f"   (subset with early_buys: train={len(sub_tr)} test={len(sub_te)})")
    for nm, fn in [
        ("snipers>=5", lambda r: r["_sf"]["snipe_distinct"] >= 5),
        ("total sniper SOL>=10", lambda r: r["_sf"]["snipe_tot_sol"] >= 10),
        ("total sniper SOL>=30", lambda r: r["_sf"]["snipe_tot_sol"] >= 30),
        ("whale max buy>=5 SOL", lambda r: r["_sf"]["snipe_max_sol"] >= 5),
        ("whale max buy>=10 SOL", lambda r: r["_sf"]["snipe_max_sol"] >= 10),
        ("low conc (top1<0.5)", lambda r: r["_sf"]["snipe_top1_share"] < 0.5),
    ]:
        scan(sub_tr, sub_te, nm, fn)

    print("\n-- metadata (knowable at creation) --")
    for nm, fn in [
        ("has_twitter", lambda r: str(r["has_twitter"]) == "True"),
        ("has_website", lambda r: str(r["has_website"]) == "True"),
        ("has_telegram", lambda r: str(r["has_telegram"]) == "True"),
        ("any social", lambda r: any(str(r[k]) == "True" for k in ["has_twitter", "has_website", "has_telegram"])),
    ]:
        scan(train, test, nm, fn)

    print("\n-- combined: deployer-prior-winner AND whale buy>=5 --")
    scan(sub_tr, sub_te, "dep_prior_win & whale>=5",
         lambda r: r["dep_has_prior_win"] == 1 and r["_sf"]["snipe_max_sol"] >= 5)


if __name__ == "__main__":
    main()
