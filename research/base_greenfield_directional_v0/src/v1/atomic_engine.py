"""Production-size atomic buy/sell outcomes for V1 profit validation."""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import numpy as np
import pandas as pd
from eth_abi import encode
from web3 import Web3

ROOT = Path("/root/base_greenfield_directional_v0")
OUT = ROOT / "out"
OUT_V1 = ROOT / "out_v1"
CACHE = OUT_V1 / "cache_atomic"
CACHE.mkdir(parents=True, exist_ok=True)

WETH = Web3.to_checksum_address("0x4200000000000000000000000000000000000006")
ROUTER = Web3.to_checksum_address("0x2626664c2603336E57B271c5C0b26F421741e481")
QUOTER = Web3.to_checksum_address("0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a")
PROBE = Web3.to_checksum_address("0x000000000000000000000000000000000000dEaD")

# Production sizing: executor $50 @ 3000 and 0.02 ETH cap → $50 wins
AMT_IN_PROD = int(50 / 3000 * 1e18)  # 16666666666666666
ENTRY_DELAY_BLOCKS = 1
GAS_DEBIT_WETH = 0.0002  # round-trip research debit (documented)
SELL_GAS = 3_000_000
BUY_GAS = 3_000_000
H = 3600
TP, SL = 0.50, 0.35
LN1P = math.log(1.0001)
ECONOMIC_TAX = 0.90

_SWAP_SIG = Web3.keccak(text="exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))")[:4]
_QUOTE_SIG = Web3.keccak(text="quoteExactInputSingle((address,address,uint256,uint24,uint160))")[:4]
_BAL_SIG = Web3.keccak(text="balanceOf(address)")[:4]
_ALLOW_SIG = Web3.keccak(text="allowance(address,address)")[:4]


def get_rpc_url() -> str:
    return Path("/tmp/rpc_url.txt").read_text().strip()


class ProdAtomicSim:
    def __init__(self):
        self.url = get_rpc_url()
        self.w3 = Web3(Web3.HTTPProvider(self.url, request_kwargs={"timeout": 60}))
        self._slot_cache: dict = {}
        self.calls = 0

    def _call(self, method, params):
        self.calls += 1
        last = None
        for i in range(3):
            try:
                return self.w3.provider.make_request(method, params)
            except Exception as e:
                last = e
                time.sleep(0.25 * (i + 1))
        raise RuntimeError(last)

    def quote_buy(self, token: str, fee: int, amt_in: int, block: int) -> dict:
        token = Web3.to_checksum_address(token)
        key = CACHE / f"buy_{token.lower()}_{fee}_{amt_in}_{block}.json"
        if key.exists():
            return json.loads(key.read_text())
        params = encode(
            ["(address,address,uint256,uint24,uint160)"],
            [(WETH, token, int(amt_in), int(fee), 0)],
        )
        data = "0x" + (_QUOTE_SIG + params).hex()
        try:
            r = self._call("eth_call", [{"to": QUOTER, "data": data}, hex(int(block))])
        except Exception as e:
            out = {"status": "INFRASTRUCTURE_UNKNOWN", "token_out": None, "reason": f"transport:{type(e).__name__}"}
            key.write_text(json.dumps(out))
            return out
        if r.get("error"):
            out = {"status": "DETERMINISTIC_REVERT", "token_out": 0, "reason": str(r["error"])[:200]}
            key.write_text(json.dumps(out))
            return out
        res = r.get("result") or "0x"
        # QuoterV2 returns (amountOut, sqrtPriceX96After, initializedTicksCrossed, gasEstimate)
        try:
            # first word = amountOut
            token_out = int(res[2:66], 16) if len(res) >= 66 else 0
        except Exception:
            token_out = 0
        if token_out <= 0:
            out = {"status": "DETERMINISTIC_REVERT", "token_out": 0, "reason": "zero_out"}
        else:
            out = {"status": "BUY_OK", "token_out": token_out, "reason": ""}
        key.write_text(json.dumps(out))
        return out

    def detect_slots(self, token: str, block):
        token = Web3.to_checksum_address(token)
        if token in self._slot_cache:
            return self._slot_cache[token]
        bslot = blay = None
        bdata = "0x" + (_BAL_SIG + encode(["address"], [PROBE])).hex()
        for slot in range(20):
            for lay in ("sol", "vyper"):
                if lay == "sol":
                    key = "0x" + Web3.keccak(encode(["address", "uint256"], [PROBE, slot])).hex()
                else:
                    key = "0x" + Web3.keccak(encode(["uint256", "address"], [slot, PROBE])).hex()
                ov = {token: {"stateDiff": {key: "0x" + (0x7777).to_bytes(32, "big").hex()}}}
                r = self._call("eth_call", [{"to": token, "data": bdata}, hex(int(block)), ov])
                val = r.get("result")
                try:
                    if val and int(val, 16) == 0x7777:
                        bslot, blay = slot, lay
                        break
                except Exception:
                    pass
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
                    ov = {token: {"stateDiff": {key: "0x" + (0x8888).to_bytes(32, "big").hex()}}}
                    r = self._call("eth_call", [{"to": token, "data": adata}, hex(int(block)), ov])
                    val = r.get("result")
                    try:
                        if val and int(val, 16) == 0x8888:
                            aslot, alay = slot, lay
                            break
                    except Exception:
                        pass
                if aslot is not None:
                    break
        self._slot_cache[token] = (bslot, blay, aslot, alay)
        return self._slot_cache[token]

    def atomic_sell(self, token: str, fee: int, amount: int, block: int) -> dict:
        token = Web3.to_checksum_address(token)
        key = CACHE / f"sell_{token.lower()}_{fee}_{amount}_{block}.json"
        if key.exists():
            return json.loads(key.read_text())
        bslot, blay, aslot, alay = self.detect_slots(token, block)
        if bslot is None or aslot is None:
            out = {"simulation_status": "INFRASTRUCTURE_UNKNOWN", "atomic_weth_out": None, "revert_reason": "slot_undetectable"}
            key.write_text(json.dumps(out))
            return out
        probe = PROBE
        if blay == "sol":
            bkey = "0x" + Web3.keccak(encode(["address", "uint256"], [probe, bslot])).hex()
        else:
            bkey = "0x" + Web3.keccak(encode(["uint256", "address"], [bslot, probe])).hex()
        if alay == "sol":
            inner = Web3.keccak(encode(["address", "uint256"], [probe, aslot]))
        else:
            inner = Web3.keccak(encode(["uint256", "address"], [aslot, probe]))
        akey = "0x" + Web3.keccak(encode(["address", "bytes32"], [ROUTER, inner])).hex()
        grant = max(int(amount) * 2, int(amount) + 1)
        gh = "0x" + grant.to_bytes(32, "big").hex()
        ov = {
            token: {"stateDiff": {bkey: gh, akey: gh}},
            probe: {"balance": "0x" + (1000 * 10**18).to_bytes(32, "big").hex()},
        }
        params = encode(
            ["(address,address,uint24,address,uint256,uint256,uint160)"],
            [(token, WETH, int(fee), probe, int(amount), 0, 0)],
        )
        data = "0x" + (_SWAP_SIG + params).hex()
        tx = {"from": probe, "to": ROUTER, "data": data, "gas": hex(SELL_GAS)}
        try:
            r = self._call("eth_call", [tx, hex(int(block)), ov])
        except Exception as e:
            out = {"simulation_status": "INFRASTRUCTURE_UNKNOWN", "atomic_weth_out": None, "revert_reason": f"transport:{type(e).__name__}"}
            key.write_text(json.dumps(out))
            return out
        if r.get("error") is not None:
            out = {"simulation_status": "DETERMINISTIC_REVERT", "atomic_weth_out": 0, "revert_reason": str(r["error"])[:200]}
            key.write_text(json.dumps(out))
            return out
        res = r.get("result") or "0x"
        try:
            wout = int(res[:66], 16) if res and res != "0x" else 0
        except Exception:
            wout = 0
        if wout <= 0:
            out = {"simulation_status": "DETERMINISTIC_REVERT", "atomic_weth_out": 0, "revert_reason": "zero_out"}
        elif wout < AMT_IN_PROD * (1.0 - ECONOMIC_TAX):
            out = {"simulation_status": "ECONOMICALLY_UNSELLABLE", "atomic_weth_out": wout, "revert_reason": "tax>=90%"}
        else:
            out = {"simulation_status": "SELL_SUCCESS", "atomic_weth_out": wout, "revert_reason": ""}
        key.write_text(json.dumps(out))
        return out


def parse_i256(x) -> int:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return 0
    if isinstance(x, (int, np.integer)):
        return int(x)
    s = str(x).strip()
    if not s:
        return 0
    try:
        if s.startswith("0x") or s.startswith("0X"):
            v = int(s, 16)
            if v >= 2**255:
                v -= 2**256
            return v
        return int(s)
    except Exception:
        return 0


def load_swaps_index() -> dict[str, pd.DataFrame]:
    swaps = pd.read_parquet(OUT / "panel" / "early_swaps.parquet")
    swaps["tick_i"] = [parse_i256(x) for x in swaps["tick"]]
    grouped = {p: g.sort_values("ts") for p, g in swaps.groupby("pool", sort=False)}
    return grouped


def barrier_exit_block(g: pd.DataFrame, decision_time: int, entry_tick: float, entry_block: int) -> tuple[int, str, float, float, float]:
    """Return exit_block, event, mfe, mae, path_ret_at_exit using tick path (comparison timing only)."""
    ts = g["ts"].to_numpy(dtype=np.int64)
    ticks = g["tick_i"].to_numpy(dtype=np.float64)
    blocks = pd.to_numeric(g["block_number"], errors="coerce").fillna(entry_block).astype(int).to_numpy()
    i0 = int(np.searchsorted(ts, decision_time, side="right"))
    i1 = int(np.searchsorted(ts, decision_time + H, side="right"))
    if i1 <= i0:
        return int(entry_block + ENTRY_DELAY_BLOCKS), "TIMEOUT", 0.0, 0.0, 0.0
    ft = ticks[i0:i1]
    fb = blocks[i0:i1]
    rets = (ft - entry_tick) * LN1P
    mfe = float(np.nanmax(rets)) if len(rets) else 0.0
    mae = float(np.nanmin(rets)) if len(rets) else 0.0
    for i, r in enumerate(rets):
        if r >= TP:
            return int(fb[i]), "TP_FIRST", mfe, mae, float(r)
        if r <= -SL:
            return int(fb[i]), "SL_FIRST", mfe, mae, float(r)
    return int(fb[-1]), "TIMEOUT", mfe, mae, float(rets[-1])


def simulate_trade(sim: ProdAtomicSim, row, swaps_grouped: dict) -> dict:
    """Full production-size atomic round-trip for one opportunity row."""
    sig = row.signal_id if hasattr(row, "signal_id") else row["signal_id"]
    cache_path = CACHE / f"trade_{sig}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text())

    def _g(name, default=None):
        if hasattr(row, name):
            v = getattr(row, name)
            if v is None or (isinstance(v, float) and np.isnan(v)):
                return default
            return v
        if isinstance(row, dict):
            return row.get(name, default)
        return default

    pool = _g("pool")
    token = _g("alt")
    fee = int(_g("fee", 3000))
    decision_block = int(_g("decision_block", 0))
    decision_time = int(_g("decision_time", 0))
    entry_tick = float(_g("entry_tick", 0) or 0)
    entry_block = decision_block + ENTRY_DELAY_BLOCKS

    buy = sim.quote_buy(token, fee, AMT_IN_PROD, entry_block)
    if buy["status"] != "BUY_OK":
        out = {
            "signal_id": sig,
            "buy_status": buy["status"],
            "simulation_status": buy["status"],
            "realized_net_weth_return": None if buy["status"] == "INFRASTRUCTURE_UNKNOWN" else -1.0,
            "barrier_event": "FATAL_FIRST" if buy["status"] != "INFRASTRUCTURE_UNKNOWN" else "UNKNOWN",
            "mfe": None, "mae": None, "mae_before_mfe": None,
            "capital_hours": 0.0,
            "amt_in_wei": AMT_IN_PROD,
            "token_inventory": 0,
            "weth_out": 0,
            "exit_block": entry_block,
            "tick_path_event": None,
            "primary_pnl_source": "atomic",
        }
        cache_path.write_text(json.dumps(out))
        return out

    token_amt = int(buy["token_out"])
    g = swaps_grouped.get(pool)
    if g is None or entry_tick == 0:
        # fallback: sell at horizon block estimate = entry + H/2s ~ H/2 blocks (~2s/block → H)
        exit_block = entry_block + max(H // 2, 1)
        tick_event = "TIMEOUT"
        mfe = mae = 0.0
        path_ret = 0.0
    else:
        exit_block, tick_event, mfe, mae, path_ret = barrier_exit_block(g, decision_time, entry_tick, entry_block)
        exit_block = max(exit_block, entry_block)

    # retry schedule on INFRASTRUCTURE_UNKNOWN
    sell = None
    for add in (0, 5, 30, 150):
        sell = sim.atomic_sell(token, fee, token_amt, exit_block + add)
        if sell["simulation_status"] != "INFRASTRUCTURE_UNKNOWN":
            exit_block = exit_block + add
            break

    status = sell["simulation_status"]
    wout = sell.get("atomic_weth_out")
    if status == "SELL_SUCCESS" and wout is not None:
        net = (wout / AMT_IN_PROD) - 1.0 - GAS_DEBIT_WETH
        event = tick_event
        # if path said TP/SL but we sold successfully, keep path event; net is atomic
    elif status == "INFRASTRUCTURE_UNKNOWN":
        net = None
        event = "UNKNOWN"
    else:
        net = -1.0
        event = "FATAL_FIRST"

    hold_s = max(exit_block - entry_block, 1) * 2.0  # ~2s/block Base
    capital_hours = hold_s / 3600.0
    mae_before_mfe = int(mae < 0 and (abs(mae) > 0) and (mfe >= 0) and True)
    # approximate mae-before-mfe from signs
    out = {
        "signal_id": sig,
        "buy_status": "BUY_OK",
        "simulation_status": status,
        "realized_net_weth_return": net,
        "barrier_event": event,
        "mfe": mfe,
        "mae": mae,
        "mae_before_mfe": mae_before_mfe,
        "capital_hours": capital_hours,
        "amt_in_wei": AMT_IN_PROD,
        "token_inventory": token_amt,
        "weth_out": wout,
        "exit_block": exit_block,
        "entry_block": entry_block,
        "tick_path_event": tick_event,
        "tick_path_return": path_ret,
        "primary_pnl_source": "atomic",
        "gas_debit_weth": GAS_DEBIT_WETH,
        "revert_reason": sell.get("revert_reason", ""),
    }
    cache_path.write_text(json.dumps(out))
    return out


def calibrate(sim: ProdAtomicSim) -> tuple[pd.DataFrame, dict]:
    """Must include success + rug + thin-sellable + not success-only."""
    # Known GL4 cases from R2 calibration file (read-only)
    r2 = pd.read_csv("/root/keystone_r2_atomic/out/atomic_simulator_calibration.csv")
    rows = []
    for r in r2.itertuples(index=False):
        truth = "SUCCESS" if r.truth == "SUCCESS_SELL" else "UNSELLABLE"
        res = sim.atomic_sell(r.token, int(r.fee), int(r.token_amount), int(r.block))
        pred_sellable = res["simulation_status"] == "SELL_SUCCESS"
        rows.append({
            "case": r.case,
            "class": "KNOWN_SUCCESS" if truth == "SUCCESS" else "KNOWN_RUG",
            "truth": truth,
            "sim_status": res["simulation_status"],
            "sim_weth_out": res.get("atomic_weth_out"),
            "actual_weth_out": r.actual_weth_out,
            "false_sellable": bool(truth == "UNSELLABLE" and pred_sellable),
            "false_unsellable": bool(truth == "SUCCESS" and res["simulation_status"] in ("DETERMINISTIC_REVERT", "ECONOMICALLY_UNSELLABLE")),
            "unknown": res["simulation_status"] == "INFRASTRUCTURE_UNKNOWN",
            "weth_out_rel_error": (
                (res["atomic_weth_out"] - r.actual_weth_out) / r.actual_weth_out
                if truth == "SUCCESS" and res.get("atomic_weth_out") and r.actual_weth_out
                else None
            ),
        })

    # Thin-but-sellable: sample from greenfield atomic calibration sample that were SELL_SUCCESS
    gcal = pd.read_csv(OUT / "labels" / "atomic_calibration_sample.csv")
    labs = pd.read_parquet(OUT / "labels" / "labels_full.parquet")
    thin = labs.nsmallest(15, "entry_liq")[["signal_id", "alt", "fee", "decision_block", "entry_liq"]]
    thin = thin.merge(gcal[["signal_id", "simulation_status"]], on="signal_id", how="left")
    for r in thin.itertuples(index=False):
        # probe small sell
        res = sim.atomic_sell(str(r.alt), int(r.fee), 10**15, int(r.decision_block))
        rows.append({
            "case": f"THIN_{r.signal_id[:8]}",
            "class": "THIN_LIQUIDITY",
            "truth": "UNKNOWN_A_PRIORI",
            "sim_status": res["simulation_status"],
            "sim_weth_out": res.get("atomic_weth_out"),
            "actual_weth_out": None,
            "false_sellable": False,
            "false_unsellable": False,
            "unknown": res["simulation_status"] == "INFRASTRUCTURE_UNKNOWN",
            "weth_out_rel_error": None,
            "entry_liq": float(r.entry_liq),
        })

    # Temporary fail / recover proxy: not available as labeled set — document as uncovered
    cal = pd.DataFrame(rows)
    known = cal[cal["class"].isin(["KNOWN_SUCCESS", "KNOWN_RUG"])]
    n_succ = int((known["truth"] == "SUCCESS").sum())
    n_rug = int((known["truth"] == "UNSELLABLE").sum())
    summary = {
        "n_success_cases": n_succ,
        "n_rug_cases": n_rug,
        "n_thin_probes": int((cal["class"] == "THIN_LIQUIDITY").sum()),
        "false_sellable_rate": float(known["false_sellable"].mean()) if len(known) else None,
        "false_unsellable_rate": float(known["false_unsellable"].mean()) if len(known) else None,
        "unknown_rate": float(cal["unknown"].mean()) if len(cal) else None,
        "weth_out_error_median": float(known["weth_out_rel_error"].dropna().abs().median()) if known["weth_out_rel_error"].notna().any() else None,
        "fatal_detection_delay": "retry_blocks=[0,5,30,150] on INFRASTRUCTURE_UNKNOWN only",
        "recover_after_temp_fail_coverage": "NOT_IN_CALIBRATION_SET",
        "success_only": False,
        "calibration_pass": bool(
            n_succ >= 1 and n_rug >= 1
            and (known["false_sellable"].sum() == 0)
            and (known["false_unsellable"].sum() == 0)
        ),
        "rpc_calls": sim.calls,
    }
    return cal, summary
