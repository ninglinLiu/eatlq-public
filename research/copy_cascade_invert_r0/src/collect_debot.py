"""Snapshot Debot boards. No RPC. No login bypass.

Two distinct Debot boards exist and MUST NOT be conflated:

- `/api/contract/discover/wallets` — open, no auth. Fields are perp-shaped
  (long/short realized pnl, max_drawdown_ratio, volume) and the route chunk
  imports `useHyperliquidWallets`. Verified: sampled wallets have Hyperliquid
  `userFills` and zero Base ERC-20 transfers. Venue = `hyperliquid_perp`.
- `/api/dashboard/wallet/smart_wallet` — the Base/BSC spot smart-money board.
  Returns `code=101 not login`. Stays blocked; no cookie forging.
"""
from __future__ import annotations

import json

from src.common import ROOT, http_get, snapshot_record, write_json

OUT = ROOT / "data/raw/debot"
SOURCE = "debot"

API = "https://debot.ai/api"
DISCOVER_WALLETS = f"{API}/contract/discover/wallets"
SPOT_SMART_WALLET = f"{API}/dashboard/wallet/smart_wallet"

DURATIONS = ("7d", "30d", "all")
PAGE_SIZE = 100
MAX_PAGES = 5

HDRS = {
    "Accept": "application/json",
    "Referer": "https://debot.ai/discover",
    "Origin": "https://debot.ai",
}


def collect() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    snapshots = []
    perp_rows = 0

    for duration in DURATIONS:
        next_page = None
        for _ in range(MAX_PAGES):
            url = f"{DISCOVER_WALLETS}?duration={duration}&page_size={PAGE_SIZE}"
            if next_page:
                url += f"&next={next_page}"
            status, body, meta = http_get(url, headers=HDRS, sleep_s=1.2)
            rec = snapshot_record(
                source=SOURCE, chain="hyperliquid", url=url, status=status, body=body,
                extra={
                    "duration": duration,
                    "venue": "hyperliquid_perp",
                    "board": "discover_wallets",
                    "auth": "none",
                    "transport_meta": meta,
                },
            )
            stamp = rec["captured_at"].replace(":", "")
            raw_path = OUT / f"discover_{duration}_p{next_page or 1}_{stamp}.bin"
            raw_path.write_bytes(body)
            rec["raw_path"] = str(raw_path)

            payload = _parse(body)
            data = (payload or {}).get("data") or {}
            records = data.get("records") or []
            rec["row_hint"] = len(records)
            rec["total"] = data.get("total")
            perp_rows += len(records)
            snapshots.append(rec)

            next_page = data.get("next")
            if not next_page or not records:
                break

    # Spot board: probe once, record the refusal verbatim. Do not bypass.
    status, body, meta = http_get(f"{SPOT_SMART_WALLET}?chain=base", headers=HDRS, sleep_s=1.2)
    payload = _parse(body)
    spot = snapshot_record(
        source=SOURCE, chain="base", url=f"{SPOT_SMART_WALLET}?chain=base",
        status=status, body=body,
        extra={
            "board": "spot_smart_wallet",
            "venue": "base_bsc_spot",
            "auth": "required",
            "vendor_code": (payload or {}).get("code"),
            "vendor_message": (payload or {}).get("message_en"),
            "transport_meta": meta,
        },
    )
    stamp = spot["captured_at"].replace(":", "")
    spot_path = OUT / f"spot_smart_wallet_base_{stamp}.bin"
    spot_path.write_bytes(body)
    spot["raw_path"] = str(spot_path)
    spot["row_hint"] = 0
    spot["ok"] = False
    snapshots.append(spot)

    summary = {
        "source": SOURCE,
        # Perp board captured; the Base/BSC spot board is what this project needs.
        "status": "CAPTURED_WRONG_VENUE" if perp_rows else "SOURCE_BLOCKED",
        "spot_board_status": "SOURCE_BLOCKED_AUTH",
        "n_requests": len(snapshots),
        "n_ok_with_rows": sum(1 for s in snapshots if s.get("row_hint", 0) > 0),
        "n_perp_rows": perp_rows,
        "venue_note": (
            "Debot's open Discover board is Hyperliquid perp, not Base/BSC spot. "
            "Perp rows must never be reconciled against Base/BSC ERC-20 FIFO."
        ),
        "spot_note": (
            "https://debot.ai/api/dashboard/wallet/smart_wallet returns code=101 "
            "'not login'. Cookies were not forged."
        ),
        "snapshots": snapshots,
    }
    write_json(OUT / "collect_summary.json", summary)
    return summary


def _parse(body: bytes):
    try:
        return json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None


if __name__ == "__main__":
    s = collect()
    print(json.dumps({k: s[k] for k in ("source", "status", "spot_board_status", "n_perp_rows")}, indent=2))
