#!/usr/bin/env python3
"""r3.0.1-shadow prospective observer.

Watches the frozen KEYSTONE entry window, scores F10 + C0, writes preoutcome.
Zero capital. Does not xadd Redis signals. Does not import or call the executor.
Does not retune thresholds or features.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import pickle
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from eth_abi import encode
from web3 import Web3

ROOT = Path("/root/keystone_r3_shadow")
R301 = ROOT / "r3.0.1"
LEDGER_DIR = ROOT / "ledger"
ENV_FILE = Path("/opt/EATLQ/infra/compose/.env")
LIFETIME_PATH = LEDGER_DIR / "lifetime.json"
ALERT_STATE_PATH = LEDGER_DIR / "instrument_alert.json"
STALL_FAIL_THRESHOLD = 10
SPEC = json.loads((R301 / "R3_PREREGISTRATION.json").read_text())
sys.path.insert(0, str(R301))
from shadow_recorder import append_preoutcome  # noqa: E402

WETH = "0x4200000000000000000000000000000000000006"
QUOTER = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
CHAIN = 8453
W = 600
WINDOW_MARGIN = 300
MIN_PRE = 5
P_CUT = 0.27
FRIC_THR = -0.05
AMT_IN = int(100 / 3000 * 1e18)  # frozen r2 research size
POLL_SEC = 30
LN1P = math.log(1.0001)
POOL_RE = re.compile(r"^0x[0-9a-f]{40}$")
KS_FEATURES = [
    "ns_pre", "buyers_pre", "senders_pre", "swaps_per_buyer", "buyer_sender_ratio",
    "early_ret", "pre_vol", "pre_pullback", "pre_runup_from_lo", "liq_log",
    "liq_trend", "liq_drawdown", "heat_accel", "mom_h1", "mom_h2",
]
F10_FEATURES = SPEC["operational_scorer"]["features_in_order"]
PRICE_FEATURES = SPEC["price_only_control"]["features_in_order"]
PRIMARY_THR = float(SPEC["operational_scorer"]["primary_threshold"])
START_BLOCK = int(SPEC["start_block_exclusive"])
QUOTE_SIG = Web3.keccak(text="quoteExactInputSingle((address,address,uint256,uint24,uint160))")[:4]
FEE_SIG = Web3.keccak(text="fee()")[:4]
UNI_FEES = {100, 500, 2500, 3000, 10000}

log = logging.getLogger("r3_observer")


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ch(query: str, timeout: int = 120) -> str:
    r = subprocess.run(
        ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client",
         "--max_memory_usage=12000000000", "--query", query],
        capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(r.stderr[-2000:] or r.stdout[-2000:])
    return r.stdout


def _load_env() -> None:
    if not ENV_FILE.is_file():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip("'").strip('"'))


def send_telegram(text: str) -> bool:
    _load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        log.warning("telegram credentials missing; alert not sent")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text[:3900]}).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as exc:
        log.warning("telegram send failed: %s", exc)
        return False


def load_lifetime() -> dict:
    if LIFETIME_PATH.exists():
        try:
            return json.loads(LIFETIME_PATH.read_text())
        except Exception:
            pass
    return {
        "first_utc": utcnow(),
        "wrote": 0,
        "ks_fail": 0,
        "fric_fail": 0,
        "retry": 0,
        "other": 0,
        "process_failed": 0,
        "candidate_cycles": 0,
        "candidates_total": 0,
    }


def save_lifetime(lt: dict) -> None:
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    lt["updated_utc"] = utcnow()
    LIFETIME_PATH.write_text(json.dumps(lt, indent=2) + "\n")


def add_lifetime(lt: dict, counts: dict) -> dict:
    for k in ("wrote", "ks_fail", "fric_fail", "retry", "other", "process_failed"):
        lt[k] = int(lt.get(k, 0)) + int(counts.get(k, 0))
    lt["candidates_total"] = int(lt.get("candidates_total", 0)) + int(counts.get("candidates", 0))
    if counts.get("candidates"):
        lt["candidate_cycles"] = int(lt.get("candidate_cycles", 0)) + 1
    save_lifetime(lt)
    return lt


def instrument_status(lt: dict) -> str:
    if int(lt.get("process_failed", 0)) >= STALL_FAIL_THRESHOLD and int(lt.get("wrote", 0)) == 0:
        return "WRITE_STALL"
    return "OK"


def maybe_alert_instrument(status: str, lt: dict) -> None:
    state = {}
    if ALERT_STATE_PATH.exists():
        try:
            state = json.loads(ALERT_STATE_PATH.read_text())
        except Exception:
            state = {}
    if status == "WRITE_STALL" and not state.get("stall_sent"):
        ok = send_telegram(
            "⚠️ r3 shadow 账本写入停滞\n"
            "扫到新池但特征查询失败，确认样本仍是 0。\n"
            "生产 Keystone / 执行器没动。\n"
            f"process_failed={lt.get('process_failed')} wrote={lt.get('wrote')}\n"
            "看 /root/keystone_r3_shadow/ledger/observer.log"
        )
        state["stall_sent"] = True
        state["stall_utc"] = utcnow()
        state["stall_telegram_ok"] = ok
    if status == "OK" and int(lt.get("wrote", 0)) > 0 and state.get("stall_sent") and not state.get("recovered"):
        ok = send_telegram(
            "✅ r3 shadow 账本恢复写入\n"
            f"累计 wrote={lt.get('wrote')}。此前空窗不回填。生产交易不受影响。"
        )
        state["recovered"] = True
        state["recovered_utc"] = utcnow()
        state["recovered_telegram_ok"] = ok
    if state:
        ALERT_STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")


def rpc_url() -> str:
    p = Path("/tmp/rpc_url.txt")
    if p.exists():
        return p.read_text().strip()
    raise RuntimeError("missing /tmp/rpc_url.txt")


def load_arm() -> dict:
    arm = ROOT / "HUMAN_ARM.json"
    if not arm.exists():
        raise RuntimeError("HUMAN_ARM.json missing")
    obj = json.loads(arm.read_text())
    if obj.get("r3_version") != "r3.0.1-shadow":
        raise RuntimeError("HUMAN_ARM version mismatch")
    if not obj.get("clock_started"):
        raise RuntimeError("clock_started is false")
    for k in ("ack_hash_resolution", "ack_no_grouped_outcome_peek", "ack_evaluation_trigger_frozen"):
        if not obj.get(k):
            raise RuntimeError(f"missing ack {k}")
    if int(obj.get("start_block_exclusive")) != START_BLOCK:
        raise RuntimeError("start_block_exclusive drifted")
    return obj


def selfcheck() -> dict:
    load_arm()
    op = Path(SPEC["operational_scorer"]["artifact_path"])
    price = Path(SPEC["price_only_control"]["artifact_path"])
    rez = R301 / "artifacts" / "r21_residualizers.pkl"
    pipe = R301 / "artifacts" / "r21_feature_pipeline.json"
    fixp = R301 / "fixtures" / "f10_golden_scores.json"
    ks_m = Path("/opt/EATLQ/services/signal_bridge/_model.pkl")
    ks_p = Path("/opt/EATLQ/services/signal_bridge/_params.json")
    pairs = [
        ("operational", op, SPEC["operational_scorer"]["artifact_sha256_expected"]),
        ("price_only", price, SPEC["price_only_control"]["artifact_sha256"]),
        ("residualizer_pkl", rez, SPEC["operational_scorer"]["residualizer_sha256"]),
        ("feature_pipeline", pipe, SPEC["operational_scorer"]["feature_pipeline_sha256"]),
        ("golden", fixp, SPEC["operational_scorer"]["golden_score_fixture_sha256"]),
        ("ks_model", ks_m, SPEC["champion_keystone"]["model_sha256"]),
        ("ks_params", ks_p, SPEC["champion_keystone"]["params_sha256"]),
    ]
    for name, path, exp in pairs:
        if not path.exists():
            raise RuntimeError(f"missing {name}: {path}")
        got = sha_file(path)
        if got != exp:
            raise RuntimeError(f"hash mismatch {name}: {got} != {exp}")
    f10 = pickle.load(open(op, "rb"))["clf"]
    fix = json.loads(fixp.read_text())
    X = pd.DataFrame([r["features"] for r in fix["rows"]], columns=fix["features_in_order"]).fillna(0.0)
    pred = f10.predict_proba(X)[:, 1]
    exp = np.array([r["expected_f10_score_r21"] for r in fix["rows"]], dtype=float)
    max_abs = float(np.max(np.abs(pred - exp)))
    if max_abs > 1e-12:
        raise RuntimeError(f"golden score drift max_abs={max_abs}")
    return {"ok": True, "golden_max_abs": max_abs, "checked_utc": utcnow()}


class Models:
    def __init__(self):
        with open("/opt/EATLQ/services/signal_bridge/_model.pkl", "rb") as f:
            self.ks = pickle.load(f)
        self.f10 = pickle.load(open(SPEC["operational_scorer"]["artifact_path"], "rb"))["clf"]
        self.price = pickle.load(open(SPEC["price_only_control"]["artifact_path"], "rb"))["clf"]
        self.rez = pickle.load(open(R301 / "artifacts" / "r21_residualizers.pkl", "rb"))


def candidates() -> list[str]:
    q = f"""
    SELECT lower(pool_address)
    FROM eatlq.uniswap_v3_swaps_decoded
    WHERE chain_id=8453 AND block_time >= now() - INTERVAL 20 MINUTE
    GROUP BY pool_address
    HAVING min(block_time) >= now() - INTERVAL {W + WINDOW_MARGIN} SECOND
       AND min(block_time) <= now() - INTERVAL {W} SECOND
    """
    pools = [ln.strip() for ln in ch(q).splitlines() if POOL_RE.match(ln.strip())]
    out = []
    for pool in pools:
        older = ch(
            "SELECT count() FROM eatlq.uniswap_v3_swaps_decoded "
            f"WHERE chain_id=8453 AND pool_address='{pool}' "
            "AND block_time < now() - INTERVAL 20 MINUTE"
        ).strip()
        if older in ("", "0"):
            out.append(pool)
    return out


def pool_meta(pool: str) -> dict | None:
    q = (
        "SELECT lower(token0), lower(token1), toUInt32(fee) "
        f"FROM census.pools WHERE lower(pool)='{pool}' LIMIT 1"
    )
    line = ch(q).strip()
    if line:
        t0, t1, fee = line.split("\t")
        return {"token0": t0, "token1": t1, "fee": int(fee)}
    q2 = (
        "SELECT lower(token0_address), lower(token1_address), toUInt32(fee_bps) "
        "FROM eatlq.discovered_pools WHERE chain_id=8453 AND "
        f"lower(pool_address)='{pool}' LIMIT 1"
    )
    try:
        line = ch(q2).strip()
    except Exception:
        line = ""
    if not line:
        return None
    t0, t1, fee = line.split("\t")
    return {"token0": t0, "token1": t1, "fee": int(fee)}


def query_raw(pool: str) -> dict | None:
    sql = f"""
    WITH fs AS (
      SELECT min(block_time) t0, min(block_number) t0_block
      FROM eatlq.uniswap_v3_swaps_decoded
      WHERE chain_id=8453 AND pool_address='{pool}' AND block_time IS NOT NULL
    )
    SELECT
      toUnixTimestamp(any(fs.t0)) t0_sec,
      any(fs.t0_block) t0_block,
      countIf(dateDiff('second', fs.t0, s.block_time) <= {W}) ns_pre,
      uniqExactIf(s.recipient, dateDiff('second', fs.t0, s.block_time) <= {W}) buyers_pre,
      uniqExactIf(s.sender, dateDiff('second', fs.t0, s.block_time) <= {W}) senders_pre,
      argMinIf(s.tick, s.block_time, dateDiff('second', fs.t0, s.block_time) <= {W}) first_tick,
      argMaxIf(s.tick, s.block_time, dateDiff('second', fs.t0, s.block_time) <= {W}) last_tick_pre,
      minIf(s.tick, dateDiff('second', fs.t0, s.block_time) <= {W}) min_tick_pre,
      maxIf(s.tick, dateDiff('second', fs.t0, s.block_time) <= {W}) max_tick_pre,
      argMinIf(toFloat64(s.liquidity), s.block_time, dateDiff('second', fs.t0, s.block_time) <= {W}) liq_first,
      argMaxIf(toFloat64(s.liquidity), s.block_time, dateDiff('second', fs.t0, s.block_time) <= {W}) liq_last_pre,
      minIf(toFloat64(s.liquidity), dateDiff('second', fs.t0, s.block_time) <= {W}) liq_min_pre,
      countIf(dateDiff('second', fs.t0, s.block_time) <= {W//2}) ns_h1,
      countIf(dateDiff('second', fs.t0, s.block_time) > {W//2} AND dateDiff('second', fs.t0, s.block_time) <= {W}) ns_h2,
      argMaxIf(s.tick, s.block_time, dateDiff('second', fs.t0, s.block_time) <= {W//2}) last_tick_h1,
      maxIf(s.block_number, dateDiff('second', fs.t0, s.block_time) <= {W}) feature_cutoff_block
    FROM eatlq.uniswap_v3_swaps_decoded s, fs
    WHERE s.chain_id=8453 AND s.pool_address='{pool}' AND s.block_time IS NOT NULL
      AND dateDiff('second', fs.t0, s.block_time) BETWEEN 0 AND {W}
    """
    line = ch(sql).strip()
    if not line:
        return None
    parts = line.split("\t")
    names = [
        "t0_sec", "t0_block", "ns_pre", "buyers_pre", "senders_pre", "first_tick",
        "last_tick_pre", "min_tick_pre", "max_tick_pre", "liq_first", "liq_last_pre",
        "liq_min_pre", "ns_h1", "ns_h2", "last_tick_h1", "feature_cutoff_block",
    ]
    d = {}
    for k, v in zip(names, parts):
        if v in ("", "\\N"):
            return None
        d[k] = float(v) if k not in ("t0_sec", "t0_block", "feature_cutoff_block") else int(float(v))
    if d["ns_pre"] < MIN_PRE:
        return None
    return d


def ks_vector(d: dict) -> np.ndarray:
    o = d["orient"]
    lp = lambda t: t * LN1P * o
    lp_first = lp(d["first_tick"])
    lp_last = lp(d["last_tick_pre"])
    lp_lo = min(lp(d["min_tick_pre"]), lp(d["max_tick_pre"]))
    lp_hi = max(lp(d["min_tick_pre"]), lp(d["max_tick_pre"]))
    lp_h1 = lp(d["last_tick_h1"]) if d.get("last_tick_h1") else lp_first
    ns_pre = max(d["ns_pre"], 1)
    buyers = max(d["buyers_pre"], 1)
    senders = max(d["senders_pre"], 1)
    liq_first = max(d["liq_first"], 1)
    ns_h1 = max(d["ns_h1"], 1)
    return np.array([
        d["ns_pre"], d["buyers_pre"], d["senders_pre"],
        ns_pre / buyers, d["buyers_pre"] / senders,
        lp_last - lp_first, lp_hi - lp_lo, lp_hi - lp_last, lp_last - lp_lo,
        np.log1p(max(d["liq_last_pre"], 0)),
        d["liq_last_pre"] / liq_first, d["liq_min_pre"] / liq_first,
        d["ns_h2"] / ns_h1, lp_h1 - lp_first, lp_last - lp_h1,
    ], dtype=np.float64)


def derived(d: dict, vec: np.ndarray) -> dict:
    names = KS_FEATURES
    out = {k: float(v) for k, v in zip(names, vec)}
    out["accel"] = out["mom_h2"] - out["mom_h1"]
    out["range_pos"] = out["pre_runup_from_lo"] / (out["pre_vol"] + 1e-9)
    out["liq_fragility_proxy"] = (
        float(out["early_ret"] > 0)
        * (1.0 - min(max(out["liq_trend"], 0.0), 2.0) / 2.0)
        * (1.0 - min(max(out["liq_drawdown"], 0.0), 1.0))
    )
    return out


def seq_part_exhaust(pool: str) -> float:
    sql = f"""
    WITH fs AS (
      SELECT min(block_time) t0
      FROM eatlq.uniswap_v3_swaps_decoded
      WHERE chain_id=8453 AND pool_address='{pool}'
    )
    SELECT
      intDiv(dateDiff('second', fs.t0, s.block_time), 100) AS bar,
      argMax(s.tick, s.block_time) AS tick_last,
      uniqExact(s.recipient) AS n_buyers,
      count() AS n_swaps
    FROM eatlq.uniswap_v3_swaps_decoded s, fs
    WHERE s.chain_id=8453 AND s.pool_address='{pool}'
      AND dateDiff('second', fs.t0, s.block_time) BETWEEN 0 AND {W}
    GROUP BY bar ORDER BY bar
    """
    rows = []
    for ln in ch(sql).splitlines():
        bar, tick, buyers, swaps = ln.split("\t")
        rows.append((int(bar), float(tick), float(buyers), float(swaps)))
    if len(rows) < 2:
        return 0.0
    ticks = np.array([r[1] for r in rows], float)
    buyers = np.array([r[2] for r in rows], float)
    swaps = np.array([r[3] for r in rows], float)
    dtick = np.diff(ticks, prepend=ticks[0])
    buyer_growth = (buyers[-1] / max(buyers[0], 1.0)) - 1.0
    conc = 1.0 - (buyers / np.maximum(swaps, 1.0))
    conc_change = float(conc[-1] - conc[0])
    return float(dtick.sum() > 0 and buyer_growth < 0 and conc_change > 0)


def active_pools_l1(t0_sec: int) -> float:
    sql = (
        "SELECT uniqExact(pool_address) FROM eatlq.uniswap_v3_swaps_decoded "
        f"WHERE chain_id=8453 AND toDate(block_time) = toDate(toDateTime({t0_sec})) - 1"
    )
    raw = ch(sql).strip()
    return float(raw or 0)


def apply_f10(models: Models, feat: dict) -> dict:
    Ximp = np.array([[feat[c] for c in models.rez["impact_xcols"]]], float)
    Xflow = np.array([[feat[c] for c in models.rez["flow_xcols"]]], float)
    Ximp = np.nan_to_num(Ximp, nan=0.0, posinf=0.0, neginf=0.0)
    Xflow = np.nan_to_num(Xflow, nan=0.0, posinf=0.0, neginf=0.0)
    impact = float(feat["early_ret"] - models.rez["impact"].predict(Ximp)[0])
    flow = float(feat["ns_pre"] - models.rez["flow"].predict(Xflow)[0])
    out = {
        "impact_residual": impact,
        "ineffective_buying": flow - impact,
        "liq_fragility_score": feat["liq_fragility_proxy"] * max(feat["early_ret"], 0.0),
        "part_exhaust_score": feat["seq_part_exhaust"] * max(feat["early_ret"], 0.0),
        "accel": feat["accel"],
    }
    return out


def onchain_fee(w3: Web3, pool: str) -> int | None:
    try:
        raw = w3.eth.call({"to": Web3.to_checksum_address(pool), "data": FEE_SIG})
        return int.from_bytes(raw[-32:], "big") if raw else None
    except Exception:
        return None


def resolve_fee(w3: Web3, pool: str, meta_fee: int, from_census: bool) -> int | None:
    if from_census and meta_fee in UNI_FEES:
        return meta_fee
    if meta_fee in UNI_FEES:
        return meta_fee
    if meta_fee * 100 in UNI_FEES:
        return meta_fee * 100
    return onchain_fee(w3, pool)


def quote_roundtrip(w3: Web3, token: str, fee: int) -> float | None:
    token = Web3.to_checksum_address(token)
    weth = Web3.to_checksum_address(WETH)
    quoter = Web3.to_checksum_address(QUOTER)

    def quote(token_in, token_out, amt):
        data = QUOTE_SIG + encode(
            ["(address,address,uint256,uint24,uint160)"],
            [(token_in, token_out, int(amt), int(fee), 0)],
        )
        try:
            raw = w3.eth.call({"to": quoter, "data": data})
        except Exception:
            return None
        if not raw or len(raw) < 32:
            return None
        return int.from_bytes(raw[:32], "big")

    bought = quote(weth, token, AMT_IN)
    if not bought or bought <= 0:
        return None
    back = quote(token, weth, bought)
    if back is None:
        return None
    return back / AMT_IN - 1.0


def chain_tip(w3: Web3) -> tuple[int, str]:
    n = int(w3.eth.block_number)
    h = w3.eth.get_block(n)["hash"].hex()
    return n, h


def load_seen() -> set[str]:
    p = LEDGER_DIR / "seen_pools.jsonl"
    out = set()
    if p.exists():
        for ln in p.read_text().splitlines():
            if not ln.strip():
                continue
            rec = json.loads(ln)
            if rec.get("pool"):
                out.add(rec["pool"])
    return out


def mark_seen(pool: str, status: str, extra: dict | None = None):
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"pool": pool, "status": status, "utc": utcnow()}
    if extra:
        rec.update(extra)
    with (LEDGER_DIR / "seen_pools.jsonl").open("a") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")


def write_health(payload: dict):
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    (LEDGER_DIR / "observer_health.json").write_text(json.dumps(payload, indent=2) + "\n")


def process_pool(models: Models, w3: Web3, pool: str) -> str:
    meta = pool_meta(pool)
    if not meta:
        return "retry_no_meta"
    if WETH not in (meta["token0"], meta["token1"]):
        mark_seen(pool, "not_weth")
        return "not_weth"
    raw = query_raw(pool)
    if raw is None:
        return "retry_minpre"
    raw["orient"] = -1 if meta["token0"] == WETH else 1
    vec = ks_vector(raw)
    feat = derived(raw, vec)
    p_fatal = float(models.ks.predict_proba(pd.DataFrame([vec], columns=KS_FEATURES))[0, 1])
    if p_fatal > P_CUT:
        mark_seen(pool, "ks_fail", {"p_fatal": p_fatal})
        return "ks_fail"
    feat["seq_part_exhaust"] = seq_part_exhaust(pool)
    feat["active_pools_l1"] = active_pools_l1(raw["t0_sec"])
    f10_feat = apply_f10(models, feat)
    f10_score = float(models.f10.predict_proba(pd.DataFrame([f10_feat], columns=F10_FEATURES).fillna(0.0))[0, 1])
    price_row = {k: feat.get(k, 0.0) for k in PRICE_FEATURES}
    price_score = float(models.price.predict_proba(pd.DataFrame([price_row], columns=PRICE_FEATURES).fillna(0.0))[0, 1])
    token = meta["token1"] if meta["token0"] == WETH else meta["token0"]
    fee = resolve_fee(w3, pool, meta["fee"], True)
    if fee is None:
        return "retry_friction"
    fric = quote_roundtrip(w3, token, fee)
    if fric is None:
        return "retry_friction"
    if fric < FRIC_THR:
        mark_seen(pool, "fric_fail", {"friction": fric, "p_fatal": p_fatal})
        return "fric_fail"
    tip, tip_hash = chain_tip(w3)
    if raw["feature_cutoff_block"] <= START_BLOCK:
        mark_seen(pool, "pre_start_block", {"block": raw["feature_cutoff_block"]})
        return "pre_start_block"
    t0_iso = datetime.fromtimestamp(raw["t0_sec"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    signal_id = f"{pool}|{t0_iso}"
    snap = json.dumps(f10_feat, sort_keys=True, separators=(",", ":"))
    write_start = time.time()
    rec = {
        "signal_id": signal_id,
        "t0_block": int(raw["t0_block"]),
        "decision_block": int(raw["feature_cutoff_block"]),
        "feature_cutoff_block": int(raw["feature_cutoff_block"]),
        "preoutcome_write_block": int(tip),
        "target_entry_block": int(raw["feature_cutoff_block"]),
        "decision_block_hash": tip_hash if tip == raw["feature_cutoff_block"] else w3.eth.get_block(int(raw["feature_cutoff_block"]))["hash"].hex(),
        "pool": pool,
        "keystone_pass": True,
        "p_fatal": p_fatal,
        "friction": fric,
        "f10_score": f10_score,
        "impact_residual_value": f10_feat["impact_residual"],
        "ineffective_buying_value": f10_feat["ineffective_buying"],
        "liq_fragility_score_value": f10_feat["liq_fragility_score"],
        "part_exhaust_score_value": f10_feat["part_exhaust_score"],
        "accel_value": f10_feat["accel"],
        "price_only_control_score": price_score,
        "feature_snapshot_hash": hashlib.sha256(snap.encode()).hexdigest(),
        "write_delay_ms": int(max(time.time() - (raw["t0_sec"] + W), 0) * 1000),
    }
    rh = append_preoutcome(rec)
    mark_seen(pool, "wrote", {"signal_id": signal_id, "record_sha256": rh, "write_ms": int((time.time() - write_start) * 1000)})
    log.info("WROTE %s p_fatal=%.3f fric=%.4f f10=%.3f pass=%s", pool, p_fatal, fric, f10_score, f10_score >= PRIMARY_THR)
    return "wrote"


def cycle(models: Models, w3: Web3, seen: set[str]) -> dict:
    counts = {
        "candidates": 0, "wrote": 0, "ks_fail": 0, "fric_fail": 0,
        "retry": 0, "other": 0, "process_failed": 0,
    }
    try:
        pools = candidates()
    except Exception:
        log.exception("candidate query failed")
        counts["process_failed"] += 1
        return counts
    counts["candidates"] = len(pools)
    for pool in pools:
        if pool in seen:
            continue
        try:
            st = process_pool(models, w3, pool)
        except Exception:
            log.exception("process failed pool=%s", pool)
            counts["process_failed"] += 1
            counts["other"] += 1
            continue
        if st == "wrote":
            seen.add(pool)
            counts["wrote"] += 1
        elif st == "ks_fail":
            seen.add(pool)
            counts["ks_fail"] += 1
        elif st == "fric_fail":
            seen.add(pool)
            counts["fric_fail"] += 1
        elif st.startswith("retry"):
            counts["retry"] += 1
        else:
            seen.add(pool)
            counts["other"] += 1
    return counts


def main():
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)sZ %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LEDGER_DIR / "observer.log"),
        ],
    )
    once = "--once" in sys.argv
    if "--selfcheck" in sys.argv:
        print(json.dumps(selfcheck(), indent=2))
        return
    chk = selfcheck()
    log.info("selfcheck ok golden_max_abs=%s", chk["golden_max_abs"])
    models = Models()
    w3 = Web3(Web3.HTTPProvider(rpc_url(), request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        raise RuntimeError("rpc not connected")
    seen = load_seen()
    lifetime = load_lifetime()
    (LEDGER_DIR / "observer.pid").write_text(str(os.getpid()) + "\n")
    write_health({
        "status": "CLOCK_STARTED",
        "shadow": True,
        "decision_impact": False,
        "emits_signals": False,
        "started_utc": utcnow(),
        "start_block_exclusive": START_BLOCK,
        "selfcheck": chk,
        "pid": os.getpid(),
        "lifetime": lifetime,
        "instrument_status": instrument_status(lifetime),
    })
    while True:
        t0 = time.time()
        counts = cycle(models, w3, seen)
        lifetime = add_lifetime(lifetime, counts)
        status = instrument_status(lifetime)
        maybe_alert_instrument(status, lifetime)
        ledger_n = 0
        ledger_p = LEDGER_DIR / "r3_preoutcome_events.jsonl"
        if ledger_p.exists():
            ledger_n = sum(1 for ln in ledger_p.read_text().splitlines() if ln.strip())
        health = {
            "status": "RUNNING",
            "utc": utcnow(),
            "counts": counts,
            "seen_n": len(seen),
            "shadow": True,
            "decision_impact": False,
            "pid": os.getpid(),
            "lifetime": lifetime,
            "instrument_status": status,
            "ledger_n": ledger_n,
        }
        write_health(health)
        if counts["candidates"] or counts["wrote"] or counts["process_failed"]:
            log.info("cycle %s instrument=%s lifetime_wrote=%s", counts, status, lifetime.get("wrote"))
        if once:
            print(json.dumps(health, indent=2))
            return
        time.sleep(max(1.0, POLL_SEC - (time.time() - t0)))


if __name__ == "__main__":
    main()
