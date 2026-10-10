"""Debot Discover board vs Hyperliquid's own public ledger.

Debot's open board is perp, so the honest counter-ledger is Hyperliquid's free
`info` API, not Base/BSC chain data. Realized PnL is summed from
`userFillsByTime.closedPnl` over exactly the window Debot labels, which is the
same quantity Debot claims to show.

No auth, no private endpoint, no EATLQ archive.
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path

ROOT = Path("/root/copy_cascade_invert_r0")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common import ROOT as PROJ, ensure_no_live, mean, median, norm_addr, read_json, utc_now, utc_now_iso, write_json
from src.public_http import http_json

HL_INFO = "https://api.hyperliquid.xyz/info"
FILL_CAP = 2000
WINDOW_DAYS = {"7d": 7, "30d": 30}
# Debot labels a wallet "7d"/"30d"; "all" has no bound we can reproduce.
UNBOUNDED = {"all"}


def hl_post(payload: dict, *, sleep_s: float = 0.09, retries: int = 3) -> tuple[object | None, int]:
    status, parsed, _ = http_json(
        HL_INFO, method="POST", payload=payload, sleep_s=sleep_s, timeout=30.0, retries=retries
    )
    if status != 200:
        return None, status
    return parsed, status


def hl_realized(address: str, days: int, *, sleep_s: float = 0.09, retries: int = 3) -> dict:
    now_ms = int(utc_now().timestamp() * 1000)
    start_ms = int((utc_now() - timedelta(days=days)).timestamp() * 1000)
    fills, status = hl_post(
        {"type": "userFillsByTime", "user": address, "startTime": start_ms, "endTime": now_ms},
        sleep_s=sleep_s,
        retries=retries,
    )
    if not isinstance(fills, list):
        # Distinguish "throttled" from "no such HL account": a throttled call is
        # a hole in our coverage, not evidence about the wallet.
        return {
            "ok": False,
            "error": "hl_throttled" if status in (429, 0, 503, 502) else "hl_fills_unavailable",
            "http_status": status,
        }
    closed = 0.0
    fees = 0.0
    n_close = 0
    n_win = 0
    last_ms = None
    coins = set()
    for f in fills:
        if not isinstance(f, dict):
            continue
        try:
            pnl = float(f.get("closedPnl") or 0)
            fee = float(f.get("fee") or 0)
        except (TypeError, ValueError):
            continue
        closed += pnl
        fees += fee
        if abs(pnl) > 1e-9:
            n_close += 1
            if pnl > 0:
                n_win += 1
        t = f.get("time")
        if isinstance(t, (int, float)) and (last_ms is None or t > last_ms):
            last_ms = int(t)
        if f.get("coin"):
            coins.add(str(f["coin"]))
    return {
        "ok": True,
        "hl_closed_pnl": closed,
        "hl_fees": fees,
        "hl_net_pnl": closed - fees,
        "hl_n_fills": len(fills),
        "hl_n_closes": n_close,
        "hl_winrate": (n_win / n_close) if n_close else None,
        "hl_last_fill_ms": last_ms,
        "hl_n_coins": len(coins),
        "fills_cap_hit": len(fills) >= FILL_CAP,
    }


def _debot_rows() -> list[dict]:
    boards = read_json(PROJ / "data/normalized/boards.json")
    out = []
    seen = set()
    for r in boards.get("rows") or []:
        if r.get("source") != "debot" or r.get("venue") != "hyperliquid_perp":
            continue
        window = str(r.get("window"))
        if window in UNBOUNDED:
            continue
        key = (norm_addr(r.get("address")), window)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _sign(v) -> int | None:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return 1 if x > 1e-6 else (-1 if x < -1e-6 else 0)


def reconcile(*, workers: int = 4) -> dict:
    ensure_no_live()
    rows = _debot_rows()
    now_ms = int(utc_now().timestamp() * 1000)

    def one(r: dict, *, sleep_s: float = 0.09, retries: int = 3) -> dict:
        window = str(r.get("window"))
        days = WINDOW_DAYS[window]
        addr = norm_addr(r.get("address"))
        hl = hl_realized(addr, days, sleep_s=sleep_s, retries=retries)
        rec = {
            "address": addr,
            "window": window,
            "debot_realized_pnl": r.get("vendor_realized_usd"),
            "debot_winrate": r.get("vendor_winrate"),
            "debot_trades": r.get("vendor_txs"),
            "debot_is_smart_money": r.get("vendor_is_smart_money"),
            "debot_max_drawdown_ratio": r.get("vendor_max_drawdown_ratio"),
            **hl,
        }
        if hl.get("ok"):
            rec["sign_debot"] = _sign(rec["debot_realized_pnl"])
            rec["sign_hl"] = _sign(hl["hl_closed_pnl"])
            if rec["sign_debot"] is not None and rec["sign_hl"] is not None:
                rec["sign_agree"] = rec["sign_debot"] == rec["sign_hl"]
            # Did the wallet even trade inside the window it is ranked in?
            rec["active_in_window"] = (hl.get("hl_n_closes") or 0) > 0
            last = hl.get("hl_last_fill_ms")
            rec["days_since_last_fill"] = (
                round((now_ms - last) / 86400000.0, 2) if last else None
            )
            d, h = rec["debot_realized_pnl"], hl["hl_closed_pnl"]
            if d is not None and abs(h) > 1.0:
                rec["ratio_debot_over_hl"] = d / h
        return rec

    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(one, r): r for r in rows}
        done = 0
        for fut in as_completed(futs):
            results.append(fut.result())
            done += 1
            if done % 50 == 0 or done == len(rows):
                write_json(
                    PROJ / "data/live_recon/progress_debot_hl.json",
                    {"done": done, "total": len(rows), "updated_at": utc_now_iso()},
                )

    # Throttled calls are coverage holes. Retry them serially before judging.
    by_key = {(r["address"], r["window"]): r for r in rows}
    retry = [r for r in results if r.get("error") == "hl_throttled"]
    if retry:
        keep = [r for r in results if r.get("error") != "hl_throttled"]
        for i, r in enumerate(retry, 1):
            src = by_key.get((r["address"], r["window"]))
            keep.append(one(src, sleep_s=0.55, retries=5) if src else r)
            if i % 50 == 0 or i == len(retry):
                write_json(
                    PROJ / "data/live_recon/progress_debot_hl.json",
                    {"retry_done": i, "retry_total": len(retry), "updated_at": utc_now_iso()},
                )
        results = keep

    ok = [r for r in results if r.get("ok")]
    n_throttled = sum(1 for r in results if r.get("error") == "hl_throttled")
    n_not_hl = sum(1 for r in results if r.get("error") == "hl_fills_unavailable")
    both = [r for r in ok if r.get("sign_agree") is not None]
    active = [r for r in ok if r.get("active_in_window")]
    inactive = [r for r in ok if not r.get("active_in_window")]
    # Our 2000-fill page cap truncates OUR sum, so capped rows cannot test
    # Debot's magnitude. Judge magnitude on uncapped rows only.
    clean = [r for r in active if not r.get("fills_cap_hit")]
    agree = sum(1 for r in both if r["sign_agree"])
    agree_active = sum(1 for r in active if r.get("sign_agree"))
    agree_clean = sum(1 for r in clean if r.get("sign_agree"))
    ratios = [abs(r["ratio_debot_over_hl"]) for r in clean if r.get("ratio_debot_over_hl") is not None]
    abs_err = [
        abs(float(r["debot_realized_pnl"]) - float(r["hl_closed_pnl"]))
        for r in clean
        if r.get("debot_realized_pnl") is not None
    ]
    within_10pct = sum(1 for x in ratios if 0.9 <= x <= 1.1)
    within_25pct = sum(1 for x in ratios if 0.75 <= x <= 1.25)
    within_2x = sum(1 for x in ratios if 0.5 <= x <= 2.0)
    cap_hit = sum(1 for r in ok if r.get("fills_cap_hit"))

    gaps = []
    if not ok:
        gaps.append("HL_LEDGER_UNAVAILABLE")
    if inactive and len(inactive) / len(ok) > 0.10:
        gaps.append("RANKED_BUT_INACTIVE_IN_WINDOW")
    rate_clean = (agree_clean / len(clean)) if clean else None
    if rate_clean is not None and len(clean) >= 20 and rate_clean < 0.90:
        gaps.append("SIGN_DISAGREEMENT_ACTIVE")
    frac10 = (within_10pct / len(ratios)) if ratios else None
    if frac10 is not None and len(ratios) >= 20 and frac10 < 0.60:
        gaps.append("MAGNITUDE_MISMATCH")
    if cap_hit:
        gaps.append("OUR_FILLS_CAP_TRUNCATION")
    if n_throttled:
        gaps.append("HL_THROTTLED_COVERAGE_HOLE")

    if "HL_LEDGER_UNAVAILABLE" in gaps:
        status = "INCONCLUSIVE_NO_HL_LEDGER"
    elif {"SIGN_DISAGREEMENT_ACTIVE", "MAGNITUDE_MISMATCH", "RANKED_BUT_INACTIVE_IN_WINDOW"} & set(gaps):
        status = "VENDOR_LEDGER_GAP"
    elif gaps:
        status = "RECONCILED_WITH_GAPS"
    else:
        status = "RECONCILED_DESCRIPTIVE"

    payload = {
        "generated_at": utc_now_iso(),
        "source": "debot",
        "venue": "hyperliquid_perp",
        "counter_ledger": "https://api.hyperliquid.xyz/info userFillsByTime.closedPnl",
        "status": status,
        "gaps": gaps,
        "n_rows": len(rows),
        "n_ok": len(ok),
        "n_fail": len(results) - len(ok),
        "n_throttled": n_throttled,
        "n_no_hl_account": n_not_hl,
        "n_active_in_window": len(active),
        "n_ranked_but_inactive": len(inactive),
        "n_our_fills_cap_hit": cap_hit,
        "n_clean_active_uncapped": len(clean),
        "sign_agree": agree,
        "sign_both": len(both),
        "sign_agree_rate": (agree / len(both)) if both else None,
        "sign_agree_rate_active": (agree_active / len(active)) if active else None,
        "sign_agree_rate_clean": rate_clean,
        "n_ratio": len(ratios),
        "frac_within_10pct": frac10,
        "frac_within_25pct": (within_25pct / len(ratios)) if ratios else None,
        "frac_within_2x": (within_2x / len(ratios)) if ratios else None,
        "median_abs_ratio": median(ratios),
        "median_abs_err_usd": median(abs_err),
        "mean_abs_err_usd": mean(abs_err),
        "eatlq_archive_used": False,
        "units_note": (
            "Debot realized_pnl (USD) vs Hyperliquid closedPnl summed over the "
            "same labeled window, gross of fees. Debot rows labeled 'all' are "
            "excluded: the window is not reproducible. Magnitude stats use only "
            "active rows below our 2000-fill page cap; capped rows undercount "
            "OUR side, not Debot's."
        ),
        "rows": results,
    }
    write_json(PROJ / "out/reconcile_debot_vs_hyperliquid.json", payload)
    _md(payload)
    return payload


def _md(p: dict) -> None:
    lines = [
        "# Debot Discover vs Hyperliquid own ledger",
        "",
        f"status: `{p['status']}`",
        f"generated_at: {p['generated_at']}",
        "",
        f"- Debot board venue: `{p['venue']}` (NOT Base/BSC spot)",
        f"- counter-ledger: `{p['counter_ledger']}`",
        f"- rows / ok / fail: {p['n_rows']} / {p['n_ok']} / {p['n_fail']}",
        f"- fail split: no HL account {p['n_no_hl_account']}, still throttled {p['n_throttled']}",
        f"- ranked wallets that actually closed a trade in the labeled window: {p['n_active_in_window']}",
        f"- **ranked but inactive in window**: {p['n_ranked_but_inactive']}",
        f"- our {FILL_CAP}-fill cap hit (excluded from magnitude): {p['n_our_fills_cap_hit']}",
        f"- clean rows judged (active, uncapped): {p['n_clean_active_uncapped']}",
        f"- sign agree (all / active / clean): {p['sign_agree_rate']} / {p['sign_agree_rate_active']} / {p['sign_agree_rate_clean']}",
        f"- magnitude within ±10% / ±25% / 2x: {p['frac_within_10pct']} / {p['frac_within_25pct']} / {p['frac_within_2x']} (n={p['n_ratio']})",
        f"- median |Debot/HL| ratio: {p['median_abs_ratio']}",
        f"- median |Debot-HL| USD: {p['median_abs_err_usd']}",
        "",
        p["units_note"],
        "",
        "Gaps: " + (", ".join(p["gaps"]) if p["gaps"] else "none"),
        "",
        "Debot's Base/BSC spot smart-money board is `SOURCE_BLOCKED_AUTH`.",
        "GO remains false.",
        "",
    ]
    (PROJ / "out/reconcile_debot_vs_hyperliquid.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    p = reconcile()
    print(json.dumps({k: p[k] for k in (
        "status", "n_ok", "n_active_in_window", "n_ranked_but_inactive",
        "sign_agree_rate_active", "frac_within_10pct", "median_abs_ratio", "gaps")}, indent=2))
