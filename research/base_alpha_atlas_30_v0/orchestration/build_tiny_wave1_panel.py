#!/usr/bin/env python3
"""Build a tiny Wave1 subset panel from local extracts only.

No ClickHouse, no RPC, no files >=50MB. nice recommended at shell.
"""
from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "normalized" / "wave1_probe"
OUT_DIR = ROOT / "shared_panel" / "tiny_wave1"
WETH = "0x4200000000000000000000000000000000000006"


def tick_to_price(tick: int) -> float:
    return 1.0001 ** tick


def load_b5_swaps(path: Path, max_rows: int = 50000):
    rows = []
    with path.open() as f:
        for i, line in enumerate(f):
            if i >= max_rows:
                break
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 8:
                continue
            pool, block, ts_ms, tx, log_index, a0, a1, tick = parts[:8]
            try:
                rows.append(
                    {
                        "pool_id": pool.lower(),
                        "block_number": int(block),
                        "event_time_ms": int(ts_ms),
                        "log_index": int(log_index),
                        "amount0": float(a0),
                        "amount1": float(a1),
                        "tick": int(tick),
                    }
                )
            except ValueError:
                continue
    return rows


def load_v3r6(path: Path, max_rows: int = 20000):
    rows = []
    with path.open(newline="") as f:
        r = csv.DictReader(f)
        for i, row in enumerate(r):
            if i >= max_rows:
                break
            try:
                rows.append(
                    {
                        "pool_id": row["pool_address"].lower(),
                        "block_number": int(row["block_number"]),
                        "event_time_ms": int(float(row["timestamp_ms"])),
                        "log_index": int(row["source_log_index"]),
                        "amount0": float(row["amount0"]),
                        "amount1": float(row["amount1"]),
                        "tick": int(float(row["tick"])),
                        "is_buy_weth_to_token": row.get("is_buy_weth_to_token") == "True",
                        "weth_leg": row.get("weth_leg"),
                    }
                )
            except (ValueError, KeyError):
                continue
    return rows


def build_1m_bars(swaps: list[dict]) -> list[dict]:
    # bucket by pool + minute
    buckets: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for s in swaps:
        minute = (s["event_time_ms"] // 1000) // 60 * 60
        buckets[(s["pool_id"], minute)].append(s)

    bars = []
    for (pool, minute), evs in sorted(buckets.items()):
        evs = sorted(evs, key=lambda x: (x["event_time_ms"], x["log_index"]))
        prices = [tick_to_price(e["tick"]) for e in evs]
        # trade sign proxy: +1 if amount of non-WETH increases via WETH in when available
        buy_n = sell_n = 0
        buy_vol = sell_vol = 0.0
        for e in evs:
            if "is_buy_weth_to_token" in e:
                if e["is_buy_weth_to_token"]:
                    buy_n += 1
                    buy_vol += abs(e.get("amount0", 0)) + abs(e.get("amount1", 0))
                else:
                    sell_n += 1
                    sell_vol += abs(e.get("amount0", 0)) + abs(e.get("amount1", 0))
            else:
                # b5 heuristic: positive amount1 often WETH out/in depending on side — mark UNKNOWN volumes
                buy_n += 1  # count only
        o, h, l, c = prices[0], max(prices), min(prices), prices[-1]
        bar_close = minute + 60
        bars.append(
            {
                "pool_id": pool,
                "event_time": bar_close,
                "bar_close_time": bar_close,
                "block_number": evs[-1]["block_number"],
                "log_index": evs[-1]["log_index"],
                "known_at": bar_close,
                "feature_available_at": bar_close,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "trade_count": len(evs),
                "buy_count": buy_n,
                "sell_count": sell_n,
                "volume_proxy": buy_vol + sell_vol,
                "buy_volume_proxy": buy_vol,
                "sell_volume_proxy": sell_vol,
            }
        )
    return bars


def write_panel(bars: list[dict], out_path: Path) -> dict:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # JSONL for simplicity without pyarrow dependency
    with out_path.open("w") as f:
        for b in bars:
            f.write(json.dumps(b, separators=(",", ":")) + "\n")
    # also CSV for easy inspection
    csv_path = out_path.with_suffix(".csv")
    if bars:
        keys = list(bars[0].keys())
        with csv_path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(bars)
    pools = sorted({b["pool_id"] for b in bars})
    manifest = {
        "panel_path": str(out_path),
        "format": "jsonl+csv",
        "n_bars": len(bars),
        "n_pools": len(pools),
        "pools_sample": pools[:20],
        "source": "normalized/wave1_probe extracts only",
        "pit_fields": ["event_time", "known_at", "feature_available_at", "bar_close_time"],
        "notes": [
            "price from tick via 1.0001**tick (token1/token0 raw); not USD",
            "volume_proxy incomplete for b5-only rows",
            "NOT a production panel; Wave1 tiny subset only",
            "backtest_enabled still false until separate authorization",
        ],
        "sha256": _sha256(out_path),
    }
    (out_path.parent / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def _sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    b5 = load_b5_swaps(PROBE / "base_b5_real_swaps_head50k.tsv")
    v3 = load_v3r6(PROBE / "run004_v3r6_candidate_pool_head20k.csv")
    # Prefer v3r6 for signed fields; add b5 for more bars
    swaps = v3 + b5
    bars = build_1m_bars(swaps)
    # Cap panel size: top pools by trade_count sum, max 30 pools, keep all their bars
    pool_counts: dict[str, int] = defaultdict(int)
    for b in bars:
        pool_counts[b["pool_id"]] += b["trade_count"]
    top = {p for p, _ in sorted(pool_counts.items(), key=lambda x: -x[1])[:30]}
    bars = [b for b in bars if b["pool_id"] in top]
    out = OUT_DIR / "base_alpha_atlas_tiny_wave1_panel.jsonl"
    manifest = write_panel(bars, out)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
