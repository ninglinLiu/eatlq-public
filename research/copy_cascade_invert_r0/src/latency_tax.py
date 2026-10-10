"""Delay-tax curve on overlapping wallets. Next-real-swap / copy_delay columns. No RPC."""
from __future__ import annotations

from src.common import CONFIG, ROOT, mean, median, read_json, top1_removed_mean, utc_now_iso, write_json
from src.tape import load_b5_for_pools, load_closed_trades, load_entries, next_tick_return, wallet_fifo


def _board_addresses() -> set[str]:
    path = ROOT / "data/normalized/boards.json"
    if not path.exists():
        return set()
    return {r["address"] for r in read_json(path).get("rows", [])}


def run_tax() -> dict:
    entries = load_entries()
    fifo = wallet_fifo(load_closed_trades())
    boards = _board_addresses()
    # Overlap preferred; if boards empty, still compute tape-wide tax as diagnostic
    overlap_entries = [e for e in entries if e["wallet"] in boards] if boards else []
    use = overlap_entries if overlap_entries else entries
    subset = "vendor_overlap" if overlap_entries else "tape_all_no_vendor_overlap"

    delays = list(CONFIG["delays_sec"])
    pools = {e["pool"] for e in use if e.get("pool")}
    b5 = load_b5_for_pools(pools)

    curves = {}
    for d in delays:
        vals = []
        src_vals = []
        for e in use:
            src = e.get("realized_wallet_pnl_weth")
            if src is None:
                w = fifo.get(e["wallet"])
                src = w["pnl_weth"] if w and w["n_closed"] else None
            delayed = None
            if d == 30:
                delayed = e.get("copy_delay_30s")
            elif d == 60:
                delayed = e.get("copy_delay_60s")
            elif d == 120:
                delayed = e.get("copy_delay_120s")
            elif d == 0:
                delayed = e.get("future_return_5m")  # unrealizable upper-bound proxy, labeled
            if delayed is None and e.get("pool") and e.get("entry_time"):
                delayed = next_tick_return(b5.get(e["pool"], []), e["entry_time"], d)
            if delayed is None:
                continue
            vals.append(delayed)
            if src is not None:
                src_vals.append(src)
        tax = None
        if src_vals and vals and len(src_vals) == len(vals):
            tax = mean(src_vals) - mean(vals)
        elif src_vals and vals:
            tax = mean(src_vals) - mean(vals)
        curves[str(d)] = {
            "delay_sec": d,
            "n": len(vals),
            "mean_delayed_return": mean(vals),
            "median_delayed_return": median(vals),
            "top1_removed_mean": top1_removed_mean(vals),
            "mean_source_episode_pnl_weth": mean(src_vals) if src_vals else None,
            "mean_tax_source_minus_delayed": tax,
            "unrealizable": d in CONFIG.get("unrealizable_delay_sec", []),
        }

    reachable = CONFIG["primary_reachable_delay_sec"]
    min_n = int(CONFIG["min_powered_n"])
    r = curves.get(str(reachable)) or {}
    src = r.get("mean_source_episode_pnl_weth")
    delayed = r.get("mean_delayed_return")
    n_r = int(r.get("n") or 0)
    copy_arm = "OPEN_FOR_TEST"
    if n_r == 0:
        copy_arm = "INCONCLUSIVE_NO_OVERLAP"
    elif n_r < min_n:
        copy_arm = "INCONCLUSIVE_SMALL_N"
    elif delayed is not None and delayed <= 0:
        copy_arm = "CLOSED_TAX_EATS_EDGE"
    elif src is not None and delayed is not None and (src - delayed) >= max(src, 0):
        copy_arm = "CLOSED_TAX_EATS_EDGE"

    payload = {
        "generated_at": utc_now_iso(),
        "rpc_used": False,
        "subset": subset,
        "n_entries_considered": len(use),
        "n_vendor_overlap_entries": len(overlap_entries),
        "n_board_addresses": len(boards),
        "curves": curves,
        "primary_reachable_delay_sec": reachable,
        "same_direction_copy_arm": copy_arm,
        "note": "0s / instant labels are unrealizable upper bounds. Atomic sell-sim skipped: INCONCLUSIVE_NO_RPC.",
        "inconclusive_no_rpc": True,
    }
    write_json(ROOT / "out/latency_tax.json", payload)
    return payload


if __name__ == "__main__":
    p = run_tax()
    print(p["same_direction_copy_arm"], p["subset"], p["n_entries_considered"])
