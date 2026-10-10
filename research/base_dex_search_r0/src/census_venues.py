"""Base factory volume census and $25/$50 tradeable-pool counts.

Writes out/venue_census.json before any recipe is scored.
"""
import json
import sys
from collections import defaultdict

sys.path.insert(0, "/root/base_dex_search_r0/src")
from ch import ch, ch_ok
from params import COST_CAP_BPS, FACTORY_NAME, OUT, WETH


def parse_tsv(txt):
    lines = [ln for ln in txt.splitlines() if ln]
    if len(lines) < 2:
        return []
    cols = lines[0].split("\t")
    rows = []
    for ln in lines[1:]:
        parts = ln.split("\t")
        rows.append({c: parts[i] if i < len(parts) else "" for i, c in enumerate(cols)})
    return rows


def name_of(factory, venue_hint=""):
    f = (factory or "").lower()
    if f in FACTORY_NAME:
        return FACTORY_NAME[f]
    if venue_hint in ("univ3", "aero_cl", "univ2", "aero_vamm"):
        return venue_hint
    return f"other:{f[:10]}" if f else (venue_hint or "unknown")


def cl_volume():
    txt = ch(f"""
        SELECT pl.factory AS factory, pl.venue AS venue,
               uniqExact(s.pool) AS pools, count() AS swaps
        FROM census.cl_swaps s INNER JOIN census.pools pl ON pl.pool = s.pool
        WHERE s.block_time >= '2025-08-18'
        GROUP BY factory, venue
        ORDER BY swaps DESC""")
    out = []
    for r in parse_tsv(txt):
        out.append(dict(
            source="census.cl_swaps",
            factory=r["factory"], venue=name_of(r["factory"], r["venue"]),
            raw_venue=r["venue"], pools=int(r["pools"]), swaps=int(r["swaps"]),
        ))
    return out


def v2_volume():
    # aero_vamm_swaps_mv is a full duplicate of uniswap_v2_swaps_decoded (26162/26162).
    # Split by discovered_pools.factory where we have it; unmatched stay univ2-style.
    txt = ch(f"""
        SELECT ifNull(d.factory_address, 'unmapped') AS factory,
               ifNull(d.pool_type, 'v2') AS pool_type,
               uniqExact(s.pair_address) AS pools, count() AS swaps
        FROM eatlq.uniswap_v2_swaps_decoded s
        LEFT JOIN (
            SELECT lower(pool_address) AS pool, lower(factory_address) AS factory_address, pool_type
            FROM eatlq.discovered_pools WHERE chain_id = 8453
        ) d ON d.pool = s.pair_address
        WHERE s.chain_id = 8453 AND s.block_time >= '2025-08-18'
        GROUP BY factory, pool_type
        ORDER BY swaps DESC""")
    out = []
    for r in parse_tsv(txt):
        fac = r["factory"]
        hint = "aero_vamm" if r["pool_type"] == "aero_vamm" else "univ2"
        out.append(dict(
            source="eatlq.uniswap_v2_swaps_decoded",
            factory=fac, venue=name_of(fac, hint),
            raw_venue=r["pool_type"], pools=int(r["pools"]), swaps=int(r["swaps"]),
        ))
    return out


def cl_tradeable(size):
    tbl = "census.pit_cost_50" if size == 50 else (
        "census.pit_cost_25" if ch_ok("EXISTS TABLE census.pit_cost_25") == "1"
        else "")
    if not tbl:
        return {}
    txt = ch(f"""
        SELECT pl.factory AS factory, pl.venue AS venue,
               uniqExact(c.pool) AS pools,
               uniqExactIf(c.pool, c.feasible = 1 AND c.cost_bps_med <= {COST_CAP_BPS}) AS cheap
        FROM {tbl} c INNER JOIN census.pools pl ON pl.pool = c.pool
        GROUP BY factory, venue""")
    out = {}
    for r in parse_tsv(txt):
        v = name_of(r["factory"], r["venue"])
        out[v] = dict(pools=int(r["pools"]), cheap=int(r["cheap"]), table=tbl, size=size)
    return out


def v2_tradeable_proxy(size):
    """Inventory proxy: a WETH v2 pair is 'cheap-enough' if some swap in the
    last year moved at least `size` dollars of WETH (someone traded our size)
    and the pair has >= 200 swaps. Fee floor is 30 bps for univ2."""
    txt = ch(f"""
        WITH p AS (
            SELECT lower(pool_address) AS pool, lower(factory_address) AS factory,
                   pool_type, token0_address, token1_address
            FROM eatlq.discovered_pools
            WHERE chain_id = 8453 AND has_weth_leg = 1
              AND pool_type IN ('v2', 'aero_vamm')
        ),
        s AS (
            SELECT pair_address,
                   count() AS n,
                   max(toFloat64(
                       if(lower(p.token0_address) = '{WETH}',
                          p0, p1))) / 1e18 AS max_weth
            FROM (
                SELECT s.pair_address AS pair_address,
                       toFloat64(s.amount0_in + s.amount0_out) AS p0,
                       toFloat64(s.amount1_in + s.amount1_out) AS p1
                FROM eatlq.uniswap_v2_swaps_decoded s
                WHERE s.chain_id = 8453 AND s.block_time >= '2025-08-18'
            ) x INNER JOIN p ON p.pool = x.pair_address
            GROUP BY pair_address
        )
        SELECT p.factory, p.pool_type, count() AS pools,
               countIf(s.n >= 200 AND s.max_weth >= {size} / 2500.0) AS cheap
        FROM p INNER JOIN s ON s.pair_address = p.pool
        GROUP BY p.factory, p.pool_type""")
    out = {}
    for r in parse_tsv(txt):
        v = name_of(r["factory"], r["pool_type"] if r["pool_type"] == "aero_vamm" else "univ2")
        out[v] = dict(pools=int(r["pools"]), cheap=int(r["cheap"]),
                      table="v2_size_proxy", size=size,
                      note="max_weth_swap >= size/2500 ETH; fee assumed 30 bps")
    return out


def v4_note():
    return dict(
        venue="univ4", source="opt/EATLQ/scripts/research/base_v4",
        swaps=None, pools=None,
        verdict="v4_alpha_candidate_no",
        note="Initialize 0.003 ETH roundtrip: clean n=112, 0% positive, median -2.6%. "
             "No ClickHouse V4 swap table. Confirmation cell only.",
    )


def main():
    cl = cl_volume()
    v2 = v2_volume()
    t50_cl = cl_tradeable(50)
    t25_cl = cl_tradeable(25)
    t50_v2 = v2_tradeable_proxy(50)
    t25_v2 = v2_tradeable_proxy(25)

    by = defaultdict(lambda: dict(swaps=0, pools=0, sources=[]))
    for row in cl + v2:
        v = row["venue"]
        by[v]["swaps"] += row["swaps"]
        by[v]["pools"] += row["pools"]
        by[v]["sources"].append(row["source"])
        by[v]["factory"] = row["factory"]

    venues = []
    for v, agg in sorted(by.items(), key=lambda kv: -kv[1]["swaps"]):
        venues.append(dict(
            venue=v, factory=agg.get("factory", ""),
            swaps=agg["swaps"], pools=agg["pools"],
            sources=sorted(set(agg["sources"])),
            tradeable_50=t50_cl.get(v) or t50_v2.get(v),
            tradeable_25=t25_cl.get(v) or t25_v2.get(v),
        ))

    ranked = [v["venue"] for v in venues if v["swaps"] > 0][:8]
    report = dict(
        venues=venues,
        ranked_for_recipes=ranked,
        v4=v4_note(),
        gaps=[
            "1-minute CL bars: not in panel (10m proxied by 15m).",
            "univ4: no CH swap table; confirmation only.",
            "aerodrome_vamm_swaps_mv is a 100% pair-overlap duplicate of "
            "uniswap_v2_swaps_decoded; venue split uses discovered_pools.factory.",
            "pit_cost_25 built separately; if missing, $25 CL cheap-count falls back to proxy/empty.",
        ],
    )
    with open(f"{OUT}/venue_census.json", "w") as f:
        json.dump(report, f, indent=2)
    print(f"ranked: {ranked}")
    for v in venues:
        t = v.get("tradeable_50") or {}
        print(f"  {v['venue']:22} swaps={v['swaps']:>12} pools={v['pools']:>6} "
              f"cheap50={t.get('cheap', '?')}")
    print("v4:", report["v4"]["verdict"])


if __name__ == "__main__":
    main()
