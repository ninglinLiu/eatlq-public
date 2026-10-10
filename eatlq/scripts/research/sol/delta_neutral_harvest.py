"""Delta-neutral funding harvest on HIGH-funding alts (the requested idea).

Position: LONG spot + SHORT perp = price-neutral. The short perp RECEIVES funding
when funding>0 (PAYS when <0). PnL ~= sum(funding collected) - trading costs.
Unlike the BTC/ETH carry (~0%/mo, funding too small), these alts have funding an
order of magnitude bigger (clipped p95 ~1.4%/8h). Does it beat costs?

Two modes:
  ALWAYS-ON: hold continuously the whole window. ceiling case.
  SELECTIVE: only hold when funding >= TRAIN top-third threshold; exit when it
             normalizes (<= half threshold) or after K settlements. Pay round-trip
             cost per episode. TRAIN-locked threshold -> judged on TEST.
Costs: 4 taker fills per episode (buy spot, sell perp, sell spot, buy perp).
Caveats (NOT modeled): spot liquidity/availability for these alts, basis-convergence
slippage, perp-margin liquidation if hedge drifts, and the -200%/8h data glitches
(clipped). So this is an OPTIMISTIC upper bound.
"""
from __future__ import annotations
import json, glob, os, statistics as st

FUND = "/opt/EATLQ/scripts/research/sol/_cache_fund_cg"
CLIP = float(os.environ.get("FUND_CLIP", "0.02"))
FILL = float(os.environ.get("FILL", "0.00075"))   # taker per fill
RT_COST = 4 * FILL                                 # spot+perp in & out
SETTLE_PER_MONTH = 3 * 30                           # 8h settlements / month
SPLIT = 0.6
K_MAX = int(os.environ.get("K_MAX", "9"))           # max 3 days per episode


def load_fund():
    out = {}
    for fn in glob.glob(f"{FUND}/*.json"):
        out[os.path.basename(fn)[:-5]] = sorted(json.load(open(fn))["rows"])
    return out


def clip(r): return max(-CLIP, min(CLIP, r))


def always_on(fund):
    print(f"\n===== ALWAYS-ON (hold whole window) cost_rt={100*RT_COST:.2f}% =====")
    tot_net = []
    for sym, rows in sorted(fund.items()):
        rates = [clip(r) for _, r in rows]
        gross = sum(rates)                      # short collects
        months = len(rates) / SETTLE_PER_MONTH
        net = gross - RT_COST
        mo = net / months if months else 0
        tot_net.append(mo)
        flag = "" if mo > 0 else "  <- net loss"
        print(f"  {sym:8s} n={len(rates):4d} mean8h={100*st.mean(rates):+.3f}% "
              f"gross={100*gross:+6.1f}% net/mo={100*mo:+5.2f}%{flag}")
    print(f"  >>> median net/mo across coins = {100*st.median(tot_net):+.2f}%  "
          f"(positive coins {sum(1 for x in tot_net if x>0)}/{len(tot_net)})")


def selective(fund):
    print(f"\n===== SELECTIVE (enter when funding high; TRAIN-locked thr) "
          f"cost_rt={100*RT_COST:.2f}% K_max={K_MAX} =====")
    # learn threshold on TRAIN portion (pooled), top-third of positive funding
    allts = sorted(ts for rows in fund.values() for ts, _ in rows)
    cut = allts[int(SPLIT * len(allts))]
    train_rates = [clip(r) for rows in fund.values() for ts, r in rows if ts <= cut and r > 0]
    thr = sorted(train_rates)[int(2/3 * len(train_rates))] if train_rates else 0.005
    exit_thr = thr / 2
    print(f"  TRAIN entry thr (top-third of positive 8h funding) = {100*thr:.3f}%/8h")
    for segname, lo, hi in [("TRAIN", 0, cut), ("TEST", cut, 1e18)]:
        eps = []
        for sym, rows in fund.items():
            i = 0; n = len(rows)
            while i < n:
                ts, r = rows[i][0], clip(rows[i][1])
                if not (lo < ts <= hi) or r < thr:
                    i += 1; continue
                collected = 0.0; held = 0
                while i < n and held < K_MAX:
                    rr = clip(rows[i][1])
                    if rr <= exit_thr and held > 0:
                        break
                    collected += rr; held += 1; i += 1
                eps.append(collected - RT_COST)
        if not eps:
            print(f"  {segname}: no episodes"); continue
        ev = st.mean(eps); win = sum(1 for e in eps if e > 0) / len(eps)
        pos = sum(e for e in eps if e > 0); neg = -sum(e for e in eps if e < 0)
        pf = pos/neg if neg else 999
        print(f"  {segname}: episodes={len(eps)} EV/episode={100*ev:+.2f}% "
              f"win={100*win:.0f}% PF={pf:.2f} total={100*sum(eps):+.1f}%")


def main():
    fund = load_fund()
    print(f"#### DELTA-NEUTRAL FUNDING HARVEST. coins={len(fund)} clip={CLIP} ####")
    print(f"NOTE: optimistic upper bound (ignores spot liquidity/basis/glitches).")
    always_on(fund)
    selective(fund)


if __name__ == "__main__":
    main()
