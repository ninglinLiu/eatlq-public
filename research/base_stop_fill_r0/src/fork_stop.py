#!/usr/bin/env python3
"""Price each drawdown episode's stop on the real historical block. See ../GOAL.md.

Two measurements per episode, both archive `eth_call`, nothing broadcast:

  peak_block  AtomicRoundTripCreateB4 buys $50 of the token and sells it back in
              the same block. The token amount bought is the inventory a strategy
              would actually be holding when the drawdown starts, and the
              round-trip loss is the calm-market friction baseline.
  stop_block  QuoterV2 sells that exact inventory. What comes back, against the
              $50 that went in, is the stop's realised fill. A second atomic
              round trip at the same block establishes that a sell is executable
              at all, which a quote cannot do.

The gap between the realised fill and the intended -30% is the number that decides
whether the +200% cell needs 26.6% or 68.6% hit rate.
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out"
EATLQ = Path("/opt/EATLQ")
sys.path.insert(0, str(EATLQ / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from params import SPEND_WEI, WETH  # noqa: E402
from v3_quoterv2_struct import (  # noqa: E402
    JsonRpcClient,
    quote_exact_input_single_struct,
)

FROM = "0x000000000000000000000000000000000000b405"


def compile_creation_bytecode() -> str:
    out = Path("/tmp/stopforgeout")
    art = out / "AtomicRoundTrip.sol" / "AtomicRoundTripCreateB4.json"
    if not art.exists():
        subprocess.run([str(Path.home() / ".foundry/bin/forge"), "build",
                        "--contracts", "scripts/research/base_b4/AtomicRoundTrip.sol",
                        "--out", str(out), "--cache-path", "/tmp/stopforgecache"],
                       cwd=str(EATLQ), check=True)
    return json.loads(art.read_text())["bytecode"]["object"].removeprefix("0x")


def atomic_calldata(bytecode: str, token: str, fee: int, spend: int) -> str:
    return ("0x" + bytecode + token.lower().removeprefix("0x").rjust(64, "0")
            + f"{int(fee):064x}" + f"{int(spend):064x}")


def decode_atomic(raw: str) -> dict:
    if not raw or len(raw) < 2 + 16 * 64:
        raise ValueError("short atomic return")
    w = [int(raw[2 + i * 64: 2 + (i + 1) * 64], 16) for i in range(16)]
    keys = ["flags", "router_buy_out", "target_before", "target_after_buy",
            "target_after_sell", "router_sell_out", "weth_after_deposit",
            "weth_after_buy", "weth_after_sell", "pin_block", "chain_id",
            "parent_block_hash", "weth_approve_error_hash", "buy_error_hash",
            "target_approve_error_hash", "sell_error_hash"]
    return dict(zip(keys, w))


def rpc_eth_call(url: str, tx: dict, block: int, override: dict | None = None) -> str:
    params = [tx, hex(int(block))]
    if override is not None:
        params.append(override)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": params}).encode()
    req = Request(url, data=body, headers={"content-type": "application/json"})
    with urlopen(req, timeout=120) as resp:
        obj = json.load(resp)
    if obj.get("error"):
        raise RuntimeError(str(obj["error"])[:240])
    return obj.get("result") or "0x"


def atomic_at(url: str, bytecode: str, token: str, fee: int, block: int) -> dict:
    tx = {"from": FROM,
          "data": atomic_calldata(bytecode, token, fee, SPEND_WEI),
          "value": hex(SPEND_WEI), "gas": hex(20_000_000)}
    # The override has to cover the value being sent. A flat 1 ETH silently turned
    # every size above that into a fake failure in the first sweep.
    bal = max(10 ** 18, 4 * SPEND_WEI)
    a = decode_atomic(rpc_eth_call(url, tx, block, {FROM: {"balance": hex(bal)}}))
    received = a["target_after_buy"] - a["target_before"]
    buy_ok = bool(a["flags"] & 2) and received > 0
    sell_ok = bool(a["flags"] & 8) and a["weth_after_sell"] > a["weth_after_buy"]
    recovered = (a["weth_after_sell"] - a["weth_after_buy"]) if sell_ok else 0
    return dict(buy_ok=buy_ok, sell_ok=sell_ok, received=received,
                recovered_weth=recovered,
                friction=(recovered / SPEND_WEI - 1.0) if sell_ok else None,
                flags=a["flags"], buy_error_hash=hex(a["buy_error_hash"]),
                sell_error_hash=hex(a["sell_error_hash"]))


def score_one(rpc, url: str, bytecode: str, e: dict) -> dict:
    rec = dict(e)
    t0 = time.time()
    try:
        pk = atomic_at(url, bytecode, e["token"], e["fee"], e["peak_block"])
    except Exception as exc:
        rec.update(label="error_peak", note=f"{type(exc).__name__}:{str(exc)[:140]}",
                   elapsed_s=round(time.time() - t0, 1))
        return rec
    rec["peak_atomic"] = pk
    if not pk["buy_ok"]:
        rec.update(label="no_entry", elapsed_s=round(time.time() - t0, 1))
        return rec
    rec["peak_friction"] = pk["friction"]
    inv = pk["received"]

    try:
        st = atomic_at(url, bytecode, e["token"], e["fee"], e["stop_block"])
        rec["stop_atomic"] = st
        rec["stop_friction"] = st["friction"]
        rec["stop_sell_ok"] = bool(st["sell_ok"])
    except Exception as exc:
        rec["stop_atomic"] = {"error": f"{type(exc).__name__}:{str(exc)[:140]}"}
        rec["stop_sell_ok"] = False
        rec["stop_friction"] = None

    q = quote_exact_input_single_struct(rpc, e["token"], WETH, inv, e["fee"],
                                       block_identifier=e["stop_block"])
    rec["stop_quote"] = {"ok": q.get("ok"), "amount_out": q.get("amount_out"),
                         "reason": q.get("reason")}
    if q.get("ok") and q.get("amount_out"):
        rec["realized_stop"] = q["amount_out"] / SPEND_WEI - 1.0
        rec["label"] = "filled" if rec["stop_sell_ok"] else "quote_only"
    else:
        rec["realized_stop"] = -1.0
        rec["label"] = "cannot_sell"
    rec["elapsed_s"] = round(time.time() - t0, 1)
    return rec


def main() -> int:
    url = os.environ.get("FORK_ARCHIVE_RPC")
    if not url:
        raise SystemExit("FORK_ARCHIVE_RPC is not set")
    eps = json.loads((OUT / "episodes.json").read_text())["episodes"]
    bytecode = compile_creation_bytecode()
    rpc = JsonRpcClient(url, delay_s=0.03)
    res, path = [], OUT / "stop_results.json"
    print(f"episodes={len(eps)} instrument=atomic_eth_call+quoterv2 spend={SPEND_WEI/1e18} ETH")
    for i, e in enumerate(eps, 1):
        r = score_one(rpc, url, bytecode, e)
        res.append(r)
        if i % 10 == 0 or i == len(eps):
            path.write_text(json.dumps({"trades": res}, indent=1), encoding="utf-8")
        rs = r.get("realized_stop")
        print(f"  [{i}/{len(eps)}] {e['symbol']:<12} {r.get('label'):<11} "
              f"intended={100*e['drop']:>7.1f}%  realized="
              f"{(f'{100*rs:>7.1f}%' if rs is not None else '    n/a')}  "
              f"peak_fric={_p(r.get('peak_friction'))} stop_fric={_p(r.get('stop_friction'))} "
              f"{r.get('elapsed_s')}s", flush=True)

    ok = [r for r in res if r.get("realized_stop") is not None
          and r.get("label") in ("filled", "quote_only", "cannot_sell")]
    rs = [r["realized_stop"] for r in ok]
    paired = [(r["stop_friction"], r["peak_friction"]) for r in res
              if r.get("stop_friction") is not None and r.get("peak_friction") is not None]
    summary = dict(
        n_episodes=len(eps), n_scored=len(ok),
        n_no_entry=sum(1 for r in res if r.get("label") == "no_entry"),
        n_error=sum(1 for r in res if str(r.get("label", "")).startswith("error")),
        n_cannot_sell=sum(1 for r in res if r.get("label") == "cannot_sell"),
        sell_ok_rate=(sum(1 for r in res if r.get("stop_sell_ok")) /
                      max(1, sum(1 for r in res if "stop_sell_ok" in r))),
        intended_stop_med=statistics.median([e["drop"] for e in eps]),
        realized_stop_med=statistics.median(rs) if rs else None,
        realized_stop_mean=statistics.fmean(rs) if rs else None,
        realized_stop_q10=(sorted(rs)[len(rs) // 10] if len(rs) >= 10 else None),
        realized_stop_q90=(sorted(rs)[9 * len(rs) // 10] if len(rs) >= 10 else None),
        peak_friction_med=statistics.median([p for _, p in paired]) if paired else None,
        stop_friction_med=statistics.median([s for s, _ in paired]) if paired else None,
        paired_delta_med=statistics.median([s - p for s, p in paired]) if paired else None,
        n_paired=len(paired), rpc_calls=rpc.calls,
        instrument="atomic_eth_call+quoterv2", broadcast=False)
    path.write_text(json.dumps(
        {"updated_at": datetime.now(timezone.utc).isoformat(),
         "summary": summary, "trades": res}, indent=1), encoding="utf-8")
    print("\nSUMMARY " + json.dumps(summary, indent=1))
    return 0


def _p(x) -> str:
    return "    n/a" if x is None else f"{100*x:>6.1f}%"


if __name__ == "__main__":
    raise SystemExit(main())
