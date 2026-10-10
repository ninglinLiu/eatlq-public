"""Delta-neutral carry, REBUILT with real spot+perp prices (non-tautological).

Per episode: enter LONG spot + SHORT perp when funding>=TRAIN thr; hold while
funding>exit_thr (<=K settlements). Net return (% of notional) =
    funding_collected  +  price_pnl  -  exec_cost
where price_pnl = (spot_exit/spot_entry-1) - (perp_exit/perp_entry-1)  [basis move],
and funding can FLIP NEGATIVE mid-hold (short then pays). So win-rate is no longer
guaranteed 100% -- this is the honest test.

Reports ALL coins and the LIQUID subset (perp depth>=$0.5M, the only realistically
tradeable names), TRAIN/TEST, at several round-trip cost assumptions.
"""
from __future__ import annotations
import json, glob, os, bisect, statistics as st

FUND = "/opt/EATLQ/scripts/research/sol/_cache_fund_cg"
CARRY = "/opt/EATLQ/scripts/research/sol/_cache_carry_cg"
CLIP = float(os.environ.get("FUND_CLIP", "0.02"))
SPLIT = 0.6
K_MAX = int(os.environ.get("K_MAX", "9"))
LIQ_MIN = 0.5e6


FUND_SCALE = 0.01   # Coinglass funding values are in PERCENT; convert to fraction
                    # (verified: Coinglass value == OKX native fundingRate * 100)


def load():
    fund, carry = {}, {}
    for fn in glob.glob(f"{FUND}/*.json"):
        rows = json.load(open(fn))["rows"]
        fund[os.path.basename(fn)[:-5]] = sorted([ts, r * FUND_SCALE] for ts, r in rows)
    for fn in glob.glob(f"{CARRY}/*.json"):
        carry[os.path.basename(fn)[:-5]] = json.load(open(fn))
    return fund, carry


def clip(r): return max(-CLIP, min(CLIP, r))


def px_at(rows, t):
    """close price at-or-before t. rows=[ts,open,close,vol]."""
    ts = [r[0] for r in rows]
    i = bisect.bisect_right(ts, t) - 1
    return rows[i][2] if i >= 0 else None


def episodes(fund, carry, lo, hi, thr, exit_thr, rt_cost):
    eps = []
    for sym, frows in fund.items():
        c = carry.get(sym)
        if not c or not c.get("spot_px") or not c.get("perp_px"):
            continue
        spot, perp = c["spot_px"], c["perp_px"]
        n = len(frows); i = 0
        while i < n:
            ts, r = frows[i][0], clip(frows[i][1])
            if not (lo < ts <= hi) or r < thr:
                i += 1; continue
            t_entry = ts
            collected = 0.0; held = 0; j = i
            while j < n and held < K_MAX:
                rr = clip(frows[j][1])
                if rr <= exit_thr and held > 0:
                    break
                collected += rr; held += 1; j += 1
            t_exit = frows[min(j, n-1)][0]
            se, sx = px_at(spot, t_entry), px_at(spot, t_exit)
            pe, px = px_at(perp, t_entry), px_at(perp, t_exit)
            if not (se and sx and pe and px):
                i = j + 1; continue
            price_pnl = (sx/se - 1) - (px/pe - 1)        # long spot - short perp
            net = collected + price_pnl - rt_cost
            eps.append((sym, net, collected, price_pnl, held))
            i = j + 1
    return eps


def summ(eps, name):
    if not eps:
        print(f"  {name}: none"); return
    nets = [e[1] for e in eps]
    win = sum(1 for x in nets if x > 0) / len(nets)
    pos = sum(x for x in nets if x > 0); neg = -sum(x for x in nets if x < 0)
    pf = pos/neg if neg else 999
    fund_avg = st.mean(e[2] for e in eps); px_avg = st.mean(e[3] for e in eps)
    print(f"  {name}: eps={len(eps):4d} EV/ep={100*st.mean(nets):+.2f}% win={100*win:3.0f}% "
          f"PF={pf:.2f} | funding={100*fund_avg:+.2f}% pricePnL={100*px_avg:+.2f}%")


def thr_from_train(fund, cut):
    tr = [clip(r) for rows in fund.values() for ts, r in rows if ts <= cut and r > 0]
    return (sorted(tr)[int(2/3*len(tr))] if tr else 0.005)


def main():
    fund, carry = load()
    liq = {s for s, c in carry.items() if c.get("depth_bids_usd", 0) >= LIQ_MIN}
    print(f"#### CARRY REAL (spot+perp prices). coins={len(fund)} liquid(depth>=$0.5M)={sorted(liq)} ####")
    allts = sorted(ts for rows in fund.values() for ts, _ in rows)
    cut = allts[int(SPLIT*len(allts))]
    thr = thr_from_train(fund, cut); exit_thr = thr/2
    print(f"TRAIN entry thr={100*thr:.3f}%/8h  exit<{100*exit_thr:.3f}%  K_max={K_MAX}")
    for rt in (0.002, 0.004, 0.006):
        print(f"\n--- round-trip exec cost = {100*rt:.1f}% ---")
        for label, lo, hi in [("TRAIN", 0, cut), ("TEST ", cut, 1e18)]:
            allc = episodes(fund, carry, lo, hi, thr, exit_thr, rt)
            liqc = [e for e in allc if e[0] in liq]
            summ(allc, f"{label} ALL  ")
            summ(liqc, f"{label} LIQUID")


if __name__ == "__main__":
    main()
