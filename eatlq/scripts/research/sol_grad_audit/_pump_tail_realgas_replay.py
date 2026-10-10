#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T+15→T+90 真实 bonding-curve 小单 replay（链上 TradeEvent + 滑点模拟）."""
from __future__ import annotations

import argparse
import base64
import csv
import importlib.util
import json
import math
import random
import statistics as st
import struct
import time
from datetime import datetime, timezone
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent
DIAG = Path("/opt/EATLQ/mvp/diagnostics/sol")
PUMP_FEE = 0.01
ENTRY_S, EXIT_S = 15, 90
SOL_USD = 150.0  # 滑点档位换算 proxy
NOTIONAL_USD = [5, 10, 20, 50]
MAX_SIG_PAGES = 8
MAX_TX_PROBE = 8

_spec = importlib.util.spec_from_file_location("samp", "/opt/EATLQ/scripts/research/sol/sol_sample_creates.py")
_s = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_s)
RPC = _s.RPC
rpc_url = _s.rpc_url
b58dec = _s.b58dec

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58enc(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n > 0:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(b) - len(b.lstrip(b"\x00"))
    return "1" * pad + out


def decode_reserves(raw: bytes, mint_b: bytes) -> tuple[float, float] | None:
    NEED = 8 + 32 + 8 + 8 + 1 + 32 + 8 + 8 + 8
    if len(raw) < NEED or raw[8:40] != mint_b:
        return None
    off = 8 + 32 + 8 + 8 + 1 + 32 + 8
    vSol = struct.unpack_from("<Q", raw, off)[0] / 1e9
    vTok = struct.unpack_from("<Q", raw, off + 8)[0] / 1e6
    if 1.0 <= vSol <= 5000.0 and 1e5 <= vTok <= 2e9:
        return vSol, vTok
    return None


def tx_reserves(rpc: RPC, sig: str, mint_b: bytes, cache: dict) -> tuple[float, float] | None:
    if sig in cache:
        return cache[sig]
    tx = rpc("getTransaction", [sig, {
        "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed",
    }])
    out = None
    if tx and not tx["meta"].get("err"):
        for L in tx["meta"].get("logMessages", []):
            if not L.startswith("Program data:"):
                continue
            try:
                raw = base64.b64decode(L.split("Program data: ")[1])
            except Exception:
                continue
            out = decode_reserves(raw, mint_b)
            if out:
                break
    cache[sig] = out
    return out


def collect_trades(rpc: RPC, bc: str, until_t: float) -> tuple[list[tuple[float, str, bool]], bool]:
    rows = []
    before = None
    reached = False
    for _ in range(MAX_SIG_PAGES):
        params = [bc, {"limit": 1000, **({"before": before} if before else {})}]
        sigs = rpc("getSignaturesForAddress", params) or []
        if not sigs:
            reached = True
            break
        before = sigs[-1]["signature"]
        for s in sigs:
            bt = s.get("blockTime")
            if bt is not None:
                rows.append((bt, s["signature"], bool(s.get("err"))))
        if (sigs[-1].get("blockTime") or 0) <= until_t:
            reached = True
            break
        if len(sigs) < 1000:
            reached = True
            break
    rows.sort()
    return rows, reached


def pick_sigs(trades: list[tuple[float, str, bool]], target_t: float, prefer_after: bool) -> list[str]:
    succ = [(t, sig) for t, sig, err in trades if not err]
    if not succ:
        return []
    if prefer_after:
        cand = [x for x in succ if x[0] >= target_t][:MAX_TX_PROBE]
        if not cand:
            cand = list(reversed([x for x in succ if x[0] <= target_t][-MAX_TX_PROBE:]))
    else:
        cand = list(reversed([x for x in succ if x[0] <= target_t][-MAX_TX_PROBE:]))
        if not cand:
            cand = [x for x in succ if x[0] >= target_t][:MAX_TX_PROBE]
    out = []
    for t, sig in cand:
        if abs(t - target_t) < 7200:
            out.append(sig)
    return out


def reserves_near(
    rpc: RPC, trades: list, target_t: float, mint_b: bytes, cache: dict, before: bool,
) -> tuple[float, float, str] | None:
    sigs = pick_sigs(trades, target_t, prefer_after=not before)
    for sig in sigs:
        rv = tx_reserves(rpc, sig, mint_b, cache)
        if rv:
            return rv[0], rv[1], sig
    return None


def curve_buy(vSol: float, vTok: float, sol_in: float, fee: float = PUMP_FEE) -> tuple[float, float, float] | None:
    """Return (tokens_out, new_vSol, new_vTok)."""
    if sol_in <= 0 or vSol <= 0 or vTok <= 0:
        return None
    eff = sol_in * (1 - fee)
    tok_out = (eff * vTok) / (vSol + eff)
    if tok_out <= 0 or tok_out >= vTok * 0.999:
        return None
    return tok_out, vSol + eff, vTok - tok_out


def curve_sell(vSol: float, vTok: float, tok_in: float, fee: float = PUMP_FEE) -> tuple[float, float] | None:
    if tok_in <= 0 or vSol <= 0 or vTok <= 0:
        return None
    sol_out = (tok_in * vSol) / (vTok + tok_in)
    sol_out *= 1 - fee
    if sol_out <= 0:
        return None
    return sol_out, sol_out / max(1e-18, tok_in)  # effective price


def simulate_roundtrip(
    entry_res: tuple[float, float],
    exit_res: tuple[float, float],
    sol_in: float,
) -> dict:
    vSol_e, vTok_e = entry_res
    vSol_x, vTok_x = exit_res
    buy = curve_buy(vSol_e, vTok_e, sol_in)
    if not buy:
        return {"ok": False, "reason": "buy_fail"}
    tok, _, _ = buy
    sell = curve_sell(vSol_x, vTok_x, tok)
    if not sell:
        return {"ok": False, "reason": "sell_fail"}
    sol_out, _ = sell
    spot_entry = vSol_e / vTok_e
    spot_exit = vSol_x / vTok_x
    spot_ret = spot_exit / spot_entry - 1
    real_ret = sol_out / sol_in - 1
    return {
        "ok": True,
        "sol_in": sol_in,
        "sol_out": sol_out,
        "tokens": tok,
        "spot_return": spot_ret,
        "real_return": real_ret,
        "slippage_vs_spot": real_ret - spot_ret,
    }


def load_meta() -> dict[str, dict]:
    meta = {}
    for f in [DIAG / "creates_labeled.jsonl", DIAG / "creates_earlypath.jsonl"]:
        for line in open(f):
            r = json.loads(line)
            m = r.get("mint")
            if m and m not in meta:
                meta[m] = r
    return meta


def build_cohort(audit_path: Path, seed: int = 42) -> list[dict]:
    audit = json.loads(audit_path.read_text())
    trades = []
    # rebuild from forensics + need full list - load from lottery audit JSON tradability
    trad = audit.get("tradability_proxy", [])
    if not trad:
        raise SystemExit("need _pump_tail_lottery_audit.json tradability_proxy")
    for t in trad:
        trades.append({"mint": t["mint"], "net_return": t["net_return"], "cohort": "all"})

    srt = sorted(trades, key=lambda x: x["net_return"], reverse=True)
    rng = random.Random(seed)
    cohort = []
    for i, t in enumerate(srt[:10]):
        cohort.append({**t, "cohort": "top10", "rank": i + 1})
    losers = [t for t in trades if t["net_return"] <= -0.03]
    small = [t for t in trades if 0 < t["net_return"] < 0.05]
    boundary = [t for t in trades if abs(t["net_return"]) <= 0.01]
    for label, pool, n in [("loss20", losers, 20), ("small_profit20", small, 20), ("boundary20", boundary, 20)]:
        pick = rng.sample(pool, min(n, len(pool)))
        for t in pick:
            cohort.append({**t, "cohort": label})
    # dedupe
    seen, out = set(), []
    for c in cohort:
        if c["mint"] not in seen:
            seen.add(c["mint"])
            out.append(c)
    return out


def grid_net(mint: str, grids: dict, cost: float) -> float | None:
    g = grids.get(mint)
    if not g:
        return None
    from _grad_audit_lib import grid_price
    pe, px = grid_price(g, ENTRY_S), grid_price(g, EXIT_S)
    if pe and px and pe > 0:
        return (px / pe) * (1 - cost) - 1
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", default=str(OUT_DIR / "_pump_tail_lottery_audit.json"))
    ap.add_argument("--cost", type=float, default=0.03)
    ap.add_argument("--output", default=str(OUT_DIR / "_pump_tail_realgas_replay.json"))
    ap.add_argument("--checkpoint", default=str(OUT_DIR / "_pump_tail_realgas_checkpoint.jsonl"))
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    from _grad_audit_lib import load_grids
    grids = load_grids()
    meta = load_meta()
    cohort = build_cohort(Path(args.audit), args.seed)
    done = {}
    cp = Path(args.checkpoint)
    if cp.exists():
        for line in open(cp):
            r = json.loads(line)
            done[r["mint"]] = r

    rpc = RPC(rpc_url(), min_interval=0.20)
    tx_cache: dict = {}
    results = list(done.values())
    pending = [c for c in cohort if c["mint"] not in done]

    print(f"[replay] cohort={len(cohort)} pending={len(pending)} rpc_start={rpc.calls}")

    with open(cp, "a") as cpf:
        for i, c in enumerate(pending):
            mint = c["mint"]
            m = meta.get(mint, {})
            bc = m.get("bonding_curve")
            created_ms = m.get("created_ms")
            if not bc or not created_ms:
                row = {"mint": mint, "cohort": c["cohort"], "ok": False, "reason": "missing_bc_or_created"}
                cpf.write(json.dumps(row) + "\n")
                cpf.flush()
                results.append(row)
                continue

            created = created_ms / 1000.0
            entry_t = created + ENTRY_S
            exit_t = created + EXIT_S
            mint_b = b58dec(mint)
            trades, reached = collect_trades(rpc, bc, created - 5)

            ent = reserves_near(rpc, trades, entry_t, mint_b, tx_cache, before=True)
            ex = reserves_near(rpc, trades, exit_t, mint_b, tx_cache, before=True)

            grid_ret = grid_net(mint, grids, args.cost)
            sims = {}
            if ent and ex:
                for usd in NOTIONAL_USD:
                    sol_in = usd / SOL_USD
                    sims[f"{usd}U"] = simulate_roundtrip((ent[0], ent[1]), (ex[0], ex[1]), sol_in)
            else:
                sims = {"error": {"ok": False, "reason": "no_reserves"}}

            ref_usd = 10
            sim10 = sims.get("10U", {})
            row = {
                "mint": mint,
                "cohort": c["cohort"],
                "grid_rank": c.get("rank"),
                "grid_net_return": c.get("net_return"),
                "grid_recalc": grid_ret,
                "bonding_curve": bc,
                "created_ms": created_ms,
                "entry_t": entry_t,
                "exit_t": exit_t,
                "trades_collected": len(trades),
                "reached_birth": reached,
                "entry_reserves": {"vSol": ent[0], "vTok": ent[1], "sig": ent[2]} if ent else None,
                "exit_reserves": {"vSol": ex[0], "vTok": ex[1], "sig": ex[2]} if ex else None,
                "simulations": sims,
                "ok": bool(ent and ex and sim10.get("ok")),
                "real_return_10U": sim10.get("real_return"),
                "spot_return": sim10.get("spot_return"),
                "slippage_vs_spot_10U": sim10.get("slippage_vs_spot"),
                "sell_fail": sim10.get("reason") == "sell_fail",
                "buy_fail": sim10.get("reason") == "buy_fail",
                "tradability": "OK" if ent and ex else "NEEDS_RPC_REPLAY",
            }
            if ent and ex and grid_ret is not None and sim10.get("real_return") is not None:
                row["grid_vs_real_delta_10U"] = sim10["real_return"] - grid_ret

            cpf.write(json.dumps(row) + "\n")
            cpf.flush()
            results.append(row)
            if (i + 1) % 5 == 0:
                print(f"  [{i+1}/{len(pending)}] rpc={rpc.calls} last={mint[:8]} ok={row['ok']}", flush=True)

    # aggregate
    top = [r for r in results if r.get("cohort") == "top10"]
    top_ok = [r for r in top if r.get("ok")]
    top_fail = [r for r in top if not r.get("ok")]
    deltas = [r["grid_vs_real_delta_10U"] for r in results if r.get("grid_vs_real_delta_10U") is not None]
    real10 = [r["real_return_10U"] for r in results if r.get("real_return_10U") is not None]
    grid10 = [r["grid_recalc"] for r in results if r.get("grid_recalc") is not None]

    def haircut(real, grid):
        if grid and abs(grid) > 1e-9:
            return 1 - real / grid
        return None

    haircuts = [haircut(r["real_return_10U"], r["grid_recalc"]) for r in results
                if r.get("real_return_10U") is not None and r.get("grid_recalc") is not None]

    verdict = "PASS_REPLAY"
    reasons = []
    if len(top_fail) / max(len(top), 1) > 0.30:
        verdict = "FAIL_UNTRADABLE"
        reasons.append(f"top10 fail rate {len(top_fail)}/{len(top)} > 30%")
    if haircuts:
        med_h = st.median(haircuts)
        if med_h > 0.5:
            verdict = "FAIL_HAIRCUT"
            reasons.append(f"median grid haircut {med_h:.1%} > 50%")
    if real10 and st.median(real10) <= -0.03:
        reasons.append(f"median real 10U return {st.median(real10):+.3f}")

    summary = {
        "n_cohort": len(cohort),
        "n_replayed": len(results),
        "rpc_calls": rpc.calls,
        "top10_ok": len(top_ok),
        "top10_fail": len(top_fail),
        "median_real_return_10U": st.median(real10) if real10 else None,
        "median_grid_return": st.median(grid10) if grid10 else None,
        "median_haircut_vs_grid": st.median(haircuts) if haircuts else None,
        "mean_haircut_vs_grid": sum(haircuts) / len(haircuts) if haircuts else None,
        "verdict": verdict,
        "reasons": reasons,
    }

    out = {
        "meta": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "entry_s": ENTRY_S,
            "exit_s": EXIT_S,
            "notional_usd": NOTIONAL_USD,
            "sol_usd_assumed": SOL_USD,
            "pump_fee": PUMP_FEE,
            "rpc_calls": rpc.calls,
        },
        "summary": summary,
        "results": results,
    }
    Path(args.output).write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # CSV
    csv_path = OUT_DIR / "_pump_tail_realgas_replay.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        fields = ["mint", "cohort", "ok", "grid_net_return", "real_return_10U", "spot_return",
                  "slippage_vs_spot_10U", "grid_vs_real_delta_10U", "tradability", "sell_fail", "buy_fail"]
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow(r)

    md = OUT_DIR / "_pump_tail_realgas_replay_readout.md"
    md.write_text(
        "\n".join([
            "# T+15→T+90 真实小单 Replay 结果",
            "",
            f"RPC 调用：**{rpc.calls}** | 样本：**{len(results)}**",
            "",
            f"## 裁定：**{verdict}**",
            "",
            "### Top10",
            f"- 可复现买卖：{len(top_ok)}/{len(top)}",
            f"- 失败：{len(top_fail)}",
            "",
            "### Grid vs 真实（10U）",
            f"- median real return：{summary['median_real_return_10U']}",
            f"- median grid return：{summary['median_grid_return']}",
            f"- median haircut vs grid：{summary['median_haircut_vs_grid']}",
            "",
            "### 原因",
            *[f"- {x}" for x in reasons],
            "",
            "详见 `_pump_tail_realgas_replay.json`",
        ]) + "\n",
        encoding="utf-8",
    )
    print(f"Done rpc={rpc.calls} verdict={verdict} -> {args.output}")


if __name__ == "__main__":
    main()
