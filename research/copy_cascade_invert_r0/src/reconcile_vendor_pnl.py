"""Vendor display PnL vs our Base FIFO vs golden overlap. No RPC."""
from __future__ import annotations

import json
from pathlib import Path

from src.common import ROOT, mean, read_json, utc_now_iso, write_json
from src.golden_join import join_goldens
from src.tape import load_closed_trades, wallet_fifo


def _dune_overlay_addresses() -> set[str]:
    path = ROOT / "data/raw/dune_overlay/dune_smart_base_20260504_q1_wallet_summary_v2.csv"
    if not path.exists():
        return set()
    # File is a Dune JSON blob, not a real CSV.
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        return set()
    rows = (((payload.get("result") or {}).get("rows")) or [])
    out = set()
    for row in rows:
        addr = (row.get("wallet_address") or "").lower()
        if addr.startswith("0x"):
            out.add(addr)
    return out


def reconcile() -> dict:
    boards_path = ROOT / "data/normalized/boards.json"
    if not boards_path.exists():
        payload = {
            "status": "DATA_LINEAGE_INSUFFICIENT",
            "reason": "boards.json missing",
            "generated_at": utc_now_iso(),
        }
        write_json(ROOT / "out/reconcile_vendor_vs_base.json", payload)
        return payload

    boards = read_json(boards_path)
    all_rows = boards.get("rows") or []
    # Perp-venue rows (Debot Discover) have no Base/BSC ERC-20 ledger to match.
    rows = [r for r in all_rows if r.get("spot_reconcilable")]
    n_excluded_perp = len(all_rows) - len(rows)
    lineage = boards.get("lineage")
    fifo = wallet_fifo(load_closed_trades())
    dune = _dune_overlay_addresses()
    golden = join_goldens([r["address"] for r in rows])

    # one row per address (prefer 7d / first numeric pnl)
    by_addr: dict[str, dict] = {}
    for r in rows:
        addr = r["address"]
        cur = by_addr.get(addr)
        if cur is None or (r.get("vendor_pnl") is not None and cur.get("vendor_pnl") is None):
            by_addr[addr] = dict(r)
        elif r.get("window") == "7d" and cur.get("window") != "7d":
            by_addr[addr] = dict(r)

    table = []
    sign_agree = 0
    sign_disagree = 0
    both = 0
    our_only = 0
    vendor_only = 0
    for addr, r in sorted(by_addr.items()):
        ours = fifo.get(addr)
        vendor_sign = None
        vp = r.get("vendor_realized_usd")
        if vp is None:
            vp = r.get("vendor_pnl")
        if vp is not None:
            vendor_sign = 1 if vp > 0 else (-1 if vp < 0 else 0)
        our_sign = ours["sign"] if ours else None
        if ours and vendor_sign is not None:
            both += 1
            if vendor_sign == our_sign:
                sign_agree += 1
            elif vendor_sign != 0 and our_sign != 0:
                sign_disagree += 1
        elif ours:
            our_only += 1
        else:
            vendor_only += 1
        table.append(
            {
                "address": addr,
                "source": r.get("source"),
                "chain": r.get("chain"),
                "window": r.get("window"),
                "vendor_pnl": vp,
                "vendor_winrate": r.get("vendor_winrate"),
                "our_n_closed": None if not ours else ours["n_closed"],
                "our_pnl_weth": None if not ours else ours["pnl_weth"],
                "our_winrate": None if not ours else ours["winrate"],
                "our_top1_pnl_share": None if not ours else ours["top1_pnl_share"],
                "sign_agree": vendor_sign == our_sign if (ours and vendor_sign is not None) else None,
                "in_b3_golden": addr in set(golden.get("overlap_board_b3") or []),
                "in_actor_flow_golden": addr in set(golden.get("overlap_board_actor_flow") or []),
                "in_dune_overlay": addr in dune,
                "units_note": "vendor usually USD or ratio; our ledger is WETH FIFO. Signs compared only.",
            }
        )

    n_board = len(by_addr)
    coverage = (both + our_only) / n_board if n_board else 0.0
    gaps = []
    if lineage == "DATA_LINEAGE_INSUFFICIENT":
        gaps.append("DATA_LINEAGE_INSUFFICIENT")
    if coverage < 0.05:
        gaps.append("LOW_TAPE_OVERLAP")
    if sign_disagree > sign_agree and both:
        gaps.append("SIGN_DISAGREEMENT")
    if both == 0 and n_board:
        gaps.append("NO_JOINT_PNL")
    status = "VENDOR_LEDGER_GAP" if gaps else "RECONCILED_DESCRIPTIVE"

    # Dune overlay is explicitly not a vendor board
    dune_intersect = sorted(set(by_addr) & dune)

    payload = {
        "generated_at": utc_now_iso(),
        "status": status,
        "gaps": gaps,
        "board_lineage": lineage,
        "source_status": boards.get("source_status"),
        "venue_counts": boards.get("venue_counts"),
        "n_excluded_perp_rows": n_excluded_perp,
        "n_vendor_addresses": n_board,
        "n_our_fifo_wallets": len(fifo),
        "n_both": both,
        "n_vendor_only": vendor_only,
        "n_our_only_on_board": our_only,
        "tape_coverage": coverage,
        "sign_agree": sign_agree,
        "sign_disagree": sign_disagree,
        "sign_agree_rate": (sign_agree / both) if both else None,
        "lookahead": "fame_now snapshot must not be used as historical fame",
        "dune_overlay": {
            "role": "overlay_only_not_vendor_board",
            "n_dune": len(dune),
            "n_intersect_vendor": len(dune_intersect),
        },
        "golden": {
            "n_overlap_b3": golden.get("n_overlap_b3"),
            "n_overlap_actor_flow": golden.get("n_overlap_actor_flow"),
            "gl2b_fifo_gate": golden.get("gl2b_fifo_gate"),
        },
        "bsc": "INCONCLUSIVE_BSC_NO_LEDGER",
        "rows": table,
    }
    write_json(ROOT / "out/reconcile_vendor_vs_base.json", payload)
    _write_md(payload)
    return payload


def _write_md(p: dict) -> None:
    lines = [
        "# Vendor vs Base FIFO vs golden",
        "",
        f"status: `{p['status']}`",
        f"generated_at: {p['generated_at']}",
        "",
        f"- vendor addresses: {p['n_vendor_addresses']}",
        f"- our FIFO wallets on tape: {p['n_our_fifo_wallets']}",
        f"- both (address ∩ tape): {p['n_both']}",
        f"- vendor only: {p['n_vendor_only']}",
        f"- tape coverage of vendor set: {p['tape_coverage']}",
        f"- sign agree/disagree: {p['sign_agree']}/{p['sign_disagree']}",
        f"- B3 golden overlap: {p['golden']['n_overlap_b3']}",
        f"- actor-flow golden overlap: {p['golden']['n_overlap_actor_flow']}",
        f"- BSC replay: `{p['bsc']}`",
        f"- venue counts: {p.get('venue_counts')}",
        f"- perp rows excluded from spot reconcile: {p.get('n_excluded_perp_rows')}",
        f"- Dune overlay ∩ vendor: {p['dune_overlay']['n_intersect_vendor']} (not a vendor board)",
        "",
        "Today's board is `fame_now`. It is not historical fame.",
        "",
        "Gaps: " + (", ".join(p["gaps"]) if p["gaps"] else "none"),
        "",
        "## Source status",
        "",
        json.dumps(p.get("source_status"), indent=2),
        "",
    ]
    (ROOT / "out/reconcile_vendor_vs_base.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    p = reconcile()
    print(json.dumps({k: p[k] for k in ("status", "n_vendor_addresses", "n_both", "gaps")}, indent=2))
