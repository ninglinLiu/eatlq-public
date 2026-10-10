#!/usr/bin/env python3
"""Arc holdings desk: live balances, unrealized PnL, DEX Screener charts."""

from __future__ import annotations

import json
import mimetypes
import secrets
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

from eth_account import Account
from web3 import Web3

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DATA_PATH = ROOT / "data" / "fills.json"
ACTION_TOKEN_PATH = ROOT / "data" / "action.token"
ENV_PATH = Path("/opt/EATLQ/infra/compose/.env")

RPC = "https://rpc.mainnet.arc.io"
CHAIN_ID = 5042
WALLET = Web3.to_checksum_address("0x4B7cd953871663474a0A25B81bddDf5C1f1195A7")
USDC = Web3.to_checksum_address("0x3600000000000000000000000000000000000000")
FACTORY = Web3.to_checksum_address("0xf0db7b58379503491d857db50ac9ece64c653918")
QUOTER = Web3.to_checksum_address("0x7dfd4f31be6814d2906bde155c3e1b146eac1468")
ROUTER = Web3.to_checksum_address("0x53bf6b0684ec7ef91e1387da3d1a1769bc5a6f77")
TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
FEE_TIERS = (10000, 3000, 500, 100)
SKIP_TOKENS = {
    USDC,
    Web3.to_checksum_address("0xffffFFFfFFffffffffffffffFfFFFfffFFFfFFfE"),
    Web3.to_checksum_address("0xffffffffffffffffffffffffffffffffffffffff"),
}
LOG_PAGE = 2000
SCAN_START_FLOOR = 21109000
BIND = "127.0.0.1"
PORT = 8787
REFRESH_SEC = 8.0
SCAN_SEC = 20.0
DEX_TTL = 12.0
UA = "arc-desk/1.0"
QUOTE_TTL_SEC = 20.0
BUY_CAP_USDC = 25.0
SLIPPAGE_BPS = 500
GAS_FLOOR_GWEI = 20

ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "a", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint8"}]},
    {"name": "symbol", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "string"}]},
    {"name": "name", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "string"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "o", "type": "address"}, {"name": "s", "type": "address"}],
     "outputs": [{"type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "s", "type": "address"}, {"name": "v", "type": "uint256"}],
     "outputs": [{"type": "bool"}]},
]
ROUTER_ABI = [{
    "name": "exactInputSingle", "type": "function", "stateMutability": "payable",
    "inputs": [{"name": "params", "type": "tuple", "components": [
        {"name": "tokenIn", "type": "address"},
        {"name": "tokenOut", "type": "address"},
        {"name": "fee", "type": "uint24"},
        {"name": "recipient", "type": "address"},
        {"name": "amountIn", "type": "uint256"},
        {"name": "amountOutMinimum", "type": "uint256"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"},
    ]}],
    "outputs": [{"name": "amountOut", "type": "uint256"}],
}]
FACTORY_ABI = [{
    "name": "getPool", "type": "function", "stateMutability": "view",
    "inputs": [{"name": "a", "type": "address"}, {"name": "b", "type": "address"}, {"name": "fee", "type": "uint24"}],
    "outputs": [{"type": "address"}],
}]
QUOTER_ABI = [{
    "name": "quoteExactInputSingle", "type": "function", "stateMutability": "nonpayable",
    "inputs": [{"name": "params", "type": "tuple", "components": [
        {"name": "tokenIn", "type": "address"},
        {"name": "tokenOut", "type": "address"},
        {"name": "amountIn", "type": "uint256"},
        {"name": "fee", "type": "uint24"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"},
    ]}],
    "outputs": [
        {"name": "amountOut", "type": "uint256"},
        {"name": "sqrtPriceX96After", "type": "uint160"},
        {"name": "initializedTicksCrossed", "type": "uint32"},
        {"name": "gasEstimate", "type": "uint256"},
    ],
}]
POOL_ABI = [
    {"name": "token0", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
    {"name": "token1", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
    {"name": "fee", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint24"}]},
    {"name": "liquidity", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint128"}]},
    {"name": "slot0", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [
         {"name": "sqrtPriceX96", "type": "uint160"},
         {"name": "tick", "type": "int24"},
         {"name": "observationIndex", "type": "uint16"},
         {"name": "observationCardinality", "type": "uint16"},
         {"name": "observationCardinalityNext", "type": "uint16"},
         {"name": "feeProtocol", "type": "uint8"},
         {"name": "unlocked", "type": "bool"},
     ]},
]

_w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 25}))
_factory = _w3.eth.contract(FACTORY, abi=FACTORY_ABI)
_quoter = _w3.eth.contract(QUOTER, abi=QUOTER_ABI)
_router = _w3.eth.contract(ROUTER, abi=ROUTER_ABI)
_lock = threading.Lock()
_trade_lock = threading.Lock()
_state: dict[str, Any] = {
    "last_scanned_block": SCAN_START_FLOOR,
    "fills": [],
    "snapshot": None,
    "error": None,
    "updated_at": 0.0,
    "last_scan_at": 0.0,
    "trade_busy": False,
    "last_trade": None,
}
_tickets: dict[str, dict[str, Any]] = {}
_meta_cache: dict[str, dict[str, Any]] = {}
_dex_cache: dict[str, Any] = {"ts": 0.0, "by_token": {}}
_acct_cache = None


def _hex(value: Any) -> str:
    raw = value.hex() if hasattr(value, "hex") else str(value)
    raw = raw.lower()
    return raw if raw.startswith("0x") else "0x" + raw


def _topic_addr(addr: str) -> str:
    return "0x" + Web3.to_checksum_address(addr)[2:].lower().zfill(64)


def _addr_from_topic(topic: Any) -> str:
    return Web3.to_checksum_address("0x" + _hex(topic)[-40:])


def _load_ledger() -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DATA_PATH.exists():
        return
    raw = json.loads(DATA_PATH.read_text())
    fills = raw.get("fills") or []
    seen = set()
    clean = []
    for f in fills:
        key = (str(f.get("tx", "")).lower(), str(f.get("token", "")).lower(), f.get("side"))
        if key in seen:
            continue
        seen.add(key)
        f["token"] = Web3.to_checksum_address(f["token"])
        if f.get("pool"):
            f["pool"] = Web3.to_checksum_address(f["pool"])
        clean.append(f)
    with _lock:
        _state["fills"] = clean
        _state["last_scanned_block"] = int(raw.get("last_scanned_block") or SCAN_START_FLOOR)


def _save_ledger() -> None:
    with _lock:
        payload = {
            "last_scanned_block": _state["last_scanned_block"],
            "fills": _state["fills"],
        }
    DATA_PATH.write_text(json.dumps(payload, indent=2))


def _http_json(url: str, timeout: float = 12.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _token_meta(token: str) -> dict[str, Any]:
    token = Web3.to_checksum_address(token)
    if token in _meta_cache:
        return _meta_cache[token]
    c = _w3.eth.contract(token, abi=ERC20_ABI)
    meta = {
        "address": token,
        "symbol": c.functions.symbol().call(),
        "name": c.functions.name().call(),
        "decimals": int(c.functions.decimals().call()),
    }
    _meta_cache[token] = meta
    return meta


def _get_logs(topics: list[str | None], start: int, end: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cur = start
    while cur <= end:
        to = min(cur + LOG_PAGE - 1, end)
        chunk = _w3.eth.get_logs({
            "fromBlock": hex(cur),
            "toBlock": hex(to),
            "topics": topics,
        })
        out.extend(chunk)
        cur = to + 1
    return out


def _decode_transfer_amount(log: Any) -> int:
    return int(_hex(log["data"]), 16)


def _scan_new_fills() -> None:
    head = int(_w3.eth.block_number)
    with _lock:
        start = max(SCAN_START_FLOOR, int(_state["last_scanned_block"]) + 1)
        known = {
            (str(f["tx"]).lower(), str(f["token"]).lower(), f["side"])
            for f in _state["fills"]
        }
    if start > head:
        return
    wallet_topic = _topic_addr(WALLET)
    incoming = _get_logs([TOPIC_TRANSFER, None, wallet_topic], start, head)
    outgoing = _get_logs([TOPIC_TRANSFER, wallet_topic, None], start, head)
    tx_hashes = []
    seen_tx = set()
    for log in list(incoming) + list(outgoing):
        txh = _hex(log["transactionHash"])
        if txh in seen_tx:
            continue
        seen_tx.add(txh)
        tx_hashes.append(txh)

    new_fills: list[dict[str, Any]] = []
    for txh in tx_hashes:
        try:
            receipt = _w3.eth.get_transaction_receipt(txh)
        except Exception:
            continue
        usdc_out = 0
        usdc_in = 0
        token_in: dict[str, int] = {}
        token_out: dict[str, int] = {}
        pools: dict[str, str] = {}
        for log in receipt.logs:
            if len(log.topics) < 3:
                continue
            topic0 = _hex(log.topics[0])
            if topic0 != TOPIC_TRANSFER:
                continue
            token = Web3.to_checksum_address(log.address)
            src = _addr_from_topic(log.topics[1])
            dst = _addr_from_topic(log.topics[2])
            amt = _decode_transfer_amount(log)
            if token in SKIP_TOKENS:
                if token == USDC:
                    if src == WALLET:
                        usdc_out += amt
                    if dst == WALLET:
                        usdc_in += amt
                continue
            if dst == WALLET:
                token_in[token] = token_in.get(token, 0) + amt
                if src != ROUTER:
                    pools[token] = src
            if src == WALLET:
                token_out[token] = token_out.get(token, 0) + amt
                if dst != ROUTER:
                    pools[token] = dst
        try:
            ts = int(_w3.eth.get_block(receipt.blockNumber).timestamp)
        except Exception:
            ts = int(time.time())
        for token, raw_qty in token_in.items():
            if usdc_out <= 0:
                continue
            key = (txh, token.lower(), "buy")
            if key in known:
                continue
            meta = _token_meta(token)
            qty = raw_qty / (10 ** meta["decimals"])
            usdc = usdc_out / 1e6
            if qty <= 0 or usdc <= 0:
                continue
            fill = {
                "tx": txh,
                "block": int(receipt.blockNumber),
                "ts": ts,
                "token": token,
                "side": "buy",
                "qty": qty,
                "usdc": usdc,
                "price": usdc / qty,
                "pool": pools.get(token),
                "fee": 10000,
            }
            new_fills.append(fill)
            known.add(key)
        for token, raw_qty in token_out.items():
            if usdc_in <= 0:
                continue
            key = (txh, token.lower(), "sell")
            if key in known:
                continue
            meta = _token_meta(token)
            qty = raw_qty / (10 ** meta["decimals"])
            usdc = usdc_in / 1e6
            if qty <= 0 or usdc <= 0:
                continue
            fill = {
                "tx": txh,
                "block": int(receipt.blockNumber),
                "ts": ts,
                "token": token,
                "side": "sell",
                "qty": qty,
                "usdc": usdc,
                "price": usdc / qty,
                "pool": pools.get(token),
                "fee": 10000,
            }
            new_fills.append(fill)
            known.add(key)

    with _lock:
        _state["fills"].extend(new_fills)
        _state["last_scanned_block"] = head
    if new_fills:
        _save_ledger()
    else:
        with _lock:
            persist = dict(_state)
        DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        DATA_PATH.write_text(json.dumps({
            "last_scanned_block": persist["last_scanned_block"],
            "fills": persist["fills"],
        }, indent=2))


def _inventory(token: str, fills: list[dict[str, Any]]) -> dict[str, float]:
    qty = 0.0
    cost = 0.0
    lots: list[dict[str, Any]] = []
    for f in sorted(fills, key=lambda x: (x.get("block") or 0, x.get("ts") or 0)):
        if Web3.to_checksum_address(f["token"]) != token:
            continue
        if f["side"] == "buy":
            qty += float(f["qty"])
            cost += float(f["usdc"])
            lots.append(f)
        else:
            sold = min(float(f["qty"]), qty)
            if qty > 0:
                cost *= (qty - sold) / qty
                qty -= sold
    avg = (cost / qty) if qty > 0 else 0.0
    return {"qty_costed": qty, "cost_usdc": cost, "avg_in": avg}


def _best_pool(token: str) -> tuple[str | None, int]:
    token = Web3.to_checksum_address(token)
    best = None
    best_fee = 10000
    best_liq = -1
    for fee in FEE_TIERS:
        try:
            pool = _factory.functions.getPool(token, USDC, fee).call()
        except Exception:
            continue
        if int(pool, 16) == 0:
            continue
        try:
            liq = int(_w3.eth.contract(Web3.to_checksum_address(pool), abi=POOL_ABI).functions.liquidity().call())
        except Exception:
            liq = 0
        if liq > best_liq:
            best_liq = liq
            best = Web3.to_checksum_address(pool)
            best_fee = fee
    return best, best_fee


def _rpc_call(fn, tries: int = 4):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as exc:
            last = exc
            text = str(exc)
            if "429" in text or "Too Many" in text or "timeout" in text.lower():
                time.sleep(0.8 * (i + 1))
                continue
            raise
    raise last


def _quote_exit(token: str, raw_amount: int, fee: int) -> float | None:
    if raw_amount <= 0:
        return 0.0
    try:
        out = _rpc_call(lambda: _quoter.functions.quoteExactInputSingle((token, USDC, raw_amount, fee, 0)).call()[0])
        return out / 1e6
    except Exception:
        return None


def _dex_pairs(tokens: list[str]) -> dict[str, dict[str, Any]]:
    now = time.time()
    with _lock:
        if now - float(_dex_cache["ts"]) < DEX_TTL and _dex_cache["by_token"]:
            return dict(_dex_cache["by_token"])
    by_token: dict[str, dict[str, Any]] = {}
    if tokens:
        addrs = ",".join(tokens)
        url = f"https://api.dexscreener.com/tokens/v1/arc/{addrs}"
        try:
            rows = _http_json(url)
            if isinstance(rows, dict):
                rows = rows.get("pairs") or []
            for row in rows or []:
                base = ((row.get("baseToken") or {}).get("address") or "").lower()
                if not base:
                    continue
                prev = by_token.get(base)
                liq = float(((row.get("liquidity") or {}).get("usd")) or 0)
                if prev and float(((prev.get("liquidity") or {}).get("usd")) or 0) >= liq:
                    continue
                by_token[base] = row
        except Exception:
            by_token = dict(_dex_cache.get("by_token") or {})
    with _lock:
        if by_token:
            _dex_cache["ts"] = now
            _dex_cache["by_token"] = by_token
    return by_token


def _dex_urls(pair: str | None) -> dict[str, str | None]:
    if not pair:
        return {"dex_url": None, "dex_embed": None, "dex_maker": None}
    pair_l = pair.lower()
    wallet_l = WALLET.lower()
    page = f"https://dexscreener.com/arc/{pair_l}"
    maker = f"{page}?maker={wallet_l}"
    embed = (
        f"{page}?embed=1&theme=dark&trades=1&info=0&chartTheme=dark"
        f"&chartType=usd&interval=1&maker={wallet_l}"
    )
    return {"dex_url": page, "dex_embed": embed, "dex_maker": maker}


def build_snapshot() -> dict[str, Any]:
    now = time.time()
    with _lock:
        due = now - float(_state.get("last_scan_at") or 0) >= SCAN_SEC
    if due:
        _scan_new_fills()
        with _lock:
            _state["last_scan_at"] = time.time()
    usdc_raw = _w3.eth.contract(USDC, abi=ERC20_ABI).functions.balanceOf(WALLET).call()
    usdc = usdc_raw / 1e6
    with _lock:
        fills = list(_state["fills"])
    tokens = []
    seen = set()
    for f in fills:
        t = Web3.to_checksum_address(f["token"])
        if t not in seen:
            seen.add(t)
            tokens.append(t)
    holdings = []
    for token in tokens:
        meta = _token_meta(token)
        raw = _w3.eth.contract(token, abi=ERC20_ABI).functions.balanceOf(WALLET).call()
        qty = raw / (10 ** meta["decimals"])
        if qty <= 1e-12:
            continue
        inv = _inventory(token, fills)
        pool, fee = _best_pool(token)
        seeded_pool = next((f.get("pool") for f in fills if Web3.to_checksum_address(f["token"]) == token and f.get("pool")), None)
        if seeded_pool:
            pool = Web3.to_checksum_address(seeded_pool)
        token_fills = [f for f in fills if Web3.to_checksum_address(f["token"]) == token]
        holdings.append({
            "token": token,
            "symbol": meta["symbol"],
            "name": meta["name"],
            "decimals": meta["decimals"],
            "qty": qty,
            "raw": str(raw),
            "cost_usdc": inv["cost_usdc"],
            "avg_in": inv["avg_in"] if inv["qty_costed"] > 0 else (inv["cost_usdc"] / qty if qty else 0),
            "pool": pool,
            "fee": fee,
            "fills": token_fills,
            "exit_value": _quote_exit(token, raw, fee),
        })

    dex = _dex_pairs([h["token"] for h in holdings])
    rows = []
    aum_mark = usdc
    aum_exit = usdc
    cost_open = 0.0
    upnl_mark = 0.0
    upnl_exit = 0.0
    for h in holdings:
        pair_row = dex.get(h["token"].lower()) or {}
        pair = h["pool"] or (pair_row.get("pairAddress") and Web3.to_checksum_address(pair_row["pairAddress"]))
        mark = None
        if pair_row.get("priceUsd") is not None:
            try:
                mark = float(pair_row["priceUsd"])
            except (TypeError, ValueError):
                mark = None
        if mark is None and h["exit_value"] is not None and h["qty"] > 0:
            mark = h["exit_value"] / h["qty"]
        mark_value = (mark * h["qty"]) if mark is not None else None
        exit_value = h["exit_value"]
        cost = float(h["cost_usdc"] or 0.0)
        pnl_m = (mark_value - cost) if mark_value is not None else None
        pnl_e = (exit_value - cost) if exit_value is not None else None
        if mark_value is not None:
            aum_mark += mark_value
        elif exit_value is not None:
            aum_mark += exit_value
        if exit_value is not None:
            aum_exit += exit_value
        elif mark_value is not None:
            aum_exit += mark_value
        cost_open += cost
        if pnl_m is not None:
            upnl_mark += pnl_m
        if pnl_e is not None:
            upnl_exit += pnl_e
        chg = pair_row.get("priceChange") or {}
        liq = pair_row.get("liquidity") or {}
        urls = _dex_urls(pair)
        rows.append({
            "token": h["token"],
            "symbol": h["symbol"],
            "name": h["name"],
            "qty": h["qty"],
            "cost_usdc": cost,
            "avg_in": h["avg_in"],
            "mark": mark,
            "mark_value": mark_value,
            "exit_value": exit_value,
            "upnl_mark": pnl_m,
            "upnl_exit": pnl_e,
            "upnl_pct": ((pnl_m / cost) * 100.0) if pnl_m is not None and cost > 0 else None,
            "chg_m5": chg.get("m5"),
            "chg_h1": chg.get("h1"),
            "chg_h6": chg.get("h6"),
            "chg_h24": chg.get("h24"),
            "liq_usd": liq.get("usd"),
            "mcap": pair_row.get("marketCap") or pair_row.get("fdv"),
            "fdv": pair_row.get("fdv") or pair_row.get("marketCap"),
            "image": ((pair_row.get("info") or {}).get("imageUrl")),
            "pair": pair,
            "fee": h["fee"],
            "dex_url": urls["dex_url"],
            "dex_embed": urls["dex_embed"],
            "dex_maker": urls["dex_maker"],
            "fills": [
                {
                    "tx": f["tx"],
                    "block": f.get("block"),
                    "ts": f.get("ts"),
                    "side": f["side"],
                    "qty": f["qty"],
                    "usdc": f["usdc"],
                    "price": f["price"],
                    "explorer": f"https://explorer.arc.io/tx/{f['tx']}",
                }
                for f in sorted(h["fills"], key=lambda x: x.get("ts") or 0)
            ],
        })

    rows.sort(key=lambda r: (r.get("mark_value") or r.get("exit_value") or 0), reverse=True)
    return {
        "ts": int(time.time()),
        "iso": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "chain_id": CHAIN_ID,
        "rpc": RPC,
        "wallet": WALLET,
        "explorer_wallet": f"https://explorer.arc.io/address/{WALLET}",
        "usdc": usdc,
        "aum_mark": aum_mark,
        "aum_exit": aum_exit,
        "cost_open": cost_open,
        "upnl_mark": upnl_mark,
        "upnl_exit": upnl_exit,
        "upnl_pct": ((upnl_mark / cost_open) * 100.0) if cost_open > 0 else 0.0,
        "holdings": rows,
        "refresh_ms": int(REFRESH_SEC * 1000),
        "trade": {
            "busy": _state.get("trade_busy"),
            "last": _state.get("last_trade"),
            "buy_cap_usdc": BUY_CAP_USDC,
            "quote_ttl_sec": QUOTE_TTL_SEC,
            "slippage_bps": SLIPPAGE_BPS,
        },
    }


def _action_secret() -> str:
    ACTION_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    if ACTION_TOKEN_PATH.exists():
        return ACTION_TOKEN_PATH.read_text().strip()
    token = secrets.token_hex(4)
    ACTION_TOKEN_PATH.write_text(token + "\n")
    ACTION_TOKEN_PATH.chmod(0o600)
    return token


def _check_action(token: str | None) -> None:
    expected = _action_secret()
    if not token or not secrets.compare_digest(str(token).strip(), expected):
        raise ValueError("口令不对")


def _load_account():
    global _acct_cache
    if _acct_cache is not None:
        return _acct_cache
    raw = None
    for line in ENV_PATH.read_text().splitlines():
        if line.startswith("EXECUTOR_PRIVATE_KEY="):
            raw = line.split("=", 1)[1].strip().strip('"').strip("'")
            break
    if not raw:
        raise ValueError("executor key missing")
    if raw.startswith("0x"):
        raw = raw[2:]
    if len(raw) != 64:
        raise ValueError("unexpected key format")
    acct = Account.from_key("0x" + raw)
    if Web3.to_checksum_address(acct.address) != WALLET:
        raise ValueError("key is not the desk wallet")
    _acct_cache = acct
    return acct


def _purge_tickets() -> None:
    now = time.time()
    dead = [k for k, v in _tickets.items() if now - float(v["ts"]) > QUOTE_TTL_SEC]
    for k in dead:
        _tickets.pop(k, None)


def _send_tx(acct, tx: dict[str, Any]) -> str:
    floor = _w3.to_wei(GAS_FLOOR_GWEI, "gwei")
    latest = _w3.eth.get_block("latest")
    base = latest.get("baseFeePerGas") or floor
    tx = dict(tx)
    tx.setdefault("from", acct.address)
    tx.setdefault("nonce", _w3.eth.get_transaction_count(acct.address, "latest"))
    tx.setdefault("chainId", CHAIN_ID)
    tx.setdefault("maxPriorityFeePerGas", floor)
    tx.setdefault("maxFeePerGas", max(int(base) * 2 + floor, floor * 2))
    tx.setdefault("type", 2)
    if "gas" not in tx:
        tx["gas"] = int(_w3.eth.estimate_gas(tx) * 1.3)
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "rawTransaction", None) or signed.raw_transaction
    return _hex(_w3.eth.send_raw_transaction(raw))


def _holding(token: str) -> dict[str, Any]:
    token = Web3.to_checksum_address(token)
    with _lock:
        snap = _state.get("snapshot") or {}
    for row in snap.get("holdings") or []:
        if Web3.to_checksum_address(row["token"]) == token:
            return row
    meta = _token_meta(token)
    pool, fee = _best_pool(token)
    return {
        "token": token,
        "symbol": meta["symbol"],
        "decimals": meta["decimals"],
        "fee": fee,
        "pool": pool,
        "qty": 0,
    }


def make_quote(body: dict[str, Any]) -> dict[str, Any]:
    _check_action(body.get("action_token"))
    side = str(body.get("side") or "").lower()
    if side not in {"buy", "sell"}:
        raise ValueError("side must be buy or sell")
    token = Web3.to_checksum_address(str(body.get("token") or ""))
    holding = _holding(token)
    fee = int(holding.get("fee") or 10000)
    decimals = int(holding.get("decimals") or 18)
    symbol = holding.get("symbol") or _token_meta(token)["symbol"]
    if side == "buy":
        usdc = float(body.get("usdc") or 0)
        if usdc < 1:
            raise ValueError("最少 1U")
        if usdc > BUY_CAP_USDC:
            raise ValueError(f"单笔买入最多 {BUY_CAP_USDC:.0f}U")
        cash = _w3.eth.contract(USDC, abi=ERC20_ABI).functions.balanceOf(WALLET).call() / 1e6
        if usdc > cash + 1e-6:
            raise ValueError(f"现金不够，现在 {cash:.2f}U")
        amount_in = int(round(usdc * 1e6))
        token_in, token_out = USDC, token
        quoted = _rpc_call(lambda: _quoter.functions.quoteExactInputSingle((token_in, token_out, amount_in, fee, 0)).call()[0])
        notional = usdc
        out_human = quoted / (10 ** decimals)
        in_human = usdc
    else:
        pct = float(body.get("sell_pct") or 0)
        if pct <= 0 or pct > 100:
            raise ValueError("卖出比例要在 0–100")
        raw_bal = _w3.eth.contract(token, abi=ERC20_ABI).functions.balanceOf(WALLET).call()
        amount_in = int(raw_bal * pct / 100)
        if amount_in <= 0:
            raise ValueError("这个币没有可卖数量")
        token_in, token_out = token, USDC
        quoted = _rpc_call(lambda: _quoter.functions.quoteExactInputSingle((token_in, token_out, amount_in, fee, 0)).call()[0])
        notional = quoted / 1e6
        out_human = notional
        in_human = amount_in / (10 ** decimals)
    min_out = quoted * (10_000 - SLIPPAGE_BPS) // 10_000
    _purge_tickets()
    ticket_id = uuid4().hex
    ticket = {
        "id": ticket_id,
        "ts": time.time(),
        "side": side,
        "token": token,
        "symbol": symbol,
        "fee": fee,
        "decimals": decimals,
        "token_in": token_in,
        "token_out": token_out,
        "amount_in": amount_in,
        "quoted_out": quoted,
        "min_out": min_out,
        "notional_usdc": notional,
        "in_human": in_human,
        "out_human": out_human,
        "sell_pct": float(body.get("sell_pct") or 0),
    }
    _tickets[ticket_id] = ticket
    return {
        "ticket": ticket_id,
        "side": side,
        "symbol": symbol,
        "token": token,
        "ttl_sec": QUOTE_TTL_SEC,
        "expires_at": int(ticket["ts"] + QUOTE_TTL_SEC),
        "in_human": in_human,
        "out_human": out_human,
        "notional_usdc": notional,
        "min_out_human": min_out / (1e6 if side == "sell" else 10 ** decimals),
        "slippage_pct": SLIPPAGE_BPS / 100,
        "confirm_symbol": symbol,
    }


def execute_ticket(body: dict[str, Any]) -> dict[str, Any]:
    _check_action(body.get("action_token"))
    ticket_id = str(body.get("ticket") or "")
    typed = str(body.get("confirm_symbol") or "").strip()
    _purge_tickets()
    ticket = _tickets.get(ticket_id)
    if not ticket:
        raise ValueError("报价已过期，重新询价")
    if time.time() - float(ticket["ts"]) > QUOTE_TTL_SEC:
        _tickets.pop(ticket_id, None)
        raise ValueError("报价已过期，重新询价")
    if typed.upper() != str(ticket["symbol"]).upper():
        raise ValueError(f"请输入代币代码 {ticket['symbol']} 再确认")
    if not _trade_lock.acquire(blocking=False):
        raise ValueError("上一笔还在上链")
    try:
        with _lock:
            _state["trade_busy"] = True
        if _w3.eth.chain_id != CHAIN_ID:
            raise ValueError(f"wrong chain {_w3.eth.chain_id}")
        acct = _load_account()
        pending = _w3.eth.get_transaction_count(WALLET, "pending")
        latest = _w3.eth.get_transaction_count(WALLET, "latest")
        if pending != latest:
            raise ValueError("有未完成交易，先等它落地")
        fresh = _rpc_call(lambda: _quoter.functions.quoteExactInputSingle(
            (ticket["token_in"], ticket["token_out"], ticket["amount_in"], ticket["fee"], 0)
        ).call()[0])
        if fresh * 100 < ticket["quoted_out"] * 97:
            raise ValueError("行情比询价差超过 3%，已取消")
        min_out = min(ticket["min_out"], fresh * (10_000 - SLIPPAGE_BPS) // 10_000)
        token_in = _w3.eth.contract(ticket["token_in"], abi=ERC20_ABI)
        if token_in.functions.balanceOf(WALLET).call() < ticket["amount_in"]:
            raise ValueError("余额不够了")
        if token_in.functions.allowance(WALLET, ROUTER).call() < ticket["amount_in"]:
            approve_tx = _send_tx(
                acct,
                token_in.functions.approve(ROUTER, ticket["amount_in"]).build_transaction({"from": WALLET}),
            )
            rec = _w3.eth.wait_for_transaction_receipt(approve_tx, timeout=180)
            if rec.status != 1:
                raise ValueError(f"approve failed {approve_tx}")
            time.sleep(1.5)
        swap_tx = _send_tx(
            acct,
            _router.functions.exactInputSingle((
                ticket["token_in"],
                ticket["token_out"],
                ticket["fee"],
                WALLET,
                ticket["amount_in"],
                min_out,
                0,
            )).build_transaction({"from": WALLET, "value": 0}),
        )
        rec = _w3.eth.wait_for_transaction_receipt(swap_tx, timeout=180)
        if rec.status != 1:
            raise ValueError(f"swap failed {swap_tx}")
        _tickets.pop(ticket_id, None)
        result = {
            "ok": True,
            "side": ticket["side"],
            "symbol": ticket["symbol"],
            "tx": swap_tx,
            "explorer": f"https://explorer.arc.io/tx/{swap_tx}",
            "notional_usdc": ticket["notional_usdc"],
        }
        with _lock:
            _state["last_trade"] = result
            _state["last_scanned_block"] = max(SCAN_START_FLOOR, int(rec.blockNumber) - 20)
        try:
            snap = build_snapshot()
            with _lock:
                _state["snapshot"] = snap
                _state["updated_at"] = time.time()
        except Exception:
            pass
        return result
    finally:
        with _lock:
            _state["trade_busy"] = False
        _trade_lock.release()


def _refresh_loop() -> None:
    while True:
        try:
            snap = build_snapshot()
            with _lock:
                _state["snapshot"] = snap
                _state["error"] = None
                _state["updated_at"] = time.time()
        except Exception as exc:
            with _lock:
                _state["error"] = f"{exc}\n{traceback.format_exc()}"
                _state["updated_at"] = time.time()
        time.sleep(REFRESH_SEC)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        if length > 8192:
            self._send(413, json.dumps({"error": "payload too large"}).encode(), "application/json")
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._send(400, json.dumps({"error": "bad json"}).encode(), "application/json")
            return
        try:
            if path == "/api/quote":
                out = make_quote(body)
            elif path == "/api/execute":
                out = execute_ticket(body)
            else:
                self._send(404, json.dumps({"error": "not found"}).encode(), "application/json")
                return
            self._send(200, json.dumps(out).encode(), "application/json")
        except ValueError as exc:
            self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")
        except Exception as exc:
            self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/snapshot":
            with _lock:
                snap = _state["snapshot"]
                err = _state["error"]
            if snap is None and err:
                self._send(503, json.dumps({"error": err}).encode(), "application/json")
                return
            if snap is None:
                self._send(503, json.dumps({"error": "warming"}).encode(), "application/json")
                return
            self._send(200, json.dumps(snap).encode(), "application/json")
            return
        if path in {"/", "/index.html"}:
            path = "/index.html"
        rel = path.lstrip("/")
        file_path = (STATIC_DIR / rel).resolve()
        if not str(file_path).startswith(str(STATIC_DIR.resolve())) or not file_path.is_file():
            self._send(404, b"not found", "text/plain")
            return
        ctype = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        self._send(200, file_path.read_bytes(), ctype)


def main() -> None:
    _load_ledger()
    _action_secret()
    t = threading.Thread(target=_refresh_loop, name="arc-desk-refresh", daemon=True)
    t.start()
    httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"arc-desk http://{BIND}:{PORT} wallet={WALLET}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
