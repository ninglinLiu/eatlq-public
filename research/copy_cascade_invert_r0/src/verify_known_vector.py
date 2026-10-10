"""Public-RPC known vector: CAT sell quote must equal 0.012344272076008947 ETH."""
from __future__ import annotations

from src.common import ROOT, write_json
from src.fifo_live import classify_tx, legs_from_receipt
from src.reconstruct_live import _receipt_and_tx
from datetime import datetime, timezone

WALLET = "0xfd7e55a555555c2f25053a38ec744de1afea4fa4"
TX = "0x2e5fa72361a0e37bb31bfc01c08aa1d65a1a04f6eca00e8243c7fd90bb3eebfe"
EXPECT_ETH = 0.012344272076008947


def verify() -> dict:
    rec, _ = _receipt_and_tx("base", TX)
    legs = legs_from_receipt(rec or {})
    fills = classify_tx(
        wallet=WALLET,
        ts=datetime(2026, 9, 15, 17, 30, 13, tzinfo=timezone.utc),
        tx=TX,
        legs=legs,
        native_out=0.0,
        native_in=0.0,
        chain="base",
        native_usd=1.0,
    )
    got = fills[0].proceeds_usd if fills and fills[0].kind == "sell" else None
    payload = {
        "tx": TX,
        "wallet": WALLET,
        "expect_eth": EXPECT_ETH,
        "got_eth": got,
        "kind": fills[0].kind if fills else None,
        "quote_kind": fills[0].quote_kind if fills else None,
        "n_receipt_logs": len((rec or {}).get("logs") or []),
        "match": got is not None and abs(got - EXPECT_ETH) < 1e-12,
        "rpc": "https://mainnet.base.org",
    }
    write_json(ROOT / "out/live_rpc_verify_base.json", payload)
    return payload


if __name__ == "__main__":
    print(verify())
