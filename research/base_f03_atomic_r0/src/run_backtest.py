"""Run F03 atomic backtest. Resume-safe via out/trades.jsonl."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from atomic_engine import AMT_IN, GAS_DEBIT, AtomicSim
from safety import research_entry_guard

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out"
TRADES = OUT / "trades.jsonl"


def pnl_row(buy: dict, sell: dict | None) -> dict:
    if buy["class"] == "INFRASTRUCTURE_UNKNOWN":
        return {"outcome": "INFRASTRUCTURE_UNKNOWN", "pnl_weth": None, "ret": None}
    if buy["class"] != "BUY_TX_SUCCESS":
        return {"outcome": buy["class"], "pnl_weth": -0.0001, "ret": None}
    if sell is None:
        return {"outcome": "INFRASTRUCTURE_UNKNOWN", "pnl_weth": None, "ret": None}
    if sell["class"] == "INFRASTRUCTURE_UNKNOWN":
        return {"outcome": "INFRASTRUCTURE_UNKNOWN", "pnl_weth": None, "ret": None}
    if sell["class"] == "SELL_SUCCESS":
        ret = sell["weth_out"] / AMT_IN - 1.0 - GAS_DEBIT
        return {"outcome": "SELL_SUCCESS", "pnl_weth": ret * (AMT_IN / 1e18), "ret": ret}
    return {"outcome": sell["class"], "pnl_weth": -(AMT_IN / 1e18) - GAS_DEBIT, "ret": -1.0 - GAS_DEBIT}


def already_done() -> set[str]:
    done = set()
    if TRADES.exists():
        for line in TRADES.read_text(encoding="utf-8").splitlines():
            if line.strip():
                done.add(json.loads(line)["signal_id"])
    return done


def main() -> None:
    research_entry_guard()
    sample = pd.read_csv(OUT / "sample.csv")
    sample["block_hour"] = pd.to_datetime(sample.block_hour, utc=True)
    sim = AtomicSim()
    smoke = sim.calibrate_smoke()
    (OUT / "calibration_smoke.json").write_text(json.dumps(smoke, indent=2), encoding="utf-8")
    if not smoke["pass"]:
        print(json.dumps({"verdict": "ATOMIC_LABEL_NOT_TRUSTWORTHY", "smoke": smoke}, indent=2))
        return
    print("CALIBRATION_SMOKE_PASS", flush=True)
    done = already_done()
    todo = sample[sample.exec_class != "NOT_EXECUTABLE"].copy()
    n = len(todo)
    with TRADES.open("a", encoding="utf-8") as fh:
        for i, r in enumerate(todo.itertuples(index=False), 1):
            if r.signal_id in done:
                continue
            if pd.isna(r.entry_block) or pd.isna(r.buy_token) or not str(r.buy_token):
                rec = {
                    "signal_id": r.signal_id,
                    "role": r.role,
                    "exec_class": r.exec_class,
                    "outcome": "MISSING_ENTRY_BLOCK",
                    "pnl_weth": None,
                    "ret": None,
                }
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                continue
            entry = int(r.entry_block)
            exitb = int(r.exit_block)
            fee_info = sim.onchain_fee(r.pool_address, entry)
            rec = {
                "signal_id": r.signal_id,
                "role": r.role,
                "exec_class": r.exec_class,
                "f03_side": r.f03_side,
                "pool": r.pool_address,
                "pair": f"{r.token0_symbol}/{r.token1_symbol}",
                "buy_token": r.buy_token,
                "buy_symbol": r.buy_symbol,
                "block_hour": str(r.block_hour),
                "entry_block": entry,
                "exit_block": exitb,
                "tick_fwd_ret_4": None if pd.isna(r.fwd_ret_4) else float(r.fwd_ret_4),
                "direction_persistence": float(r.direction_persistence),
                "onchain_fee": fee_info.get("fee"),
                "fee_ok": fee_info.get("ok"),
            }
            if not fee_info.get("ok"):
                rec.update({"outcome": "FEE_CALL_FAILED", "pnl_weth": None, "ret": None, "buy": fee_info})
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                print(f"[{i}/{n}] {r.signal_id} FEE_FAIL", flush=True)
                continue
            fee = int(fee_info["fee"])
            q_buy = sim.quote("0x4200000000000000000000000000000000000006", r.buy_token, fee, AMT_IN, entry)
            rec["qv2_buy"] = q_buy
            buy = sim.router_buy(r.buy_token, fee, AMT_IN, entry)
            rec["buy"] = buy
            sell = None
            q_sell = None
            if buy["class"] == "BUY_TX_SUCCESS":
                amt = int(buy["token_out"])
                for add in (0, 5, 30, 150):
                    sell = sim.atomic_sell(r.buy_token, fee, amt, exitb + add)
                    rec["exit_block_used"] = exitb + add
                    if sell["class"] != "INFRASTRUCTURE_UNKNOWN":
                        break
                rec["sell"] = sell
                q_sell = sim.quote(r.buy_token, "0x4200000000000000000000000000000000000006", fee, amt, rec["exit_block_used"])
                rec["qv2_sell"] = q_sell
                if q_buy.get("ok") and q_sell.get("ok") and q_sell.get("amount_out"):
                    rec["qv2_ret"] = q_sell["amount_out"] / AMT_IN - 1.0
                else:
                    rec["qv2_ret"] = None
            rec.update(pnl_row(buy, sell))
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            print(f"[{i}/{n}] {r.signal_id} {rec['outcome']} ret={rec.get('ret')}", flush=True)
    summarize(sim.calls)


def summarize(rpc_calls: int) -> None:
    rows = [json.loads(l) for l in TRADES.read_text(encoding="utf-8").splitlines() if l.strip()]
    df = pd.DataFrame(rows)
    df.to_json(OUT / "trades.json", orient="records")
    slim = df.drop(columns=[c for c in ("buy", "sell", "qv2_buy", "qv2_sell") if c in df.columns], errors="ignore")
    slim.to_csv(OUT / "trades_slim.csv", index=False)

    def pack(q: pd.DataFrame) -> dict:
        labeled = q[q.outcome != "INFRASTRUCTURE_UNKNOWN"]
        filled = q[q.outcome == "SELL_SUCCESS"]
        fatal = q[q.outcome.isin(["DETERMINISTIC_REVERT", "ECONOMICALLY_UNSELLABLE"])]
        revert = q[q.outcome == "ENTRY_TX_REVERT"]
        rets = filled["ret"].dropna().astype(float)
        tick = q["tick_fwd_ret_4"].dropna().astype(float)
        return {
            "n": int(len(q)),
            "n_labeled": int(len(labeled)),
            "n_filled": int(len(filled)),
            "n_entry_revert": int(len(revert)),
            "n_fatal_sell": int(len(fatal)),
            "n_unknown": int((q.outcome == "INFRASTRUCTURE_UNKNOWN").sum()),
            "fill_rate_among_attempted": float((q.outcome == "SELL_SUCCESS").mean()) if len(q) else None,
            "mean_atomic_ret_filled": float(rets.mean()) if len(rets) else None,
            "median_atomic_ret_filled": float(rets.median()) if len(rets) else None,
            "mean_pnl_weth_labeled": float(labeled.pnl_weth.dropna().mean()) if labeled.pnl_weth.notna().any() else None,
            "sum_pnl_weth_labeled": float(labeled.pnl_weth.dropna().sum()) if labeled.pnl_weth.notna().any() else None,
            "mean_tick_ret": float(tick.mean()) if len(tick) else None,
            "mean_qv2_ret": float(q.qv2_ret.dropna().mean()) if "qv2_ret" in q and q.qv2_ret.notna().any() else None,
        }

    primary = df[df.role == "PRIMARY_LONG_TICK"]
    secondary = df[df.role == "SECONDARY_SHORT_VIA"]
    shadow = df[df.role == "SHADOW"]
    # time holdout on primary: last 30% by hour
    verdict = "UNDERPOWERED"
    prim_pack = pack(primary)
    if prim_pack["n_filled"] is not None and prim_pack["n_filled"] >= 30:
        if (prim_pack["mean_atomic_ret_filled"] or 0) > 0 and (prim_pack["fill_rate_among_attempted"] or 0) >= 0.10:
            verdict = "ATOMIC_EDGE_IN_WETH"
        else:
            verdict = "TICK_NOT_ATOMIC"
    elif prim_pack["n"] < 10:
        verdict = "NOT_EXECUTABLE_FOR_SPOT"
    summary = {
        "verdict": verdict,
        "highest_status": "SHADOW_CANDIDATE",
        "usd_capacity": "UNVERIFIED",
        "rpc_calls": rpc_calls,
        "primary_long_tick": prim_pack,
        "secondary_short_via": pack(secondary) if len(secondary) else {},
        "shadow": pack(shadow) if len(shadow) else {},
        "shadow_rows": shadow[["signal_id", "pair", "exec_class", "outcome", "ret", "tick_fwd_ret_4"]].to_dict("records")
        if len(shadow)
        else [],
        "note": "Quoter and tick are comparison only. Rejects are not -100%.",
    }
    (OUT / "FINAL_ATOMIC.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
