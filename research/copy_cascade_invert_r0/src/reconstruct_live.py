"""Rebuild contemporaneous FIFO from public Base index + public RPC.

Does not use EATLQ archive RPC, ClickHouse, or any private endpoint.
Writes only under this project.
"""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlencode

ROOT = Path("/root/copy_cascade_invert_r0")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common import (
    ROOT as PROJ,
    ensure_no_live,
    norm_addr,
    parse_iso,
    read_json,
    utc_now,
    utc_now_iso,
    write_json,
)
from src.fifo_live import (
    FifoResult,
    apply_fifo,
    classify_tx,
    legs_from_blockscout_items,
    legs_from_receipt,
    native_usd_at,
)
from src.public_http import http_json, rpc_batch, rpc_call

LOOKBACK_DAYS = 40
MAX_TRANSFER_PAGES = 60
TX_CACHE_LOCK = Lock()
PROGRESS_LOCK = Lock()
_BS_LOCK = Lock()
_BS_NEXT = [0.0]

CHAIN_CFG = {
    "base": {
        "blockscout": "https://base.blockscout.com/api/v2",
        "rpc": [
            "https://mainnet.base.org",
            "https://base.publicnode.com",
            "https://1rpc.io/base",
        ],
        "gmgn": "base",
        "referer": "https://gmgn.ai/base/address/",
    },
    "bsc": {
        "blockscout": "https://bsc.blockscout.com/api/v2",
        "rpc": [
            "https://bsc-dataseed.binance.org",
            "https://bsc.publicnode.com",
            "https://1rpc.io/bnb",
        ],
        "gmgn": "bsc",
        "referer": "https://gmgn.ai/bsc/address/",
    },
}


def _cutoff() -> datetime:
    return utc_now() - timedelta(days=LOOKBACK_DAYS)


def fetch_wallet_new(chain: str, address: str) -> dict:
    cfg = CHAIN_CFG[chain]
    url = f"https://gmgn.ai/defi/quotation/v1/smartmoney/{cfg['gmgn']}/walletNew/{address}"
    status, parsed, body = http_json(
        url,
        headers={"Referer": cfg["referer"] + address},
        sleep_s=0.18,
        timeout=25.0,
    )
    data = (parsed or {}).get("data") if isinstance(parsed, dict) else None
    raw_dir = PROJ / "data/raw/gmgn/wallet_new" / chain
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / f"{address}.json").write_bytes(body or b"")
    return {
        "ok": status == 200 and isinstance(data, dict),
        "http_status": status,
        "address": address,
        "chain": chain,
        "realized_profit_1d": _f((data or {}).get("realized_profit_1d")),
        "realized_profit_7d": _f((data or {}).get("realized_profit_7d")),
        "realized_profit_30d": _f((data or {}).get("realized_profit_30d")),
        "pnl_1d": _f((data or {}).get("pnl_1d")),
        "pnl_7d": _f((data or {}).get("pnl_7d")),
        "pnl_30d": _f((data or {}).get("pnl_30d")),
        "buy_7d": _f((data or {}).get("buy_7d")),
        "sell_7d": _f((data or {}).get("sell_7d")),
        "buy_30d": _f((data or {}).get("buy_30d")),
        "sell_30d": _f((data or {}).get("sell_30d")),
        "winrate": _f((data or {}).get("winrate")),
        "updated_at": (data or {}).get("updated_at"),
    }


def _f(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_native_price(chain: str) -> tuple[float, dict[str, float]]:
    cache = PROJ / "data/live_recon" / f"prices_{chain}.json"
    if cache.exists():
        try:
            prev = read_json(cache)
            if (prev.get("daily") or {}) and prev.get("spot"):
                return float(prev["spot"]), {str(k): float(v) for k, v in prev["daily"].items()}
        except Exception:
            pass
    cfg = CHAIN_CFG[chain]
    daily: dict[str, float] = {}
    spot = None
    status, parsed, _ = http_json(f"{cfg['blockscout']}/stats", sleep_s=0.05)
    if isinstance(parsed, dict) and parsed.get("coin_price"):
        try:
            spot = float(parsed["coin_price"])
        except (TypeError, ValueError):
            spot = None
    status, parsed, _ = http_json(f"{cfg['blockscout']}/stats/charts/market", sleep_s=0.05)
    if isinstance(parsed, dict):
        for row in parsed.get("chart_data") or []:
            try:
                daily[str(row["date"])] = float(row["closing_price"])
            except (KeyError, TypeError, ValueError):
                continue
    if spot is None and daily:
        spot = daily[max(daily)]
    if spot is None or not daily:
        # public fallbacks; never EATLQ
        cg_id = "ethereum" if chain == "base" else "binancecoin"
        st, parsed, _ = http_json(
            f"https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart?vs_currency=usd&days=30",
            sleep_s=0.1,
            timeout=30.0,
        )
        if isinstance(parsed, dict):
            for ts_ms, price in parsed.get("prices") or []:
                try:
                    day = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date().isoformat()
                    daily[day] = float(price)
                except (TypeError, ValueError, OSError):
                    continue
            if daily and spot is None:
                spot = daily[max(daily)]
        if spot is None:
            st, parsed, _ = http_json(
                f"https://api.coinbase.com/v2/prices/{'ETH' if chain=='base' else 'BNB'}-USD/spot",
                sleep_s=0.05,
            )
            try:
                spot = float(((parsed or {}).get("data") or {}).get("amount"))
            except (TypeError, ValueError):
                spot = 0.0
    if spot is None:
        spot = 0.0
    write_json(cache, {"spot": spot, "daily": daily, "generated_at": utc_now_iso()})
    return spot, daily


def rpc_head(chain: str) -> dict:
    cfg = CHAIN_CFG[chain]
    try:
        hex_n = rpc_call(cfg["rpc"], "eth_blockNumber", [])
        n = int(hex_n, 16)
        return {"ok": True, "head": n, "head_hex": hex_n, "rpc": cfg["rpc"][0]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:240], "rpc": cfg["rpc"][0]}


def blockscout_get(url: str) -> tuple[int, Any]:
    """One Blockscout request at a time. Public API 429s under fan-out."""
    with _BS_LOCK:
        wait = _BS_NEXT[0] - time.time()
        if wait > 0:
            time.sleep(wait)
        status, parsed, _ = http_json(url, sleep_s=0.0, retries=1, timeout=30.0)
        if status == 429:
            time.sleep(8.0)
            status, parsed, _ = http_json(url, sleep_s=0.0, retries=4, timeout=30.0)
        gap = 1.15 if status == 429 else 0.42
        _BS_NEXT[0] = time.time() + gap
        return status, parsed


def _paginate_blockscout(url: str, cutoff: datetime, max_pages: int) -> tuple[list[dict], dict]:
    items: list[dict] = []
    next_params = None
    truncated = False
    pages = 0
    while pages < max_pages:
        full = url
        if next_params:
            sep = "&" if "?" in url else "?"
            full = url + sep + urlencode({k: v for k, v in next_params.items() if v is not None})
        status, parsed = blockscout_get(full)
        pages += 1
        if status != 200 or not isinstance(parsed, dict):
            return items, {"pages": pages, "truncated": True, "http_status": status, "error": "page_fail"}
        if parsed.get("message") and "too many" in str(parsed.get("message")).lower():
            return items, {"pages": pages, "truncated": True, "http_status": status, "error": "page_fail"}
        batch = parsed.get("items") or []
        items.extend(batch)
        stop = False
        for row in batch:
            ts = parse_iso(row.get("timestamp"))
            if ts and ts < cutoff:
                stop = True
        next_params = parsed.get("next_page_params")
        if stop or not next_params or not batch:
            break
    else:
        truncated = True
    # drop older than cutoff
    kept = []
    for row in items:
        ts = parse_iso(row.get("timestamp"))
        if ts is None or ts >= cutoff:
            kept.append(row)
    return kept, {"pages": pages, "truncated": truncated, "n_raw": len(items), "n_kept": len(kept)}


def _tx_cache_path(chain: str, tx: str) -> Path:
    h = tx.lower()
    d = PROJ / "data/live_recon/tx_cache" / chain / h[2:4]
    return d / f"{h}.json"


def fetch_tx_token_transfers(chain: str, tx: str) -> dict:
    path = _tx_cache_path(chain, tx)
    if path.exists():
        try:
            return read_json(path)
        except Exception:  # noqa: BLE001
            pass
    cfg = CHAIN_CFG[chain]
    status, parsed, _ = http_json(
        f"{cfg['blockscout']}/transactions/{tx}/token-transfers",
        sleep_s=0.04,
        timeout=25.0,
    )
    rec = {
        "tx": tx.lower(),
        "ok": status == 200 and isinstance(parsed, dict),
        "http_status": status,
        "items": (parsed or {}).get("items") if isinstance(parsed, dict) else [],
    }
    if rec["ok"]:
        with TX_CACHE_LOCK:
            write_json(path, rec)
    return rec


def _receipt_and_tx(chain: str, tx: str) -> tuple[dict | None, dict | None]:
    cfg = CHAIN_CFG[chain]
    try:
        rec, txobj = rpc_batch(
            cfg["rpc"],
            [
                ("eth_getTransactionReceipt", [tx]),
                ("eth_getTransactionByHash", [tx]),
            ],
        )
        return rec if isinstance(rec, dict) else None, txobj if isinstance(txobj, dict) else None
    except Exception:
        try:
            rec = rpc_call(cfg["rpc"], "eth_getTransactionReceipt", [tx])
            return rec if isinstance(rec, dict) else None, None
        except Exception:
            return None, None


def fetch_tx_native(chain: str, tx: str) -> tuple[str, float]:
    """Return (from_addr, value_native)."""
    cfg = CHAIN_CFG[chain]
    status, parsed, _ = http_json(
        f"{cfg['blockscout']}/transactions/{tx}",
        sleep_s=0.04,
        timeout=25.0,
    )
    if status != 200 or not isinstance(parsed, dict):
        return "", 0.0
    try:
        value = int(parsed.get("value") or 0) / 1e18
    except (TypeError, ValueError):
        value = 0.0
    frm = norm_addr((parsed.get("from") or {}).get("hash"))
    return frm, value


def unique_board_addresses(chain: str) -> list[str]:
    boards = read_json(PROJ / "data/normalized/boards.json")
    out = []
    seen = set()
    for row in boards.get("rows") or []:
        if row.get("source") != "gmgn" or row.get("chain") != chain:
            continue
        addr = norm_addr(row.get("address"))
        if addr and addr not in seen:
            seen.add(addr)
            out.append(addr)
    return out


def _tx_cap(buy_30d) -> int:
    b = int(buy_30d or 0)
    if b > 300:
        return 800
    if b > 100:
        return 450
    return 280


def reconstruct_wallet(
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
            prev = read_json(out_path)
            if (
                prev.get("ok")
                and prev.get("lookback_days") == LOOKBACK_DAYS
                and prev.get("method") == "blockscout_address_list_public_rpc_receipts"
            ):
                return prev
        except Exception:  # noqa: BLE001
            pass

    cfg = CHAIN_CFG[chain]
    cutoff = _cutoff()
    xfers, xmeta = _paginate_blockscout(
        f"{cfg['blockscout']}/addresses/{address}/token-transfers?type=ERC-20",
        cutoff,
        MAX_TRANSFER_PAGES,
    )
    if xmeta.get("http_status") not in (None, 200) and not xfers:
        rec = {
            "ok": False,
            "address": address,
            "chain": chain,
            "error": "blockscout_transfers_failed",
            "meta": xmeta,
            "generated_at": utc_now_iso(),
        }
        write_json(out_path, rec)
        return rec

    by_tx: dict[str, list[dict]] = {}
    tx_ts: dict[str, datetime] = {}
    for row in xfers:
        txh = (row.get("transaction_hash") or "").lower()
        if not txh.startswith("0x"):
            continue
        by_tx.setdefault(txh, []).append(row)
        ts = parse_iso(row.get("timestamp"))
        if ts and (txh not in tx_ts or ts < tx_ts[txh]):
            tx_ts[txh] = ts

    # most-recent txs first for cap, then FIFO in chronological order
    ordered_txs = sorted(tx_ts.items(), key=lambda kv: kv[1], reverse=True)
    cap = _tx_cap((vendor or {}).get("buy_30d"))
    if len(ordered_txs) > cap:
        ordered_txs = ordered_txs[:cap]
    want = [h for h, _ in ordered_txs]

    decimals: dict[str, int] = {}
    for row in xfers:
        tok = row.get("token") or {}
        addr_t = norm_addr(tok.get("address_hash") or tok.get("address"))
        dec = (row.get("total") or {}).get("decimals") or tok.get("decimals")
        if addr_t and dec not in (None, ""):
            try:
                decimals[addr_t] = int(dec)
            except (TypeError, ValueError):
                pass

    fills = []
    n_tx_fetch = 0
    n_native_fetch = 0
    receipts: dict[str, tuple[dict | None, dict | None]] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_receipt_and_tx, chain, txh): txh for txh in want}
        for fut in as_completed(futs):
            receipts[futs[fut]] = fut.result()
            n_tx_fetch += 1
    for txh in want:
        receipt, txobj = receipts.get(txh) or (None, None)
        if receipt:
            legs = legs_from_receipt(receipt, decimals)
        else:
            legs = legs_from_blockscout_items(by_tx[txh])
        ts = tx_ts[txh]
        usd = native_usd_at(ts, daily, native_spot)
        native_out = 0.0
        if txobj:
            n_native_fetch += 1
            frm = norm_addr(txobj.get("from"))
            try:
                raw_v = txobj.get("value") or "0x0"
                native_val = int(raw_v, 16) if isinstance(raw_v, str) else int(raw_v)
            except (TypeError, ValueError):
                native_val = 0
            if frm == address:
                native_out = native_val / 1e18
        classified = classify_tx(
            wallet=address,
            ts=ts,
            tx=txh,
            legs=legs,
            native_out=native_out,
            native_in=0.0,
            chain=chain,
            native_usd=usd,
        )
        fills.extend(classified)

    fifo: FifoResult = apply_fifo(fills, now=now)
    rec = {
        "ok": True,
        "address": address,
        "chain": chain,
        "generated_at": utc_now_iso(),
        "lookback_days": LOOKBACK_DAYS,
        "n_address_transfers": len(xfers),
        "n_unique_txs": len(by_tx),
        "n_txs_used": len(want),
        "n_tx_fetch": n_tx_fetch,
        "n_native_fetch": n_native_fetch,
        "method": "blockscout_address_list_public_rpc_receipts",
        "truncated": bool(xmeta.get("truncated")) or len(by_tx) > cap,
        "transfer_meta": xmeta,
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
    }
    if vendor and rec["sign_7d_ours"] is not None and rec["sign_7d_vendor"] is not None:
        rec["sign_agree_7d"] = rec["sign_7d_ours"] == rec["sign_7d_vendor"]
    write_json(out_path, rec)
    return rec


def _sign(v) -> int | None:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x > 1e-9:
        return 1
    if x < -1e-9:
        return -1
    return 0


def refresh_vendors(chain: str, addresses: list[str], workers: int = 6) -> dict[str, dict]:
    cached = PROJ / "data/live_recon" / f"vendor_{chain}.json"
    if cached.exists():
        try:
            prev = read_json(cached)
            rows = prev.get("rows") or {}
            if len(rows) >= len(addresses) * 0.95:
                return {norm_addr(k): v for k, v in rows.items()}
        except Exception:
            pass
    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_wallet_new, chain, a): a for a in addresses}
        for fut in as_completed(futs):
            rec = fut.result()
            out[rec["address"]] = rec
    write_json(PROJ / "data/live_recon" / f"vendor_{chain}.json", {"n": len(out), "rows": out})
    return out


def reconstruct_chain(chain: str, *, workers: int = 2) -> dict:
    ensure_no_live()
    # Blockscout public tier 429s if the previous run was hot
    time.sleep(12.0)
    now = utc_now()
    addresses = unique_board_addresses(chain)
    head = rpc_head(chain)
    native_spot, daily = fetch_native_price(chain)
    vendors = refresh_vendors(chain, addresses)
    results = []
    done = 0

    def one(addr: str):
        return reconstruct_wallet(
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
            if done % 15 == 0 or done == len(addresses):
                with PROGRESS_LOCK:
                    write_json(
                        PROJ / "data/live_recon" / f"progress_{chain}.json",
                        {
                            "done": done,
                            "total": len(addresses),
                            "updated_at": utc_now_iso(),
                            "last": rec.get("address"),
                            "last_ok": rec.get("ok"),
                        },
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
        "index": "public_blockscout",
        "rpc_kind": "public_https_only",
        "eatlq_archive_used": False,
        "rows": results,
    }
    write_json(PROJ / "data/live_recon" / f"reconstruct_{chain}.json", payload)
    return payload


def gather_reconstruct(chain: str) -> dict:
    """Rebuild reconstruct_*.json from per-wallet files after retries."""
    prev_path = PROJ / "data/live_recon" / f"reconstruct_{chain}.json"
    prev = read_json(prev_path) if prev_path.exists() else {}
    rows = []
    for path in sorted((PROJ / "data/live_recon/wallets" / chain).glob("*.json")):
        try:
            rows.append(read_json(path))
        except Exception:
            continue
    ok_rows = [r for r in rows if r.get("ok")]
    payload = {
        "generated_at": utc_now_iso(),
        "chain": chain,
        "rpc": prev.get("rpc") or rpc_head(chain),
        "native_spot_usd": prev.get("native_spot_usd"),
        "n_daily_prices": prev.get("n_daily_prices"),
        "n_addresses": len(unique_board_addresses(chain)),
        "n_ok": len(ok_rows),
        "n_fail": len(rows) - len(ok_rows),
        "lookback_days": LOOKBACK_DAYS,
        "index": prev.get("index") or "public_blockscout",
        "rpc_kind": "public_https_only",
        "eatlq_archive_used": False,
        "method": prev.get("method") or "blockscout_address_list_public_rpc_receipts",
        "rows": rows,
    }
    write_json(prev_path, payload)
    return payload


def verify_rpc_sample(chain: str, n: int = 12) -> dict:
    """Confirm Blockscout token-transfers vs public RPC receipts for a sample."""
    cache = list((PROJ / "data/live_recon/tx_cache" / chain).glob("*/*.json"))
    cache.sort()
    pick = cache[:n] if len(cache) <= n else [cache[i] for i in range(0, len(cache), max(1, len(cache) // n))][:n]
    cfg = CHAIN_CFG[chain]
    transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    agree = 0
    rows = []
    for path in pick:
        rec = read_json(path)
        tx = rec.get("tx")
        try:
            receipt = rpc_call(cfg["rpc"], "eth_getTransactionReceipt", [tx])
        except Exception as exc:  # noqa: BLE001
            rows.append({"tx": tx, "ok": False, "error": str(exc)[:160]})
            continue
        logs = receipt.get("logs") or []
        n_xfer = sum(1 for lg in logs if (lg.get("topics") or [None])[0] == transfer_topic)
        n_bs = len(rec.get("items") or [])
        # Blockscout may hide some hop transfers; equality is sufficient, near is noted
        match = n_xfer == n_bs or (n_bs > 0 and abs(n_xfer - n_bs) <= 2)
        agree += int(bool(match) and rec.get("ok"))
        rows.append({"tx": tx, "rpc_transfer_logs": n_xfer, "blockscout_items": n_bs, "match": match})
    out = {
        "chain": chain,
        "n": len(rows),
        "n_agree": agree,
        "rpc": cfg["rpc"][0],
        "rows": rows,
    }
    write_json(PROJ / "out" / f"live_rpc_verify_{chain}.json", out)
    return out


if __name__ == "__main__":
    chain = sys.argv[1] if len(sys.argv) > 1 else "base"
    p = reconstruct_chain(chain)
    print(json.dumps({k: p[k] for k in ("chain", "n_addresses", "n_ok", "n_fail", "native_spot_usd")}, indent=2))
