#!/usr/bin/env python3
"""R1 attach path: primary-chain hourly volume share. No PnL. No R0 overwrite."""
from __future__ import annotations

import argparse
import hashlib
import re
from collections import defaultdict

from common import TABLES, dump_json, fnum, inum, load_csv, write_csv
from http_cache import get_json
from no_live import require_no_live

GT_NET = {"bsc": "bsc", "solana": "solana", "base": "base", "ethereum": "eth"}
MAX_TOKENS = 8
MAX_OHLCV_PAGES = 8
DUST_USD = 500.0
BROKE_HOURS = 6

PATH_FIELDS = [
    "event_id",
    "status",
    "n_primary_all",
    "n_selected",
    "n_series",
    "path_attach_contract_id",
    "t_attach_unix",
    "attach_share",
    "second_share",
    "earliest_primary_contract_id",
    "earliest_equals_path_attach",
    "broke",
    "hours_from_earliest_to_attach",
    "notes",
]


def cache_name(prefix: str, key: str) -> str:
    h = hashlib.sha1(key.encode()).hexdigest()[:16]
    safe = re.sub(r"[^a-zA-Z0-9_\-]+", "_", key)[:60]
    return f"{prefix}_{safe}_{h}.json"


def select_tokens(event_id: str, primary: str, all_rows: list[dict]) -> list[dict]:
    prim = [
        r
        for r in all_rows
        if r["event_id"] == event_id and r.get("on_primary_chain") == "1"
    ]
    dated = [r for r in prim if inum(r.get("pair_created_at_unix")) is not None]
    dated.sort(key=lambda r: inum(r["pair_created_at_unix"]) or 10**18)
    earliest = dated[0] if dated else None
    prim_sorted = sorted(prim, key=lambda r: fnum(r.get("volume_24h_usd_at_collect")), reverse=True)
    picked: list[dict] = []
    seen: set[str] = set()
    for r in prim_sorted:
        if r["contract_id"] in seen:
            continue
        picked.append(r)
        seen.add(r["contract_id"])
        if len(picked) >= MAX_TOKENS:
            break
    if earliest and earliest["contract_id"] not in seen:
        if len(picked) >= MAX_TOKENS:
            picked[-1] = earliest
        else:
            picked.append(earliest)
    return picked


def gt_best_pool(network: str, address: str) -> str | None:
    url = f"https://api.geckoterminal.com/api/v2/networks/{network}/tokens/{address}/pools?page=1"
    data = get_json(url, cache_name("gt_pools", f"{network}_{address}"))
    if data.get("error"):
        return None
    rows = data.get("data") or []
    best_id = None
    best_vol = -1.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        attrs = row.get("attributes") or {}
        vol = fnum(str((attrs.get("volume_usd") or {}).get("h24") or attrs.get("volume_usd_h24") or 0))
        pid = attrs.get("address") or row.get("id") or ""
        if isinstance(pid, str) and pid.startswith(f"{network}_"):
            pid = pid.split("_", 1)[-1]
        if vol >= best_vol and pid:
            best_vol = vol
            best_id = pid
    return best_id


def gt_ohlcv(network: str, pool: str) -> list[tuple[int, float]]:
    """Return [(ts, volume_usd), ...] ascending."""
    rows: list[tuple[int, float]] = []
    before = None
    for page in range(MAX_OHLCV_PAGES):
        url = (
            f"https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool}/ohlcv/hour"
            f"?aggregate=1&limit=1000&currency=usd"
        )
        if before is not None:
            url += f"&before_timestamp={before}"
        data = get_json(url, cache_name("gt_ohlcv", f"{network}_{pool}_{page}"))
        if data.get("error"):
            break
        lst = (((data.get("data") or {}).get("attributes") or {}).get("ohlcv_list")) or []
        if not lst:
            break
        for item in lst:
            if not isinstance(item, list) or len(item) < 6:
                continue
            try:
                ts = int(item[0])
                vol = float(item[5] or 0)
            except (TypeError, ValueError):
                continue
            rows.append((ts, vol))
        before = int(min(x[0] for x in lst))
        if len(lst) < 50:
            break
    by_ts: dict[int, float] = {}
    for ts, vol in rows:
        by_ts[ts] = vol
    return sorted(by_ts.items())


def share_ok(share: float, second: float) -> bool:
    return share >= 0.5 or (share >= 0.35 and share >= 1.4 * max(second, 1e-12))


def compute_path(event_id: str, primary: str, selected: list[dict]) -> dict:
    n_primary = sum(
        1
        for r in load_csv(TABLES / "contracts_all.csv")
        if r["event_id"] == event_id and r.get("on_primary_chain") == "1"
    )
    earliest_id = ""
    dated = [r for r in selected if inum(r.get("pair_created_at_unix")) is not None]
    if dated:
        dated.sort(key=lambda r: inum(r["pair_created_at_unix"]) or 10**18)
        # earliest among ALL primary, not just selected — recompute from file
    all_prim = [
        r
        for r in load_csv(TABLES / "contracts_all.csv")
        if r["event_id"] == event_id and r.get("on_primary_chain") == "1"
    ]
    all_dated = [r for r in all_prim if inum(r.get("pair_created_at_unix")) is not None]
    all_dated.sort(key=lambda r: inum(r["pair_created_at_unix"]) or 10**18)
    if all_dated:
        earliest_id = all_dated[0]["contract_id"]
    earliest_ts = inum(all_dated[0]["pair_created_at_unix"]) if all_dated else None

    net = GT_NET.get(primary.lower())
    if not all_prim:
        return _row(event_id, "no_primary", n_primary, 0, 0, "", "", "", "", earliest_id, "", "", "", "no primary-chain tokens")
    if not net:
        return _row(event_id, "unsupported_network", n_primary, len(selected), 0, "", "", "", "", earliest_id, "", "", "", f"primary={primary}")

    series: dict[str, dict[int, float]] = {}
    for r in selected:
        addr = r["address"]
        pair = (r.get("pair_address") or "").strip()
        pool = None
        if pair and ":" not in pair and " " not in pair:
            pool = pair
        if not pool:
            pool = gt_best_pool(net, addr)
        if not pool:
            continue
        ohlcv = gt_ohlcv(net, pool)
        if not ohlcv:
            # Dex pair is often not the GT-indexed pool (Bonk/WEN).
            alt = gt_best_pool(net, addr)
            if alt and alt != pool:
                ohlcv = gt_ohlcv(net, alt)
        if not ohlcv:
            continue
        series[r["contract_id"]] = {ts: vol for ts, vol in ohlcv}

    if not series:
        return _row(event_id, "no_ohlcv", n_primary, len(selected), 0, "", "", "", "", earliest_id, "", "", "", "gt empty")
    if len(series) == 1:
        only = next(iter(series))
        return _row(event_id, "single_series", n_primary, len(selected), 1, only, "", "", "", earliest_id, "", "", "", "only one ohlcv series")

    hours = sorted({ts for s in series.values() for ts in s})
    first_cross = None
    saw_unattached = False
    broke = "0"
    post_low = 0
    crossed = False
    last_share = 0.0
    last_second = 0.0
    attach_id = ""

    for ts in hours:
        vols = {cid: s.get(ts, 0.0) for cid, s in series.items()}
        total = sum(vols.values())
        if total < DUST_USD:
            continue
        ranked = sorted(vols.items(), key=lambda x: x[1], reverse=True)
        top_id, top_v = ranked[0]
        second_v = ranked[1][1] if len(ranked) > 1 else 0.0
        share = top_v / total
        second = second_v / total
        last_share, last_second = share, second
        attached_now = share_ok(share, second)
        if not attached_now:
            saw_unattached = True
            if crossed:
                post_low += 1
                if post_low >= BROKE_HOURS:
                    broke = "1"
        else:
            post_low = 0
            if first_cross is None:
                first_cross = (ts, top_id, share, second)
            crossed = True

    if first_cross is None:
        return _row(
            event_id,
            "never",
            n_primary,
            len(selected),
            len(series),
            "",
            "",
            f"{last_share:.4f}" if last_share else "",
            f"{last_second:.4f}" if last_second else "",
            earliest_id,
            "",
            "0",
            "",
            "no crossing after dust filter",
        )

    ts, aid, share, second = first_cross
    status = "observed" if saw_unattached else "left_censored"
    hours_gap = ""
    if earliest_ts is not None:
        hours_gap = str(round((ts - earliest_ts) / 3600, 2))
    eq = "1" if aid == earliest_id else "0"
    return _row(
        event_id,
        status,
        n_primary,
        len(selected),
        len(series),
        aid,
        str(ts),
        f"{share:.4f}",
        f"{second:.4f}",
        earliest_id,
        eq,
        broke,
        hours_gap,
        "gt_hourly_usd;dust=500;cap=8",
    )


def _row(
    event_id: str,
    status: str,
    n_primary: int,
    n_sel: int,
    n_series: int,
    attach_id: str,
    t_attach: str,
    share: str,
    second: str,
    earliest_id: str,
    eq: str,
    broke: str,
    hours_gap: str,
    notes: str,
) -> dict:
    return {
        "event_id": event_id,
        "status": status,
        "n_primary_all": str(n_primary),
        "n_selected": str(n_sel),
        "n_series": str(n_series),
        "path_attach_contract_id": attach_id,
        "t_attach_unix": t_attach,
        "attach_share": share,
        "second_share": second,
        "earliest_primary_contract_id": earliest_id,
        "earliest_equals_path_attach": eq,
        "broke": broke,
        "hours_from_earliest_to_attach": hours_gap,
        "notes": notes,
    }


def main() -> None:
    require_no_live()
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()

    events = load_csv(TABLES / "events.csv")
    all_rows = load_csv(TABLES / "contracts_all.csv")
    if args.smoke:
        events = [e for e in events if e["event_id"] == "E01_niulai"]

    out: list[dict] = []
    qa: dict[str, object] = {"events": []}
    for ev in events:
        eid = ev["event_id"]
        primary = ev["chain_primary"]
        selected = select_tokens(eid, primary, all_rows)
        print(f"[r1-path] {eid} primary={primary} selected={len(selected)}")
        row = compute_path(eid, primary, selected)
        out.append(row)
        qa["events"].append(
            {
                "event_id": eid,
                "status": row["status"],
                "n_selected": row["n_selected"],
                "n_series": row["n_series"],
                "selected_ids": [r["contract_id"] for r in selected],
            }
        )
        print(f"  -> {row['status']} attach={row['path_attach_contract_id']} eq={row['earliest_equals_path_attach']}")

    write_csv(TABLES / "attach_path.csv", out, PATH_FIELDS)
    dump_json(TABLES / "attach_hourly_qa.json", qa)
    print("[done] attach_path.csv", len(out))


if __name__ == "__main__":
    main()
