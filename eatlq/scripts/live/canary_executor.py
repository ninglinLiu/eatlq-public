#!/usr/bin/env python3
"""
Canary executor for the corrected fishing method.

Consumes CLEAN candidates from the detector ledger and executes a small real-money
round-trip (buy -> hold -> sell) under HARD caps. DRY-RUN by default; real money
only with --live.

HARD CAPS (refuse to exceed; no override):
  TOTAL_CAP_USD      = 50     # max cumulative capital deployed
  PER_TRADE_MIN/MAX  = 5 / 10 # per-trade notional
  MAX_TRADES         = 10
  MAX_REALIZED_LOSS  = -20    # halt all new buys if (realized + open unrealized) PnL <= this
  HOLD_SEC           = 7200   # 2h time stop (matches sprint097 validated horizon)

EXIT (trailing_5_pull3_2h, the sprint097/sprint095 validated convex exit):
  +5%  -> sell 50%      | +20% -> sell another 30%
  trailing: arm at peak>=+5%, exit remainder on 3% pullback from peak
  hard stop: -10% exits the entire remainder | time stop: 2h exits any remainder

Reads CLEAN candidates schema-safely (canary_ledger), re-verifies QuoterV2
two-sided sellability immediately before every buy, and re-prices every open
position each cycle for the exit ladder + equity-aware halt. Every action is
appended to the live ledger.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

EXEC_SRC = "/opt/EATLQ/services/executor/src"
sys.path.insert(0, EXEC_SRC)
sys.path.insert(0, "/opt/EATLQ/scripts/lib")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from web3 import Web3  # noqa: E402
from eth_account import Account  # noqa: E402
import registry as reg  # noqa: E402
import swap as swp  # noqa: E402
from v3_quoterv2_struct import JsonRpcClient, quote_exact_input_single_struct, WETH_BASE  # noqa: E402
from canary_ledger import read_clean_candidates as _read_clean_candidates  # noqa: E402

CAND_LEDGER = os.environ.get("CANARY_CAND_LEDGER", "/opt/EATLQ/mvp/diagnostics/live/candidates_ledger.csv")
LIVE_LEDGER = os.environ.get("CANARY_LIVE_LEDGER", "/opt/EATLQ/mvp/diagnostics/live/live_ledger.csv")
STATE = os.environ.get("CANARY_STATE", "/opt/EATLQ/mvp/diagnostics/live/canary_state.json")

TOTAL_CAP_USD = 50.0
PER_TRADE_MIN, PER_TRADE_MAX = 5.0, 10.0
MAX_TRADES = 10
MAX_REALIZED_LOSS = -20.0
HOLD_SEC = int(os.environ.get("CANARY_HOLD_SEC", "7200"))

# ── Exit policy: trailing_5_pull3_2h (the exit validated in sprint097/sprint095) ──
# Replaces the previous naive "hold a flat 2h then dump" which diverged from the
# backtested convex exit. All thresholds are fractions of the entry price.
TP1_GAIN, TP1_FRAC = 0.05, 0.50   # +5%  -> sell 50% of the original position
TP2_GAIN, TP2_FRAC = 0.20, 0.30   # +20% -> sell another 30% of the original position
TRAIL_ARM = 0.05                   # trailing stop arms once peak gain >= +5%
TRAIL_PULL = 0.03                  # ...then exit the remainder on a 3% pullback from peak
STOP_LOSS = -0.10                  # hard stop: -10% -> exit the entire remainder
# HOLD_SEC above is the time stop (default 2h): exit any remainder at the horizon.

ERC20_DEC = "0x313ce567"  # decimals()


def now():
    return datetime.now(timezone.utc)


def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {"trades_done": 0, "deployed_usd": 0.0, "realized_pnl_usd": 0.0,
            "traded_pools": [], "open": []}


def save_state(s):
    json.dump(s, open(STATE, "w"), indent=2)


def ledger(row):
    cols = ["ts", "action", "pool", "alt", "size_usd", "tx_hash", "amount_in", "amount_out",
            "realized_pnl_usd", "dry_run", "note"]
    h = not os.path.exists(LIVE_LEDGER)
    with open(LIVE_LEDGER, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if h:
            w.writeheader()
        w.writerow(row)


def alt_decimals(rpc, token):
    try:
        r = rpc.call("eth_call", [{"to": token, "data": ERC20_DEC}, "latest"])
        return int(r, 16) if r and r != "0x" else 18
    except Exception:
        return 18


def read_clean_candidates():
    # Schema-safe read (see canary_ledger): immune to the header/row column drift
    # that previously made every CLEAN row invisible to the executor.
    return _read_clean_candidates(CAND_LEDGER)


def value_weth(rpc, alt, fee, tokens):
    """Current sellable WETH (wei) for `tokens` of alt, via QuoterV2 (read-only)."""
    tokens = int(tokens)
    if tokens <= 0:
        return 0
    q = quote_exact_input_single_struct(rpc, alt, WETH_BASE, tokens, fee)
    return int(q.get("amount_out", 0)) if q.get("ok") else 0


def do_sell(dry, rpc, w3, acct, p, qty, reason, eth_usd):
    """Sell `qty` tokens of position `p`. Returns (weth_received_wei, tx_or_err)."""
    qty = int(qty)
    if qty <= 0:
        return 0, "noop"
    if dry:
        return value_weth(rpc, p["alt"], p["fee"], qty), "DRY"
    pool = reg.PoolInfo(**p["poolinfo"])
    res = swp.execute_sell(w3, acct, pool, qty, reason=reason)
    return (res.amount_out if res.success else 0), (res.tx_hash or res.error)


def _pos_defaults(p):
    """Backfill exit-ladder fields for positions opened before this schema."""
    tokens = int(p.get("tokens", 0))
    p.setdefault("tokens0", tokens)
    if not p.get("tokens0"):
        p["tokens0"] = tokens or 1
    weth_in = float(p.get("weth_in", 0))
    p.setdefault("entry_px", weth_in / max(int(p["tokens0"]), 1))
    p.setdefault("peak_px", p["entry_px"])
    p.setdefault("tp1_done", False)
    p.setdefault("tp2_done", False)
    return p


def manage_open_positions(rpc, w3, acct, s, dry, eth_usd):
    """Apply trailing_5_pull3_2h to every open position. Mutates s; returns the
    total *unrealized* USD PnL of positions still open after this pass (for the
    equity-aware halt)."""
    still_open = []
    unrealized_usd = 0.0
    for p in s["open"]:
        p = _pos_defaults(p)
        remaining = int(p["tokens"])
        if remaining <= 0:
            continue  # fully exited already; drop

        entry_px = float(p["entry_px"]) or 1e-18
        cur_val = value_weth(rpc, p["alt"], p["fee"], remaining)
        time_due = time.time() >= p["sell_at"]

        # Unpriceable right now (transient quote failure or no liquidity):
        # never let this masquerade as a -100% stop. Only force a real exit at
        # the time horizon; otherwise hold and retry next cycle.
        if cur_val <= 0:
            if time_due and not dry:
                weth_back, txh = do_sell(dry, rpc, w3, acct, p, remaining, "time_decay", eth_usd)
                if weth_back > 0 or txh not in ("noop",):
                    pnl = (weth_back - entry_px * remaining) / 1e18 * eth_usd
                    s["realized_pnl_usd"] += pnl
                    ledger({"ts": now().isoformat(), "action": "SELL", "pool": p["pool"],
                            "alt": p["alt"], "size_usd": p["size_usd"], "tx_hash": txh,
                            "amount_in": remaining, "amount_out": weth_back,
                            "realized_pnl_usd": round(pnl, 4), "dry_run": dry,
                            "note": f"time_exit_unpriceable hold={int(time.time()-p['buy_at'])}s"})
                    print(f"[canary] SELL(time/unpriceable) {p['pool']} pnl=${pnl:+.3f}")
                    continue
            print(f"[canary] HOLD {p['pool']}: unpriceable now, retry next cycle")
            still_open.append(p)
            continue

        cur_px = cur_val / remaining
        if cur_px > p["peak_px"]:
            p["peak_px"] = cur_px
        gain = cur_px / entry_px - 1.0
        peak_gain = p["peak_px"] / entry_px - 1.0

        action = None  # (reason, qty, closes)
        if gain <= STOP_LOSS:
            action = ("stop_loss", remaining, True)
        elif time_due:
            action = ("time_decay", remaining, True)
        elif peak_gain >= TRAIL_ARM and cur_px <= p["peak_px"] * (1 - TRAIL_PULL):
            action = ("trailing_stop", remaining, True)
        elif (not p["tp2_done"]) and gain >= TP2_GAIN:
            qty = min(remaining, int(int(p["tokens0"]) * TP2_FRAC))
            action = ("take_profit", qty, False)
            p["tp2_done"] = True; p["tp1_done"] = True
        elif (not p["tp1_done"]) and gain >= TP1_GAIN:
            qty = min(remaining, int(int(p["tokens0"]) * TP1_FRAC))
            action = ("take_profit", qty, False)
            p["tp1_done"] = True

        if action is None:
            unrealized_usd += remaining * (cur_px - entry_px) / 1e18 * eth_usd
            still_open.append(p)
            print(f"[canary] HOLD {p['pool']} gain={gain:+.1%} peak={peak_gain:+.1%} "
                  f"rem={remaining} hold={int(time.time()-p['buy_at'])}s")
            continue

        reason, qty, closes = action
        qty = min(int(qty), remaining)
        if qty <= 0:
            still_open.append(p); continue
        weth_back, txh = do_sell(dry, rpc, w3, acct, p, qty, reason, eth_usd)
        if (not dry) and weth_back <= 0 and txh == "noop":
            still_open.append(p); continue
        pnl = (weth_back - entry_px * qty) / 1e18 * eth_usd
        s["realized_pnl_usd"] += pnl
        p["tokens"] = remaining - qty
        ledger({"ts": now().isoformat(), "action": "SELL", "pool": p["pool"], "alt": p["alt"],
                "size_usd": p["size_usd"], "tx_hash": txh, "amount_in": qty,
                "amount_out": weth_back, "realized_pnl_usd": round(pnl, 4), "dry_run": dry,
                "note": f"{reason} gain={gain:+.1%} sold={qty}/{p['tokens0']} hold={int(time.time()-p['buy_at'])}s"})
        print(f"[canary] SELL({reason}) {p['pool']} gain={gain:+.1%} qty={qty} "
              f"pnl=${pnl:+.3f} (cum=${s['realized_pnl_usd']:+.2f})")
        if not closes and int(p["tokens"]) > 0:
            unrealized_usd += int(p["tokens"]) * (cur_px - entry_px) / 1e18 * eth_usd
            still_open.append(p)
        # closes == True OR fully sold -> position dropped (not re-appended)

    s["open"] = still_open
    save_state(s)
    return unrealized_usd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default=os.environ.get("BASE_RPC_URL", "https://mainnet.base.org"))
    ap.add_argument("--live", action="store_true", help="REAL MONEY. Omit for dry-run.")
    ap.add_argument("--size-usd", type=float, default=8.0)
    ap.add_argument("--eth-usd", type=float, default=3000.0)
    ap.add_argument("--max-concurrent", type=int, default=2)
    args = ap.parse_args()

    size_usd = max(PER_TRADE_MIN, min(PER_TRADE_MAX, args.size_usd))
    rpc = JsonRpcClient(args.rpc, delay_s=0.03)
    dry = not args.live
    mode = "DRY-RUN" if dry else "*** LIVE REAL MONEY ***"

    s = load_state()
    print(f"[canary] {mode} size=${size_usd} caps: total<=${TOTAL_CAP_USD} trades<={MAX_TRADES} "
          f"loss_halt<=${MAX_REALIZED_LOSS}")
    print(f"[canary] state: trades={s['trades_done']} deployed=${s['deployed_usd']:.2f} "
          f"realized=${s['realized_pnl_usd']:.2f} open={len(s['open'])}")

    w3 = None; acct = None
    if args.live:
        pk = os.environ.get("EXECUTOR_PRIVATE_KEY", "")
        if not pk:
            print("[canary] ABORT: EXECUTOR_PRIVATE_KEY not set"); return
        w3 = Web3(Web3.HTTPProvider(args.rpc))
        acct = Account.from_key(pk)
        print(f"[canary] wallet={acct.address}")

    # 1) Manage open positions: trailing_5_pull3_2h (TP1/TP2/trailing/hard SL/time)
    unrealized_usd = manage_open_positions(rpc, w3, acct, s, dry, args.eth_usd)

    # 2) HALT checks before any new buy
    if s["realized_pnl_usd"] <= MAX_REALIZED_LOSS:
        print(f"[canary] HALT: realized loss ${s['realized_pnl_usd']:.2f} <= ${MAX_REALIZED_LOSS}. No new buys.")
        return
    # equity-aware halt: realized + open unrealized (catches positions bleeding
    # before they are sold, not just already-realized losses).
    equity_pnl = s["realized_pnl_usd"] + unrealized_usd
    if equity_pnl <= MAX_REALIZED_LOSS:
        print(f"[canary] HALT: equity PnL ${equity_pnl:.2f} (realized ${s['realized_pnl_usd']:.2f} "
              f"+ unrealized ${unrealized_usd:.2f}) <= ${MAX_REALIZED_LOSS}. No new buys.")
        return
    if s["trades_done"] >= MAX_TRADES:
        print(f"[canary] cap reached: {s['trades_done']} trades. No new buys."); return
    if s["deployed_usd"] >= TOTAL_CAP_USD:
        print(f"[canary] cap reached: deployed ${s['deployed_usd']:.2f}. No new buys."); return
    if len(s["open"]) >= args.max_concurrent:
        print(f"[canary] max concurrent {args.max_concurrent} open. Wait."); return

    # 3) BUY new CLEAN candidates.
    # The ledger holds one CLEAN row PER SCAN, so a single pool appears many
    # times. Collapse to one entry per pool (keep the freshest = last seen) and
    # exclude pools we already traded or currently hold, so we never open the
    # same pool twice.
    open_pools = {o.get("pool") for o in s["open"]}
    by_pool = {}
    for c in read_clean_candidates():
        if c["pool"] in s["traded_pools"] or c["pool"] in open_pools:
            continue
        by_pool[c["pool"]] = c  # later rows overwrite -> freshest scan wins
    cands = list(by_pool.values())
    if not cands:
        print("[canary] no new CLEAN candidate. No trade. (standards NOT lowered)"); return

    for c in cands:
        if (s["trades_done"] >= MAX_TRADES or s["deployed_usd"] + size_usd > TOTAL_CAP_USD
                or len(s["open"]) >= args.max_concurrent):
            break
        pool_addr = c["pool"]; alt = c["alt"]; fee = int(c["fee"])
        if pool_addr in s["traded_pools"] or any(o.get("pool") == pool_addr for o in s["open"]):
            continue
        size_wei = int(size_usd / args.eth_usd * 1e18)
        # FRESH re-verify two-sided sellability right before buying
        qin = quote_exact_input_single_struct(rpc, WETH_BASE, alt, size_wei, fee)
        if not qin.get("ok"):
            print(f"[canary] skip {pool_addr}: entry quote fail now"); continue
        qout = quote_exact_input_single_struct(rpc, alt, WETH_BASE, int(qin["amount_out"]), fee)
        if not qout.get("ok") or int(qout["amount_out"]) <= 0:
            print(f"[canary] skip {pool_addr}: NOT sellable now (honeypot)"); continue

        dec = alt_decimals(rpc, alt)
        weth_t1 = (c.get("alt") == c.get("alt")) and True  # detector kept WETH-paired; determine order
        # determine token0/token1 ordering for PoolInfo
        # query pool token0
        try:
            t0 = "0x" + rpc.call("eth_call", [{"to": pool_addr, "data": "0x0dfe1681"}, "latest"])[-40:]
        except Exception:
            t0 = WETH_BASE
        t0 = t0.lower()
        fee_bps = fee // 100  # swap.py buy_v3 does fee_bps*100 to recover the raw uniswap fee
        if t0 == WETH_BASE.lower():
            pi = dict(pool_address=pool_addr, pool_type="v3", router=reg.UNISWAP_V3_ROUTER,
                      token0=WETH_BASE, token0_symbol="WETH", token0_decimals=18,
                      token1=alt, token1_symbol="ALT", token1_decimals=dec, fee_bps=fee_bps)
        else:
            pi = dict(pool_address=pool_addr, pool_type="v3", router=reg.UNISWAP_V3_ROUTER,
                      token0=alt, token0_symbol="ALT", token0_decimals=dec,
                      token1=WETH_BASE, token1_symbol="WETH", token1_decimals=18, fee_bps=fee_bps)

        if dry:
            tokens = int(qin["amount_out"]); txh = "DRY"; ok = True
        else:
            pool = reg.PoolInfo(**pi)
            res = swp.execute_buy(w3, acct, pool, size_wei)
            ok = res.success; tokens = res.amount_out; txh = res.tx_hash or res.error
        if not ok:
            print(f"[canary] BUY failed {pool_addr}: {txh}"); continue
        s["trades_done"] += 1; s["deployed_usd"] += size_usd
        s["traded_pools"].append(pool_addr)
        entry_px = size_wei / max(int(tokens), 1)
        s["open"].append({"pool": pool_addr, "alt": alt, "fee": fee,
                          "tokens": tokens, "tokens0": tokens,
                          "weth_in": size_wei, "entry_px": entry_px, "peak_px": entry_px,
                          "tp1_done": False, "tp2_done": False,
                          "size_usd": size_usd, "buy_at": time.time(),
                          "sell_at": time.time() + HOLD_SEC, "poolinfo": pi})
        ledger({"ts": now().isoformat(), "action": "BUY", "pool": pool_addr, "alt": alt,
                "size_usd": size_usd, "tx_hash": txh, "amount_in": size_wei, "amount_out": tokens,
                "realized_pnl_usd": "", "dry_run": dry, "note": f"sell_at +{HOLD_SEC}s"})
        print(f"[canary] BUY {pool_addr} ${size_usd} tokens={tokens} tx={txh}")
        save_state(s)

    save_state(s)
    print(f"[canary] done. trades={s['trades_done']} deployed=${s['deployed_usd']:.2f} "
          f"realized=${s['realized_pnl_usd']:+.2f} open={len(s['open'])}")


if __name__ == "__main__":
    main()
