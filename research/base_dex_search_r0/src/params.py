"""Frozen search parameters. Written before any recipe PnL was computed."""
from itertools import combinations, product

TOTAL_USD = 1000.0
SIZES = (25, 50)
MONTHLY_TARGET = 5000.0
COST_CAP_BPS = 100.0
WETH = "0x4200000000000000000000000000000000000006"
# Paper |ret| above this is the same class as the 1e38 tick overflows
# already killed in round 46 (PRECISION_SHORT / ABTC). Drop, do not clip:
# clipping would still mint +500% fake winners.
MAX_ABS_RET = 5.0
MIN_PX = 1e-18
MAX_LINEAR_PX = 1e20

HORIZONS = ("1m", "10m", "1h", "4h", "ks4h")
HORIZON_HOURS = {"1m": None, "10m": 0.25, "1h": 1.0, "4h": 4.0, "ks4h": 4.0}
ENTRIES = ("ks_as_is", "p_fatal_loose", "p_fatal_tight", "ks_complement", "random")
EXITS = ("time", "tp_sl", "ks_50_4h", "sl_only", "time_only")
KS5 = ("ns_pre", "buyers_pre", "early_ret", "liq_log", "pre_vol")
FEATURE_SETS = [",".join(c) for n in range(1, 6) for c in combinations(KS5, n)]
FEATURE_SETS += ["flow_only", "path_only", "liq_only"]

FACTORY_NAME = {
    "0x33128a8fc17869897dce68ed026d694621f6fdfd": "univ3",
    "0x5e7bb104d84c7cb9b682aac2f3d509f5f406809a": "aero_cl",
    "0x8909dc15e40173ff4699343b6eb8132c65e18ec6": "univ2",
    "0x420dd381b31aef6683db6b902084cb0ffece40da": "aero_vamm",
    "0xc35dadb65012ec5796536bd9864ed8773abc74c4": "sushi_v3",
    "0x0fd83557b2be93617c9c1c1b6fd549401c74558c": "alienbase_cl",
    "0x498581ff718922c3f8e6a244956aF099B2652b2b".lower(): "univ4_poolmanager",
}

OUT = "/root/base_dex_search_r0/out"


def slots(size):
    return int(TOTAL_USD // size)


def enumerate_recipes(venues):
    rows = []
    rid = 0
    for venue, size, hor, entry, ex, feat in product(
            venues, SIZES, HORIZONS, ENTRIES, EXITS, FEATURE_SETS):
        rid += 1
        rows.append(dict(
            id=rid, venue=venue, size_usd=size, horizon=hor,
            entry=entry, exit=ex, features=feat,
            slots=slots(size),
        ))
    return rows
