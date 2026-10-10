"""Wave2 final detection + forward tick/trail metrics (no lookahead)."""
from __future__ import annotations

import math
from typing import Optional

from v4_swap_timeline import weth_is_currency0

# --- timing ---
WAVE1_BLOCKS = 300
FORWARD_HORIZON = 7200
ENTRY_LAG = 1  # entry_block = signal_block + 1

# --- wave2 gates (final) ---
MIN_WAVE1_SWAPS = 3
MIN_PRE_SWAPS = 8
MIN_WAVE2_BUYERS = 2
MIN_NET_WETH_IN_WEI = int(0.005 * 1e18)  # signed flow post-wave1
PRICE_RECOVERY = 0.02
MAX_DEPLOYER_WETH_SELLS = 2
BUYER_HOLD_BLOCKS = 50
MIN_WAVE2_NEW_BUYERS = 1
MIN_WAVE1_RETAINED = 1

KNOWN_ROUTERS = {
    "0x6ff5693b99212da76ad316178b5686813e6c9482",
    "0x2626664c2603336E57B271c5C0b26F421741e481",
    "0x0000000000000000000000000000000000000000",
}

# --- exit / cost (V3 mirror) ---
TRAIL = 0.25
HARD_STOP = math.log(0.5)
GAS_USD_RT = 0.15
STAKE_USD = 50.0
HAIRCUT = 0.02

PATH_OFFSETS = [0, 30, 300, 600, 1200, 1800, 3600, 7200]


def _sqrt_price(row: dict) -> int:
    return int(row.get("sqrt_price_x96") or 0)


def weth_volume_wei(swap: dict, weth_c0: bool) -> int:
    amt = swap["amount0"] if weth_c0 else swap["amount1"]
    return abs(int(amt))


def net_weth_flow(swaps: list[dict], weth_c0: bool) -> int:
    net = 0
    for s in swaps:
        vol = weth_volume_wei(s, weth_c0)
        if s.get("weth_in"):
            net += vol
        elif s.get("weth_out"):
            net -= vol
    return net


def mark_ret(entry_sp: int, cur_sp: int, weth_c0: bool) -> float:
    if entry_sp <= 0 or cur_sp <= 0:
        return 0.0
    if weth_c0:
        return (cur_sp / entry_sp) ** 2 - 1.0
    return (entry_sp / cur_sp) ** 2 - 1.0


def adj_ret(raw: float) -> float:
    return float(raw) - GAS_USD_RT / STAKE_USD - HAIRCUT


def simulate_trail(path: list[tuple[int, float]], trail: float = TRAIL) -> dict:
    """path: [(block, mark_ret), ...] sorted by block."""
    if not path:
        return {"trail_ret": None, "exit_block": None, "exit_why": "empty", "mfe": None, "mae": None}
    rets = [r for _, r in path]
    mfe = max(rets)
    mae = min(rets)
    peak_log = -1e9
    entry_log = math.log(max(1e-9, 1 + path[0][1]))
    thr = math.log(1 - trail)
    exit_blk, exit_ret = path[0]
    why = "time_exit"
    for blk, ret in path:
        v = math.log(max(1e-9, 1 + ret))
        peak_log = max(peak_log, v)
        if (v - entry_log) <= HARD_STOP:
            return {"trail_ret": ret, "exit_block": blk, "exit_why": "hard_stop", "mfe": mfe, "mae": mae}
        if (v - peak_log) <= thr:
            return {"trail_ret": ret, "exit_block": blk, "exit_why": "trail", "mfe": mfe, "mae": mae}
        exit_blk, exit_ret = blk, ret
    return {"trail_ret": exit_ret, "exit_block": exit_blk, "exit_why": why, "mfe": mfe, "mae": mae}


def tick_path_from_timeline(
    timeline: dict,
    entry_block: int,
    *,
    horizon: int = FORWARD_HORIZON,
    weth_c0: bool,
) -> list[tuple[int, float]]:
    swaps = timeline.get("swaps") or []
    entry_sp = None
    for s in swaps:
        if s["block"] <= entry_block and _sqrt_price(s) > 0:
            entry_sp = _sqrt_price(s)
    if entry_sp is None:
        return []
    end = entry_block + horizon
    path = [(entry_block, 0.0)]
    for s in swaps:
        b = s["block"]
        if b <= entry_block or b > end:
            continue
        sp = _sqrt_price(s)
        if sp > 0:
            path.append((b, mark_ret(entry_sp, sp, weth_c0)))
    terminal = end
    last_sp = _sqrt_price(swaps[-1]) if swaps else entry_sp
    for s in reversed(swaps):
        if s["block"] <= end and _sqrt_price(s) > 0:
            last_sp = _sqrt_price(s)
            break
    if terminal > path[-1][0]:
        path.append((terminal, mark_ret(entry_sp, last_sp, weth_c0)))
    return path


def detect_wave2_final(
    timeline: dict,
    pool_row: dict,
    *,
    init_from: Optional[str] = None,
) -> dict:
    swaps = timeline.get("swaps") or []
    mods = timeline.get("modify_liquidity") or []
    weth_c0 = weth_is_currency0(pool_row)

    if len(swaps) < MIN_WAVE1_SWAPS:
        return {"ok": False, "reason": "too_few_swaps", "n_swaps": len(swaps)}

    first_swap_blk = swaps[0]["block"]
    wave1_end = first_swap_blk + WAVE1_BLOCKS
    wave1 = [s for s in swaps if first_swap_blk <= s["block"] <= wave1_end]
    if len(wave1) < MIN_WAVE1_SWAPS:
        return {"ok": False, "reason": "wave1_thin", "n_wave1": len(wave1)}

    wave1_low = min(_sqrt_price(s) for s in wave1 if _sqrt_price(s) > 0) or 0
    if wave1_low <= 0:
        return {"ok": False, "reason": "no_wave1_price"}

    if any(m["remove"] for m in mods if m["block"] <= wave1_end):
        return {"ok": False, "reason": "lp_pull_wave1"}

    if init_from:
        ds = sum(1 for s in wave1 if s.get("weth_out") and s["sender"].lower() == init_from.lower())
        if ds > MAX_DEPLOYER_WETH_SELLS:
            return {"ok": False, "reason": "deployer_dump_wave1", "deployer_sells": ds}

    wave1_buyers = {
        s["sender"].lower() for s in wave1
        if s.get("weth_in") and s["sender"].lower() not in KNOWN_ROUTERS
    }
    wave1_flip = set()
    for b in wave1_buyers:
        buy_blk = min(s["block"] for s in wave1 if s.get("weth_in") and s["sender"].lower() == b)
        for s in swaps:
            if s["block"] > buy_blk and s["block"] <= buy_blk + BUYER_HOLD_BLOCKS:
                if s.get("weth_out") and s["sender"].lower() == b:
                    wave1_flip.add(b)
                    break
    wave1_retained = wave1_buyers - wave1_flip

    post = [s for s in swaps if s["block"] > wave1_end]
    if not post:
        return {"ok": False, "reason": "dead_after_wave1"}

    recovery_thr = int(wave1_low * (1 + PRICE_RECOVERY))
    signal_block = None
    wave2_buyers: set[str] = set()
    wave1_buyer_set = wave1_buyers

    for s in post:
        if s.get("weth_in") and s["sender"].lower() not in KNOWN_ROUTERS:
            wave2_buyers.add(s["sender"].lower())
        pre = [x for x in swaps if x["block"] <= s["block"]]
        post_w1 = [x for x in pre if x["block"] > wave1_end]
        net_in = net_weth_flow(post_w1, weth_c0)
        w2_new = {b for b in wave2_buyers if b not in wave1_buyer_set}
        router_buys = sum(1 for x in post_w1 if x.get("weth_in") and x["sender"].lower() in KNOWN_ROUTERS)
        organic_buys = sum(1 for x in post_w1 if x.get("weth_in") and x["sender"].lower() not in KNOWN_ROUTERS)
        price_ok = _sqrt_price(s) >= recovery_thr
        if (
            len(pre) >= MIN_PRE_SWAPS
            and len(wave2_buyers) >= MIN_WAVE2_BUYERS
            and len(w2_new) >= MIN_WAVE2_NEW_BUYERS
            and len(wave1_retained) >= MIN_WAVE1_RETAINED
            and net_in >= MIN_NET_WETH_IN_WEI
            and price_ok
        ):
            signal_block = s["block"]
            break

    if signal_block is None:
        return {
            "ok": False,
            "reason": "no_wave2_confirmation",
            "wave1_retained": len(wave1_retained),
            "net_weth_post_wave1": net_weth_flow(post, weth_c0),
        }

    entry_block = signal_block + ENTRY_LAG
    post_entry_lp = any(
        m["remove"] for m in mods
        if entry_block < m["block"] <= entry_block + 300
    )

    return {
        "ok": True,
        "reason": "wave2_confirmed",
        "signal_block": signal_block,
        "entry_block": entry_block,
        "entry_lag_blocks": ENTRY_LAG,
        "first_swap_block": first_swap_blk,
        "init_to_entry": entry_block - int(timeline.get("from_block", entry_block)),
        "wave1_swaps": len(wave1),
        "wave1_retained_buyers": len(wave1_retained),
        "wave2_buyers": len(wave2_buyers),
        "wave2_new_buyers": len(wave2_buyers - wave1_buyer_set),
        "net_weth_in_post_wave1": net_weth_flow(
            [x for x in swaps if wave1_end < x["block"] <= signal_block], weth_c0
        ),
        "sqrt_price_recovery_pct": round(
            (_sqrt_price(next(s for s in post if s["block"] >= signal_block)) / wave1_low - 1) * 100, 2
        ),
        "router_buys_post_wave1": sum(
            1 for x in post if x["block"] <= signal_block and x.get("weth_in")
            and x["sender"].lower() in KNOWN_ROUTERS
        ),
        "organic_buys_post_wave1": sum(
            1 for x in post if x["block"] <= signal_block and x.get("weth_in")
            and x["sender"].lower() not in KNOWN_ROUTERS
        ),
        "lp_remove_entry_plus_300": post_entry_lp,
    }


def core_table_row(rows: list[dict]) -> dict:
    import numpy as np

    if not rows:
        return {"n": 0}
    mfe = np.array([r["mfe"] for r in rows], dtype=float)
    mae = np.array([r["mae"] for r in rows], dtype=float)
    adj = np.array([r["adj_ret"] for r in rows], dtype=float)
    trail = np.array([r["trail_ret"] for r in rows], dtype=float)
    return {
        "n": len(rows),
        "median_mfe": round(float(np.median(mfe)), 4),
        "p_mfe_gt_5pct": round(float((mfe > 0.05).mean()), 3),
        "p_mfe_gt_10pct": round(float((mfe > 0.10).mean()), 3),
        "p_mfe_gt_25pct": round(float((mfe > 0.25).mean()), 3),
        "median_mae": round(float(np.median(mae)), 4),
        "median_trail_ret": round(float(np.median(trail)), 4),
        "median_adj_ret": round(float(np.median(adj)), 4),
        "trail_winrate": round(float((adj > 0).mean()), 3),
    }
