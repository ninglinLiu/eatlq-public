"""BSC contemporaneous reconstruct.

Public BSC RPC is live. There is no free Blockscout/Etherscan-v2 index for
BSC on this host, so the tx universe is GMGN public wallet_activity hashes;
token legs come from public-RPC receipts (not GMGN quote_amount).
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path("/root/copy_cascade_invert_r0")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common import (
    ROOT as PROJ,
    ensure_no_live,
    norm_addr,
    utc_now,
    utc_now_iso,
    write_json,
)
from src.fifo_live import apply_fifo, classify_tx, legs_from_receipt, native_usd_at
from src.public_http import http_json, rpc_call
from src.reconstruct_live import (
    CHAIN_CFG,
    LOOKBACK_DAYS,
    _sign,
    _tx_cap,
    fetch_native_price,
    refresh_vendors,
    rpc_head,
    unique_board_addresses,
)

MAX_ACTIVITY_PAGES = 20


def fetch_gmgn_activity(chain: str, address: str, cutoff: datetime) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    cursor = None
    pages = 0
    truncated = False
    while pages < MAX_ACTIVITY_PAGES:
        url = (
            f"https://gmgn.ai/vas/api/v1/wallet_activity/{chain}"
            f"?wallet={address}&limit=50&type=buy,sell"
        )
        if cursor:
            url += f"&cursor={cursor}"
        status, parsed, _ = http_json(
            url,
            headers={"Referer": f"https://gmgn.ai/{chain}/address/{address}"},
            sleep_s=0.16,
            timeout=25.0,
        )
        pages += 1
        if status != 200 or not isinstance(parsed, dict):
            return rows, {"pages": pages, "truncated": True, "http_status": status, "error": "activity_fail"}
        data = parsed.get("data") or {}
        acts = data.get("activities") or []
        stop = False
        for a in acts:
            if not isinstance(a, dict):
                continue
            if (a.get("event_type") or "") not in {"buy", "sell"}:
                continue
            ts = a.get("timestamp")
            try:
                dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                dt = None
            if dt and dt < cutoff:
                stop = True
                continue
            rows.append(a)
        cursor = data.get("next")
        if stop or not cursor or not acts:
            break
    else:
        truncated = True
    return rows, {"pages": pages, "truncated": truncated, "n": len(rows)}


def _decimals_map(acts: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for a in acts:
        tok = a.get("token") or {}
        addr = norm_addr(tok.get("address") or tok.get("token_address"))
        q = a.get("quote_token") or {}
        qa = norm_addr(q.get("token_address") or q.get("address") or a.get("quote_address"))
        for item_addr, item in ((addr, tok), (qa, q)):
            if not item_addr:
                continue
            dec = item.get("decimals")
            if dec in (None, ""):
                continue
            try:
                out[item_addr] = int(dec)
            except (TypeError, ValueError):
                continue
    return out


def fetch_receipt(chain: str, tx: str) -> dict | None:
    cfg = CHAIN_CFG[chain]
    try:
        return rpc_call(cfg["rpc"], "eth_getTransactionReceipt", [tx])
    except Exception:
        return None


def reconstruct_wallet_activity_rpc(
    chain: str,
    address: str,
    *,
    native_spot: float,
    daily: dict[str, float],
    vendor: dict | None,
    now: datetime,
) -> dict:
    address = norm_addr(address)
    out_path = PROJ / "data/live_recon/wallets" / chain / f"{address}.json"
    if out_path.exists():
        try:
            prev = json.loads(out_path.read_text())
            if prev.get("ok") and prev.get("method") == "gmgn_activity_hashes_public_rpc_legs":
                return prev
        except Exception:
            pass

    cutoff = now - timedelta(days=LOOKBACK_DAYS)
    acts, ameta = fetch_gmgn_activity(chain, address, cutoff)
    decimals = _decimals_map(acts)
    # unique txs most recent first
    seen = []
    have = set()
    tx_ts = {}
    for a in acts:
        txh = (a.get("tx_hash") or "").lower()
        if not txh.startswith("0x") or txh in have:
            continue
        have.add(txh)
        try:
            tx_ts[txh] = datetime.fromtimestamp(int(a.get("timestamp")), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            tx_ts[txh] = now
        seen.append(txh)
    cap = _tx_cap((vendor or {}).get("buy_30d"))
    want = seen[:cap]

    receipts = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch_receipt, chain, txh): txh for txh in want}
        for fut in as_completed(futs):
            receipts[futs[fut]] = fut.result()

    fills = []
    n_receipt_ok = 0
    for txh in want:
        rec = receipts.get(txh)
        if not rec:
            continue
        n_receipt_ok += 1
        legs = legs_from_receipt(rec, decimals)
        ts = tx_ts[txh]
        usd = native_usd_at(ts, daily, native_spot)
        try:
            native_val = int(rec.get("value") or rec.get("l1_value") or 0)
            if isinstance(rec.get("value"), str):
                native_val = int(rec.get("value"), 16) if str(rec.get("value")).startswith("0x") else int(rec.get("value") or 0)
        except (TypeError, ValueError):
            native_val = 0
        native_eth = native_val / 1e18
        frm = norm_addr(rec.get("from"))
        classified = classify_tx(
            wallet=address,
            ts=ts,
            tx=txh,
            legs=legs,
            native_out=native_eth if frm == address else 0.0,
            native_in=0.0,
            chain=chain,
            native_usd=usd,
        )
        fills.extend(classified)

    fifo = apply_fifo(fills, now=now)
    rec = {
        "ok": True,
        "address": address,
        "chain": chain,
        "method": "gmgn_activity_hashes_public_rpc_legs",
        "generated_at": utc_now_iso(),
        "lookback_days": LOOKBACK_DAYS,
        "n_activity": len(acts),
        "n_unique_txs": len(seen),
        "n_txs_used": len(want),
        "n_receipt_ok": n_receipt_ok,
        "truncated": bool(ameta.get("truncated")) or len(seen) > cap,
        "activity_meta": ameta,
        "our_realized_usd_1d": fifo.realized_usd_1d,
        "our_realized_usd_7d": fifo.realized_usd_7d,
        "our_realized_usd_30d": fifo.realized_usd_30d,
        "n_buy": fifo.n_buy,
        "n_sell": fifo.n_sell,
        "n_transfer_in": fifo.n_transfer_in,
        "n_transfer_out": fifo.n_transfer_out,
        "n_closed_7d": fifo.n_closed_7d,
        "n_unmatched_sell": fifo.n_unmatched_sell,
        "unmatched_sell_qty": fifo.unmatched_sell_qty,
        "n_fills": fifo.n_fills,
        "vendor": vendor,
        "sign_7d_ours": _sign(fifo.realized_usd_7d),
        "sign_7d_vendor": _sign((vendor or {}).get("realized_profit_7d")),
        "index_note": "BSC has no free public Blockscout on this host; hashes from GMGN activity, legs from public RPC",
    }
    if vendor and rec["sign_7d_ours"] is not None and rec["sign_7d_vendor"] is not None:
        rec["sign_agree_7d"] = rec["sign_7d_ours"] == rec["sign_7d_vendor"]
    write_json(out_path, rec)
    return rec


def reconstruct_bsc(*, workers: int = 6) -> dict:
    ensure_no_live()
    chain = "bsc"
    now = utc_now()
    addresses = unique_board_addresses(chain)
    head = rpc_head(chain)
    native_spot, daily = fetch_native_price(chain)
    vendors = refresh_vendors(chain, addresses, workers=5)
    results = []
    done = 0

    def one(addr: str):
        return reconstruct_wallet_activity_rpc(
            chain,
            addr,
            native_spot=native_spot,
            daily=daily,
            vendor=vendors.get(addr),
            now=now,
        )

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(one, a): a for a in addresses}
        for fut in as_completed(futs):
            rec = fut.result()
            results.append(rec)
            done += 1
            if done % 20 == 0 or done == len(addresses):
                write_json(
                    PROJ / "data/live_recon/progress_bsc.json",
                    {"done": done, "total": len(addresses), "updated_at": utc_now_iso(), "last": rec.get("address")},
                )

    ok_rows = [r for r in results if r.get("ok")]
    payload = {
        "generated_at": utc_now_iso(),
        "chain": chain,
        "rpc": head,
        "native_spot_usd": native_spot,
        "n_daily_prices": len(daily),
        "n_addresses": len(addresses),
        "n_ok": len(ok_rows),
        "n_fail": len(results) - len(ok_rows),
        "lookback_days": LOOKBACK_DAYS,
        "index": "gmgn_activity_hashes",
        "rpc_kind": "public_https_only",
        "eatlq_archive_used": False,
        "method": "gmgn_activity_hashes_public_rpc_legs",
        "rows": results,
    }
    write_json(PROJ / "data/live_recon/reconstruct_bsc.json", payload)
    return payload


if __name__ == "__main__":
    p = reconstruct_bsc()
    print(json.dumps({k: p[k] for k in ("chain", "n_addresses", "n_ok", "n_fail", "native_spot_usd")}, indent=2))
