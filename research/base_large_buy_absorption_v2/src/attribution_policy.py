"""Canonical Base V3 wallet-attribution policy (adapted from EATLQ base_attribution)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

CFG = Path("/root/base_actor_flow_oos_v0/configs")
MANIFEST_PATH = CFG / "base_attribution_exclusions.json"
ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")


def load_manifest(path: Path = MANIFEST_PATH) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    addresses = [row["address"] for row in data["addresses"]]
    if len(addresses) != len(set(addresses)):
        raise ValueError("duplicate attribution exclusion address")
    if not all(ADDRESS_RE.fullmatch(address) for address in addresses):
        raise ValueError("non-canonical address in attribution exclusion manifest")
    return data


MANIFEST = load_manifest()
CHAIN_ID = int(MANIFEST["chain_id"])
EXCLUSION_ROWS = tuple(MANIFEST["addresses"])
EXCLUDED_ADDRESSES = frozenset(row["address"] for row in EXCLUSION_ROWS)
QUOTE_TOKENS = frozenset(
    row["address"] for row in EXCLUSION_ROWS if row["category"] == "quote_token"
)
ROUTER_ADDRESSES = frozenset(
    row["address"] for row in EXCLUSION_ROWS if row["category"] in ("router", "aggregator", "npm", "quoter", "permit", "account_infra")
)


@dataclass(frozen=True)
class Attribution:
    address: str
    source: str


def normalize_address(value: object) -> str:
    address = str(value or "").strip().lower()
    return address if ADDRESS_RE.fullmatch(address) else ""


def is_attributable(value: object) -> bool:
    address = normalize_address(value)
    return bool(address and address not in EXCLUDED_ADDRESSES)


def resolve_eoa(tx_from: object, recipient: object, sender: object) -> Attribution:
    """Return the first valid, non-infrastructure address in canonical order."""
    for source, candidate in (
        ("tx_from", tx_from),
        ("recipient", recipient),
        ("sender", sender),
    ):
        address = normalize_address(candidate)
        if address and address not in EXCLUDED_ADDRESSES:
            return Attribution(address, source)
    return Attribution("", "unresolved")


def target_trade_side(
    amount0: int, amount1: int, token0: object, token1: object
) -> str:
    """Classify side relative to the non-quote token."""
    t0, t1 = normalize_address(token0), normalize_address(token1)
    if t0 in QUOTE_TOKENS and t1 not in QUOTE_TOKENS:
        target_amount = int(amount1)
    elif t1 in QUOTE_TOKENS and t0 not in QUOTE_TOKENS:
        target_amount = int(amount0)
    else:
        return "unknown"
    if target_amount < 0:
        return "buy"
    if target_amount > 0:
        return "sell"
    return "unknown"
