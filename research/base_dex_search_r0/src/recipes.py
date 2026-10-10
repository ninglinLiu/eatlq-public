"""Enumerate the preregistered recipe space. Count is reported before Tier 1."""
import json
import sys

sys.path.insert(0, "/root/base_dex_search_r0/src")
from params import ENTRIES, EXITS, FEATURE_SETS, HORIZONS, OUT, SIZES, enumerate_recipes


def venues_from_census():
    with open(f"{OUT}/venue_census.json") as f:
        c = json.load(f)
    ranked = list(c["ranked_for_recipes"])
    if "univ4" not in ranked:
        ranked.append("univ4")
    return ranked


def main():
    venues = venues_from_census()
    rows = enumerate_recipes(venues)
    n = len(rows)
    with open(f"{OUT}/recipes.json", "w") as f:
        json.dump(dict(n=n, venues=venues, sizes=list(SIZES),
                       horizons=list(HORIZONS), entries=list(ENTRIES),
                       exits=list(EXITS), n_feature_sets=len(FEATURE_SETS),
                       recipes=rows), f)
    print(f"recipes {n:,}  venues={venues}  "
          f"({len(venues)}×{len(SIZES)}×{len(HORIZONS)}×{len(ENTRIES)}×"
          f"{len(EXITS)}×{len(FEATURE_SETS)})")
    if n < 1000:
        raise SystemExit("recipe space smaller than intended; check census")


if __name__ == "__main__":
    main()
