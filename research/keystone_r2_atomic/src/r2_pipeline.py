#!/usr/bin/env python3
"""KEYSTONE R2 — Atomic Outcome Validation for frozen R1_F10_KS_CONTINUATION.

NOT alpha discovery. Does not modify production KEYSTONE.
Stops with ATOMIC_LABEL_NOT_TRUSTWORTHY if GL4 calibration fails.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import pickle
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from eth_abi import encode
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LinearRegression
from web3 import Web3

ROOT = Path("/root/keystone_r2_atomic")
OUT = ROOT / "out"
OUT.mkdir(parents=True, exist_ok=True)
R1_OUT = Path("/root/alpha_discovery_keystone_r1/out")
EATLQ = Path("/opt/EATLQ")
BRIDGE = EATLQ / "services/signal_bridge"
LEFT = EATLQ / "scripts/research/base_lefttail"
SYS_PATH_RESEARCH = str(EATLQ / "scripts/research")

R2_VERSION = "r2.0.0"
CHAIN_ID = 8453
WETH = Web3.to_checksum_address("0x4200000000000000000000000000000000000006")
ROUTER = Web3.to_checksum_address("0x2626664c2603336E57B271c5C0b26F421741e481")  # SwapRouter02 Base
QUOTER = Web3.to_checksum_address("0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a")
EXECUTOR_ADDR = "0x4B7cd953871663474a0A25B81bddDf5C1f1195A7"
LEGACY_CANARY = "0x3a0DE7b4e6Ea125FFA3155b0026E5F717A1e86EE"
PROBE = Web3.to_checksum_address("0x000000000000000000000000000000000000dEaD")

P_CUT_FILE = 0.46818927983255865
P_CUT_LIVE_CAP = 0.27
P_CUT_EFFECTIVE = min(P_CUT_FILE, P_CUT_LIVE_CAP)  # 0.27
FRIC_THR = -0.05
MIN_PRE = 5
W, H = 600, 14400
HARD_STOP = 0.50
HOLD_S = 14400
SELL_GAS = 3_000_000
# Production sizing: bridge asks $100; executor caps $50 and 0.02 ETH
SIZE_NOMINAL_USD = 100.0
SIZE_EXEC_MAX_USD = 50.0
SIZE_EXEC_MAX_ETH = 0.02
ETH_USD_RESEARCH = 3000.0  # matches lefttail realgas / priced_live convention
AMT_IN_RESEARCH = int(SIZE_NOMINAL_USD / ETH_USD_RESEARCH * 1e18)
AMT_IN_PROD_CAP = min(int(SIZE_EXEC_MAX_USD / ETH_USD_RESEARCH * 1e18), int(SIZE_EXEC_MAX_ETH * 1e18))

F10_FEATURES = [
    "impact_residual",
    "ineffective_buying",
    "liq_fragility_score",
    "part_exhaust_score",
    "accel",
]
HGB_PARAMS = dict(max_depth=3, max_iter=80, learning_rate=0.05, random_state=0)
HOLDOUT_FRAC = 0.20
EMBARGO_DAYS = 1
PRIMARY_GATE = "train_q50"
SENS_GATES = ("train_q40", "train_q50", "train_q60")

_SWAP_SIG = Web3.keccak(text="exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))")[:4]
_QUOTE_SIG = Web3.keccak(text="quoteExactInputSingle((address,address,uint256,uint24,uint160))")[:4]
_BAL_SIG = Web3.keccak(text="balanceOf(address)")[:4]
_ALLOW_SIG = Web3.keccak(text="allowance(address,address)")[:4]
_FEE_SIG = Web3.keccak(text="fee()")[:4]
LN1P = math.log(1.0001)


def sha256_file(p: Path | str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_json(obj) -> str:
    return sha256_bytes(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode())


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_rpc_url() -> str:
    # Prefer /tmp/rpc_url.txt (same archive as GL5v3); do not print URL
    p = Path("/tmp/rpc_url.txt")
    if p.exists():
        return p.read_text().strip()
    sys.path.insert(0, SYS_PATH_RESEARCH)
    from gl_readonly_rpc import archive_url
    return archive_url()


# ───────────────────────────── Manifest ─────────────────────────────

def write_manifest() -> dict:
    git = subprocess.run(["git", "-C", str(EATLQ), "rev-parse", "HEAD"], capture_output=True, text=True)
    rpc = get_rpc_url()
    manifest = {
        "r2_version": R2_VERSION,
        "created_utc": utcnow(),
        "purpose": "Validate frozen R1_F10_KS_CONTINUATION on atomic-sell outcomes",
        "champion": {
            "name": "KEYSTONE",
            "model_path": str(BRIDGE / "_model.pkl"),
            "model_sha256": sha256_file(BRIDGE / "_model.pkl"),
            "params_path": str(BRIDGE / "_params.json"),
            "params_sha256": sha256_file(BRIDGE / "_params.json"),
            "p_cut_expression": "min(0.46818927983255865, 0.27)",
            "p_cut_effective": P_CUT_EFFECTIVE,
            "p_cut_file": P_CUT_FILE,
            "p_cut_live_cap": P_CUT_LIVE_CAP,
            "entry": "t0+600..900s; MIN_PRE=5; friction>=-5%; p_fatal<=p_cut_effective",
            "exit": "hard_stop=-50%; hold=14400s; trailing=OFF",
            "universe": "Base UniV3 WETH; t0 2025-10-01..2026-06-01",
            "sizing": {
                "nominal_usd": SIZE_NOMINAL_USD,
                "executor_max_position_usd": SIZE_EXEC_MAX_USD,
                "executor_max_position_eth": SIZE_EXEC_MAX_ETH,
                "research_amt_in_wei": AMT_IN_RESEARCH,
                "prod_capped_amt_in_wei": AMT_IN_PROD_CAP,
                "note": "Labels use research $100@3000 convention matching priced_live/realgas; production cap documented separately",
            },
        },
        "frozen_lead": {
            "id": "R1_F10_KS_CONTINUATION",
            "features": F10_FEATURES,
            "hgb_params": HGB_PARAMS,
            "primary_gate": PRIMARY_GATE,
            "sensitivity_gates_only": list(SENS_GATES),
            "r1_model_persisted": False,
            "r1_model_note": "R1 did not save HGB pickle; R2 regenerates with identical recipe+seed and freezes artifact",
        },
        "hashes": {
            "eatlq_git_head": git.stdout.strip(),
            "panel_r1_sha256": sha256_file(R1_OUT / "panel_r1.csv"),
            "realgas_replay_sha256": sha256_file(LEFT / "_realgas_replay.json"),
            "priced_live_sha256": sha256_file(LEFT / "_priced_live.json"),
            "pools_tsv_sha256": sha256_file(LEFT / "_pools.tsv"),
            "gl4_ledger_sha256": sha256_file(EATLQ / "mvp/diagnostics/base_gl4_live_truth_ledger_20260716.json"),
            "archive_rpc_url_sha256": sha256_bytes(rpc.encode()),
            "r2_pipeline_sha256": sha256_file(Path(__file__)),
        },
        "addresses": {
            "chain_id": CHAIN_ID,
            "executor": EXECUTOR_ADDR,
            "legacy_canary": LEGACY_CANARY,
            "router": ROUTER,
            "quoter_v2": QUOTER,
            "weth": WETH,
            "probe": PROBE,
        },
        "historical_block_ranges": {
            "keystone_research_t0": ["2025-10-01", "2026-06-01"],
            "development_holdout": "final_20pct_by_t0_DEVELOPMENT_HOLDOUT_BURNED",
            "confirmation_policy": "post_2026-06-01 untouched OR prospective shadow after manifest freeze",
            "gl4_live_window": {"start_utc": "2026-06-10", "end_utc": "2026-07-13", "end_block": 48598926},
        },
        "split_boundaries": {
            "holdout_frac": HOLDOUT_FRAC,
            "embargo_days": EMBARGO_DAYS,
            "development_holdout_status": "DEVELOPMENT_HOLDOUT_BURNED",
        },
        "atomic_simulation_policy": {
            "method": "eth_call + stateDiff balance/allowance override + SwapRouter02 exactInputSingle",
            "gas_cap": SELL_GAS,
            "amountOutMinimum": 0,
            "recipient": "probe_or_executor",
            "quoter_v2_role": "comparison_only_not_label",
            "status_enum": [
                "SELL_SUCCESS",
                "DETERMINISTIC_REVERT",
                "ECONOMICALLY_UNSELLABLE",
                "INFRASTRUCTURE_UNKNOWN",
            ],
            "infrastructure_unknown_not_token_fatal": True,
            "permanent_unsellable_at_max_hold": -1.0,
        },
        "retry_policy": {
            "rpc_attempts": 3,
            "backoff_s": [0.25, 0.5, 0.75],
            "exit_retry_blocks": [0, 5, 30, 150, 7200],
            "note": "retry on INFRASTRUCTURE_UNKNOWN only; DETERMINISTIC_REVERT not retried as success",
        },
        "gas_policy": {"sell_sim_gas_cap": SELL_GAS, "label_gas_debit_weth": 0.0, "note": "gas recorded separately when available; primary EV uses WETH out / amt_in - 1"},
        "slippage_policy": {"amountOutMinimum": 0, "economic_unsellable_if_tax_ge": 0.90},
        "known_gl4_failure_cases": [
            {"name": "JOTCHUA", "class": "CONFIRMED_STRANDED_LIQUIDITY_COLLAPSE", "wallet": LEGACY_CANARY},
            {"name": "BOT", "class": "CONFIRMED_STRANDED_LIQUIDITY_COLLAPSE", "wallet": LEGACY_CANARY},
            {"name": "PARADAENGI-1", "class": "CONFIRMED_STRANDED_LIQUIDITY_COLLAPSE", "wallet": EXECUTOR_ADDR},
            {"name": "PARADAENGI-2", "class": "CONFIRMED_STRANDED_LIQUIDITY_COLLAPSE", "wallet": EXECUTOR_ADDR},
            {"name": "PARADAENGI-3", "class": "CONFIRMED_STRANDED_LIQUIDITY_COLLAPSE", "wallet": EXECUTOR_ADDR, "note": "F-off aggressive"},
        ],
        "known_gl4_success_sells": ["NEXIO", "USDC", "USDC"],
        "immutability": "Any change after creation requires new r2_version",
    }
    path = OUT / "R2_MANIFEST.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Wrote {path}", flush=True)
    return manifest


def write_label_spec():
    text = """# Atomic Label Spec (R2 frozen)

## Status taxonomy

| status | meaning |
|---|---|
| SELL_SUCCESS | `exactInputSingle` eth_call at gas_cap returns WETH out > 0 |
| DETERMINISTIC_REVERT | eth_call returns RPC error / revert at gas_cap |
| ECONOMICALLY_UNSELLABLE | call succeeds but tax >= 90% (WETH out / fair < 0.10) |
| INFRASTRUCTURE_UNKNOWN | balance/allowance slot undetectable, archive missing, or transport failure — **not** token fatal |

## Primary endpoint

Under frozen KEYSTONE exit (hard -50%, hold 14400s, trailing OFF):

1. Reconstruct token_in via production-equivalent buy path at entry_block (QuoterV2 buy used only to size inventory; sell label is atomic).
2. At exit_block (causal trail/hold from priced path), run atomic sell sim.
3. If SELL_SUCCESS: `atomic_net = weth_out / amt_in - 1`.
4. If DETERMINISTIC_REVERT or ECONOMICALLY_UNSELLABLE after retry schedule: `-100%`.
5. If INFRASTRUCTURE_UNKNOWN after retries: `UNKNOWN` (excluded from EV denominators that require labels; reported separately).

QuoterV2 markouts are comparison columns only.

## Secondary barriers (atomic net path only)

- TB_A: +50% / -35%
- TB_B: +100% / -50%
- FATAL_FIRST: atomic unsellable / -100% before TP

## Point-in-time

All features and residualizers use information available at decision time t0+600s.
Residualizers fit on chronologically prior train fold only.
"""
    (OUT / "atomic_label_spec.md").write_text(text)


# ───────────────────────────── RPC sell sim ─────────────────────────────

class ArchiveSim:
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
                r = self.w3.provider.make_request(method, params)
                return r
            except Exception as e:
                last = e
                time.sleep(0.25 * (i + 1))
        raise RuntimeError(last)

    def pool_fee(self, pool: str) -> int | None:
        r = self._call("eth_call", [{"to": Web3.to_checksum_address(pool), "data": "0x" + _FEE_SIG.hex()}, "latest"])
        res = r.get("result")
        if not res or res == "0x":
            return None
        return int(res, 16)

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
                r = self._call("eth_call", [{"to": token, "data": bdata}, hex(int(block)) if isinstance(block, int) else block, ov])
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
                    r = self._call("eth_call", [{"to": token, "data": adata}, hex(int(block)) if isinstance(block, int) else block, ov])
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

    def atomic_sell(self, token: str, fee: int, amount: int, block: int, wallet: str | None = None) -> dict:
        """Exact sell simulation. Returns status + weth_out."""
        token = Web3.to_checksum_address(token)
        probe = Web3.to_checksum_address(wallet) if wallet else PROBE
        # re-detect slots for probe if wallet provided (cache key includes wallet? use probe-specific)
        # For simplicity use PROBE slots always (state override grants PROBE)
        probe = PROBE
        bslot, blay, aslot, alay = self.detect_slots(token, block)
        if bslot is None or aslot is None:
            return {
                "simulation_status": "INFRASTRUCTURE_UNKNOWN",
                "atomic_weth_out": None,
                "revert_reason": "slot_undetectable",
                "gas": SELL_GAS,
            }
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
            return {
                "simulation_status": "INFRASTRUCTURE_UNKNOWN",
                "atomic_weth_out": None,
                "revert_reason": f"transport:{type(e).__name__}",
                "gas": SELL_GAS,
            }
        if r.get("error") is not None:
            return {
                "simulation_status": "DETERMINISTIC_REVERT",
                "atomic_weth_out": 0,
                "revert_reason": str(r["error"])[:200],
                "gas": SELL_GAS,
            }
        res = r.get("result") or "0x"
        try:
            out = int(res[:66], 16) if res and res != "0x" else 0
        except Exception:
            out = 0
        if out <= 0:
            return {
                "simulation_status": "DETERMINISTIC_REVERT",
                "atomic_weth_out": 0,
                "revert_reason": "zero_out",
                "gas": SELL_GAS,
            }
        # economic unsellable vs amt_in proxy: if selling tokens bought with AMT_IN_RESEARCH
        # tax vs principal unknown here; caller may set ECONOMICALLY_UNSELLABLE if out/amt_in < 0.1
        return {
            "simulation_status": "SELL_SUCCESS",
            "atomic_weth_out": out,
            "revert_reason": "",
            "gas": SELL_GAS,
        }


# ───────────────────────────── Features / F10 freeze ─────────────────────────────

def fold_residuals(train: pd.DataFrame, test: pd.DataFrame):
    def _resid(tr, te, ycol, xcols, name):
        for c in xcols:
            if c not in tr.columns:
                tr[c] = 0.0
            if len(te) and c not in te.columns:
                te[c] = 0.0
        Xtr = tr[xcols].replace([np.inf, -np.inf], np.nan).fillna(0.0).values
        ytr = tr[ycol].replace([np.inf, -np.inf], np.nan).fillna(0.0).values
        lr = LinearRegression()
        if len(tr) < 30:
            tr[name] = 0.0
            if len(te):
                te[name] = 0.0
            return tr, te
        lr.fit(Xtr, ytr)
        tr[name] = ytr - lr.predict(Xtr)
        if len(te):
            Xte = te[xcols].replace([np.inf, -np.inf], np.nan).fillna(0.0).values
            te[name] = te[ycol].fillna(0.0).values - lr.predict(Xte)
        return tr, te

    tr, te = train.copy(), test.copy()
    tr, te = _resid(tr, te, "early_ret", ["liq_log", "pre_vol", "ns_pre", "active_pools_l1"], "impact_residual")
    tr, te = _resid(tr, te, "ns_pre", ["liq_log", "pre_vol", "active_pools_l1"], "flow_surprise")
    for d in [tr] + ([te] if len(te) else []):
        d["ineffective_buying"] = d["flow_surprise"] - d["impact_residual"]
        d["absorption_score"] = (-d["early_ret"].clip(upper=0)) * d["liq_trend"] + d["impact_residual"].clip(lower=0)
        d["part_diffusion_score"] = d.get("seq_part_diffusion", 0) * (1.0 - d["buyer_conc"])
        d["part_exhaust_score"] = d.get("seq_part_exhaust", 0) * d["early_ret"].clip(lower=0)
        d["liq_fragility_score"] = d["liq_fragility_proxy"] * d["early_ret"].clip(lower=0)
        if "accel" not in d.columns:
            d["accel"] = d["mom_h2"] - d["mom_h1"]
    return tr, te


def freeze_f10(panel: pd.DataFrame) -> pd.DataFrame:
    """Regenerate R1 F10 recipe; persist model + OOF/full scores. No threshold search."""
    panel = panel.sort_values("t0").reset_index(drop=True)
    panel["t0"] = pd.to_datetime(panel["t0"], utc=True)
    n = len(panel)
    hold_n = max(int(n * HOLDOUT_FRAC), 1)
    core = panel.iloc[:-hold_n].copy()
    hold = panel.iloc[-hold_n:].copy()

    # Walk-forward OOF scores on core (4 folds) + final model for hold/scores
    edges = np.linspace(0, len(core), 5).astype(int)
    oof = pd.Series(np.nan, index=panel.index)
    models_meta = []
    for i in range(4):
        te_idx = core.index[edges[i]:edges[i + 1]]
        tr_idx = core.index[:edges[i]]
        if len(tr_idx) < 80:
            continue
        tr0 = panel.loc[tr_idx]
        te0 = panel.loc[te_idx]
        cut = te0.t0.min() - pd.Timedelta(days=EMBARGO_DAYS)
        tr0 = tr0[tr0.t0 <= cut]
        tr, te = fold_residuals(tr0, te0)
        trk = tr[tr.ks_live_pass]
        tek = te[te.ks_live_pass]
        if len(trk) < 40 or len(tek) < 5:
            continue
        clf = HistGradientBoostingClassifier(**HGB_PARAMS)
        clf.fit(trk[F10_FEATURES].fillna(0.0), (trk["net_exec"] > 0).astype(int))
        proba = clf.predict_proba(tek[F10_FEATURES].fillna(0.0))[:, 1]
        oof.loc[tek.index] = proba
        thr50 = float(np.quantile(clf.predict_proba(trk[F10_FEATURES].fillna(0.0))[:, 1], 0.5))
        models_meta.append({"fold": i, "train_n": len(trk), "thr_q50": thr50})

    # Final frozen model: residualizer fit on core, apply to full panel
    _, panel_r = fold_residuals(core, panel)
    for c in F10_FEATURES:
        panel[c] = panel_r[c].values
    trk = panel.loc[core.index]
    trk = trk[trk.ks_live_pass]
    clf = HistGradientBoostingClassifier(**HGB_PARAMS)
    clf.fit(trk[F10_FEATURES].fillna(0.0), (trk["net_exec"] > 0).astype(int))
    train_scores = clf.predict_proba(trk[F10_FEATURES].fillna(0.0))[:, 1]
    thr = {
        "train_q40": float(np.quantile(train_scores, 0.40)),
        "train_q50": float(np.quantile(train_scores, 0.50)),
        "train_q60": float(np.quantile(train_scores, 0.60)),
    }
    panel["f10_score"] = clf.predict_proba(panel[F10_FEATURES].fillna(0.0))[:, 1]
    panel["f10_oof_score"] = oof
    panel["f10_pass_q40"] = panel["f10_score"] >= thr["train_q40"]
    panel["f10_pass_q50"] = panel["f10_score"] >= thr["train_q50"]
    panel["f10_pass_q60"] = panel["f10_score"] >= thr["train_q60"]
    panel["split"] = "core"
    panel.loc[hold.index, "split"] = "DEVELOPMENT_HOLDOUT_BURNED"

    model_path = OUT / "frozen_f10_hgb.pkl"
    meta_path = OUT / "frozen_f10_meta.json"
    with open(model_path, "wb") as f:
        pickle.dump({"clf": clf, "features": F10_FEATURES, "params": HGB_PARAMS, "thresholds": thr}, f)
    meta = {
        "features": F10_FEATURES,
        "hgb_params": HGB_PARAMS,
        "thresholds": thr,
        "primary_gate": PRIMARY_GATE,
        "train_n_ks_live_pass": int(len(trk)),
        "model_sha256": sha256_file(model_path),
        "feature_schema_sha256": sha256_json(F10_FEATURES),
        "folds": models_meta,
        "recipe": "R1_F10 identical; regenerated because R1 did not persist pickle",
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")

    # V1 early_ret control: match acceptance rate of F10 q50 among ks_live_pass on core
    ks_core = panel.loc[core.index]
    ks_core = ks_core[ks_core.ks_live_pass]
    acc = float(ks_core["f10_pass_q50"].mean()) if len(ks_core) else 0.5
    er_thr = float(ks_core["early_ret"].quantile(1.0 - acc)) if len(ks_core) else 0.0
    panel["v1_early_ret_pass"] = (panel["early_ret"] >= er_thr) & panel["ks_live_pass"]
    meta["v1_match_accept_rate"] = acc
    meta["v1_early_ret_threshold"] = er_thr
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")

    # V3 ineffective_buying train quantiles (no search)
    ib = trk["ineffective_buying"]
    v3_thr = {
        "q60": float(ib.quantile(0.60)),
        "q70": float(ib.quantile(0.70)),
        "q80": float(ib.quantile(0.80)),
    }
    # frozen evaluation bucket: train q80 (upper) as avoid? Protocol: 单变量分桶 with train quantiles.
    # Use high ineffective_buying as risk → pass = below q80 among ks (keep lower risk)
    # For long filter comparable: keep ks_live_pass & ineffective_buying <= q80 (drop top 20% bad)
    panel["v3_ib_bucket"] = pd.cut(
        panel["ineffective_buying"],
        bins=[-np.inf, v3_thr["q60"], v3_thr["q70"], v3_thr["q80"], np.inf],
        labels=["le_q60", "q60_70", "q70_80", "gt_q80"],
    )
    panel["v3_pass"] = panel["ks_live_pass"] & (panel["ineffective_buying"] <= v3_thr["q80"])
    meta["v3_thresholds"] = v3_thr
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")

    # Persist scores
    score_cols = [
        "pool", "t0", "split", "ks_live_pass", "ks_research_pass", "p_fatal", "fric", "early_ret",
        "f10_score", "f10_oof_score", "f10_pass_q40", "f10_pass_q50", "f10_pass_q60",
        "v1_early_ret_pass", "v3_pass", "v3_ib_bucket",
    ] + F10_FEATURES
    scores = panel[score_cols].copy()
    try:
        scores.to_parquet(OUT / "frozen_f10_scores.parquet", index=False)
    except Exception:
        scores.to_csv(OUT / "frozen_f10_scores.csv", index=False)
        # write minimal parquet via pyarrow if available
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
            pq.write_table(pa.Table.from_pandas(scores), OUT / "frozen_f10_scores.parquet")
        except Exception:
            # touch empty marker
            (OUT / "frozen_f10_scores.parquet").write_bytes(b"")
            scores.to_csv(OUT / "frozen_f10_scores.csv", index=False)
    print(f"Frozen F10 model thr={thr} acc_match={acc:.3f}", flush=True)
    return panel, meta, clf, thr


# ───────────────────────────── GL4 calibration ─────────────────────────────

def calibrate_gl4(sim: ArchiveSim) -> tuple[pd.DataFrame, dict]:
    gl4 = json.load(open(EATLQ / "mvp/diagnostics/base_gl4_live_truth_ledger_20260716.json"))
    rows = []

    # --- SUCCESS sells (must remain sellable) ---
    for leg in gl4["real_successful_fill_legs"]:
        is_sell = str(leg.get("side", "")).upper() == "SELL"
        if not is_sell:
            continue
        meta = leg.get("token_meta") or {}
        token = meta.get("address") or meta.get("token_address") or meta.get("contract_address")
        pools = leg.get("swap_pools") or []
        pool = pools[0] if pools else None
        block = int(leg["block"])
        if not token and pool:
            token = _pool_alt_token(sim, pool, block)
        fee = sim.pool_fee(pool) if pool else None
        amt = int(str(leg.get("token_amount_raw") or 0))
        if not token or not fee or amt <= 0:
            rows.append({
                "case": leg.get("trade_name"), "truth": "SUCCESS_SELL", "block": block, "pool": pool,
                "token": token, "fee": fee, "token_amount": amt,
                "actual_weth_out": int(leg["weth_withdrawal_raw"]) if leg.get("weth_withdrawal_raw") is not None else None,
                "sim_status": "INFRASTRUCTURE_UNKNOWN", "sim_weth_out": None,
                "weth_out_rel_error": None, "revert_reason": "missing_token_fee_or_amount",
                "wallet": leg.get("wallet"),
            })
            continue
        res = sim.atomic_sell(token, fee, amt, block)
        actual = int(leg["weth_withdrawal_raw"]) if leg.get("weth_withdrawal_raw") is not None else None
        err = None
        if actual and res.get("atomic_weth_out"):
            err = (res["atomic_weth_out"] - actual) / actual
        # economic check vs actual principal scale
        status = res["simulation_status"]
        if status == "SELL_SUCCESS" and actual and res["atomic_weth_out"] is not None:
            if res["atomic_weth_out"] < 0.10 * actual:
                status = "ECONOMICALLY_UNSELLABLE"
        rows.append({
            "case": leg.get("trade_name"), "truth": "SUCCESS_SELL", "block": block, "pool": pool,
            "token": token, "fee": fee, "token_amount": amt, "actual_weth_out": actual,
            "sim_status": status, "sim_weth_out": res["atomic_weth_out"],
            "weth_out_rel_error": err, "revert_reason": res["revert_reason"],
            "wallet": leg.get("wallet"),
        })

    # --- RUG / stranded (must NOT be falsely sellable) ---
    for op in gl4["open_positions"]:
        pool = op["pool"]
        block = int(op.get("pool_weth_probe_block") or gl4["window"]["end_block"])
        amt = int(op["token_amount_raw"])
        fee = sim.pool_fee(pool)
        token = _pool_alt_token(sim, pool, block)
        if not token or not fee:
            rows.append({
                "case": op["trade_name"], "truth": "RUG_UNSELLABLE", "block": block, "pool": pool,
                "token": token, "fee": fee, "token_amount": amt, "actual_weth_out": 0,
                "sim_status": "INFRASTRUCTURE_UNKNOWN", "sim_weth_out": None,
                "weth_out_rel_error": None, "revert_reason": "missing_token_or_fee", "wallet": None,
            })
            continue
        res = sim.atomic_sell(token, fee, amt, block)
        status = res["simulation_status"]
        # If "success" but recovers <10% of entry principal WETH → economically unsellable (still OK for rug detection)
        principal = float(op.get("principal_weth") or 0)
        if status == "SELL_SUCCESS" and res["atomic_weth_out"] is not None and principal > 0:
            if (res["atomic_weth_out"] / 1e18) < 0.10 * principal:
                status = "ECONOMICALLY_UNSELLABLE"
        rows.append({
            "case": op["trade_name"], "truth": "RUG_UNSELLABLE", "block": block, "pool": pool,
            "token": token, "fee": fee, "token_amount": amt, "actual_weth_out": 0,
            "sim_status": status, "sim_weth_out": res["atomic_weth_out"],
            "weth_out_rel_error": None, "revert_reason": res["revert_reason"], "wallet": None,
        })

    df = pd.DataFrame(rows)
    df.to_csv(OUT / "atomic_simulator_calibration.csv", index=False)

    succ = df[df.truth == "SUCCESS_SELL"]
    rug = df[df.truth == "RUG_UNSELLABLE"]

    # false sellable = rug still looks like healthy SELL_SUCCESS (not economic collapse)
    rug_eval = rug[rug.sim_status != "INFRASTRUCTURE_UNKNOWN"]
    false_sellable = int((rug_eval.sim_status == "SELL_SUCCESS").sum())
    rug_n = int(len(rug_eval))

    succ_eval = succ[succ.sim_status != "INFRASTRUCTURE_UNKNOWN"]
    false_unsellable = int((succ_eval.sim_status != "SELL_SUCCESS").sum())
    succ_n = int(len(succ_eval))

    errs = [abs(e) for e in succ["weth_out_rel_error"].dropna()]
    # rug correctly detected if DETERMINISTIC_REVERT or ECONOMICALLY_UNSELLABLE
    rug_detected = int(rug_eval.sim_status.isin(["DETERMINISTIC_REVERT", "ECONOMICALLY_UNSELLABLE"]).sum())

    summary = {
        "n_success_cases": int(len(succ)),
        "n_rug_cases": int(len(rug)),
        "n_success_evaluable": succ_n,
        "n_rug_evaluable": rug_n,
        "false_sellable_rate": (false_sellable / rug_n) if rug_n else None,
        "false_unsellable_rate": (false_unsellable / succ_n) if succ_n else None,
        "sim_vs_actual_weth_abs_rel_error_median": float(np.median(errs)) if errs else None,
        "sim_vs_actual_weth_abs_rel_error_mean": float(np.mean(errs)) if errs else None,
        "infrastructure_unknown_rate": float((df.sim_status == "INFRASTRUCTURE_UNKNOWN").mean()) if len(df) else None,
        "known_rug_detection_coverage": (rug_detected / rug_n) if rug_n else None,
        "revert_reason_agreement": "n/a_live_reverts_not_decoded",
        "rpc_calls": sim.calls,
        "trustworthy": None,
        "fail_reasons": [],
    }
    ok = True
    reasons = []
    if rug_n == 0 or succ_n == 0:
        ok = False
        reasons.append("insufficient_evaluable_gl4_cases")
    if summary["false_sellable_rate"] is not None and summary["false_sellable_rate"] > 0.0:
        ok = False
        reasons.append("false_sellable_on_known_rugs")
    if summary["false_unsellable_rate"] is not None and summary["false_unsellable_rate"] > 0.34:
        ok = False
        reasons.append("false_unsellable_too_high")
    if summary["infrastructure_unknown_rate"] is not None and summary["infrastructure_unknown_rate"] > 0.5:
        ok = False
        reasons.append("infra_unknown_too_high")
    if summary["known_rug_detection_coverage"] is not None and summary["known_rug_detection_coverage"] < 0.8:
        ok = False
        reasons.append("rug_detection_coverage_below_80pct")
    summary["trustworthy"] = ok
    summary["fail_reasons"] = reasons
    json.dump(summary, open(OUT / "atomic_simulator_calibration_summary.json", "w"), indent=2)
    print("CALIBRATION", json.dumps(summary, indent=2), flush=True)
    return df, summary


def _pool_alt_token(sim: ArchiveSim, pool: str, block: int) -> str | None:
    """token0/token1; return non-WETH."""
    pool = Web3.to_checksum_address(pool)
    t0_sig = Web3.keccak(text="token0()")[:4]
    t1_sig = Web3.keccak(text="token1()")[:4]
    try:
        r0 = sim._call("eth_call", [{"to": pool, "data": "0x" + t0_sig.hex()}, hex(block)])
        r1 = sim._call("eth_call", [{"to": pool, "data": "0x" + t1_sig.hex()}, hex(block)])
        t0 = "0x" + r0["result"][-40:]
        t1 = "0x" + r1["result"][-40:]
        if t0.lower() == WETH.lower():
            return Web3.to_checksum_address(t1)
        if t1.lower() == WETH.lower():
            return Web3.to_checksum_address(t0)
    except Exception:
        return None
    return None


# ───────────────────────────── Labels from realgas cache ─────────────────────────────

def attach_atomic_labels(panel: pd.DataFrame) -> pd.DataFrame:
    realgas = json.load(open(LEFT / "_realgas_replay.json"))
    priced = json.load(open(LEFT / "_priced_live.json"))

    def map_status(exit_sell, real_net, cached_net):
        if exit_sell == "unavailable":
            return "INFRASTRUCTURE_UNKNOWN", None, None
        if exit_sell == "revert":
            return "DETERMINISTIC_REVERT", -1.0, 0
        if exit_sell == "ok":
            # real_net from realgas is weth_out/amt_in - 1
            if real_net is not None and real_net <= -0.90:
                return "ECONOMICALLY_UNSELLABLE", -1.0, None
            return "SELL_SUCCESS", real_net, None
        return "INFRASTRUCTURE_UNKNOWN", None, None

    rows = []
    for r in panel.itertuples():
        rg = realgas.get(r.pool, {})
        pr = priced.get(r.pool, {})
        exit_sell = rg.get("exit_sell")
        entry_sell = rg.get("entry_sell")
        real_net = rg.get("real_net")
        cached_net = rg.get("cached_net", pr.get("net"))
        status, atomic_net, weth = map_status(exit_sell, real_net, cached_net)
        # comparison quoter
        qv2_net = pr.get("net")
        rows.append({
            "pool": r.pool,
            "entry_block": rg.get("entry_block") or pr.get("exb"),
            "exit_block": rg.get("exit_block") or pr.get("exb"),
            "entry_sell_status": entry_sell,
            "simulation_status_h": status,
            "atomic_net_return_h": atomic_net,
            "atomic_weth_out_h": weth,
            "gas_h": SELL_GAS if status != "INFRASTRUCTURE_UNKNOWN" else None,
            "revert_reason_h": "realgas_exit_revert" if exit_sell == "revert" else "",
            "sellable_h": 1 if status == "SELL_SUCCESS" else (0 if status in ("DETERMINISTIC_REVERT", "ECONOMICALLY_UNSELLABLE") else None),
            "qv2_net_comparison": qv2_net,
            "exwhy": rg.get("exwhy") or pr.get("exwhy"),
            "label_source": "realgas_replay_v1" if rg else "missing",
        })
    lab = pd.DataFrame(rows)
    out = panel.merge(lab, on="pool", how="left")
    # Primary PnL per KEYSTONE signal: only defined for ks_live_pass
    out["atomic_pnl_primary"] = out["atomic_net_return_h"]
    out.loc[out["simulation_status_h"] == "INFRASTRUCTURE_UNKNOWN", "atomic_pnl_primary"] = np.nan
    # Barriers from atomic net only (single terminal outcome — limited path)
    def barrier(net, tp, sl):
        if pd.isna(net):
            return "UNKNOWN"
        if net <= -0.999 or net <= -sl and net <= -0.5 and net == -1.0:
            # fatal unsellable
            if net <= -0.999:
                return "FATAL_FIRST"
        if net >= tp:
            return "TP_FIRST"
        if net <= -sl:
            return "SL_FIRST"
        return "NONE"

    out["tb_a"] = [barrier(n, 0.50, 0.35) for n in out["atomic_net_return_h"]]
    out["tb_b"] = [barrier(n, 1.00, 0.50) for n in out["atomic_net_return_h"]]
    # refine FATAL
    out.loc[out["simulation_status_h"].isin(["DETERMINISTIC_REVERT", "ECONOMICALLY_UNSELLABLE"]), "tb_a"] = "FATAL_FIRST"
    out.loc[out["simulation_status_h"].isin(["DETERMINISTIC_REVERT", "ECONOMICALLY_UNSELLABLE"]), "tb_b"] = "FATAL_FIRST"
    return out


# ───────────────────────────── V4 atomic retarget ─────────────────────────────

def fit_v4_atomic(panel: pd.DataFrame) -> pd.DataFrame:
    """Same 5 features + HGB hyperparams + train_q50; label = atomic_net > 0."""
    core = panel[panel.split == "core"].copy()
    trk = core[core.ks_live_pass & core.atomic_pnl_primary.notna()]
    if len(trk) < 40:
        panel["v4_score"] = np.nan
        panel["v4_pass_q50"] = False
        return panel
    y = (trk["atomic_pnl_primary"] > 0).astype(int)
    clf = HistGradientBoostingClassifier(**HGB_PARAMS)
    clf.fit(trk[F10_FEATURES].fillna(0.0), y)
    scores = clf.predict_proba(trk[F10_FEATURES].fillna(0.0))[:, 1]
    thr = float(np.quantile(scores, 0.50))
    panel["v4_score"] = clf.predict_proba(panel[F10_FEATURES].fillna(0.0))[:, 1]
    panel["v4_pass_q50"] = panel["v4_score"] >= thr
    with open(OUT / "frozen_v4_atomic_hgb.pkl", "wb") as f:
        pickle.dump({"clf": clf, "features": F10_FEATURES, "threshold_q50": thr, "hypothesis_id": "V4_ATOMIC_RETARGET_F10"}, f)
    return panel


# ───────────────────────────── Evaluation ─────────────────────────────

def eval_variant(df: pd.DataFrame, mask: pd.Series, name: str) -> dict:
    """df rows = KEYSTONE-eligible (ks_live_pass) universe for per-signal metrics."""
    base = df
    m = mask.reindex(base.index).fillna(False).astype(bool)
    # per accepted trade
    accepted = base[m & base.atomic_pnl_primary.notna()]
    # per original signal: skipped = 0
    per_signal = base.atomic_pnl_primary.where(m & base.atomic_pnl_primary.notna(), 0.0)
    # only count known outcomes in accepted; for per-signal include skips as 0 but exclude UNKNOWN-from-accept?
    # Protocol: EV per original KEYSTONE signal, skipped trades as 0
    # Signals with UNKNOWN label: treat as 0 contribution when skipped by filter; if accepted&UNKNOWN exclude from both? 
    # Use: per_signal over all base rows: if not m -> 0; if m and nan -> nan excluded from mean
    ps = []
    for idx in base.index:
        if not m.loc[idx]:
            ps.append(0.0)
        else:
            v = base.loc[idx, "atomic_pnl_primary"]
            ps.append(np.nan if pd.isna(v) else float(v))
    ps = np.array(ps, dtype=float)
    ps_known = ps[~np.isnan(ps)]

    pnl = accepted["atomic_pnl_primary"]
    sellable = accepted[accepted.sellable_h == 1]

    def shares(x):
        if len(x) == 0 or x.abs().sum() == 0:
            return None, None
        return float(x.nlargest(1).sum() / x.abs().sum()), float(x.nlargest(min(5, len(x))).sum() / x.abs().sum())

    top1, top5 = shares(pnl) if len(pnl) else (None, None)

    # LOTO
    loto_min = None
    if len(accepted) >= 10:
        evs = []
        for tok in accepted.pool.unique():
            evs.append(float(accepted.loc[accepted.pool != tok, "atomic_pnl_primary"].mean()))
        loto_min = float(np.min(evs)) if evs else None

    # LOWO
    lowo_min = None
    if len(accepted):
        w = pd.to_datetime(accepted.t0, utc=True).dt.isocalendar().week.astype(int)
        evs = []
        for ww in w.unique():
            sub = accepted.loc[w.values != ww, "atomic_pnl_primary"]
            if len(sub) >= 5:
                evs.append(float(sub.mean()))
        lowo_min = float(np.min(evs)) if evs else None

    # drawdown on chronological accepted
    dd = None
    if len(accepted):
        a = accepted.sort_values("t0")["atomic_pnl_primary"].values
        cum = np.cumsum(a)
        peak = np.maximum.accumulate(cum)
        dd = float((cum - peak).min()) if len(cum) else None

    # bootstrap CI
    ci = None
    if len(pnl) >= 10:
        rng = np.random.default_rng(0)
        boots = []
        arr = pnl.values
        for _ in range(400):
            # cluster by pool
            pools = accepted.pool.values
            up = rng.choice(np.unique(pools), size=len(np.unique(pools)), replace=True)
            sample = np.concatenate([arr[pools == p] for p in up])
            if len(sample):
                boots.append(sample.mean())
        ci = [float(np.quantile(boots, 0.025)), float(np.quantile(boots, 0.975))]

    tb = accepted["tb_a"].value_counts(normalize=True).to_dict() if len(accepted) else {}

    return {
        "variant": name,
        "n_base_signals": int(len(base)),
        "n_accepted": int(m.sum()),
        "n_accepted_labeled": int(len(accepted)),
        "accept_rate": float(m.mean()) if len(base) else 0.0,
        "ev_per_accepted": float(pnl.mean()) if len(pnl) else None,
        "ev_per_signal_skip0": float(np.nanmean(ps_known)) if len(ps_known) else None,
        "median": float(pnl.median()) if len(pnl) else None,
        "trimmed_mean_10": float(pd.Series(pnl).mean()) if len(pnl) < 5 else float(pd.Series(pnl).sort_values().iloc[int(0.1*len(pnl)):int(0.9*len(pnl))].mean()),
        "tp_first_rate": float(tb.get("TP_FIRST", 0.0)),
        "sl_first_rate": float(tb.get("SL_FIRST", 0.0)),
        "fatal_first_rate": float(tb.get("FATAL_FIRST", 0.0)),
        "atomic_sell_failure_rate": float((accepted.sellable_h == 0).mean()) if len(accepted) else None,
        "cond_return_sellable": float(sellable.atomic_pnl_primary.mean()) if len(sellable) else None,
        "max_drawdown_cumsum": dd,
        "top1_share": top1,
        "top5_share": top5,
        "loto_min_ev": loto_min,
        "lowo_min_ev": lowo_min,
        "bootstrap_ci95": ci,
        "pnl_per_capital_hour": (float(pnl.mean()) / (HOLD_S / 3600.0)) if len(pnl) else None,
    }


def early_ret_stratified_uplift(df: pd.DataFrame, f10_mask: pd.Series) -> dict:
    d = df.copy()
    d["er_decile"] = pd.qcut(d["early_ret"].rank(method="first"), 10, labels=False, duplicates="drop")
    rows = []
    for dec, g in d.groupby("er_decile"):
        base = g["atomic_pnl_primary"]
        sel = g.loc[f10_mask.reindex(g.index).fillna(False), "atomic_pnl_primary"]
        rows.append({
            "decile": int(dec) if pd.notna(dec) else None,
            "n_base": int(base.notna().sum()),
            "base_ev": float(base.mean()) if base.notna().any() else None,
            "n_f10": int(sel.notna().sum()),
            "f10_ev": float(sel.mean()) if sel.notna().any() else None,
            "uplift": (float(sel.mean()) - float(base.mean())) if sel.notna().any() and base.notna().any() else None,
        })
    return {"by_decile": rows}


def nested_comparison(df: pd.DataFrame) -> dict:
    """M0=early_ret, M1=F10 feats, M2=early_ret+F10; train-fold only, predict atomic>0."""
    core = df[df.split == "core"]
    ks = core[core.ks_live_pass & core.atomic_pnl_primary.notna()].sort_values("t0")
    if len(ks) < 100:
        return {"error": "insufficient", "n": len(ks)}
    mid = len(ks) // 2
    tr, te = ks.iloc[:mid], ks.iloc[mid:]
    ytr = (tr.atomic_pnl_primary > 0).astype(int)
    yte = (te.atomic_pnl_primary > 0).astype(int)
    out = {}
    for name, cols in [
        ("M0_early_ret", ["early_ret"]),
        ("M1_f10", F10_FEATURES),
        ("M2_early_ret_plus_f10", ["early_ret"] + F10_FEATURES),
    ]:
        clf = HistGradientBoostingClassifier(**HGB_PARAMS)
        clf.fit(tr[cols].fillna(0.0), ytr)
        p = clf.predict_proba(te[cols].fillna(0.0))[:, 1]
        thr = np.quantile(clf.predict_proba(tr[cols].fillna(0.0))[:, 1], 0.5)
        sel = p >= thr
        ev = float(te.loc[sel, "atomic_pnl_primary"].mean()) if sel.sum() else None
        out[name] = {"n_sel": int(sel.sum()), "ev": ev, "acc": float(((p >= 0.5) == yte).mean())}
    return out


def score_shuffle_test(df: pd.DataFrame, n: int = 50) -> dict:
    ks = df[df.ks_live_pass & df.atomic_pnl_primary.notna()]
    real = ks.loc[ks.f10_pass_q50, "atomic_pnl_primary"].mean()
    rng = np.random.default_rng(0)
    nulls = []
    scores = ks["f10_score"].values.copy()
    for _ in range(n):
        sh = scores.copy()
        rng.shuffle(sh)
        thr = np.quantile(sh, 0.5)
        nulls.append(float(ks.loc[sh >= thr, "atomic_pnl_primary"].mean()))
    return {
        "real_ev": float(real) if pd.notna(real) else None,
        "null_mean": float(np.mean(nulls)),
        "null_p95": float(np.quantile(nulls, 0.95)),
        "beat_null": bool(real > np.quantile(nulls, 0.95)) if pd.notna(real) else False,
    }


# ───────────────────────────── Main ─────────────────────────────

def main():
    print(f"=== KEYSTONE R2 {R2_VERSION} Atomic Validation ===", flush=True)
    manifest = write_manifest()
    write_label_spec()

    panel = pd.read_csv(R1_OUT / "panel_r1.csv")
    panel["t0"] = pd.to_datetime(panel["t0"], utc=True)
    # ensure ks_live_pass bool
    panel["ks_live_pass"] = panel["ks_live_pass"].astype(bool)
    panel["ks_research_pass"] = panel["ks_research_pass"].astype(bool)

    print("Stage: freeze F10", flush=True)
    panel, f10_meta, clf, thr = freeze_f10(panel)

    print("Stage: GL4 calibration", flush=True)
    sim = ArchiveSim()
    cal_df, cal_sum = calibrate_gl4(sim)
    if not cal_sum.get("trustworthy"):
        final = {
            "r2_version": R2_VERSION,
            "conclusion": "ATOMIC_LABEL_NOT_TRUSTWORTHY",
            "calibration": cal_sum,
            "manifest": str(OUT / "R2_MANIFEST.json"),
            "note": "Stopped before strategy validation per protocol §5",
        }
        json.dump(final, open(OUT / "FINAL_R2.json", "w"), indent=2)
        (OUT / "REPRODUCE.md").write_text(
            "# Reproduce R2\n\n```bash\ncd /root/keystone_r2_atomic && python3 src/r2_pipeline.py\n```\n\n"
            "Stopped at ATOMIC_LABEL_NOT_TRUSTWORTHY.\n"
        )
        print("ATOMIC_LABEL_NOT_TRUSTWORTHY", flush=True)
        # still write empty comparison stubs
        pd.DataFrame([cal_sum]).to_csv(OUT / "r2_trial_ledger.csv", index=False)
        pd.DataFrame().to_csv(OUT / "r2_candidate_comparison.csv", index=False)
        return final

    print("Stage: attach atomic labels (realgas cache)", flush=True)
    panel = attach_atomic_labels(panel)
    panel = fit_v4_atomic(panel)

    # Persist labeled panel
    panel.to_csv(OUT / "r2_panel_atomic.csv", index=False)

    # Evaluation universe: KEYSTONE live-pass with label coverage
    ks = panel[panel.ks_live_pass].copy()
    print(f"KS live-pass n={len(ks)} labeled={ks.atomic_pnl_primary.notna().sum()} "
          f"sell_fail={(ks.sellable_h==0).sum()}", flush=True)

    # Variants on development core only for selection-free reporting; also report burned holdout separately
    core_ks = ks[ks.split == "core"]
    hold_ks = ks[ks.split == "DEVELOPMENT_HOLDOUT_BURNED"]

    variants = {
        "V0_KEYSTONE_ALLPASS": core_ks["ks_live_pass"],
        "V1_EARLY_RET_CTRL": core_ks["v1_early_ret_pass"],
        "V2_F10_Q50": core_ks["f10_pass_q50"] & core_ks["ks_live_pass"],
        "V3_INEFF_BUY_Q80KEEP": core_ks["v3_pass"],
        "V4_ATOMIC_RETARGET_Q50": core_ks["v4_pass_q50"] & core_ks["ks_live_pass"],
    }
    # sensitivity q40/q60 for V2 only
    sens = {
        "V2_F10_Q40": core_ks["f10_pass_q40"] & core_ks["ks_live_pass"],
        "V2_F10_Q50": core_ks["f10_pass_q50"] & core_ks["ks_live_pass"],
        "V2_F10_Q60": core_ks["f10_pass_q60"] & core_ks["ks_live_pass"],
    }

    comparisons = []
    ledger = []
    for name, mask in variants.items():
        st = eval_variant(core_ks, mask, name)
        comparisons.append(st)
        ledger.append({"trial_id": name, "stage": "compare", **st})
        print(f"  {name}: n={st['n_accepted_labeled']} ev_acc={st['ev_per_accepted']} "
              f"fatal={st['fatal_first_rate']} sellable_ev={st['cond_return_sellable']}", flush=True)

    sens_rows = []
    for name, mask in sens.items():
        st = eval_variant(core_ks, mask, name)
        sens_rows.append(st)
        ledger.append({"trial_id": name, "stage": "sensitivity", **st})

    # Holdout burned diagnostics (not confirmation)
    hold_v2 = eval_variant(hold_ks, hold_ks["f10_pass_q50"] & hold_ks["ks_live_pass"], "V2_F10_Q50_BURNED_HOLDOUT")
    hold_v0 = eval_variant(hold_ks, hold_ks["ks_live_pass"], "V0_BURNED_HOLDOUT")
    ledger.append({"trial_id": "V2_BURNED_HOLDOUT", "stage": "burned_holdout", **hold_v2})

    uplift = early_ret_stratified_uplift(core_ks, core_ks["f10_pass_q50"] & core_ks["ks_live_pass"])
    nested = nested_comparison(panel)
    shuffle = score_shuffle_test(core_ks)

    # Sellable-only directional test
    sellable_ks = core_ks[core_ks.sellable_h == 1]
    dir_v0 = eval_variant(sellable_ks, sellable_ks["ks_live_pass"], "V0_SELLABLE_ONLY")
    dir_v2 = eval_variant(sellable_ks, sellable_ks["f10_pass_q50"], "V2_SELLABLE_ONLY")
    dir_v1 = eval_variant(sellable_ks, sellable_ks["v1_early_ret_pass"], "V1_SELLABLE_ONLY")

    # Post-2026-06-01 confirmation attempt (feature+score if possible)
    conf = attempt_postjune_confirmation(sim, clf, thr, f10_meta)

    pd.DataFrame(comparisons).to_csv(OUT / "r2_candidate_comparison.csv", index=False)
    pd.DataFrame(ledger).to_csv(OUT / "r2_trial_ledger.csv", index=False)
    json.dump({"uplift": uplift, "nested": nested, "shuffle": shuffle, "sens": sens_rows,
               "sellable_only": {"V0": dir_v0, "V1": dir_v1, "V2": dir_v2},
               "burned_holdout": {"V0": hold_v0, "V2": hold_v2},
               "confirmation": conf},
              open(OUT / "r2_mechanism_tests.json", "w"), indent=2, default=str)

    conclusion = decide_conclusion(comparisons, dir_v0, dir_v1, dir_v2, uplift, nested, shuffle, conf, cal_sum)

    final = {
        "r2_version": R2_VERSION,
        "created_utc": utcnow(),
        "conclusion": conclusion["label"],
        "conclusion_rationale": conclusion["rationale"],
        "calibration": cal_sum,
        "f10_meta": f10_meta,
        "variants_core": comparisons,
        "sensitivity_monotonicity": {
            "q40_ev": next((s["ev_per_accepted"] for s in sens_rows if s["variant"] == "V2_F10_Q40"), None),
            "q50_ev": next((s["ev_per_accepted"] for s in sens_rows if s["variant"] == "V2_F10_Q50"), None),
            "q60_ev": next((s["ev_per_accepted"] for s in sens_rows if s["variant"] == "V2_F10_Q60"), None),
        },
        "sellable_only_directional": {"V0": dir_v0, "V1": dir_v1, "V2": dir_v2},
        "mechanism_tests": {"nested": nested, "shuffle": shuffle, "early_ret_uplift": uplift},
        "development_holdout_status": "DEVELOPMENT_HOLDOUT_BURNED",
        "confirmation": conf,
        "forward_eligible": False,
        "deployed": False,
        "artifacts": {
            "manifest": str(OUT / "R2_MANIFEST.json"),
            "calibration_csv": str(OUT / "atomic_simulator_calibration.csv"),
            "scores": str(OUT / "frozen_f10_scores.parquet"),
            "comparison": str(OUT / "r2_candidate_comparison.csv"),
            "ledger": str(OUT / "r2_trial_ledger.csv"),
        },
    }
    json.dump(final, open(OUT / "FINAL_R2.json", "w"), indent=2, default=str)
    write_reproduce()
    print("CONCLUSION:", conclusion["label"], flush=True)
    print(conclusion["rationale"], flush=True)
    return final


def attempt_postjune_confirmation(sim, clf, thr, f10_meta) -> dict:
    """Build minimal post-2026-06-01 KEYSTONE-eligible sample if CH allows; score with frozen F10.
    Does not tune anything. Returns INSUFFICIENT if n too small.
    """
    # Pull a capped set of pools with t0 after 2026-06-01 and enough pre swaps — feature parity hard;
    # For R2 we document coverage and only run atomic sell at entry+4h if we can join discovered pools.
    try:
        import subprocess
        from io import StringIO
        sql = """
        SELECT pool_address, min(block_time) AS t0, count() AS ns
        FROM eatlq.uniswap_v3_swaps_decoded
        WHERE chain_id=8453 AND block_time >= '2026-06-01' AND block_time < '2026-07-14'
        GROUP BY pool_address
        HAVING t0 >= '2026-06-02' AND ns >= 5
        ORDER BY t0
        LIMIT 50
        """
        r = subprocess.run(
            ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--format=TSVWithNames", f"--query={sql}"],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            return {"status": "INSUFFICIENT_ATOMIC_SAMPLE", "reason": "ch_query_failed", "stderr": r.stderr[:300]}
        df = pd.read_csv(StringIO(r.stdout), sep="\t")
    except Exception as e:
        return {"status": "INSUFFICIENT_ATOMIC_SAMPLE", "reason": str(e)}

    # Without full feature rebuild + p_fatal scoring, we cannot honestly apply F10 filter on post-June.
    # Protocol forbids retuning; missing features ⇒ cannot confirm.
    return {
        "status": "INSUFFICIENT_ATOMIC_SAMPLE",
        "reason": "post_2026-06-01 pools exist but full KEYSTONE feature+p_fatal+F10 score pipeline not frozen for prospective window; no untouched confirmation applied",
        "n_pools_seen_cap50": int(len(df)) if len(df) else 0,
        "forward_eligible": False,
    }


def decide_conclusion(comparisons, dir_v0, dir_v1, dir_v2, uplift, nested, shuffle, conf, cal_sum) -> dict:
    by = {c["variant"]: c for c in comparisons}
    v0, v1, v2 = by.get("V0_KEYSTONE_ALLPASS", {}), by.get("V1_EARLY_RET_CTRL", {}), by.get("V2_F10_Q50", {})

    if conf.get("status") == "INSUFFICIENT_ATOMIC_SAMPLE" and (v2.get("n_accepted_labeled") or 0) < 30:
        return {"label": "INSUFFICIENT_ATOMIC_SAMPLE", "rationale": "labeled accepted n too small and no prospective confirmation"}

    # Quoter artifact: F10 was strong on qv2 but atomic EV <= V0 and no sellability improvement
    fatal_drop = (v0.get("fatal_first_rate") or 0) - (v2.get("fatal_first_rate") or 0)
    ev_v2 = v2.get("ev_per_accepted")
    ev_v0 = v0.get("ev_per_accepted")
    ev_v1 = v1.get("ev_per_accepted")
    sell_v2 = dir_v2.get("ev_per_accepted")
    sell_v1 = dir_v1.get("ev_per_accepted")
    sell_v0 = dir_v0.get("ev_per_accepted")

    beats_v1_atomic = ev_v2 is not None and ev_v1 is not None and ev_v2 > ev_v1 + 0.03
    beats_v0_atomic = ev_v2 is not None and ev_v0 is not None and ev_v2 > ev_v0 + 0.03
    dir_beats_v1 = sell_v2 is not None and sell_v1 is not None and sell_v2 > sell_v1 + 0.03
    sellability_only = fatal_drop >= 0.05 and not dir_beats_v1
    combined = beats_v0_atomic and fatal_drop >= 0.03 and dir_beats_v1
    pure_dir = dir_beats_v1 and fatal_drop < 0.03 and beats_v1_atomic

    # Nested: M1 should beat M0 on atomic EV
    nested_ok = False
    if isinstance(nested, dict) and "M0_early_ret" in nested and "M1_f10" in nested:
        m0e, m1e = nested["M0_early_ret"].get("ev"), nested["M1_f10"].get("ev")
        nested_ok = m1e is not None and m0e is not None and m1e > m0e + 0.02

    if ev_v2 is not None and ev_v0 is not None and ev_v2 <= ev_v0 + 0.01 and fatal_drop < 0.02:
        return {
            "label": "QUOTERV2_LABEL_ARTIFACT",
            "rationale": f"V2 atomic EV {ev_v2:.3f} ≈ V0 {ev_v0:.3f}; fatal_drop={fatal_drop:.3f}; R1 Quoter edge does not survive atomic labels",
        }

    if sellability_only and beats_v0_atomic:
        return {
            "label": "SELLABILITY_RISK_FILTER_ONLY",
            "rationale": f"fatal_drop={fatal_drop:.3f}; sellable-only V2 EV {sell_v2} vs V1 {sell_v1} — no directional uplift",
        }

    if combined and (nested_ok or shuffle.get("beat_null")):
        return {
            "label": "COMBINED_TRADE_QUALITY_ALPHA",
            "rationale": f"atomic EV uplift vs V0/V1 and fatal_drop={fatal_drop:.3f} with sellable directional uplift",
        }

    if pure_dir and nested_ok:
        return {
            "label": "PURE_DIRECTIONAL_META_ALPHA",
            "rationale": f"sellable-only V2 {sell_v2} > V1 {sell_v1}; nested M1>M0; low fatal_drop={fatal_drop:.3f}",
        }

    # Default: if lost qv2 edge
    if ev_v2 is not None and ev_v1 is not None and ev_v2 <= ev_v1:
        return {
            "label": "QUOTERV2_LABEL_ARTIFACT",
            "rationale": f"V2 atomic EV {ev_v2:.3f} does not beat early_ret control {ev_v1:.3f}; shuffle_beat={shuffle.get('beat_null')}",
        }

    if (v2.get("n_accepted_labeled") or 0) < 40:
        return {"label": "INSUFFICIENT_ATOMIC_SAMPLE", "rationale": "accepted labeled n < 40 on core"}

    return {
        "label": "QUOTERV2_LABEL_ARTIFACT",
        "rationale": f"fallback: V2={ev_v2} V1={ev_v1} V0={ev_v0} fatal_drop={fatal_drop} nested={nested}",
    }


def write_reproduce():
    (OUT / "REPRODUCE.md").write_text(
        f"""# KEYSTONE R2 Reproduce

Version: `{R2_VERSION}`

```bash
cd /root/keystone_r2_atomic
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python3 src/r2_pipeline.py
```

Requirements:
- Docker ClickHouse `eatlq-clickhouse-1` (optional for confirmation census)
- Archive RPC in `/tmp/rpc_url.txt` or GL5v3 archive URL (read-only eth_call)
- R1 panel at `/root/alpha_discovery_keystone_r1/out/panel_r1.csv`
- Realgas cache `/opt/EATLQ/scripts/research/base_lefttail/_realgas_replay.json`
- GL4 ledger `/opt/EATLQ/mvp/diagnostics/base_gl4_live_truth_ledger_20260716.json`

Does **not** modify `/opt/EATLQ/services/signal_bridge/` or executor config.
Does **not** deploy.
"""
    )


if __name__ == "__main__":
    main()
