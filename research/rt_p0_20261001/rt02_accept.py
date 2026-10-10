#!/usr/bin/env python3
"""RT02 identifier. Classifies a supplied book. Does not measure markets."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEC = ROOT / "RT02_RIGHT_TAIL_ACCEPTANCE.md"
FREEZE = ROOT / "rt02_freeze.json"
VERSION = "RT02-2026-10-01"

ESTIMANDS = ("wallet_pnl", "our_copy_pnl", "selection_gain", "holding_gain")
CONFIRMED = {"confirmed_fill", "confirmed_fail"}


def spec_sha256() -> str:
    return hashlib.sha256(SPEC.read_bytes()).hexdigest()


def assert_frozen() -> dict:
    freeze = json.loads(FREEZE.read_text())
    got = spec_sha256()
    if freeze.get("version") != VERSION or freeze.get("spec_sha256") != got:
        raise SystemExit(
            f"RT02 spec moved after freeze: file {got} freeze {freeze.get('spec_sha256')}"
        )
    return freeze


def evaluate(lots: list[dict], estimand: str) -> dict:
    if estimand not in ESTIMANDS:
        raise ValueError(estimand)
    rows = [r for r in lots if r.get("estimand") == estimand]
    rows = sorted(rows, key=lambda r: (r["time"], r["lot_id"]))

    quote = [r for r in rows if r["exit_status"] == "quote_only"]
    unknown = [r for r in rows if r["exit_status"] == "unknown"]
    confirmed = [r for r in rows if r["exit_status"] in CONFIRMED]
    inventory_bad = [r for r in rows if not r.get("inventory_complete")]

    wins = [r for r in confirmed if r["net"] > 0]
    point = sum(r["net"] for r in confirmed)
    bound_low = point + sum(-abs(r["notional"]) for r in unknown)
    bound_high = point + sum(0.0 for _ in unknown)

    by_event: dict[str, float] = {}
    for r in confirmed:
        by_event[r["event_id"]] = by_event.get(r["event_id"], 0.0) + r["net"]
    event_nets = list(by_event.values())
    top_share = None
    drop_top = None
    if event_nets and point != 0:
        top = max(event_nets, key=abs)
        top_share = top / point
        drop_top = point - top

    path = []
    cash = 0.0
    for r in confirmed:
        cash += r["net"]
        path.append({"time": r["time"], "lot_id": r["lot_id"], "equity": cash})

    reason = None
    certified = False
    if not rows:
        reason = "EMPTY_ESTIMAND"
    elif quote:
        reason = "UNREALIZED_QUOTE"
    elif inventory_bad:
        reason = "OPENING_INVENTORY_MISSING"
    elif unknown:
        reason = "UNKNOWN_EXIT"
    elif point > 0:
        certified = True
        reason = "ABSOLUTE_NET_POSITIVE"
    else:
        reason = "ABSOLUTE_NET_NOT_POSITIVE"

    return {
        "version": VERSION,
        "estimand": estimand,
        "certified": certified,
        "reason": reason,
        "n": len(rows),
        "n_confirmed": len(confirmed),
        "n_unknown": len(unknown),
        "n_quote": len(quote),
        "winrate_confirmed": (len(wins) / len(confirmed)) if confirmed else None,
        "net_confirmed": point,
        "bound_low_unknown_as_full_loss": bound_low,
        "bound_high_unknown_as_zero": bound_high,
        "n_events": len(by_event),
        "top_event_share": top_share,
        "net_without_top_event": drop_top,
        "equity_path": path,
        "diagnostics_are_not_vetoes": True,
    }


def _lot(i, net, status="confirmed_fill", inventory=True, event=None, estimand="our_copy_pnl"):
    return {
        "lot_id": f"L{i}",
        "event_id": event or f"E{i}",
        "entity_id": "fixture",
        "token_id": event or f"T{i}",
        "time": f"2026-01-01T00:{i:02d}:00Z",
        "estimand": estimand,
        "net": net,
        "exit_status": status,
        "inventory_complete": inventory,
        "notional": abs(net) if net else 1.0,
    }


def fixtures() -> dict:
    assert_frozen()
    low = [_lot(i, -1.0) for i in range(9)] + [_lot(9, 20.0, event="winner")]
    quote = [_lot(i, -1.0) for i in range(9)] + [
        _lot(9, 20.0, status="quote_only", event="winner")
    ]
    missing = [_lot(i, -1.0, inventory=False) for i in range(9)] + [
        _lot(9, 20.0, inventory=False, event="winner")
    ]
    unknown = [
        _lot(0, 5.0, event="known"),
        {
            "lot_id": "U",
            "event_id": "unk",
            "entity_id": "fixture",
            "token_id": "unk",
            "time": "2026-01-01T00:01:00Z",
            "estimand": "our_copy_pnl",
            "net": None,
            "exit_status": "unknown",
            "inventory_complete": True,
            "notional": 10.0,
        },
    ]
    median_neg = [_lot(0, -1), _lot(1, -1), _lot(2, -1), _lot(3, 10)]
    out = {
        "low_winrate_positive": evaluate(low, "our_copy_pnl"),
        "unrealized_quote": evaluate(quote, "our_copy_pnl"),
        "missing_inventory": evaluate(missing, "our_copy_pnl"),
        "unknown_exit": evaluate(unknown, "our_copy_pnl"),
        "median_negative_net_positive": evaluate(median_neg, "our_copy_pnl"),
    }
    checks = {
        "low_winrate_certified": out["low_winrate_positive"]["certified"] is True
        and out["low_winrate_positive"]["winrate_confirmed"] == 0.1
        and out["low_winrate_positive"]["net_confirmed"] == 11.0,
        "quote_rejected": out["unrealized_quote"]["reason"] == "UNREALIZED_QUOTE"
        and out["unrealized_quote"]["certified"] is False,
        "inventory_rejected": out["missing_inventory"]["reason"] == "OPENING_INVENTORY_MISSING",
        "unknown_not_certified": out["unknown_exit"]["reason"] == "UNKNOWN_EXIT"
        and out["unknown_exit"]["n_unknown"] == 1
        and out["unknown_exit"]["bound_low_unknown_as_full_loss"] == -5.0
        and out["unknown_exit"]["bound_high_unknown_as_zero"] == 5.0,
        "median_not_a_veto": out["median_negative_net_positive"]["certified"] is True
        and out["median_negative_net_positive"]["net_confirmed"] == 7.0,
    }
    if not all(checks.values()):
        raise SystemExit(json.dumps({"checks": checks, "out": out}, indent=2))
    return {"checks": checks, "results": out}


if __name__ == "__main__":
    print(json.dumps(fixtures()["checks"], indent=2))
