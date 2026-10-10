#!/usr/bin/env python3
"""Pinned-block B20 backtest.

Anvil cannot execute B20 precompiles (OpcodeNotFound). Official fills use the
B4/B8 AtomicRoundTrip archive eth_call at the entry/exit block, plus QuoterV2
for the hold. FORK_ARCHIVE_RPC required. No broadcast.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out"
EATLQ = Path("/opt/EATLQ")
LIB = EATLQ / "scripts" / "lib"
sys.path.insert(0, str(LIB))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from params import FRIC_FLOOR, SPEND_ETH, SPEND_WEI, WETH  # noqa: E402
from v3_quoterv2_struct import (  # noqa: E402
    JsonRpcClient,
    quote_exact_input_single_struct,
)

FROM = "0x000000000000000000000000000000000000b405"
GAS_ASSUME_BUY = 600_000
GAS_ASSUME_SELL = 600_000


def compile_creation_bytecode() -> str:
    out = Path("/tmp/b20forgeout")
    art = out / "AtomicRoundTrip.sol" / "AtomicRoundTripCreateB4.json"
    if not art.exists():
        subprocess.run(
            [
                str(Path.home() / ".foundry/bin/forge"),
                "build",
                "--contracts",
                "scripts/research/base_b4/AtomicRoundTrip.sol",
                "--out",
                str(out),
                "--cache-path",
                "/tmp/b20forgecache",
            ],
            cwd=str(EATLQ),
            check=True,
        )
    return json.loads(art.read_text())["bytecode"]["object"].removeprefix("0x")


def atomic_calldata(bytecode: str, token: str, fee: int, spend: int) -> str:
    return (
        "0x"
        + bytecode
        + token.lower().removeprefix("0x").rjust(64, "0")
        + f"{int(fee):064x}"
        + f"{int(spend):064x}"
    )


def decode_atomic(raw: str) -> dict:
    if not raw or len(raw) < 2 + 16 * 64:
        raise ValueError("short atomic return")
    w = [int(raw[2 + i * 64 : 2 + (i + 1) * 64], 16) for i in range(16)]
    keys = [
        "flags", "router_buy_out", "target_before", "target_after_buy",
        "target_after_sell", "router_sell_out", "weth_after_deposit",
        "weth_after_buy", "weth_after_sell", "pin_block", "chain_id",
        "parent_block_hash", "weth_approve_error_hash", "buy_error_hash",
        "target_approve_error_hash", "sell_error_hash",
    ]
    return dict(zip(keys, w))


def rpc_eth_call(url: str, tx: dict, block: int, override: dict | None = None) -> str:
    params = [tx, hex(int(block))]
    if override is not None:
        params.append(override)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": params}).encode()
    req = Request(url, data=body, headers={"content-type": "application/json"})
    with urlopen(req, timeout=120) as resp:
        obj = json.load(resp)
    if obj.get("error"):
        raise RuntimeError(str(obj["error"])[:240])
    return obj.get("result") or "0x"


def atomic_at(url: str, bytecode: str, token: str, fee: int, block: int) -> dict:
    tx = {
        "from": FROM,
        "data": atomic_calldata(bytecode, token, fee, SPEND_WEI),
        "value": hex(SPEND_WEI),
        "gas": hex(20_000_000),
    }
    raw = rpc_eth_call(url, tx, block, {FROM: {"balance": hex(10**18)}})
    a = decode_atomic(raw)
    received = a["target_after_buy"] - a["target_before"]
    buy_ok = bool(a["flags"] & 2) and received > 0
    sell_ok = bool(a["flags"] & 8) and a["weth_after_sell"] > a["weth_after_buy"]
    recovered = (a["weth_after_sell"] - a["weth_after_buy"]) if sell_ok else 0
    slip = (recovered / SPEND_WEI - 1.0) if sell_ok else None
    return {
        "buy_ok": buy_ok,
        "sell_ok": sell_ok,
        "received": received,
        "recovered_weth": recovered,
        "slippage": slip,
        "pin_block": a["pin_block"],
        "flags": a["flags"],
        "buy_error_hash": hex(a["buy_error_hash"]),
        "sell_error_hash": hex(a["sell_error_hash"]),
    }


def base_fee(rpc: JsonRpcClient, block: int) -> int:
    blk = rpc.call("eth_getBlockByNumber", [hex(int(block)), False]) or {}
    return int(blk.get("baseFeePerGas") or "0x0", 16)


def score_one(rpc: JsonRpcClient, url: str, bytecode: str, row: dict) -> dict:
    rec = dict(row)
    rec["entered"] = False
    rec["label"] = None
    rec["instrument"] = "atomic_eth_call"
    t0 = time.time()
    try:
        ent = atomic_at(url, bytecode, row["token"], int(row["fee"]), int(row["entry_block"]))
    except Exception as exc:
        rec["label"] = "error"
        rec["note"] = f"entry_atomic:{type(exc).__name__}:{str(exc)[:160]}"
        rec["elapsed_s"] = round(time.time() - t0, 1)
        return rec
    rec["entry_atomic"] = ent
    if not ent["buy_ok"]:
        rec["label"] = "dead"
        rec["elapsed_s"] = round(time.time() - t0, 1)
        return rec
    if not ent["sell_ok"]:
        rec["label"] = "instant_hp"
        rec["official_net"] = -1.0
        rec["elapsed_s"] = round(time.time() - t0, 1)
        return rec
    rec["fric"] = ent["slippage"]
    if ent["slippage"] is None or ent["slippage"] < FRIC_FLOOR:
        rec["label"] = "friction_skip"
        rec["elapsed_s"] = round(time.time() - t0, 1)
        return rec

    rec["entered"] = True
    buy_q = quote_exact_input_single_struct(
        rpc, WETH, row["token"], SPEND_WEI, int(row["fee"]), block_identifier=int(row["entry_block"])
    )
    rec["qv2_buy"] = {"ok": buy_q.get("ok"), "amount_out": buy_q.get("amount_out"), "reason": buy_q.get("reason")}
    tokens = int(buy_q.get("amount_out") or 0)
    if tokens > 0:
        sell_q = quote_exact_input_single_struct(
            rpc, row["token"], WETH, tokens, int(row["fee"]), block_identifier=int(row["exit_block"])
        )
        rec["qv2_sell"] = {
            "ok": sell_q.get("ok"),
            "amount_out": sell_q.get("amount_out"),
            "reason": sell_q.get("reason"),
        }
        rec["qv2_net"] = (sell_q["amount_out"] / SPEND_WEI - 1.0) if sell_q.get("ok") and sell_q.get("amount_out") else -1.0
    else:
        rec["qv2_net"] = -1.0
        rec["qv2_sell"] = {"ok": False, "reason": "entry_quote_zero"}

    try:
        ex = atomic_at(url, bytecode, row["token"], int(row["fee"]), int(row["exit_block"]))
        rec["exit_atomic"] = ex
        stranded = not (ex["buy_ok"] and ex["sell_ok"])
    except Exception as exc:
        rec["exit_atomic"] = {"error": f"{type(exc).__name__}:{str(exc)[:160]}"}
        stranded = True

    gp_e = base_fee(rpc, int(row["entry_block"]))
    gp_x = base_fee(rpc, int(row["exit_block"]))
    rec["gas_eth"] = (GAS_ASSUME_BUY * gp_e + GAS_ASSUME_SELL * gp_x) / 1e18
    if stranded:
        rec["label"] = "stranded"
        rec["official_net"] = -1.0
    else:
        rec["label"] = "entered"
        rec["official_net"] = float(rec["qv2_net"]) - rec["gas_eth"]
    rec["elapsed_s"] = round(time.time() - t0, 1)
    return rec


def main() -> int:
    if not os.environ.get("FORK_ARCHIVE_RPC"):
        raise SystemExit("FORK_ARCHIVE_RPC is not set")
    sig_path = OUT / "signals.json"
    payload = json.loads(sig_path.read_text(encoding="utf-8"))
    rows = payload.get("arm_a") or []
    out_path = OUT / "fork_results.json"
    bytecode = compile_creation_bytecode()
    url = os.environ["FORK_ARCHIVE_RPC"]
    rpc = JsonRpcClient(url, delay_s=0.03)
    trades = []
    print(f"arm_a n={len(rows)} instrument=atomic_eth_call")
    for i, row in enumerate(rows, 1):
        rec = score_one(rpc, url, bytecode, row)
        trades.append(rec)
        out_path.write_text(
            json.dumps({"updated_at": datetime.now(timezone.utc).isoformat(), "trades": trades}, indent=2),
            encoding="utf-8",
        )
        print(
            f"  [{i}/{len(rows)}] {row.get('sym','?'):12} {rec.get('label')} "
            f"net={rec.get('official_net')} fric={rec.get('fric')} {rec.get('elapsed_s')}s",
            flush=True,
        )

    entered = [t for t in trades if t.get("official_net") is not None]
    nets = [float(t["official_net"]) for t in entered]
    summary = {
        "n_signals": len(rows),
        "n_replayed": len(trades),
        "n_dead": sum(1 for t in trades if t.get("label") == "dead"),
        "n_instant_hp": sum(1 for t in trades if t.get("label") == "instant_hp"),
        "n_friction_skip": sum(1 for t in trades if t.get("label") == "friction_skip"),
        "n_stranded": sum(1 for t in trades if t.get("label") == "stranded"),
        "n_entered_clean": sum(1 for t in trades if t.get("label") == "entered"),
        "n_official": len(nets),
        "instrument": "atomic_eth_call+quoterv2",
        "rpc_calls": rpc.calls,
        "anvil_note": "anvil OpcodeNotFound on B20; not used for official net",
    }
    if nets:
        import statistics

        summary["mean"] = statistics.fmean(nets)
        summary["median"] = statistics.median(nets)
        summary["win_rate"] = sum(1 for x in nets if x > 0) / len(nets)
        summary["best"] = max(nets)
        summary["worst"] = min(nets)
        summary["sum_eth"] = sum(x * SPEND_ETH for x in nets)
    out_path.write_text(
        json.dumps(
            {"updated_at": datetime.now(timezone.utc).isoformat(), "summary": summary, "trades": trades},
            indent=2,
        ),
        encoding="utf-8",
    )
    print("SUMMARY", json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
