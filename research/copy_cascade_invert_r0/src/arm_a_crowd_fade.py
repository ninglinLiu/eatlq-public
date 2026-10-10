"""Arm A: fade a copy cascade after PIT fame. Today's board is not historical fame."""
from __future__ import annotations

import random
from collections import defaultdict

from src.common import CONFIG, ROOT, mean, median, read_json, top1_removed_mean, utc_now_iso, write_json
from src.tape import load_closed_trades, load_entries


def _pit_pnl(closed: list[dict], t) -> dict[str, float]:
    pnl = defaultdict(float)
    if t is None:
        return {}
    for tr in closed:
        xt = tr.get("exit_time")
        if xt is not None and xt < t:
            pnl[tr["wallet"]] += tr.get("pnl_weth") or 0.0
    return dict(pnl)


def running_pit(closed: list[dict]):
    """Yield (exit_time, wallet, cumulative_pnl_map) after applying each close."""
    ordered = sorted(
        [tr for tr in closed if tr.get("exit_time")],
        key=lambda tr: tr["exit_time"],
    )
    return ordered


def _famous_set(pnl: dict[str, float], top_n: int) -> set[str]:
    ranked = sorted(pnl.items(), key=lambda kv: kv[1], reverse=True)
    return {w for w, v in ranked[:top_n] if v > 0}


def _fade_return(entry: dict) -> float | None:
    r30 = entry.get("copy_delay_30s")
    r5 = entry.get("future_return_5m")
    if r30 is None or r5 is None:
        return None
    if r30 <= -0.999:
        return None
    long_after = (1.0 + r5) / (1.0 + r30) - 1.0
    return -long_after


def _signals(entries, closed, *, permute_seed: int | None = None) -> list[float]:
    wallets = [e["wallet"] for e in entries]
    if permute_seed is not None:
        rng = random.Random(permute_seed)
        perm = wallets[:]
        rng.shuffle(perm)
        remap = dict(zip(wallets, perm))
        entries = [{**e, "wallet": remap[e["wallet"]]} for e in entries]

    top_n = int(CONFIG["fame_top_n"])
    dt = float(CONFIG["cascade_dt_sec"])
    k_min = int(CONFIG["cascade_min_followers"])
    size_ratio = float(CONFIG["follower_size_ratio"])
    rets = []
    by_pool = defaultdict(list)
    for i, e in enumerate(entries):
        if e.get("pool") and e.get("entry_time"):
            by_pool[e["pool"]].append(i)

    closes = running_pit(closed)
    pnl: dict[str, float] = defaultdict(float)
    c_i = 0
    indexed = [(i, e) for i, e in enumerate(entries) if e.get("entry_time") and e.get("pool")]
    indexed.sort(key=lambda p: p[1]["entry_time"])

    for i, e in indexed:
        t = e["entry_time"]
        while c_i < len(closes) and closes[c_i]["exit_time"] < t:
            tr = closes[c_i]
            pnl[tr["wallet"]] += tr.get("pnl_weth") or 0.0
            c_i += 1
        famous = _famous_set(pnl, top_n)
        if e["wallet"] not in famous:
            continue
        src_sz = e.get("amount_weth") or 0.0
        followers = 0
        for j in by_pool[e["pool"]]:
            if j == i:
                continue
            o = entries[j]
            ot = o.get("entry_time")
            if ot is None:
                continue
            delta = (ot - t).total_seconds()
            if 0 < delta <= dt and o["wallet"] != e["wallet"]:
                oz = o.get("amount_weth") or 0.0
                if src_sz <= 0 or oz <= src_sz * size_ratio:
                    followers += 1
        if followers < k_min:
            continue
        fr = _fade_return(e)
        if fr is not None:
            rets.append(fr)
    return rets


def _window_means(entries, closed) -> list[float]:
    """Split tape in half by time; return powered window means."""
    timed = [e for e in entries if e.get("entry_time")]
    if len(timed) < 4:
        return []
    timed = sorted(timed, key=lambda e: e["entry_time"])
    mid = timed[len(timed) // 2]["entry_time"]
    left = [e for e in timed if e["entry_time"] < mid]
    right = [e for e in timed if e["entry_time"] >= mid]
    out = []
    for part in (left, right):
        r = _signals(part, closed)
        if len(r) >= 3:
            m = mean(r)
            if m is not None:
                out.append(m)
    return out


def run_arm_a() -> dict:
    entries = load_entries()
    closed = load_closed_trades()
    boards = []
    bp = ROOT / "data/normalized/boards.json"
    if bp.exists():
        boards = read_json(bp).get("rows") or []
    captured = None
    if boards:
        captured = max((r.get("captured_at") or "") for r in boards)

    rets = _signals(entries, closed)
    perm = _signals(entries, closed, permute_seed=int(CONFIG["seed"]))
    windows = _window_means(entries, closed)

    n = len(rets)
    m = mean(rets)
    gates = {
        "mean_gt_0": bool(m is not None and m > 0 and n >= int(CONFIG["min_powered_n"])),
        "top1_removed_gt_0": bool(
            top1_removed_mean(rets) is not None and top1_removed_mean(rets) > 0 and n >= int(CONFIG["min_powered_n"])
        ),
        "permute_weaker": bool(m is not None and mean(perm) is not None and m > mean(perm)),
        "two_windows": len(windows) >= 2 and all(w > 0 for w in windows),
        "not_using_fame_now_as_history": True,
    }
    passed = all(gates.values())
    payload = {
        "generated_at": utc_now_iso(),
        "arm": "A_CROWD_FADE",
        "n_signals": n,
        "mean": m,
        "median": median(rets),
        "top1_removed_mean": top1_removed_mean(rets),
        "permute_mean": mean(perm),
        "permute_n": len(perm),
        "window_means": windows,
        "gates": gates,
        "passed": passed,
        "fame_now_captured_at": captured,
        "prospective_on_this_tape": "INCONCLUSIVE_NO_FORWARD_TAPE",
        "note": "PIT fame from our closed FIFO before T. Vendor snapshot is fame_now only.",
        "same_direction_forbidden_for_board_wallets": True,
    }
    write_json(ROOT / "out/arm_a.json", payload)
    return payload


if __name__ == "__main__":
    p = run_arm_a()
    print(p["n_signals"], p["mean"], p["passed"])
