"""Contemporaneous FIFO from classified fills. Pure functions. No I/O."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

from src.common import norm_addr

ZERO_ADDR = "0x0000000000000000000000000000000000000000"

# Base + BSC quote set. Token-token memes stay unclassified for USD.
WETH_BASE = "0x4200000000000000000000000000000000000006"
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
STABLES = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC Base
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
    "0x50c5725949a6f0c72e6c4a641f990157d79dbb8e",  # DAI Base
    "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2",  # USDT Base
    "0x55d398326f99059ff775485246999027b3197955",  # USDT BSC
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",  # USDC BSC
    "0xe9e7cea3dedca5984780bafc599bd69add087d56",  # BUSD
    "0x8d0d000ee44948fc98c9b98a4fa4921476f08b0d",  # USD1 BSC
}

QUOTE_SYMBOLS = {"WETH", "ETH", "WBNB", "BNB", "USDC", "USDT", "DAI", "USDB", "USDBC", "BUSD", "USD1"}


def is_weth(addr: str, symbol: str | None, chain: str) -> bool:
    a = norm_addr(addr)
    sym = (symbol or "").upper()
    if chain == "bsc":
        return a == WBNB or sym in {"WBNB", "BNB"}
    return a == WETH_BASE or sym in {"WETH", "ETH"}


def is_stable(addr: str, symbol: str | None) -> bool:
    a = norm_addr(addr)
    if a in STABLES:
        return True
    return (symbol or "").upper() in {"USDC", "USDT", "DAI", "USDB", "USDBC", "BUSD", "USD1"}


def is_quote(addr: str, symbol: str | None, chain: str) -> bool:
    return is_weth(addr, symbol, chain) or is_stable(addr, symbol)


@dataclass
class TransferLeg:
    token: str
    symbol: str
    qty: float
    frm: str
    to: str


@dataclass
class Fill:
    kind: str  # buy / sell / transfer_in / transfer_out
    token: str
    symbol: str
    qty: float
    ts: datetime
    tx: str
    cost_usd: float = 0.0
    proceeds_usd: float = 0.0
    quote_kind: str = ""
    contaminated: bool = False


@dataclass
class FifoResult:
    realized_usd_1d: float = 0.0
    realized_usd_7d: float = 0.0
    realized_usd_30d: float = 0.0
    n_buy: int = 0
    n_sell: int = 0
    n_transfer_in: int = 0
    n_transfer_out: int = 0
    n_closed_7d: int = 0
    unmatched_sell_qty: float = 0.0
    n_unmatched_sell: int = 0
    n_token_token: int = 0
    n_fills: int = 0
    windows_ok: bool = True
    notes: list[str] = field(default_factory=list)


def _qty(raw, decimals) -> float:
    try:
        r = float(raw)
        d = int(decimals) if decimals not in (None, "") else 18
    except (TypeError, ValueError):
        return 0.0
    if d < 0 or d > 36:
        d = 18
    return r / (10 ** d)


TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
WETH_DEPOSIT = "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c"
WETH_WITHDRAWALS = {
    "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65",
    "0x7fcf532c15f0a6db0bd6d0e04733f8be43c988b5b2043c770fbb60b82d869a07",
}


def legs_from_receipt(receipt: dict, decimals_by_token: dict[str, int] | None = None) -> list[TransferLeg]:
    """Parse ERC-20 Transfer + WETH Deposit/Withdrawal from a public-RPC receipt.

    Uniswap V4 wrap/unwrap often emits Deposit/Withdrawal without Transfer(0x0, …).
    """
    decimals_by_token = decimals_by_token or {}
    out: list[TransferLeg] = []
    for lg in receipt.get("logs") or []:
        topics = lg.get("topics") or []
        if not topics:
            continue
        topic0 = str(topics[0]).lower()
        token = norm_addr(lg.get("address"))
        try:
            raw = int(lg.get("data") or "0x0", 16)
        except ValueError:
            continue
        dec = int(decimals_by_token.get(token, 18) or 18)
        if dec < 0 or dec > 36:
            dec = 18
        if raw.bit_length() > 96:
            continue
        try:
            qty = raw / (10 ** dec)
        except OverflowError:
            continue
        if qty <= 0:
            continue
        if topic0 == TRANSFER_TOPIC and len(topics) >= 3:
            frm = "0x" + str(topics[1])[-40:]
            to = "0x" + str(topics[2])[-40:]
            out.append(TransferLeg(token=token, symbol="", qty=qty, frm=norm_addr(frm), to=norm_addr(to)))
        elif topic0 == WETH_DEPOSIT and len(topics) >= 2:
            dst = "0x" + str(topics[1])[-40:]
            out.append(TransferLeg(token=token, symbol="WETH", qty=qty, frm=ZERO_ADDR, to=norm_addr(dst)))
        elif topic0 in WETH_WITHDRAWALS and len(topics) >= 2:
            src = "0x" + str(topics[1])[-40:]
            out.append(TransferLeg(token=token, symbol="WETH", qty=qty, frm=norm_addr(src), to=ZERO_ADDR))
    return out


def legs_from_blockscout_items(items: list[dict]) -> list[TransferLeg]:
    out = []
    for t in items or []:
        tok = t.get("token") or {}
        addr = norm_addr(tok.get("address_hash") or tok.get("address") or tok.get("hash"))
        if not addr:
            continue
        total = t.get("total") or {}
        qty = _qty(total.get("value"), total.get("decimals") or tok.get("decimals"))
        if qty <= 0:
            continue
        out.append(
            TransferLeg(
                token=addr,
                symbol=str(tok.get("symbol") or ""),
                qty=qty,
                frm=norm_addr((t.get("from") or {}).get("hash")),
                to=norm_addr((t.get("to") or {}).get("hash")),
            )
        )
    return out


def classify_tx(
    *,
    wallet: str,
    ts: datetime,
    tx: str,
    legs: list[TransferLeg],
    native_out: float,
    native_in: float,
    chain: str,
    native_usd: float,
) -> list[Fill]:
    """Turn one tx's token legs + native ETH/BNB into fills for `wallet`."""
    w = norm_addr(wallet)
    weth_wallet = 0.0
    stable_wallet = 0.0
    weth_mint = 0.0
    weth_burn = 0.0
    tokens: dict[str, list] = defaultdict(lambda: [0.0, ""])  # delta, symbol

    for leg in legs:
        signed = 0.0
        if leg.to == w:
            signed = leg.qty
        elif leg.frm == w:
            signed = -leg.qty
        if is_weth(leg.token, leg.symbol, chain):
            if signed:
                weth_wallet += signed
            if leg.frm == ZERO_ADDR:
                weth_mint += leg.qty
            if leg.to == ZERO_ADDR:
                weth_burn += leg.qty
        elif is_stable(leg.token, leg.symbol):
            if signed:
                stable_wallet += signed
        elif signed:
            tokens[leg.token][0] += signed
            tokens[leg.token][1] = leg.symbol

    wallet_eth = weth_wallet + native_in - native_out
    inferred = max(weth_mint, weth_burn)

    # drop dust tokens
    toks = [(a, d, s) for a, (d, s) in tokens.items() if abs(d) > 1e-18]
    if not toks:
        return []

    # one dominant non-quote token (typical swap). Extra dust ignored.
    toks.sort(key=lambda x: abs(x[1]), reverse=True)
    if len(toks) > 1 and abs(toks[1][1]) > 0.05 * abs(toks[0][1]) and abs(toks[1][1]) > 1e-6:
        # two significant wallet tokens: token-token. Only keep if one side is still quote (already excluded).
        # Mark as token-token: no USD unless we have wallet_eth/stable.
        pass

    fills: list[Fill] = []
    # allocate the whole quote to the largest |delta| token to avoid double count
    primary = toks[0]
    extras = toks[1:]

    def quote_usd_for(delta: float) -> tuple[float, str, bool]:
        if wallet_eth < -1e-12 and delta > 0:
            return (-wallet_eth) * native_usd, "wallet_eth_out", False
        if wallet_eth > 1e-12 and delta < 0:
            return wallet_eth * native_usd, "wallet_eth_in", False
        if stable_wallet < -1e-8 and delta > 0:
            return -stable_wallet, "stable_out", False
        if stable_wallet > 1e-8 and delta < 0:
            return stable_wallet, "stable_in", False
        if inferred > 1e-12:
            return inferred * native_usd, "weth_mint_burn", False
        return 0.0, "none", True

    token, delta, symbol = primary
    usd, qk, missing = quote_usd_for(delta)
    if delta > 0:
        kind = "transfer_in" if missing else "buy"
        fills.append(
            Fill(
                kind=kind,
                token=token,
                symbol=symbol,
                qty=delta,
                ts=ts,
                tx=tx,
                cost_usd=0.0 if missing else usd,
                quote_kind=qk,
                contaminated=missing,
            )
        )
    else:
        kind = "transfer_out" if missing else "sell"
        fills.append(
            Fill(
                kind=kind,
                token=token,
                symbol=symbol,
                qty=-delta,
                ts=ts,
                tx=tx,
                proceeds_usd=0.0 if missing else usd,
                quote_kind=qk,
                contaminated=missing,
            )
        )

    for token, delta, symbol in extras:
        # hop / dust / second token: do not take the same quote
        if abs(delta) <= 1e-12:
            continue
        if delta > 0:
            fills.append(
                Fill(
                    kind="transfer_in",
                    token=token,
                    symbol=symbol,
                    qty=delta,
                    ts=ts,
                    tx=tx,
                    quote_kind="secondary_token",
                    contaminated=True,
                )
            )
        else:
            fills.append(
                Fill(
                    kind="transfer_out",
                    token=token,
                    symbol=symbol,
                    qty=-delta,
                    ts=ts,
                    tx=tx,
                    quote_kind="secondary_token",
                    contaminated=True,
                )
            )
    return fills


def apply_fifo(fills: Iterable[Fill], *, now: datetime | None = None) -> FifoResult:
    now = now or datetime.now(timezone.utc)
    lots: dict[str, deque] = defaultdict(deque)  # (qty, cost_per_unit)
    res = FifoResult()
    ordered = sorted(fills, key=lambda f: (f.ts, f.tx, f.kind, f.token))
    res.n_fills = len(ordered)

    def in_window(ts: datetime, days: int) -> bool:
        return ts >= now - timedelta(days=days)

    for f in ordered:
        if f.kind == "buy":
            res.n_buy += 1
            cpu = (f.cost_usd / f.qty) if f.qty else 0.0
            lots[f.token].append([f.qty, cpu])
        elif f.kind == "transfer_in":
            res.n_transfer_in += 1
            lots[f.token].append([f.qty, 0.0])
        elif f.kind in {"sell", "transfer_out"}:
            if f.kind == "sell":
                res.n_sell += 1
            else:
                res.n_transfer_out += 1
            remaining = f.qty
            cost = 0.0
            while remaining > 1e-18 and lots[f.token]:
                lq, cpu = lots[f.token][0]
                take = min(lq, remaining)
                cost += cpu * take
                lq -= take
                remaining -= take
                if lq <= 1e-18:
                    lots[f.token].popleft()
                else:
                    lots[f.token][0][0] = lq
            if remaining > 1e-12:
                res.unmatched_sell_qty += remaining
                res.n_unmatched_sell += 1
            proceeds = f.proceeds_usd if f.kind == "sell" else 0.0
            pnl = proceeds - cost
            if in_window(f.ts, 1):
                res.realized_usd_1d += pnl
            if in_window(f.ts, 7):
                res.realized_usd_7d += pnl
                res.n_closed_7d += 1
            if in_window(f.ts, 30):
                res.realized_usd_30d += pnl
    return res


def native_usd_at(ts: datetime, daily: dict[str, float], fallback: float) -> float:
    key = ts.date().isoformat()
    if key in daily:
        return daily[key]
    # nearest previous
    prev = [k for k in daily if k <= key]
    if prev:
        return daily[max(prev)]
    return fallback
