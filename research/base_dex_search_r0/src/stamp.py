"""Apply family verdicts back onto the 10k recipe list. No extra backtests."""
import json
import sys

sys.path.insert(0, "/root/base_dex_search_r0/src")
from params import OUT


def fam_key(v, size, hor):
    if v in ("univ2", "aero_vamm"):
        v = "univ2+aero_vamm"
    return f"{v}|{size}|{hor}"


def main():
    recs = json.load(open(f"{OUT}/recipes.json"))
    gate = json.load(open(f"{OUT}/dollar_gate.json"))
    by = {}
    for f in gate["families"]:
        by[fam_key(f.get("venue"), f.get("size_usd"), f.get("horizon"))] = f.get("verdict")
    counts = {}
    stamped = []
    for r in recs["recipes"]:
        k = fam_key(r["venue"], r["size_usd"], r["horizon"])
        if r["venue"] == "univ4":
            v = "FAMILY_DEAD"
        elif str(r["venue"]).startswith("other:"):
            v = "DATA_GAP"
        elif r["horizon"] == "1m":
            v = "DATA_GAP"
        elif r["entry"] in ("ks_as_is", "p_fatal_loose", "p_fatal_tight", "ks_complement"):
            # production Keystone features are not on the year panel
            base = by.get(k, "FAMILY_DEAD")
            v = "KS_INAPPLICABLE" if base != "LEAD" else base
        else:
            v = by.get(k, "FAMILY_DEAD")
        r = dict(r, verdict=v)
        stamped.append(r)
        counts[v] = counts.get(v, 0) + 1
    json.dump(dict(n=len(stamped), counts=counts), open(f"{OUT}/recipe_verdicts.json", "w"), indent=1)
    print("stamped", len(stamped), counts)


if __name__ == "__main__":
    main()
