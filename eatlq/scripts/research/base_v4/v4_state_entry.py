#!/usr/bin/env python3
"""State-transition entry: buy confirmed demand wave 2, not Initialize/first_swap.

Mirrors V3 live MIN_PRE spirit but targets *second-wave confirmation*:
  - wave1: first activity burst after first swap
  - survival: swaps continue after wave1
  - wave2: new WETH-in buyers + price recovery from wave1 low
  - safety: no LP pull in wave1; deployer not net dumping in wave1

Then roundtrip PnL @ state_entry_block (anvil, 0 counter RPC).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from live_gate import executor_healthy, wait_for_live_clear
from v4_alpha_readout import (
    ALT_ENTRY_CACHE,
    PNL_CACHE,
    SPEND_ETH,
    SPEND_WEI,
    SWAP_INDEX,
    _load_json,
    _pool_meta,
    _strong_labels,
    _summarize_pnls,
    pnl_frac,
)
from v4_fork_replay import replay_at_block
from v4_swap_timeline import (
    DEFAULT_HORIZON,
    DEPLOYER_CACHE,
    TIMELINE_PATH,
    build_timelines,
    ensure_deployers,
    weth_is_currency0,
)

OUT_SPEC = os.path.join(HERE, "_state_entry_spec.md")
OUT_DETECT = os.path.join(HERE, "_state_entry_detect.json")
OUT_PNL = os.path.join(HERE, "_state_entry_pnl_cache.json")
OUT_SUMMARY = os.path.join(HERE, "_state_entry_summary.json")

# Wave detection defaults (tunable)
WAVE1_BLOCKS = 300
MIN_WAVE1_SWAPS = 3
MIN_PRE_SWAPS = 8          # V3 MIN_PRE=5 analogue, stricter for wave2
MIN_WAVE2_BUYERS = 2       # distinct WETH-in senders after wave1
PRICE_RECOVERY = 0.02      # sqrtPrice >= wave1_low * (1+2%)
MAX_DEPLOYER_WETH_SELLS = 2  # deployer weth_out swaps in wave1
PNL_OFFSETS = [0, 30, 300, 1800, 7200]

KNOWN_ROUTERS = {
    "0x6ff5693b99212da76ad316178b5686813e6c9482",  # Universal Router
    "0x2626664c2603336E57B271c5C0b26F421741e481",
    "0x0000000000000000000000000000000000000000",
}


def _sqrt_price(row: dict) -> int:
    return int(row.get("sqrt_price_x96") or 0)


def detect_wave2_entry(
    timeline: dict,
    *,
    init_from: Optional[str] = None,
    wave1_blocks: int = WAVE1_BLOCKS,
) -> dict:
    swaps = timeline.get("swaps") or []
    mods = timeline.get("modify_liquidity") or []
    if len(swaps) < MIN_WAVE1_SWAPS:
        return {"ok": False, "reason": "too_few_swaps", "n_swaps": len(swaps)}

    first_swap_blk = swaps[0]["block"]
    wave1_end = first_swap_blk + wave1_blocks
    wave1 = [s for s in swaps if first_swap_blk <= s["block"] <= wave1_end]
    if len(wave1) < MIN_WAVE1_SWAPS:
        return {
            "ok": False,
            "reason": "wave1_thin",
            "n_wave1": len(wave1),
            "first_swap_block": first_swap_blk,
        }

    wave1_low = min(_sqrt_price(s) for s in wave1 if _sqrt_price(s) > 0) or 0
    if wave1_low <= 0:
        return {"ok": False, "reason": "no_wave1_price", "first_swap_block": first_swap_blk}

    lp_pull_wave1 = any(m["remove"] for m in mods if m["block"] <= wave1_end)
    if lp_pull_wave1:
        return {"ok": False, "reason": "lp_pull_wave1", "first_swap_block": first_swap_blk}

    if init_from:
        deployer_sells = sum(
            1 for s in wave1
            if s.get("weth_out") and s.get("sender", "").lower() == init_from.lower()
        )
        if deployer_sells > MAX_DEPLOYER_WETH_SELLS:
            return {
                "ok": False,
                "reason": "deployer_dump_wave1",
                "deployer_sells": deployer_sells,
                "first_swap_block": first_swap_blk,
            }

    post = [s for s in swaps if s["block"] > wave1_end]
    if not post:
        return {"ok": False, "reason": "dead_after_wave1", "first_swap_block": first_swap_blk}

    recovery_thr = int(wave1_low * (1 + PRICE_RECOVERY))
    wave2_buyers: set[str] = set()
    entry_block = None
    for s in post:
        if s.get("weth_in") and s.get("sender", "").lower() not in KNOWN_ROUTERS:
            wave2_buyers.add(s["sender"].lower())
        pre = [x for x in swaps if x["block"] <= s["block"]]
        price_ok = _sqrt_price(s) >= recovery_thr
        if (
            len(pre) >= MIN_PRE_SWAPS
            and len(wave2_buyers) >= MIN_WAVE2_BUYERS
            and price_ok
        ):
            entry_block = s["block"]
            break

    if entry_block is None:
        return {
            "ok": False,
            "reason": "no_wave2_confirmation",
            "first_swap_block": first_swap_blk,
            "wave1_end": wave1_end,
            "n_post_wave1": len(post),
            "wave2_buyers_seen": len(wave2_buyers),
        }

    return {
        "ok": True,
        "reason": "wave2_confirmed",
        "entry_block": entry_block,
        "first_swap_block": first_swap_blk,
        "init_to_first_swap": first_swap_blk - int(timeline.get("from_block", first_swap_blk)),
        "init_to_entry": entry_block - int(timeline.get("from_block", entry_block)),
        "wave1_swaps": len(wave1),
        "wave2_buyers": len(wave2_buyers),
        "pre_swaps_at_entry": len([x for x in swaps if x["block"] <= entry_block]),
        "price_recovery_pct": round((_sqrt_price(next(s for s in post if s["block"] >= entry_block)) / wave1_low - 1) * 100, 2),
    }


def detect_all(
    pool_ids: Optional[set[str]] = None,
    *,
    build: bool = True,
) -> dict:
    meta = _pool_meta()
    strong = _strong_labels()
    targets = pool_ids or {p for p, r in strong.items() if r.get("label") == "clean"}
    rows = {pid: meta[pid] for pid in targets if pid in meta}

    if build:
        ensure_deployers(rows)
        swap_index = _load_json(SWAP_INDEX).get("pools", {}) if os.path.exists(SWAP_INDEX) else {}
        build_timelines(rows, horizon=DEFAULT_HORIZON, swap_index=swap_index)

    timeline = _load_json(TIMELINE_PATH)
    deployers = _load_json(DEPLOYER_CACHE).get("pools", {})
    results = []
    reason_ctr = Counter()
    for pid in sorted(targets):
        if pid not in timeline.get("pools", {}):
            continue
        tl = timeline["pools"][pid]
        init_from = (deployers.get(pid) or {}).get("init_from")
        det = detect_wave2_entry(tl, init_from=init_from)
        det["poolId"] = pid
        det["strong_label"] = strong.get(pid, {}).get("label")
        det["hook_template"] = strong.get(pid, {}).get("hook_template")
        results.append(det)
        reason_ctr[det.get("reason") or det.get("ok")] += 1

    out = {
        "config": {
            "wave1_blocks": WAVE1_BLOCKS,
            "min_wave1_swaps": MIN_WAVE1_SWAPS,
            "min_pre_swaps": MIN_PRE_SWAPS,
            "min_wave2_buyers": MIN_WAVE2_BUYERS,
            "price_recovery": PRICE_RECOVERY,
            "horizon_blocks": DEFAULT_HORIZON,
        },
        "n_pools": len(results),
        "n_confirmed": sum(1 for r in results if r.get("ok")),
        "reason_counts": dict(reason_ctr),
        "pools": results,
    }
    json.dump(out, open(OUT_DETECT, "w"), indent=2)
    return out


def refresh_state_pnl(*, incremental: bool = True) -> dict:
    if not executor_healthy() or not wait_for_live_clear(max_wait_s=120):
        raise RuntimeError("live gate blocked")
    detect = _load_json(OUT_DETECT) if os.path.exists(OUT_DETECT) else detect_all(build=False)
    meta = _pool_meta()
    cache = _load_json(OUT_PNL) if incremental and os.path.exists(OUT_PNL) else {"pools": {}}
    pools_cache = cache.setdefault("pools", {})
    done = 0
    t0 = time.time()
    for row in detect.get("pools", []):
        if not row.get("ok"):
            continue
        pid = row["poolId"]
        pool_row = meta.get(pid)
        if not pool_row:
            continue
        entry = int(row["entry_block"])
        rec = pools_cache.setdefault(pid, {
            "poolId": pid,
            "entry_mode": "state_wave2",
            "entry_block": entry,
            "detection": row,
            "spend_wei": SPEND_WEI,
            "pnl_by_offset": {},
        })
        for off in PNL_OFFSETS:
            key = str(off)
            if incremental and key in rec.get("pnl_by_offset", {}):
                continue
            blk = entry + off
            rep = replay_at_block(blk, pool_row, SPEND_WEI)
            pf = pnl_frac(rep)
            rec["pnl_by_offset"][key] = {
                "offset": off,
                "block": blk,
                "pnl_frac": round(pf, 5),
                "buy_ok": rep.get("buy_ok"),
                "sell_ok": rep.get("sell_ok"),
            }
            done += 1
            if done % 10 == 0:
                json.dump(cache, open(OUT_PNL, "w"), indent=2)
                print(f"  state-entry pnl {done} replays {int(time.time()-t0)}s", flush=True)
    cache["meta"] = {"entry_mode": "state_wave2", "offsets": PNL_OFFSETS, "spend_eth": SPEND_ETH}
    json.dump(cache, open(OUT_PNL, "w"), indent=2)
    return cache


def summarize() -> dict:
    detect = _load_json(OUT_DETECT)
    pnl_cache = _load_json(OUT_PNL)
    alt = _load_json(ALT_ENTRY_CACHE)
    init = _load_json(PNL_CACHE)

    confirmed = [r for r in detect.get("pools", []) if r.get("ok")]
    entry_pnls = []
    by_tmpl = defaultdict(list)
    comparisons = []
    for r in confirmed:
        pid = r["poolId"]
        pnl_row = pnl_cache.get("pools", {}).get(pid, {}).get("pnl_by_offset", {}).get("0")
        if not pnl_row:
            continue
        pf = pnl_row["pnl_frac"]
        entry_pnls.append(pf)
        tmpl = r.get("hook_template") or "none"
        by_tmpl[tmpl].append(pf)
        fs = alt.get("pools", {}).get(pid, {}).get("first_swap_pnl_by_offset", {}).get("0", {})
        ini = init.get("pools", {}).get(pid, {}).get("pnl_by_offset", {}).get("0", {})
        comparisons.append({
            "poolId": pid,
            "state_entry_pnl": pf,
            "first_swap_pnl": fs.get("pnl_frac"),
            "initialize_pnl": ini.get("pnl_frac"),
            "init_to_entry_blocks": r.get("init_to_entry"),
        })

    summary = {
        "detection": {
            "n_clean": detect.get("n_pools"),
            "n_confirmed": detect.get("n_confirmed"),
            "reason_counts": detect.get("reason_counts"),
        },
        "state_wave2_entry": _summarize_pnls(entry_pnls),
        "by_template": {k: _summarize_pnls(v) for k, v in sorted(by_tmpl.items())},
        "vs_early_entry": {
            "n_with_all_three": len(comparisons),
            "median_delta_vs_first_swap": round(
                float(__import__("numpy").median([
                    c["state_entry_pnl"] - (c["first_swap_pnl"] or 0)
                    for c in comparisons
                    if c["state_entry_pnl"] is not None and c["first_swap_pnl"] is not None
                ])),
                4,
            ) if comparisons else None,
        },
        "comparisons": comparisons[:20],
    }
    json.dump(summary, open(OUT_SUMMARY, "w"), indent=2)
    print(json.dumps(summary, indent=2))
    return summary


def write_spec() -> None:
    text = """# V4 State-Transition Entry (Demand Wave 2)

## Problem with current entry

Initialize / first_swap answers: *"Can I roundtrip at pool birth?"*
That measures **hook friction at genesis**, not **demand migration**.

## Target question

*"After natural selection, did a second cohort pay a higher price?"*

## Observable proxies (archive RPC + anvil)

| Signal | Proxy | Source |
|--------|-------|--------|
| Not pure friction | roundtrip PnL improves vs early entry | anvil replay |
| Survived wave1 sell pressure | swaps continue after wave1 window | Swap logs |
| Second-wave demand | ≥2 distinct WETH-in senders post-wave1 | Swap logs |
| Price recovery | sqrtPrice ≥ wave1_low + 2% | Swap logs |
| LP held | no ModifyLiquidity remove in wave1 | ModifyLiquidity logs |
| Deployer not dumping | init tx.from WETH-out ≤ 2 in wave1 | tx.from + Swap |
| Routing / bot quality | exclude Universal Router from buyer count | sender filter |

## Entry block

First block where all wave2 gates pass (see `v4_state_entry.py`).

## Not yet modeled (future)

- Aggregator route quality score
- Non-bot buyer classifier (nonce / funding graph)
- Tick-path trail exit (reuse V3 path once entry timing fixed)

## Repro

```bash
cd /opt/EATLQ/scripts/research/base_v4
python3 live_gate.py --check-open-pools
bash run_in_cgroup.sh python3 v4_state_entry.py --detect --pnl
python3 v4_state_entry.py --summarize
```

## v1 results (112 clean, 2026-06-25)

| Stage | n | Note |
|-------|---|------|
| wave2 confirmed | **7 / 112** (6%) | entry ~500–5000 blocks after init |
| too_few_swaps | 63 | swap index only sees recent activity window |
| no_wave2 / dead / lp_pull | 42 | filtered by design |

**PnL @ state entry:** median **−2.4%**, positive **0%** (n=7).  
vs first_swap: median delta **0** — later entry does not create upside on this sample.
"""
    open(OUT_SPEC, "w").write(text)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--detect", action="store_true", help="build timeline + detect wave2 entry")
    ap.add_argument("--pnl", action="store_true", help="anvil roundtrip at state entry (0 counter)")
    ap.add_argument("--summarize", action="store_true")
    ap.add_argument("--write-spec", action="store_true")
    ap.add_argument("--no-build", action="store_true", help="detect from cached timeline only")
    args = ap.parse_args()

    if args.write_spec or not any([args.detect, args.pnl, args.summarize]):
        write_spec()
        print(f"Wrote {OUT_SPEC}")

    if args.detect:
        det = detect_all(build=not args.no_build)
        print(json.dumps({"n_confirmed": det["n_confirmed"], "reasons": det["reason_counts"]}, indent=2))

    if args.pnl:
        refresh_state_pnl()
        print(f"Wrote {OUT_PNL}")

    if args.summarize:
        summarize()

    return 0


if __name__ == "__main__":
    sys.exit(main())
