"""Compare reconstructed public-index FIFO vs GMGN walletNew PnL."""
from __future__ import annotations

import json
import math
from pathlib import Path

from src.common import ROOT, mean, median, utc_now_iso, write_json


def _sign(v) -> int | None:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x > 1e-6:
        return 1
    if x < -1e-6:
        return -1
    return 0


def _corr(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 8:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx < 1e-12 or dy < 1e-12:
        return None
    return num / (dx * dy)


def compare_chain(chain: str) -> dict:
    path = ROOT / "data/live_recon" / f"reconstruct_{chain}.json"
    if not path.exists():
        payload = {
            "status": "LIVE_RECON_MISSING",
            "chain": chain,
            "generated_at": utc_now_iso(),
        }
        write_json(ROOT / "out" / f"reconcile_gmgn_vs_live_{chain}.json", payload)
        return payload

    recon = json.loads(path.read_text())
    rows = []
    agree = {1: 0, 7: 0, 30: 0}
    disagree = {1: 0, 7: 0, 30: 0}
    both = {1: 0, 7: 0, 30: 0}
    xs7: list[float] = []
    ys7: list[float] = []
    abs_err7: list[float] = []
    n_ok = 0
    n_fail = 0
    n_contam = 0
    n_unmatched = 0
    n_trunc = 0

    for r in recon.get("rows") or []:
        if not r.get("ok"):
            n_fail += 1
            continue
        n_ok += 1
        if (r.get("n_transfer_in") or 0) + (r.get("n_transfer_out") or 0) > 0:
            n_contam += 1
        if (r.get("n_unmatched_sell") or 0) > 0:
            n_unmatched += 1
        if r.get("truncated"):
            n_trunc += 1
        v = r.get("vendor") or {}
        item = {
            "address": r.get("address"),
            "our_1d": r.get("our_realized_usd_1d"),
            "our_7d": r.get("our_realized_usd_7d"),
            "our_30d": r.get("our_realized_usd_30d"),
            "vendor_1d": v.get("realized_profit_1d"),
            "vendor_7d": v.get("realized_profit_7d"),
            "vendor_30d": v.get("realized_profit_30d"),
            "n_buy": r.get("n_buy"),
            "n_sell": r.get("n_sell"),
            "n_transfer_in": r.get("n_transfer_in"),
            "n_unmatched_sell": r.get("n_unmatched_sell"),
            "truncated": r.get("truncated"),
        }
        for days, our_k, ven_k, key in (
            (1, "our_1d", "vendor_1d", 1),
            (7, "our_7d", "vendor_7d", 7),
            (30, "our_30d", "vendor_30d", 30),
        ):
            so, sv = _sign(item[our_k]), _sign(item[ven_k])
            item[f"sign_agree_{days}d"] = (so == sv) if (so is not None and sv is not None) else None
            if so is not None and sv is not None:
                both[key] += 1
                if so == sv:
                    agree[key] += 1
                elif so != 0 and sv != 0:
                    disagree[key] += 1
        if item["our_7d"] is not None and item["vendor_7d"] is not None:
            xs7.append(float(item["our_7d"]))
            ys7.append(float(item["vendor_7d"]))
            abs_err7.append(abs(float(item["our_7d"]) - float(item["vendor_7d"])))
        rows.append(item)

    active_pairs = [
        (x, y)
        for x, y in zip(xs7, ys7)
        if abs(x) >= 1.0 or abs(y) >= 1.0
    ]
    active_agree = 0
    for x, y in active_pairs:
        if _sign(x) == _sign(y):
            active_agree += 1
    xs_a = [p[0] for p in active_pairs]
    ys_a = [p[1] for p in active_pairs]

    rate7 = (agree[7] / both[7]) if both[7] else None
    gaps = []
    if n_ok == 0:
        gaps.append("LIVE_INDEX_EMPTY")
    if both[7] == 0:
        gaps.append("NO_JOINT_PNL")
    if both[7] and both[7] < 20:
        gaps.append("INCONCLUSIVE_SMALL_N")
    if rate7 is not None and rate7 < 0.55 and disagree[7] > agree[7] * 0.5:
        gaps.append("SIGN_DISAGREEMENT")
    n_zero_mismatch = both[7] - agree[7] - disagree[7]
    active_rate = (active_agree / len(active_pairs)) if active_pairs else None
    if active_pairs and len(active_pairs) >= 20 and active_rate is not None and active_rate < 0.55:
        gaps.append("SIGN_DISAGREEMENT_ACTIVE")
    if both[7] and n_zero_mismatch / both[7] > 0.25:
        gaps.append("ZERO_VS_NONZERO")
    if n_contam / n_ok > 0.5 if n_ok else False:
        gaps.append("HIGH_TRANSFER_CONTAMINATION")
    if n_unmatched / n_ok > 0.4 if n_ok else False:
        gaps.append("HIGH_UNMATCHED_SELLS")

    if "LIVE_INDEX_EMPTY" in gaps:
        status = "LIVE_RECON_FAILED"
    elif "NO_JOINT_PNL" in gaps:
        status = "VENDOR_LEDGER_GAP"
    elif "SIGN_DISAGREEMENT" in gaps or "SIGN_DISAGREEMENT_ACTIVE" in gaps:
        status = "VENDOR_LEDGER_GAP"
    elif gaps:
        status = "LIVE_RECONCILED_WITH_GAPS"
    else:
        status = "LIVE_RECONCILED_DESCRIPTIVE"

    payload = {
        "generated_at": utc_now_iso(),
        "chain": chain,
        "status": status,
        "gaps": gaps,
        "rpc_head": (recon.get("rpc") or {}).get("head"),
        "rpc": (recon.get("rpc") or {}).get("rpc"),
        "native_spot_usd": recon.get("native_spot_usd"),
        "lookback_days": recon.get("lookback_days"),
        "n_addresses": recon.get("n_addresses"),
        "n_ok": n_ok,
        "n_fail": n_fail,
        "n_with_transfers": n_contam,
        "n_unmatched": n_unmatched,
        "n_truncated": n_trunc,
        "both_1d": both[1],
        "both_7d": both[7],
        "both_30d": both[30],
        "sign_agree_1d": agree[1],
        "sign_agree_7d": agree[7],
        "sign_agree_30d": agree[30],
        "sign_disagree_7d": disagree[7],
        "n_zero_vs_nonzero_7d": n_zero_mismatch,
        "sign_agree_rate_7d": rate7,
        "sign_agree_rate_1d": (agree[1] / both[1]) if both[1] else None,
        "sign_agree_rate_30d": (agree[30] / both[30]) if both[30] else None,
        "pearson_7d": _corr(xs7, ys7),
        "n_active_7d": len(active_pairs),
        "sign_agree_rate_7d_active": (active_agree / len(active_pairs)) if active_pairs else None,
        "pearson_7d_active": _corr(xs_a, ys_a),
        "median_abs_err_7d_usd": median(abs_err7),
        "median_abs_err_7d_active_usd": median([abs(x - y) for x, y in active_pairs]),
        "mean_abs_err_7d_usd": mean(abs_err7),
        "median_our_7d": median(xs7),
        "median_vendor_7d": median(ys7),
        "eatlq_archive_used": False,
        "units_note": (
            "vendor = GMGN walletNew realized_profit_* USD; "
            "ours = public Blockscout ERC-20 + tx WETH mint/burn FIFO, "
            "daily native USD from Blockscout market chart. "
            "Token-token (non-ETH/stable) quotes are transfer-contaminated and "
            "under-count vs GMGN."
        ),
        "rows": rows,
    }
    write_json(ROOT / "out" / f"reconcile_gmgn_vs_live_{chain}.json", payload)
    _md(payload)
    return payload


def _md(p: dict) -> None:
    lines = [
        f"# GMGN vs live {p['chain']} FIFO",
        "",
        f"status: `{p['status']}`",
        f"generated_at: {p['generated_at']}",
        "",
        f"- public RPC head: `{p.get('rpc')}` #{p.get('rpc_head')}",
        f"- native USD spot: {p.get('native_spot_usd')}",
        f"- addresses / ok / fail: {p.get('n_addresses')} / {p.get('n_ok')} / {p.get('n_fail')}",
        f"- lookback: {p.get('lookback_days')}d",
        f"- 7d both / sign agree / disagree: {p.get('both_7d')} / {p.get('sign_agree_7d')} / {p.get('sign_disagree_7d')}",
        f"- 7d sign-agree rate: {p.get('sign_agree_rate_7d')} (active |pnl|>=$1: {p.get('sign_agree_rate_7d_active')} n={p.get('n_active_7d')})",
        f"- 1d / 30d sign-agree rate: {p.get('sign_agree_rate_1d')} / {p.get('sign_agree_rate_30d')}",
        f"- 7d Pearson all / active: {p.get('pearson_7d')} / {p.get('pearson_7d_active')}",
        f"- 7d median |our-vendor| USD: {p.get('median_abs_err_7d_usd')}",
        f"- wallets with transfer-in/out: {p.get('n_with_transfers')}",
        f"- wallets with unmatched sells: {p.get('n_unmatched')}",
        f"- truncated: {p.get('n_truncated')}",
        "",
        p.get("units_note") or "",
        "",
        "Gaps: " + (", ".join(p.get("gaps") or []) if p.get("gaps") else "none"),
        "",
        "This is contemporaneous reconstruct. July offline tape is not used here.",
        "GO remains false.",
        "",
    ]
    (ROOT / "out" / f"reconcile_gmgn_vs_live_{p['chain']}.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    import sys

    chain = sys.argv[1] if len(sys.argv) > 1 else "base"
    p = compare_chain(chain)
    print(json.dumps({k: p[k] for k in ("status", "n_ok", "both_7d", "sign_agree_rate_7d", "gaps") if k in p}, indent=2))
