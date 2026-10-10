"""Dollar gate: can a T3 survivor print $5,000/month on a $1,000 sleeve?

Frozen in GOAL.md §7. Parallel slots K=floor(1000/S). Cycles per month from
horizon. End-of-month wealth must be >= $6,000. Anything short is not LEAD.

Paper/mix EV above +200% per trade is junk-tick scale (round 46) and cannot
become LEAD. Overflow is not a dollar number.
"""
import json
import math
import sys

sys.path.insert(0, "/root/base_dex_search_r0/src")
from params import MONTHLY_TARGET, OUT, TOTAL_USD, slots

HOURS = {"1m": 1 / 60, "10m": 0.25, "1h": 1.0, "4h": 4.0, "ks4h": 4.0}
HOURS_MONTH = 30.4 * 24
JUNK_EV = 2.0  # +200% per trade is not a measured atomic EV


def n_cycles(horizon):
    h = HOURS.get(horizon)
    if not h:
        return 0.0
    return HOURS_MONTH / h


def month_wealth(ev, size, horizon):
    """Compound each slot independently at `ev` per hold, K slots, then sum."""
    k = slots(size)
    n = n_cycles(horizon)
    if n <= 0 or ev is None or not math.isfinite(ev) or ev <= -1:
        return 0.0
    if ev > JUNK_EV:
        return float("inf")
    try:
        per = size * (1.0 + ev) ** n
    except OverflowError:
        return float("inf")
    if not math.isfinite(per):
        return float("inf")
    return k * per


def need_ev(size, horizon):
    n = n_cycles(horizon)
    if n <= 0:
        return float("nan"), float("nan")
    # compound: (1+ev)^n >= 6  →  ev >= 6^(1/n) - 1
    need_c = 6.0 ** (1.0 / n) - 1.0
    # linear restake of the $1,000 sleeve (no slot compounding)
    need_l = MONTHLY_TARGET / (TOTAL_USD * n)
    return need_c, need_l


def main():
    t3 = json.load(open(f"{OUT}/tier3.json"))
    leads = []
    rows = []
    for fam in t3["families"]:
        row = dict(fam)
        size = fam.get("size_usd") or 50
        hor = fam.get("horizon") or "4h"
        need_c, need_l = need_ev(size, hor)
        row["need_ev_compound"] = need_c
        row["need_ev_linear"] = need_l
        row["cycles"] = n_cycles(hor)
        if fam.get("verdict") != "SURVIVE_T3":
            row["dollar"] = fam.get("verdict")
            rows.append(row)
            continue
        ev = fam.get("atomic_mix_ev")
        same = fam.get("same_sign")
        if ev is None or not math.isfinite(ev) or ev > JUNK_EV:
            row["verdict"] = "FAMILY_DEAD"
            row["dollar"] = "mix EV missing or junk-tick scale"
            rows.append(row)
            print(f"  {fam['venue']} ${size} {hor} -> FAMILY_DEAD (junk/missing EV)",
                  flush=True)
            continue
        wealth = month_wealth(ev, size, hor)
        linear = TOTAL_USD * ev * n_cycles(hor)
        profit = (wealth - TOTAL_USD) if math.isfinite(wealth) else float("nan")
        row["month_wealth"] = wealth if math.isfinite(wealth) else None
        row["month_profit"] = profit if math.isfinite(profit) else None
        row["month_profit_linear"] = linear
        row["need"] = MONTHLY_TARGET
        half_ok = same is True or same is None  # missing half-split: do not invent pass
        # GOAL: both halves same sign (positive) for LEAD
        rules = fam.get("rules") or {}
        best = fam.get("best_rule")
        st = rules.get(best) if isinstance(rules.get(best), dict) else {}
        halves_pos = bool(st.get("same_sign") and st.get("h1_mean", 0) > 0
                          and st.get("h2_mean", 0) > 0)
        if (math.isfinite(profit) and profit >= MONTHLY_TARGET and ev > 0
                and halves_pos):
            row["verdict"] = "LEAD"
            row["dollar"] = "PASS"
            leads.append(row)
        else:
            short_c = (need_c - ev) if math.isfinite(need_c) else float("nan")
            short_l = (need_l - ev) if math.isfinite(need_l) else float("nan")
            row["verdict"] = "UNDERPOWERED"
            row["dollar"] = (
                f"month_profit ${profit if profit==profit else float('nan'):,.0f} "
                f"< ${MONTHLY_TARGET:,.0f} (ev={ev:+.4f}, need_compound={need_c:+.4f} "
                f"Δ={short_c:+.4f}, need_linear={need_l:+.4f} Δ={short_l:+.4f}, "
                f"halves_pos={halves_pos}, horizon={hor})"
            )
        rows.append(row)
        print(f"  {fam['venue']} ${size} {hor} ev={ev:+.4f} "
              f"profit=${profit if profit==profit else float('nan'):,.0f} "
              f"-> {row['verdict']}", flush=True)

    json.dump(dict(n_lead=len(leads), families=rows, leads=leads),
              open(f"{OUT}/dollar_gate.json", "w"), indent=1)
    print(f"LEADs {len(leads)}")
    if not leads:
        print("NO LEAD. Monthly $5,000 on $1,000 is not reached by any survivor.")


if __name__ == "__main__":
    main()
