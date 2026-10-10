"""Tier 2: capacity gate at $25/$50. A family that cannot fill half its slots dies."""
import json
import sys

sys.path.insert(0, "/root/base_dex_search_r0/src")
from ch import ch
from params import COST_CAP_BPS, OUT, slots


def parse(txt):
    lines = [ln for ln in txt.splitlines() if ln]
    if len(lines) < 2:
        return []
    cols = lines[0].split("\t")
    return [dict(zip(cols, ln.split("\t"))) for ln in lines[1:]]


def cheap_count(venue, size):
    if venue in ("univ2", "aero_vamm", "univ2+aero_vamm"):
        tbl = f"census.v2_pit_cost_{int(size)}"
        try:
            q = f"""SELECT uniqExactIf(pool, feasible=1 AND cost_bps_med<={COST_CAP_BPS}) c
                    FROM {tbl}"""
            if venue == "univ2":
                q = f"""SELECT uniqExactIf(pool, feasible=1 AND cost_bps_med<={COST_CAP_BPS} AND venue='univ2') c
                        FROM {tbl}"""
            elif venue == "aero_vamm":
                q = f"""SELECT uniqExactIf(pool, feasible=1 AND cost_bps_med<={COST_CAP_BPS} AND venue='aero_vamm') c
                        FROM {tbl}"""
            return int(ch(q, fmt="TSV").strip() or 0)
        except Exception:
            return 0
    if venue == "univ4":
        return 0
    tbl = f"census.pit_cost_{int(size)}" if size != 5000 else "census.pit_cost"
    # $25 table may still be building; fall back to scaled $50
    try:
        exists = ch(f"EXISTS TABLE {tbl}", fmt="TSV").strip()
    except Exception:
        exists = "0"
    if exists != "1":
        tbl = "census.pit_cost_50"
    venue_sql = {
        "univ3": "pl.venue='univ3'",
        "aero_cl": "pl.venue='aero_cl'",
        "sushi_v3": "pl.factory='0xc35dadb65012ec5796536bd9864ed8773abc74c4'",
        "alienbase_cl": "pl.factory='0x0fd83557b2be93617c9c1c1b6fd549401c74558c'",
        "unknown": "pl.venue='unknown'",
    }.get(venue, "1")
    q = f"""
        SELECT uniqExactIf(c.pool, c.feasible=1 AND c.cost_bps_med<={COST_CAP_BPS})
        FROM {tbl} c INNER JOIN census.pools pl ON pl.pool=c.pool
        WHERE {venue_sql}
    """
    try:
        return int(ch(q, fmt="TSV").strip() or 0)
    except Exception:
        return 0


def main():
    t1 = json.load(open(f"{OUT}/tier1.json"))
    out = []
    for fam in t1["families"]:
        row = dict(fam)
        if fam.get("verdict") != "SURVIVE_T1":
            row["tier2"] = fam.get("verdict", fam.get("status"))
            out.append(row)
            continue
        size = fam.get("size_usd") or 50
        need = max(slots(size) // 2, 1)
        n = cheap_count(fam["venue"], size)
        row["cheap_pools"] = n
        row["need_half_slots"] = need
        if n < need:
            row["verdict"] = "CAPACITY_DEAD"
            row["tier2"] = "CAPACITY_DEAD"
        else:
            row["verdict"] = "SURVIVE_T2"
            row["tier2"] = "SURVIVE_T2"
        out.append(row)
        print(f"  {fam['venue']} ${size} {fam.get('horizon')} cheap={n} need={need} -> {row['verdict']}",
              flush=True)
    n2 = sum(1 for r in out if r.get("verdict") == "SURVIVE_T2")
    json.dump(dict(n_survive_t2=n2, families=out), open(f"{OUT}/tier2.json", "w"), indent=1)
    print(f"tier2 survivors {n2}")


if __name__ == "__main__":
    main()
