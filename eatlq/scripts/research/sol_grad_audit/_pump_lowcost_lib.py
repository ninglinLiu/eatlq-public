#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared helpers for low-cost Pump collect (Phase A/B/C)."""
from __future__ import annotations

import json
import random
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from _pump_monthly_lib import (
    CachedRPC,
    DATA_ROOT as MONTHLY_ROOT,
    DATE_HI,
    DATE_LO,
    ENTRY_S,
    EXIT_S,
    MINT_AUTHORITY,
    RPC_DB,
    SNAPSHOT_T,
    b58enc,
    day_unix_window,
    iter_days,
    load_base_rpc,
    load_json,
    load_jsonl,
    net_ret,
    parse_create_tx,
    price_at_or_before,
    save_json,
)

OUT_DIR = Path(__file__).resolve().parent
LC_ROOT = OUT_DIR / "data" / "pump_lowcost"
DIAG = Path("/opt/EATLQ/mvp/diagnostics/sol")
PUMP_API = "https://frontend-api-v3.pump.fun/coins/{}"

SAMPLE_PER_DAY = 300
EARLYPATH_PAGES = 2
SPARSE_SIG_PAGES = 4
MAX_TX_PROBE = 6
COST_RT = 0.03


def mmdd(day_yyyy: str) -> str:
    return day_yyyy[5:]  # 2026-06-11 -> 06-11


def api_get(url: str, tries: int = 4) -> dict | list | None:
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "pump-lowcost/1"})
            return json.loads(urllib.request.urlopen(req, timeout=15).read())
        except Exception:
            time.sleep(0.4 * (i + 1))
    return None


def label_via_api(mint: str) -> dict | None:
    d = api_get(PUMP_API.format(mint))
    if not d or "created_timestamp" not in d:
        return None
    created = d["created_timestamp"]
    ath = d.get("ath_market_cap")
    ath_ts = d.get("ath_market_cap_timestamp")
    t2ath = ((ath_ts - created) / 1000.0) if (ath_ts and created) else None
    return {
        "mint": mint,
        "created_ms": created,
        "bonding_curve": d.get("bonding_curve"),
        "creator": d.get("creator"),
        "ath_mc": ath,
        "ath_ts": ath_ts,
        "time_to_ath_s": round(t2ath, 1) if t2ath is not None else None,
        "graduated": bool(d.get("complete")),
        "cur_mc_usd": d.get("usd_market_cap"),
        "source": "pump_api",
    }


def load_legacy_labeled() -> dict[str, dict]:
    out: dict[str, dict] = {}
    p = DIAG / "creates_labeled.jsonl"
    if not p.exists():
        return out
    for r in load_jsonl(p):
        m = r.get("mint")
        if m:
            out[m] = r
    return out


def load_legacy_smart_buyers() -> dict[str, dict]:
    import glob

    out: dict[str, dict] = {}
    for f in sorted(glob.glob(str(DIAG / "smart_buyers*.jsonl"))):
        for r in load_jsonl(Path(f)):
            m = r.get("mint")
            if m and m not in out:
                out[m] = r
    return out


def phase_a_manifest() -> dict:
    labeled = load_legacy_labeled()
    sb = load_legacy_smart_buyers()
    phase_a_days = {mmdd(d) for d in iter_days() if d >= "2026-06-11" and d <= "2026-06-15"}

    by_day_labeled: dict[str, int] = {}
    by_day_sb: dict[str, int] = {}
    for r in labeled.values():
        if not r.get("created_ms"):
            continue
        dy = datetime.fromtimestamp(r["created_ms"] / 1000, tz=timezone.utc).strftime("%m-%d")
        by_day_labeled[dy] = by_day_labeled.get(dy, 0) + 1
    for r in sb.values():
        d = r.get("day") or ""
        if d:
            by_day_sb[d] = by_day_sb.get(d, 0) + 1

    overlap = set(labeled) & set(sb)
    return {
        "phase": "A",
        "cost_usd": 0,
        "labeled_n": len(labeled),
        "smart_buyers_n": len(sb),
        "overlap_n": len(overlap),
        "by_day_labeled": by_day_labeled,
        "by_day_smart_buyers": by_day_sb,
        "phase_a_core_days": sorted(phase_a_days),
        "lc_manifest": str(LC_ROOT / "phase_a" / "manifest.json"),
    }


def pages_for_day(day: str, hi: str = DATE_HI) -> int:
    """Older days need more sig pages when paging backward from chain head."""
    d = datetime.strptime(day, "%Y-%m-%d")
    d_hi = datetime.strptime(hi, "%Y-%m-%d")
    days_back = max(0, (d_hi - d).days)
    return max(800, 400 + days_back * 60)


def sample_creates_for_day(day: str, n: int, rng: random.Random) -> list[dict]:
    """Prefer cached full-day enum; else cheap partial enum until n hits."""
    monthly_creates = MONTHLY_ROOT / "raw" / day / "creates.jsonl"
    if monthly_creates.exists():
        rows = load_jsonl(monthly_creates)
        if len(rows) >= n:
            rng.shuffle(rows)
            return rows[:n]
        if rows:
            return rows

    lo, hi = day_unix_window(day)
    samp = load_base_rpc()
    rpc = samp.RPC(samp.rpc_url(), min_interval=0.22)
    hits: list[dict] = []
    before = None
    pages = 0
    max_pages = pages_for_day(day)
    while pages < max_pages and len(hits) < n:
        params = [MINT_AUTHORITY, {"limit": 1000, **({"before": before} if before else {})}]
        sigs = rpc("getSignaturesForAddress", params) or []
        pages += 1
        if not sigs:
            break
        before = sigs[-1]["signature"]
        oldest = sigs[-1].get("blockTime") or 0
        for s in sigs:
            bt = s.get("blockTime")
            if bt is None or s.get("err"):
                continue
            if lo <= bt < hi:
                hits.append({"sig": s["signature"], "slot": s.get("slot"), "block_time": bt, "day": day})
                if len(hits) >= n:
                    break
        if oldest and oldest < lo:
            break
    rng.shuffle(hits)
    return hits[:n]


def resolve_create_rpc(rpc: CachedRPC, sig: str, block_time: int, day: str) -> dict | None:
    tx = rpc.get_transaction(sig)
    info = parse_create_tx(tx)
    if not info:
        return None
    info.update({"create_sig": sig, "created_ms": int(block_time * 1000), "block_time": block_time, "day": day})
    return info


def earlypath_features(rpc: CachedRPC, bonding_curve: str, created_s: float) -> dict:
    times: list[float] = []
    before = None
    reached = False
    for _ in range(EARLYPATH_PAGES):
        sigs = rpc.get_signatures_for_address(bonding_curve, before=before)
        if not sigs:
            reached = True
            break
        before = sigs[-1]["signature"]
        for s in sigs:
            bt = s.get("blockTime")
            if bt is not None:
                times.append(bt)
        if (sigs[-1].get("blockTime") or 0) <= created_s + 5:
            reached = True
            break
        if len(sigs) < 1000:
            reached = True
            break
    times.sort()
    rel = [t - created_s for t in times if t >= created_s - 2]
    n180 = sum(1 for x in rel if x <= 180)
    return {
        "n_trades_180s": n180,
        "n_trades_60s": sum(1 for x in rel if x <= 60),
        "alive_at_180s": any(120 <= x <= 180 for x in rel),
        "time_to_5th_s": rel[4] if len(rel) >= 5 else None,
        "reached_birth": reached,
    }


def collect_sig_list(rpc: CachedRPC, bc: str, created_s: float, window_s: float = 305) -> list[tuple[float, str]]:
    end_t = created_s + window_s
    rows: list[tuple[float, str]] = []
    before = None
    for _ in range(SPARSE_SIG_PAGES):
        sigs = rpc.get_signatures_for_address(bc, before=before)
        if not sigs:
            break
        before = sigs[-1]["signature"]
        oldest = sigs[-1].get("blockTime") or 0
        for s in sigs:
            bt = s.get("blockTime")
            if bt is None or s.get("err"):
                continue
            if created_s - 2 <= bt <= end_t:
                rows.append((bt, s["signature"]))
        if oldest <= created_s - 10:
            break
        if len(sigs) < 1000:
            break
    rows.sort()
    return rows


def sparse_snapshots(rpc: CachedRPC, mint: str, bc: str, created_ms: int) -> dict:
    samp = load_base_rpc()
    mint_b = samp.b58dec(mint)
    created_s = created_ms / 1000.0
    sig_rows = collect_sig_list(rpc, bc, created_s)
    trades: list[dict] = []
    seen: set[str] = set()
    targets = {created_s + t for t in SNAPSHOT_T}

    # pick sigs near each snapshot
    probe_sigs: list[str] = []
    for t_target in sorted(targets):
        cand = sorted(sig_rows, key=lambda x: abs(x[0] - t_target))[:MAX_TX_PROBE]
        for _, sig in cand:
            if sig not in seen:
                seen.add(sig)
                probe_sigs.append(sig)

    for sig in probe_sigs[:MAX_TX_PROBE * 2]:
        ev = rpc.decode_tx_trade(sig, mint_b)
        if not ev:
            continue
        bt = next((bt for bt, s in sig_rows if s == sig), ev.get("ts", created_s))
        trades.append(
            {
                "sig": sig,
                "block_time": bt,
                "t_rel": round(bt - created_s, 3),
                "price": ev["price"],
                "vSol": ev["vSol"],
                "vTok": ev["vTok"],
            }
        )

    snaps = {}
    for t in SNAPSHOT_T:
        row = price_at_or_before(trades, created_s + t)
        snaps[str(t)] = row

    p15 = (snaps.get("15") or {}).get("price") if snaps.get("15") else None
    p90 = (snaps.get("90") or {}).get("price") if snaps.get("90") else None
    ret = net_ret(p15, p90, COST_RT)

    return {
        "snapshots": snaps,
        "sparse_ret_t15_t90": ret,
        "n_tx_decoded": len(trades),
        "n_sigs_scanned": len(sig_rows),
    }


def api_hit_labels(labeled: dict) -> dict:
    ath = labeled.get("ath_mc") or 0
    pe = labeled.get("sparse_entry_price")  # optional chain
    # use ATH mc vs entry mc proxy if no chain; else chain ret
    return {
        "hit100_api": int(ath >= 20000) if ath else 0,  # rough ATH $20k ~ 2x from ~$10k start - weak proxy
        "graduation_api": int(bool(labeled.get("graduated"))),
    }


def day_dir(phase: str, day: str) -> Path:
    p = LC_ROOT / phase / day
    p.mkdir(parents=True, exist_ok=True)
    return p
