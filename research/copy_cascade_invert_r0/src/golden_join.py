"""Join vendor addresses to existing attribution / FIFO goldens. No RPC."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from src.common import ROOT, norm_addr, read_json, utc_now_iso, write_json


def _b3_accounts() -> set[str]:
    path = ROOT / "data/raw/golden/base_b3_inventory_golden_20260714.json"
    if not path.exists():
        return set()
    payload = read_json(path)
    acc = set()
    for row in payload.get("account_results", []):
        acc.add(norm_addr(row.get("account")))
    return {a for a in acc if a}


def _b3_sample_accounts() -> set[str]:
    path = ROOT / "data/raw/golden/base_b3_entity_attribution_sample_v2_20260714.json"
    if not path.exists():
        return set()
    payload = read_json(path)
    acc = set()
    rows = payload.get("rows") or payload.get("sample_rows") or payload.get("transactions") or []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in ("resolved_account", "account", "resolved_entity", "tx_from"):
                if row.get(key):
                    acc.add(norm_addr(row[key]))
    # fallback: walk
    if not acc:
        blob = json.dumps(payload)
        # last resort already handled by inventory golden
    return {a for a in acc if a}


def _gl2b_tx() -> set[str]:
    path = ROOT / "data/raw/golden/base_gl2_b_fifo_cashleg_golden_20260715.json"
    if not path.exists():
        return set()
    payload = read_json(path)
    return {norm_addr(c.get("tx_hash")) for c in payload.get("checks", []) if c.get("tx_hash")}


def _actor_flow_actors() -> set[str]:
    path = ROOT / "data/raw/golden/golden_audit_rows.csv"
    if not path.exists():
        return set()
    out = set()
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            out.add(norm_addr(row.get("resolved_actor")))
    return {a for a in out if a}


def join_goldens(board_addresses: list[str]) -> dict:
    boards = {norm_addr(a) for a in board_addresses if a}
    b3 = _b3_accounts() | _b3_sample_accounts()
    af = _actor_flow_actors()
    gl2_tx = _gl2b_tx()
    overlap_b3 = sorted(boards & b3)
    overlap_af = sorted(boards & af)
    gl2_final = ROOT / "data/raw/golden/base_gl2_b_final_20260715.json"
    gl2_gate = None
    if gl2_final.exists():
        gf = read_json(gl2_final)
        gl2_gate = {
            "fifo_golden_gate": (gf.get("fifo_golden") or {}).get("gate"),
            "target_balance_all_integer_equal": (gf.get("fifo_golden") or {}).get(
                "target_balance_all_integer_equal"
            ),
            "eoa_mismatch_pct": (gf.get("attribution_gate") or {}).get("eoa_mismatch_pct"),
            "note": "GL2-B FIFO is a 20/20 integer gate on a frozen sample, not vendor-PnL truth.",
        }
    payload = {
        "generated_at": utc_now_iso(),
        "n_board_addresses": len(boards),
        "n_b3_golden_accounts": len(b3),
        "n_actor_flow_golden_actors": len(af),
        "n_gl2b_fifo_txs": len(gl2_tx),
        "overlap_board_b3": overlap_b3,
        "overlap_board_actor_flow": overlap_af,
        "n_overlap_b3": len(overlap_b3),
        "n_overlap_actor_flow": len(overlap_af),
        "gl2b_fifo_gate": gl2_gate,
        "rule": "If a board address hits a golden account, our FIFO for that address must not contradict the golden integer checks. Vendor PnL is never golden.",
    }
    write_json(ROOT / "out/golden_join.json", payload)
    return payload


if __name__ == "__main__":
    boards_path = ROOT / "data/normalized/boards.json"
    addrs = []
    if boards_path.exists():
        addrs = [r["address"] for r in read_json(boards_path).get("rows", [])]
    print(json.dumps({k: join_goldens(addrs)[k] for k in ("n_board_addresses", "n_overlap_b3")}, indent=2))
