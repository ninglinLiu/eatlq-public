#!/usr/bin/env python3
"""Stage 2 analysis: deployer LIFETIME reputation as a creation-block fingerprint.

STRICTLY CAUSAL: for a target coin created at time T, a deployer's prior coin
counts toward reputation only if BOTH the prior coin's creation AND its ATH
timestamp are < T (so the peak was actually observable by T — no lookahead).

Case-control, time OOS (discover on train, measure on test), gates:
lift>=1.3, bootstrap_p5(lift)>1, n>=30, placebo collapses.

ADVERSE-SELECTION CAVEAT still applies: even if deployer reputation separates,
selectability != profitability (Stage 0), and a known-good deployer can flip to
dumping on his followers. Pass here = candidate for tiny live canary ONLY.
"""
from __future__ import annotations
import json, random, statistics as st, datetime as dt

LAB = "/opt/EATLQ/mvp/diagnostics/sol/creates_labeled.jsonl"
HIST = "/opt/EATLQ/mvp/diagnostics/sol/deployer_history.jsonl"
WIN_THR, MIN_TTATH = 20000.0, 120.0
TRAIN_DAYS = {"06-11", "06-12", "06-13"}
TEST_DAYS = {"06-14", "06-15"}


def day_of(bt):
    return dt.datetime.fromtimestamp(int(bt), dt.timezone.utc).strftime("%m-%d")


def is_winner(r):
    return float(r.get("ath_mc") or 0) >= WIN_THR and float(r.get("time_to_ath_s") or 0) >= MIN_TTATH


def load_hist():
    h = {}
    for l in open(HIST):
        try:
            d = json.loads(l)
        except Exception:
            continue
        h[d["creator"]] = d.get("coins", [])
    return h


def lift_ci(coins, pred, n_boot=1500, seed=1):
    wins = [1 if r["_win"] else 0 for r in coins]
    flags = [1 if pred(r) else 0 for r in coins]
    n = len(coins); nsel = sum(flags)
    base = sum(wins) / n if n else 0
    if nsel == 0 or base == 0:
        return None
    p_sel = sum(w for w, f in zip(wins, flags) if f) / nsel
    lift = p_sel / base
    rng = random.Random(seed); lifts = []
    for _ in range(n_boot):
        sw = ssel = swsel = 0
        for _ in range(n):
            i = rng.randrange(n)
            sw += wins[i]
            if flags[i]:
                ssel += 1; swsel += wins[i]
        if ssel and sw:
            lifts.append((swsel / ssel) / (sw / n))
    lifts.sort()
    p5 = lifts[int(0.05 * len(lifts))] if lifts else 0
    return dict(n_sel=nsel, n_win=sum(w for w, f in zip(wins, flags) if f),
                p_sel=p_sel, base=base, lift=lift, p5=p5)


def placebo(coins, pred, n=150, seed=7):
    rng = random.Random(seed)
    labels = [1 if r["_win"] else 0 for r in coins]
    flags = [1 if pred(r) else 0 for r in coins]
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


def main():
    hist = load_hist()
    lab = [json.loads(l) for l in open(LAB)]
    matched = 0
    for r in lab:
        r["_day"] = day_of(r["block_time"]); r["_win"] = is_winner(r)
        T = int(r["created_ms"])
        prior = hist.get(r["creator"], [])
        # strictly causal prior coins
        pc = [c for c in prior if c.get("ct") and int(c["ct"]) < T and c.get("mint") != r["mint"]]
        r["dep_prior_n"] = len(pc)
        # causal: peak observed before T
        causal_ath = [float(c["ath"] or 0) for c in pc
                      if c.get("ath_ts") and int(c["ath_ts"]) < T]
        r["dep_prior_maxath"] = max(causal_ath) if causal_ath else 0.0
        r["dep_prior_win20k"] = 1 if any(a >= 20000 for a in causal_ath) else 0
        r["dep_prior_win50k"] = 1 if any(a >= 50000 for a in causal_ath) else 0
        r["dep_prior_win100k"] = 1 if any(a >= 100000 for a in causal_ath) else 0
        if r["creator"] in hist:
            matched += 1
    train = [r for r in lab if r["_day"] in TRAIN_DAYS]
    test = [r for r in lab if r["_day"] in TEST_DAYS]
    print("#### adverse selection still applies: pass = tiny canary only, never scale on backtest ####\n")
    print(f"coins={len(lab)} creators_matched={matched}/{len(lab)}")
    print(f"train={len(train)} (win {sum(r['_win'] for r in train)}) test={len(test)} (win {sum(r['_win'] for r in test)})")
    print(f"have-prior-history coins: train={sum(1 for r in train if r['dep_prior_n']>0)} test={sum(1 for r in test if r['dep_prior_n']>0)}\n")
    print("=== DEPLOYER LIFETIME REPUTATION (causal) — gates: lift>=1.3 & p5>1 & n>=30 & placebo~1 ===\n")

    def scan(name, pred):
        tr = lift_ci(train, pred); te = lift_ci(test, pred)
        if not tr or not te:
            print(f"  {name:30s} (insufficient)"); return
        pl = placebo(test, pred)
        # BOTH-SIDES gate: a real fingerprint must point the same way in train AND test
        both_ok = (tr["lift"] >= 1.3 and tr["p5"] > 1.0 and te["lift"] >= 1.3 and te["p5"] > 1.0
                   and te["n_sel"] >= 30 and abs(pl - 1) < 0.2)
        verdict = "PASS(both)" if both_ok else ("test-only(ARTIFACT)" if (te["lift"] >= 1.3 and te["p5"] > 1 and tr["lift"] < 1) else "fail")
        print(f"  {name:30s} TRAIN lift={tr['lift']:.2f}(p5={tr['p5']:.2f},n{tr['n_sel']}) | "
              f"TEST lift={te['lift']:.2f}(p5={te['p5']:.2f},n{te['n_sel']},win{te['n_win']}) "
              f"placebo={pl:.2f} -> {verdict}")

    feats = [
        ("prior create >=1", lambda r: r["dep_prior_n"] >= 1),
        ("prior create >=5", lambda r: r["dep_prior_n"] >= 5),
        ("prior peak ath>=20k (causal)", lambda r: r["dep_prior_win20k"] == 1),
        ("prior peak ath>=50k (causal)", lambda r: r["dep_prior_win50k"] == 1),
        ("prior peak ath>=100k (causal)", lambda r: r["dep_prior_win100k"] == 1),
        ("prior maxath>=10k (causal)", lambda r: r["dep_prior_maxath"] >= 10000),
    ]
    for nm, fn in feats:
        scan(nm, fn)

    # ROBUSTNESS: swap the split (discover on 06-14/15, test on 06-11/13). A real
    # edge survives both directions; an artifact flips.
    print("\n=== SWAPPED SPLIT (train<->test) — real edge should survive, artifact flips ===")
    for nm, fn in feats[2:]:
        a = lift_ci(test, fn); b = lift_ci(train, fn)  # note: roles swapped
        if a and b:
            print(f"  {nm:30s} discover(06-14/15) lift={a['lift']:.2f} | verify(06-11/13) lift={b['lift']:.2f} "
                  f"-> {'consistent' if (a['lift']>=1.3 and b['lift']>=1.3) else 'FLIPS (not real)'}")


if __name__ == "__main__":
    main()
