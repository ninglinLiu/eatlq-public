"""Tier 3: atomic / real-sell sampling for families that survived T2.

CL: reuse the year-panel paper nets as an upper bound, then apply the
stop-fill death rate already measured (35.1% unsellable on spike-like names).
That is conservative relative to claiming paper PnL. Full eth_call sampling
of every survivor is not required to kill a family whose paper hi is already
below the dollar gate after a 35.1% wipe mix.

V4: confirmation cell already FAMILY_DEAD.

Any family still alive after the mix goes to dollar_gate.py. No LEAD without it.
"""
import json
import math
import sys

sys.path.insert(0, "/root/base_dex_search_r0/src")
from params import OUT

UNSELLABLE = 0.351  # base_stop_fill_r0 measured token-death rate on +200% universe


def mix_mean(paper_mean):
    """E[R] after 35.1% of losing-or-random exits become -100% of the trade.
    Applied to the paper mean as a haircut: remaining mass keeps paper, wiped mass = -1.
    This is an upper bound on atomic EV if paper EV was computed on tick closes.
    """
    if paper_mean is None or not math.isfinite(paper_mean):
        return float("nan")
    return (1.0 - UNSELLABLE) * paper_mean + UNSELLABLE * (-1.0)


def best_rule(fam):
    best = None
    cands = []
    for name, st in (fam.get("rules") or {}).items():
        if name == "random" or not isinstance(st, dict) or st.get("mean") is None:
            continue
        cands.append(dict(name=name, exit="ret_time", **st))
    for name, by_ex in (fam.get("exits") or {}).items():
        if name == "random" or not isinstance(by_ex, dict):
            continue
        for ex, st in by_ex.items():
            if isinstance(st, dict) and st.get("mean") is not None:
                cands.append(dict(name=name, exit=ex, **st))
    for c in cands:
        if best is None or c["mean"] > best["mean"]:
            best = c
    return best


def main():
    t2 = json.load(open(f"{OUT}/tier2.json"))
    out = []
    for fam in t2["families"]:
        row = dict(fam)
        if fam.get("verdict") != "SURVIVE_T2":
            row["tier3"] = fam.get("verdict")
            out.append(row)
            continue
        b = best_rule(fam)
        if not b:
            row["verdict"] = "FAMILY_DEAD"
            row["tier3"] = "no_scored_rule"
            out.append(row)
            continue
        if abs(b["mean"]) > 5.0 or abs(b.get("hi") or 0) > 5.0:
            row["verdict"] = "FAMILY_DEAD"
            row["tier3"] = "junk_tick_paper_mean"
            row["best_rule"] = b["name"]
            row["paper_mean"] = b["mean"]
            out.append(row)
            print(f"  {fam['venue']} ${fam.get('size_usd')} {fam.get('horizon')} "
                  f"paper={b['mean']:+.3f} -> FAMILY_DEAD junk_tick", flush=True)
            continue
        atomic_ev = mix_mean(b["mean"])
        row["best_rule"] = b["name"]
        row["paper_mean"] = b["mean"]
        row["paper_lo"] = b.get("lo")
        row["paper_hi"] = b.get("hi")
        row["same_sign"] = b.get("same_sign")
        row["atomic_mix_ev"] = atomic_ev
        row["atomic_label_spec"] = "keystone_r2_atomic/out/atomic_label_spec.md"
        if atomic_ev < 0:
            row["verdict"] = "FAMILY_DEAD"
            row["tier3"] = "atomic_mix_ev<0"
        else:
            row["verdict"] = "SURVIVE_T3"
            row["tier3"] = "SURVIVE_T3"
        out.append(row)
        print(f"  {fam['venue']} ${fam.get('size_usd')} {fam.get('horizon')} "
              f"paper={b['mean']:+.3f} mix={atomic_ev:+.3f} -> {row['verdict']}", flush=True)
    n3 = sum(1 for r in out if r.get("verdict") == "SURVIVE_T3")
    json.dump(dict(n_survive_t3=n3, unsellable=UNSELLABLE, families=out),
              open(f"{OUT}/tier3.json", "w"), indent=1)
    print(f"tier3 survivors {n3}")


if __name__ == "__main__":
    main()
