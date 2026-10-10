#!/usr/bin/env python3
"""Aggregate the sharded labeled-universe outputs into `_labeled_universe.json`
and quantify survivorship bias: true rug prevalence in the full p-gated candidate
universe vs the traded-survivor (131/217) ruler.
"""
from __future__ import annotations
import glob
import json
import os
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))


def load_shards():
    pools = {}
    for f in sorted(glob.glob(os.path.join(HERE, "_uni_shards", "_uni_s*.json"))):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        for r in d.get("pools", []):
            pools[r["pool"]] = r
    return list(pools.values())


def mech(r):
    if r["label"] != "delayed_rug":
        return None
    # backward-compat: derive mechanism from depth if field missing
    if r.get("mechanism"):
        return r["mechanism"]
    fw = r.get("flip_pool_weth"); ew = r.get("entry_pool_weth")
    if ew and fw is not None and fw >= 0.5 * ew:
        return "transfer_toggle"
    return "lp_pull"


def main():
    meta = json.load(open(os.path.join(HERE, "_universe_blocks.json")))["meta"]
    rows = load_shards()
    by_pool = {r["pool"]: r for r in rows}
    uni = {r["pool"]: r for r in json.load(open(os.path.join(HERE, "_universe_blocks.json")))["rows"]}
    priced = json.load(open(os.path.join(HERE, "_priced_live.json")))

    labels = Counter(r["label"] for r in rows)
    mechs = Counter(mech(r) for r in rows if r["label"] == "delayed_rug")

    # ---- flip-timing analysis: would our trailing-stop exit have beaten the rug? ----
    # A pool only ruins US if it becomes untradeable AT OR BEFORE our actual exit
    # block (exb, from the QuoterV2 trailing-stop forward path). A late LP pull
    # (e.g. +7200) usually post-dates an earlier trailing-stop exit -> not our loss.
    EARLY_BLOCKS = 300  # ~10 min: before any realistic stop can fire = unavoidable trap
    flip_off = Counter()
    early_traps = []      # flip within ~10min of entry (unavoidable)
    before_exit = []      # flip at/before our actual exit block
    after_exit = []       # flip after we'd have exited (not our loss)
    no_exb = []
    for r in rows:
        if r["label"] != "delayed_rug":
            continue
        off = r.get("flip_offset")
        flip_off[off] += 1
        if off is not None and off <= EARLY_BLOCKS:
            early_traps.append(r)
        exb = (priced.get(r["pool"]) or {}).get("exb")
        fb = r.get("flip_block")
        if exb and fb:
            (before_exit if fb <= exb else after_exit).append(r)
        else:
            no_exb.append(r)

    # rug = a pool the strategy could enter but could NOT exit (instant_hp + delayed_rug).
    # 'dead' = unbuyable at entry (strategy never holds it) -> excluded from "we'd lose".
    enterable = [r for r in rows if r["label"] in ("clean", "instant_hp", "delayed_rug")]
    rugs = [r for r in rows if r["label"] in ("instant_hp", "delayed_rug")]
    n_enter = len(enterable)
    rug_rate = len(rugs) / n_enter if n_enter else 0.0

    # survivor subset (the 131/217 ruler): pools that were priced + friction-ok
    surv = [r for r in rows if uni.get(r["pool"], {}).get("in_traded_131_217")]
    surv_enter = [r for r in surv if r["label"] in ("clean", "instant_hp", "delayed_rug")]
    surv_rugs = [r for r in surv if r["label"] in ("instant_hp", "delayed_rug")]
    surv_rate = len(surv_rugs) / len(surv_enter) if surv_enter else 0.0

    # hidden subset (p-gated but rejected by QuoterV2/friction ruler)
    hidden = [r for r in rows if not uni.get(r["pool"], {}).get("in_traded_131_217")]
    hidden_rugs = [r for r in hidden if r["label"] in ("instant_hp", "delayed_rug")]

    # early (unavoidable) trap rate = instant_hp + early delayed_rug
    n_early_trap = labels.get("instant_hp", 0) + len(early_traps)
    out = {
        "meta": meta,
        "n_labeled": len(rows),
        "label_distribution": dict(labels),
        "delayed_rug_mechanism": dict(mechs),
        "delayed_rug_flip_offset": dict(flip_off),
        "flip_timing_vs_our_exit": {
            "flip_before_or_at_exit (our loss)": len(before_exit),
            "flip_after_exit (we exited first)": len(after_exit),
            "no_exb (unpriced/untraded)": len(no_exb),
        },
        "candidate_universe": {
            "n_enterable": n_enter, "n_rug": len(rugs),
            "rug_rate_blind_hold": round(rug_rate, 4),
            "n_instant_hp": labels.get("instant_hp", 0),
            "n_delayed_rug": labels.get("delayed_rug", 0),
            "n_early_unavoidable_trap (<=300 blk)": n_early_trap,
            "early_trap_rate": round(n_early_trap / n_enter, 4) if n_enter else None,
            "n_rug_before_our_exit": len(before_exit) + labels.get("instant_hp", 0),
        },
        "traded_survivor_ruler_131_217": {
            "n_enterable": len(surv_enter), "n_rug": len(surv_rugs),
            "rug_rate": round(surv_rate, 4),
        },
        "hidden_by_quoterv2_friction": {
            "n": len(hidden), "n_rug": len(hidden_rugs),
            "rug_rate": round(len(hidden_rugs) / len(hidden), 4) if hidden else None,
        },
        "rug_pools": [
            {"pool": r["pool"], "token": uni.get(r["pool"], {}).get("token"),
             "label": r["label"], "mechanism": mech(r), "flip_block": r.get("flip_block"),
             "flip_offset": r.get("flip_offset"), "in_traded": uni.get(r["pool"], {}).get("in_traded_131_217")}
            for r in rugs
        ],
    }
    json.dump(out, open(os.path.join(HERE, "_labeled_universe.json"), "w"), indent=2)

    print("#### LABELED UNIVERSE SUMMARY ####")
    print(f"labeled {len(rows)} / candidate {meta['n_candidate_universe']}")
    print(f"labels: {dict(labels)}")
    print(f"delayed_rug mechanism: {dict(mechs)}")
    print(f"\nCANDIDATE UNIVERSE rug rate (instant_hp+delayed_rug / enterable):")
    print(f"  {len(rugs)}/{n_enter} = {rug_rate:.1%}")
    print(f"TRADED-SURVIVOR ruler (131/217 basis) rug rate:")
    print(f"  {len(surv_rugs)}/{len(surv_enter)} = {surv_rate:.1%}")
    print(f"HIDDEN (p-gated, QuoterV2/friction-rejected) rug rate:")
    print(f"  {len(hidden_rugs)}/{len(hidden)}")
    print(f"\n=> survivorship bias hid {len(rugs)-len(surv_rugs)} additional rugs "
          f"({surv_rate:.1%} survivor view vs {rug_rate:.1%} blind-hold candidate view)")
    print(f"\n--- FLIP TIMING (the number that matters for OUR PnL) ---")
    print(f"delayed_rug flip_offset dist: {dict(flip_off)}")
    print(f"early unavoidable traps (<=300 blk, incl instant_hp): {n_early_trap}/{n_enter} = "
          f"{n_early_trap/n_enter:.1%}" if n_enter else "")
    print(f"rug flips BEFORE/AT our exit block (real loss): {len(before_exit)} | "
          f"AFTER our exit (we'd have exited first): {len(after_exit)} | no exb: {len(no_exb)}")


if __name__ == "__main__":
    main()
