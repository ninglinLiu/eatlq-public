"""Step 2: resolve gauge address for every aero_cl pool in edge.bars_universe_50."""

import sys

sys.path.insert(0, "/root/base_aero_emissions_r0/src")

import common as C
import rpc as R

ZERO = "0x" + "0" * 40


def chunks(xs, n):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def main():
    pools = [r["pool"].lower() for r in C.ch_rows(
        "SELECT pool FROM edge.bars_universe_50 WHERE venue='aero_cl' ORDER BY pool")]
    print(f"aero_cl pools in universe: {len(pools)}")

    # 1. Voter.gauges(pool)
    gauge_of = {}
    for batch in chunks(pools, 40):
        items = [(C.VOTER, C.SEL["gauges(address)"] + R.enc_addr(p)) for p in batch]
        for p, (res, err) in zip(batch, R.eth_call_batch(items)):
            gauge_of[p] = None if err else R.dec_addr(res)
    resolved = {p: g for p, g in gauge_of.items() if g and g != ZERO}
    print(f"Voter.gauges() -> non-zero for {len(resolved)}/{len(pools)}")

    # 2. round-trip verification: gauge.pool() and Voter.poolForGauge(gauge)
    glist = sorted(set(resolved.values()))
    gpool, gfactory, grewardtok, galive, gnft = {}, {}, {}, {}, {}
    for batch in chunks(glist, 25):
        for key, target, data in (
            ("pool", None, C.SEL["pool()"]),
            ("factory", None, C.SEL["gaugeFactory()"]),
            ("rtok", None, C.SEL["rewardToken()"]),
            ("nft", None, C.SEL["nft()"]),
        ):
            items = [(g, data) for g in batch]
            out = R.eth_call_batch(items)
            for g, (res, err) in zip(batch, out):
                v = None if err else R.dec_addr(res)
                {"pool": gpool, "factory": gfactory, "rtok": grewardtok,
                 "nft": gnft}[key][g] = v
        items = [(C.VOTER, C.SEL["isAlive(address)"] + R.enc_addr(g)) for g in batch]
        for g, (res, err) in zip(batch, R.eth_call_batch(items)):
            galive[g] = None if err else bool(R.dec_uint(res))

    rows, problems = [], []
    for p in pools:
        g = gauge_of.get(p)
        if not g or g == ZERO:
            problems.append({"pool": p, "reason": "Voter.gauges(pool) == address(0) "
                                                  "(no gauge ever created / not whitelisted)"})
            continue
        back = (gpool.get(g) or "").lower()
        ok = back == p
        if not ok:
            problems.append({"pool": p, "gauge": g,
                             "reason": f"gauge.pool() round-trip mismatch: {back}"})
        rows.append((
            p, g,
            "Voter.gauges(pool) eth_call @latest; verified gauge.pool()==pool"
            if ok else "Voter.gauges(pool) eth_call @latest; ROUND-TRIP FAILED",
            gfactory.get(g) or "", grewardtok.get(g) or "", gnft.get(g) or "",
            1 if galive.get(g) else 0,
            1 if ok else 0,
        ))

    C.ch("DROP TABLE IF EXISTS census.aero_gauge_map")
    C.ch("""
    CREATE TABLE IF NOT EXISTS census.aero_gauge_map (
        pool String,
        gauge String,
        resolved_via String,
        gauge_factory String,
        reward_token String,
        nft_manager String,
        is_alive UInt8,
        roundtrip_ok UInt8,
        built_at DateTime DEFAULT now()
    ) ENGINE = MergeTree ORDER BY pool
    """)
    n = C.ch_insert_tsv(
        "census.aero_gauge_map",
        ["pool", "gauge", "resolved_via", "gauge_factory", "reward_token",
         "nft_manager", "is_alive", "roundtrip_ok"], rows)
    print(f"inserted {n} rows into census.aero_gauge_map")

    from collections import Counter
    print("gauge_factory distribution:", Counter(gfactory.values()))
    print("reward_token distribution:", Counter(grewardtok.values()))
    print("nft manager distribution:", Counter(gnft.values()))
    print("is_alive:", Counter(galive.values()))
    C.save_json("gauge_map_problems.json", problems)
    print(f"problems: {len(problems)}")
    for pr in problems[:20]:
        print("  ", pr)
    print("rpc stats:", R.STATS)


if __name__ == "__main__":
    main()
