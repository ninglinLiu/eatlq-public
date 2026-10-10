#!/usr/bin/env python3
"""Replay the KEYSTONE backtest ledger with a REAL-GAS executable-sell check.

WHY: the cached backtest (`_priced_live.json`) prices the EXIT with QuoterV2
`eth_call` at the node's *default near-unlimited gas*. A class of honeypots
("gas-bomb" / post-buy toggle) lets that quote succeed while a REAL sell tx
(capped at realistic gas) reverts -> the position is actually unsellable
(-100%). This replay re-prices each trade's exit at a realistic gas cap using
state-override (grant the probe the token balance + router allowance) and books
unsellable exits as -100%, then recomputes the return distribution.

It also probes the ENTRY block the same way, so we can split:
  - sellable at entry AND exit            -> genuine trade (keep quoted net)
  - sellable at entry, UNsellable at exit -> "buy then honeypot" (toggle/rug)
  - UNsellable at entry                   -> would be blocked pre-buy by our gate

Causal: every check runs at a block <= the action it gates.
"""
from __future__ import annotations
import csv, json, os, sys, time
import numpy as np
from web3 import Web3
from eth_abi import encode

HERE = os.path.dirname(os.path.abspath(__file__))
POOLS = os.path.join(HERE, "_pools.tsv")
PCACHE = os.path.join(HERE, "_priced_live.json")
OUT = os.path.join(HERE, "_realgas_replay.json")

WETH = Web3.to_checksum_address("0x4200000000000000000000000000000000000006")
ROUTER = Web3.to_checksum_address("0x2626664c2603336E57B271c5C0b26F421741e481")
QUOTER = Web3.to_checksum_address("0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a")
PROBE = Web3.to_checksum_address("0x000000000000000000000000000000000000dEaD")
NOTIONAL_USD, ETH_USD = 100.0, 3000.0
AMT_IN = int(NOTIONAL_USD / ETH_USD * 1e18)
SELL_GAS = 3_000_000
FRIC_THR = -0.05

rpc_url = open("/tmp/rpc_url.txt").read().strip()
w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))

COLS = ["pool","orient","alt","t0","ns_pre","buyers_pre","senders_pre","first_tick",
        "last_tick_pre","min_tick_pre","max_tick_pre","liq_first","liq_last_pre",
        "liq_min_pre","ns_h1","ns_h2","last_tick_h1","entry_block","ns_fwd",
        "min_tick_fwd","max_tick_fwd","last_tick_fwd"]

_QUOTE_SIG = Web3.keccak(text="quoteExactInputSingle((address,address,uint256,uint24,uint160))")[:4]
_SWAP_SIG = Web3.keccak(text="exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))")[:4]
_BAL_SIG = Web3.keccak(text="balanceOf(address)")[:4]
_ALLOW_SIG = Web3.keccak(text="allowance(address,address)")[:4]

_slot_cache: dict[str, tuple] = {}


def _block_tag(b):
    return hex(int(b)) if isinstance(b, (int, float)) else b


def _parse_uint(res):
    if not res or res == "0x":
        return None
    try:
        return int(res, 16)
    except (ValueError, TypeError):
        return None


def quote_buy(alt, fee, block):
    """High-gas QuoterV2 buy WETH->alt at block -> tokens out (mirrors backtest)."""
    params = encode(["(address,address,uint256,uint24,uint160)"],
                    [(WETH, alt, AMT_IN, fee, 0)])
    data = "0x" + (_QUOTE_SIG + params).hex()
    try:
        r = w3.provider.make_request("eth_call", [{"to": QUOTER, "data": data}, _block_tag(block)])
    except Exception:
        return None
    res = r.get("result")
    if not res or res == "0x":
        return None
    return _parse_uint(res[:66])


def detect_slots(alt, fee, block):
    """Detect balance + allowance storage slots for `alt` (cached). (bslot,blay,aslot,alay) or None."""
    if alt in _slot_cache:
        return _slot_cache[alt]
    found = None
    bdata = "0x" + (_BAL_SIG + encode(["address"], [PROBE])).hex()
    bslot = blay = None
    for slot in range(20):
        for lay in ("sol", "vyper"):
            if lay == "sol":
                key = "0x" + Web3.keccak(encode(["address", "uint256"], [PROBE, slot])).hex()
            else:
                key = "0x" + Web3.keccak(encode(["uint256", "address"], [slot, PROBE])).hex()
            ov = {alt: {"stateDiff": {key: "0x" + (0x7777).to_bytes(32, "big").hex()}}}
            try:
                r = w3.provider.make_request("eth_call", [{"to": alt, "data": bdata}, _block_tag(block), ov])
            except Exception:
                r = {}
            if _parse_uint(r.get("result")) == 0x7777:
                bslot, blay = slot, lay
                break
        if bslot is not None:
            break
    aslot = alay = None
    if bslot is not None:
        adata = "0x" + (_ALLOW_SIG + encode(["address", "address"], [PROBE, ROUTER])).hex()
        for slot in range(20):
            for lay in ("sol", "vyper"):
                if lay == "sol":
                    inner = Web3.keccak(encode(["address", "uint256"], [PROBE, slot]))
                else:
                    inner = Web3.keccak(encode(["uint256", "address"], [slot, PROBE]))
                key = "0x" + Web3.keccak(encode(["address", "bytes32"], [ROUTER, inner])).hex()
                ov = {alt: {"stateDiff": {key: "0x" + (0x8888).to_bytes(32, "big").hex()}}}
                try:
                    r = w3.provider.make_request("eth_call", [{"to": alt, "data": adata}, _block_tag(block), ov])
                except Exception:
                    r = {}
                if _parse_uint(r.get("result")) == 0x8888:
                    aslot, alay = slot, lay
                    break
            if aslot is not None:
                break
    found = (bslot, blay, aslot, alay)
    _slot_cache[alt] = found
    return found


def realgas_sell_ok(alt, fee, amount, block):
    """Return (verdict, out_eth). verdict in {'ok','revert','unavailable'}."""
    bslot, blay, aslot, alay = detect_slots(alt, fee, block)
    if bslot is None or aslot is None:
        return "unavailable", 0
    if blay == "sol":
        bkey = "0x" + Web3.keccak(encode(["address", "uint256"], [PROBE, bslot])).hex()
    else:
        bkey = "0x" + Web3.keccak(encode(["uint256", "address"], [bslot, PROBE])).hex()
    if alay == "sol":
        inner = Web3.keccak(encode(["address", "uint256"], [PROBE, aslot]))
    else:
        inner = Web3.keccak(encode(["uint256", "address"], [aslot, PROBE]))
    akey = "0x" + Web3.keccak(encode(["address", "bytes32"], [ROUTER, inner])).hex()
    grant = max(int(amount) * 2, int(amount) + 1)
    gh = "0x" + grant.to_bytes(32, "big").hex()
    ov = {alt: {"stateDiff": {bkey: gh, akey: gh}},
          PROBE: {"balance": "0x" + (1000 * 10**18).to_bytes(32, "big").hex()}}
    params = encode(["(address,address,uint24,address,uint256,uint256,uint160)"],
                    [(alt, WETH, fee, PROBE, int(amount), 0, 0)])
    data = "0x" + (_SWAP_SIG + params).hex()
    tx = {"from": PROBE, "to": ROUTER, "data": data, "gas": hex(SELL_GAS)}
    try:
        r = w3.provider.make_request("eth_call", [tx, _block_tag(block), ov])
    except Exception:
        return "unavailable", 0
    if r.get("error") is not None:
        return "revert", 0
    out = _parse_uint((r.get("result") or "0x")[:66]) or 0
    if out <= 0:
        return "revert", 0
    return "ok", out


def main():
    # load ledger
    pools = {}
    with open(POOLS) as f:
        for row in csv.reader(f, delimiter="\t"):
            d = dict(zip(COLS, row))
            pools[d["pool"]] = {"alt": d["alt"], "entry_block": int(float(d["entry_block"]))}
    cache = json.load(open(PCACHE))
    out = json.load(open(OUT)) if os.path.exists(OUT) else {}

    ledger = []
    for pool, rec in cache.items():
        if rec.get("why") != "ok" or rec.get("net") is None or not rec.get("exb"):
            continue
        if rec.get("fric") is None or rec["fric"] < FRIC_THR:
            continue
        if pool not in pools:
            continue
        ledger.append((pool, rec))
    print(f"replaying {len(ledger)} friction-gated trades (already done {len(out)})", flush=True)

    t0 = time.time()
    for i, (pool, rec) in enumerate(ledger):
        if pool in out:
            continue
        alt = Web3.to_checksum_address(pools[pool]["alt"])
        fee = int(rec["fee"])
        eb = pools[pool]["entry_block"]
        xb = int(rec["exb"])
        tokens = quote_buy(alt, fee, eb)
        if not tokens or tokens <= 0:
            out[pool] = {"status": "entry_quote_zero", "cached_net": rec["net"]}
        else:
            ev, _ = realgas_sell_ok(alt, fee, tokens, eb)       # sellable at ENTRY (real gas)?
            xv, xout = realgas_sell_ok(alt, fee, tokens, xb)    # sellable at EXIT (real gas)?
            if xv == "ok":
                real_net = xout / AMT_IN - 1.0
            elif xv == "revert":
                real_net = -1.0
            else:
                real_net = None  # unavailable -> keep cached
            out[pool] = {
                "status": "ok", "fee": fee, "entry_block": eb, "exit_block": xb,
                "entry_sell": ev, "exit_sell": xv,
                "cached_net": rec["net"], "real_net": real_net,
                "exwhy": rec.get("exwhy"),
            }
        if (i + 1) % 25 == 0:
            json.dump(out, open(OUT, "w"))
            el = time.time() - t0
            print(f"  {i+1}/{len(ledger)}  elapsed={el:.0f}s", flush=True)
    json.dump(out, open(OUT, "w"))

    # ---- analysis ----
    rows = [r for r in out.values() if r.get("status") == "ok"]
    cached = np.array([r["cached_net"] for r in rows], dtype=float)
    real = np.array([r["real_net"] if r["real_net"] is not None else r["cached_net"] for r in rows], dtype=float)
    verified = np.array([r["real_net"] is not None for r in rows])
    exit_revert = np.array([r["exit_sell"] == "revert" for r in rows])
    entry_ok = np.array([r["entry_sell"] == "ok" for r in rows])
    unavail = np.array([r["exit_sell"] == "unavailable" for r in rows])

    # hidden honeypot = cached said sellable (>-0.99) but real-gas exit reverts
    hidden = exit_revert & (cached > -0.99)
    buy_then_hp = entry_ok & exit_revert  # sellable at entry, dead at exit

    def stat(tag, arr):
        a = arr[~np.isnan(arr)]
        print(f"  {tag:30s} n={len(a):4d} mean={a.mean():+.3f} med={np.median(a):+.3f} "
              f"WR={(a>0).mean():.2f} fatal(<=-50%)={(a<=-0.5).mean():.3f}")

    print("\n" + "=" * 78)
    print(f"REAL-GAS REPLAY  (gas_cap={SELL_GAS:,}, notional=${NOTIONAL_USD:.0f}, friction>= {FRIC_THR})")
    print("=" * 78)
    print(f"trades replayed:           {len(rows)}")
    print(f"  verified by real-gas sim: {verified.sum()}  ({verified.mean()*100:.0f}%)")
    print(f"  unverifiable (slot/rpc):  {unavail.sum()}  -> kept cached net")
    print(f"  exit reverts at real gas: {exit_revert.sum()}")
    print(f"  HIDDEN honeypots (quote-sellable but real-gas-dead): {hidden.sum()}")
    print(f"  'buy-then-honeypot' (sellable@entry, dead@exit):     {buy_then_hp.sum()}")
    print()
    stat("CACHED backtest (QuoterV2)", cached)
    stat("REAL-GAS corrected", real)
    print()
    # monthly impact (gate-only universe). te_days from cache window not stored; use ratio.
    print("per-trade mean drop: %+.3f -> %+.3f  (%.0f%% of the mean P&L was illusory)"
          % (np.nanmean(cached), np.nanmean(real),
             (1 - np.nanmean(real) / np.nanmean(cached)) * 100 if np.nanmean(cached) else 0))


if __name__ == "__main__":
    main()
