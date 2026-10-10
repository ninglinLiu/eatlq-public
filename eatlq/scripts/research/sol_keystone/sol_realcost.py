#!/usr/bin/env python3
"""Task 2 — Solana real-cost roundtrip labels (bonding-curve grid, zero RPC)."""
from __future__ import annotations

from common import (
    COST_RT,
    ENTRY_S,
    EXIT_OFFSETS,
    FATAL_STOP,
    build_universe_rows,
    load_grids,
    roundtrip_ret,
    write_json,
)


def classify_label(rets: dict[str, float | None]) -> str:
    r0 = rets.get("instant")
    r5 = rets.get("5m")
    r30 = rets.get("30m")
    if r0 is None:
        return "unsellable"
    if r0 <= -0.90 or r0 <= FATAL_STOP:
        return "instant_rug"
    if r5 is not None and r5 <= FATAL_STOP:
        return "delayed_rug"
    if r30 is not None and r30 <= -0.90:
        return "dead"
    if r0 <= -0.30:
        return "delayed_rug"
    return "clean"


def main():
    grids = load_grids()
    universe = {r["mint"]: r for r in build_universe_rows()}
    labels = []
    for mint, gwrap in grids.items():
        grid = gwrap["grid"]
        meta = universe.get(mint, {})
        entry_s = ENTRY_S
        rets = {}
        for name, off in EXIT_OFFSETS.items():
            rets[name] = roundtrip_ret(grid, entry_s, entry_s + off, COST_RT)
        primary = rets.get("4h") or rets.get("30m") or rets.get("5m")
        post = []
        for k, v in grid.items():
            try:
                t = float(k)
            except ValueError:
                continue
            if t >= entry_s and v:
                gross = roundtrip_ret(grid, entry_s, t, 0)
                if gross is not None:
                    post.append(gross)
        min_gross = min(post) if post else None
        fatal = (
            (primary is not None and primary <= FATAL_STOP)
            or (min_gross is not None and min_gross <= FATAL_STOP)
        )
        labels.append({
            "mint": mint,
            "day": meta.get("day"),
            "split": meta.get("split"),
            "entry_slot_s": entry_s,
            "venue": "pump_bonding_curve",
            "cost_rt": COST_RT,
            "returns": rets,
            "primary_return_4h": rets.get("4h"),
            "primary_return_30m": rets.get("30m"),
            "min_gross_fwd": min_gross,
            "fatal": fatal,
            "label": classify_label(rets),
            "grid_source": gwrap.get("source"),
        })
    by_label: dict[str, int] = {}
    for x in labels:
        by_label[x["label"]] = by_label.get(x["label"], 0) + 1
    summary = {
        "n": len(labels),
        "fatal_rate": sum(1 for x in labels if x["fatal"]) / len(labels) if labels else 0,
        "label_counts": by_label,
        "rpc_calls": 0,
        "note": "4h grid capped at create+7200s in cached data; 30m is primary hold horizon",
    }
    write_json("_sol_realcost_labels.json", {"summary": summary, "labels": labels})
    print(f"realcost n={len(labels)} fatal={summary['fatal_rate']:.3f} labels={by_label}")


if __name__ == "__main__":
    main()
