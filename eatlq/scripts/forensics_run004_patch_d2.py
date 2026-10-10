"""
Run-004 Patch D2: executable label failure forensics (read-only diagnostic).

Replays the same stratified 400 sample as validate_run004_executable_labels (default seed/params)
and records intermediate quote values, sanity-clip detail, and exit-path diagnostics without
modifying label_dashcam_executable.py.

Env: same as validate_run004 + RUN004_D2_OUT_JSON (default mvp/diagnostics/run004_patch_d2_failure_forensics.json)
"""

from __future__ import annotations

import csv
import json
import logging
import os
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import label_dashcam_executable as le  # noqa: E402

import validate_run004_executable_labels as vr  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)sZ %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("forensics_d2")

QUOTER = le.QUOTER_V2_BASE
_SEL_Q = "0xc6a5026a"


def _pad32(hex_: str) -> str:
    s = hex_.lstrip("0x").lower()
    return s.rjust(64, "0")


def _addr_pad(addr: str) -> str:
    return _pad32(addr.lower().lstrip("0x"))


def _uint_pad(n: int) -> str:
    return _pad32(hex(n)[2:])


def sanity_detail(net: float) -> str:
    max_abs = float(os.environ.get("LABEL_MAX_ABS_NET", "10.0"))
    if not (net == net):
        return "nan_roi"
    if net > max_abs:
        return "too_high_roi"
    if net < -1.0:
        return "too_low_roi"
    return "pass"


def call_quoter_v3_detail(
    rpc: le.RPC, token_in: str, token_out: str, amount_in: int, fee: int, block: int,
) -> dict[str, Any]:
    """Return amount out (0 on empty) and optional eth_call error message."""
    data = _SEL_Q + _addr_pad(token_in) + _addr_pad(token_out) + _uint_pad(amount_in) + _uint_pad(fee) + _uint_pad(0)
    out: dict[str, Any] = {"amount": 0, "error": None, "reverted": False}
    try:
        r = rpc.call("eth_call", [{"to": QUOTER, "data": data}, hex(int(block))])
    except Exception as e:
        out["error"] = str(e)[:500]
        out["reverted"] = True
        return out
    if isinstance(r, dict) and "error" in r:
        err = r.get("error") or {}
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        out["error"] = str(msg)[:500]
        out["reverted"] = True
        return out
    res = (r or {}).get("result") or "0x"
    raw = res.lstrip("0x")
    if len(raw) < 64:
        return out
    out["amount"] = int(raw[0:64], 16)
    return out


def run_v2_path(
    rpc: le.RPC, pool: str, weth: str, notional: int, entry_b: int, exit_b: int,
) -> dict[str, Any]:
    o: dict[str, Any] = {"pool_type": "v2"}
    min_weth = int(os.environ.get("LABEL_MIN_WETH_WEI", str(int(0.1 * 1e18))))
    try:
        t0 = le.get_token0_cached(rpc, pool, entry_b)
        o["token0"] = t0
        o["side_weth_leg"] = "token0" if t0 == weth else "token1"
        r0, r1 = le.call_get_reserves(rpc, pool, entry_b)
        if t0 == weth:
            r_we, r_to = r0, r1
        else:
            r_we, r_to = r1, r0
        o["entry_reserves_weth"] = r_we
        o["entry_reserves_token"] = r_to
        r0x, r1x = le.call_get_reserves(rpc, pool, exit_b)
        if t0 == weth:
            r_wx, r_tx = r0x, r1x
        else:
            r_wx, r_tx = r1x, r0x
        o["exit_reserves_weth"] = r_wx
        o["exit_reserves_token"] = r_tx
        if r_we < min_weth:
            o["labeler_reason"] = "fail_low_liquidity"
            o["triage"] = "TRUE_UNEXECUTABLE_OR_TOXIC_SAMPLE"
            return o
        if r_to <= 0:
            o["labeler_reason"] = "fail_zero_tok_reserve"
            o["triage"] = "TRUE_UNEXECUTABLE_OR_TOXIC_SAMPLE"
            return o
        if notional * 100 > r_we:
            o["labeler_reason"] = "fail_notional_gt_1pct"
            o["triage"] = "FILTER_RULE_NEEDED"
            return o
        tokens = le.quote_v2_out(r_we, r_to, notional)
        o["entry_quote_out_tokens"] = int(tokens)
        o["gross_inferred_entry"] = (tokens / r_to) if r_to else None
        if tokens <= 0:
            o["labeler_reason"] = "fail_entry_quote_zero"
            o["triage"] = "FILTER_RULE_NEEDED"
            return o
        if r_wx <= 0 or r_tx <= 0:
            o["labeler_reason"] = "executable_exit_drained"
            o["triage"] = "TRUE_UNEXECUTABLE_OR_TOXIC_SAMPLE"
            o["exit_liquidity_note"] = "zero_reserve_at_exit"
            return o
        weth_back = le.quote_v2_out(r_tx, r_wx, int(tokens))
        o["exit_quote_out_weth"] = int(weth_back)
        if weth_back <= 0:
            o["labeler_reason"] = "executable_exit_drained"
            o["triage"] = "TRUE_UNEXECUTABLE_OR_TOXIC_SAMPLE"
            o["exit_liquidity_note"] = "zero_out_quote"
            return o
        gross = weth_back / notional - 1.0
        net = gross - le.ROUND_TRIP_COST
        o["gross_return"] = gross
        o["net_return_before_sanity"] = net
        o["round_trip_cost"] = le.ROUND_TRIP_COST
        sd = sanity_detail(net)
        o["sanity_detail"] = sd
        if sd != "pass":
            o["labeler_reason"] = "fail_sanity_clip"
            o["triage"] = "FILTER_RULE_NEEDED" if sd == "too_high_roi" else (
                "TRUE_UNEXECUTABLE_OR_TOXIC_SAMPLE" if sd in ("too_low_roi", "nan_roi") else "LABELER_BUG_SUSPECTED"
            )
            return o
        o["labeler_reason"] = "executable"
        o["triage"] = "ok"
        o["binary_label"] = 1 if net > 0 else 0
        return o
    except Exception as e:
        o["labeler_reason"] = "fail_rpc_exception"
        o["error"] = str(e)[:500]
        o["triage"] = "LABELER_BUG_SUSPECTED"
        return o


def run_v3_path(
    rpc: le.RPC, pool: str, t0: str, t1: str, fee_bps: int, weth: str, notional: int,
    entry_b: int, exit_b: int,
) -> dict[str, Any]:
    o: dict[str, Any] = {"pool_type": "v3", "t0": t0, "t1": t1, "fee_bps": fee_bps}
    fee_q = int(fee_bps) * 100
    token = t1 if t0 == weth else t0
    o["target_token"] = token
    o["quote_direction_entry"] = f"WETH->{token[:10]}.."
    o["quote_direction_exit"] = f"{token[:10]}..->WETH"
    try:
        d_in = call_quoter_v3_detail(rpc, weth, token, notional, fee_q, entry_b)
        o["entry_quoter"] = d_in
        tokens = d_in.get("amount", 0) or 0
        o["entry_quote_out_tokens"] = int(tokens)
        if d_in.get("error") or d_in.get("reverted"):
            o["labeler_reason"] = "fail_v3_entry_zero"
            o["triage"] = "LABELER_BUG_SUSPECTED" if d_in.get("reverted") or d_in.get("error") else "TRUE_UNEXECUTABLE_OR_TOXIC_SAMPLE"
            o["entry_revert_or_error"] = d_in.get("error")
            return o
        if tokens <= 0:
            o["labeler_reason"] = "fail_v3_entry_zero"
            o["triage"] = "TRUE_UNEXECUTABLE_OR_TOXIC_SAMPLE"
            return o
        d_ex = call_quoter_v3_detail(rpc, token, weth, int(tokens), fee_q, exit_b)
        o["exit_quoter"] = d_ex
        weth_back = d_ex.get("amount", 0) or 0
        o["exit_quote_out_weth"] = int(weth_back)
        if weth_back <= 0 and (d_ex.get("error") or d_ex.get("reverted")):
            o["labeler_reason"] = "executable_exit_drained"
            o["triage"] = "LABELER_BUG_SUSPECTED"
            o["exit_note"] = "quoter_revert"
            o["exit_revert"] = d_ex.get("error")
            return o
        if weth_back <= 0:
            o["labeler_reason"] = "executable_exit_drained"
            o["triage"] = "TRUE_UNEXECUTABLE_OR_TOXIC_SAMPLE"
            o["exit_note"] = "zero_amount_out"
            return o
        gross = weth_back / notional - 1.0
        net = gross - le.ROUND_TRIP_COST
        o["gross_return"] = gross
        o["net_return_before_sanity"] = net
        sd = sanity_detail(net)
        o["sanity_detail"] = sd
        if sd != "pass":
            o["labeler_reason"] = "fail_sanity_clip"
            o["triage"] = "FILTER_RULE_NEEDED" if sd == "too_high_roi" else (
                "TRUE_UNEXECUTABLE_OR_TOXIC_SAMPLE" if sd in ("too_low_roi", "nan_roi") else "LABELER_BUG_SUSPECTED"
            )
            return o
        o["labeler_reason"] = "executable"
        o["triage"] = "ok"
        o["binary_label"] = 1 if net > 0 else 0
        return o
    except Exception as e:
        o["labeler_reason"] = "fail_rpc_exception"
        o["error"] = str(e)[:500]
        o["triage"] = "LABELER_BUG_SUSPECTED"
        return o


def _norm_topic(t: str) -> str:
    return vr._norm_topic(t)


def main() -> int:
    csv_path = Path(os.environ.get("RUN004_CANDIDATES_CSV", str(REPO / "logs" / "run004_candidates_swaponly_full.csv")))
    out_json = Path(
        os.environ.get("RUN004_D2_OUT_JSON", str(REPO / "mvp" / "diagnostics" / "run004_patch_d2_failure_forensics.json")),
    )

    ch_url = os.environ.get("CLICKHOUSE_URL", "http://clickhouse:8123")
    ch_db = os.environ.get("CLICKHOUSE_DB", "eatlq")
    ch_user = os.environ.get("CLICKHOUSE_USER", "default")
    ch_pw = os.environ.get("CLICKHOUSE_PASSWORD", "")
    chain_id = int(os.environ.get("CHAIN_ID", "8453"))
    redis_url = (os.environ.get("REDIS_URL") or "").strip()
    rpc_url = (os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL") or "").strip()
    if not rpc_url:
        log.error("LABEL_RPC_URL or EVM_HTTP_URL required")
        return 1

    exit_horizon = int(os.environ.get("LABEL_EXIT_HORIZON_SEC", "1800"))
    notional = int(os.environ.get("LABEL_NOTIONAL_WEI", str(int(0.0005e18))))
    max_per = int(os.environ.get("RUN004_D_MAX_PER_POOL", "5") or 5)
    top_tier = int(os.environ.get("RUN004_D_TOP_POOLS", "60") or 60)
    max_total = int(os.environ.get("RUN004_D_MAX_SAMPLE_ROWS", "400") or 400)
    seed = int(os.environ.get("RUN004_D_STRATIFY_SEED", "42") or 42)
    weth = le.WETH_BASE

    le.TOKEN0_CACHE.clear()

    trainable: list[dict[str, Any]] = []
    with csv_path.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("candidate_status") or "") != "trainable_candidate":
                continue
            pt = (row.get("pool_type") or "").lower()
            if pt not in ("v2", "v3"):
                continue
            trainable.append(row)
    if not trainable:
        return 1

    samples = vr.stratified_sample(trainable, max_per, top_tier, max_total, seed)
    pools_s = {r.get("pool_address", "").lower() for r in samples}
    chm = vr.load_pool_meta_full(ch_url, ch_db, ch_user, ch_pw, chain_id, pools_s)
    rm = vr.load_pool_meta_redis(redis_url, pools_s)
    meta = vr.merge_meta(chm, rm)

    ts_lo = min(int(r.get("timestamp", 0) or 0) for r in samples) // 1000
    ts_hi = max(int(r.get("timestamp", 0) or 0) for r in samples) // 1000 + exit_horizon
    rpc = le.RPC(rpc_url)
    bn_arr, ts_arr = le.build_ts_grid(rpc, ts_lo, ts_hi)

    rows_out: list[dict[str, Any]] = []
    for r in samples:
        pool = (r.get("pool_address") or "").strip().lower()
        raw_pt = (r.get("pool_type") or "").lower()
        t0c = _norm_topic(r.get("topic0", ""))
        ts = int(r.get("timestamp", 0) or 0)
        m = meta.get(pool) or {}
        ptype = (m.get("pool_type") or raw_pt).lower()
        cr = int(m.get("created_block", 0) or 0)
        sig, exs = ts // 1000, ts // 1000 + exit_horizon
        entry_b = le.ts_to_block(bn_arr, ts_arr, sig)
        exit_b = le.ts_to_block(bn_arr, ts_arr, exs)
        t0d, t1d = int(m.get("t0_dec", 0) or 0), int(m.get("t1_dec", 0) or 0)
        rec: dict[str, Any] = {
            "pool_address": pool,
            "token_address": (r.get("token_address") or "").lower(),
            "pool_type_row": raw_pt,
            "topic0": t0c,
            "final_score": float(r.get("final_score", 0) or 0),
            "entry_block": entry_b,
            "exit_block": exit_b,
            "created_block": cr,
            "pool_age_blocks_at_entry": (entry_b - cr) if cr > 0 else None,
            "t0_decimals": t0d,
            "t1_decimals": t1d,
        }
        if raw_pt not in ("v2", "v3") or t0c.lower() not in vr.SWAP_TOPICS:
            rec["skip"] = "precheck"
            rec["triage"] = "FILTER_RULE_NEEDED"
            rows_out.append(rec)
            continue
        t0, t1 = m.get("t0") or "", m.get("t1") or ""
        if ptype not in ("v2", "v3") or not t0 or not t1 or (ptype == "v3" and int(m.get("fee_bps", 0) or 0) <= 0):
            rec["skip"] = "bad_meta"
            rec["triage"] = "LABELER_BUG_SUSPECTED"
            rows_out.append(rec)
            continue
        if t0d > 36 or t1d > 36 or (t0d == 0 and t1d == 0):
            rec["skip"] = "bad_decimals"
            rec["triage"] = "FILTER_RULE_NEEDED"
            rows_out.append(rec)
            continue
        if cr <= 0 or entry_b < cr or exit_b <= entry_b:
            rec["skip"] = "time_or_created"
            rec["triage"] = "TRUE_UNEXECUTABLE_OR_TOXIC_SAMPLE"
            rows_out.append(rec)
            continue
        if ptype == "v2":
            detail = run_v2_path(rpc, pool, weth, notional, entry_b, exit_b)
        else:
            detail = run_v3_path(
                rpc, pool, t0, t1, int(m.get("fee_bps", 30) or 30), weth, notional, entry_b, exit_b,
            )
        rec["path"] = detail
        rows_out.append(rec)

    # Also run stock labeler for every row to compare (fast path)
    stock_reasons: list[str] = []
    for r, built in zip(samples, rows_out, strict=True):
        if built.get("skip"):
            stock_reasons.append(f"skip_{built.get('skip')}")
            continue
        pool = (r.get("pool_address") or "").lower()
        ptype = (meta.get(pool) or {}).get("pool_type", (r.get("pool_type") or "")).lower()
        t0, t1 = (meta.get(pool) or {}).get("t0", ""), (meta.get(pool) or {}).get("t1", "")
        ts = int(r.get("timestamp", 0) or 0)
        entry_b = built["entry_block"]
        exit_b = built["exit_block"]
        m2 = {
            "pool_type": ptype, "t0": t0, "t1": t1, "fee_bps": int((meta.get(pool) or {}).get("fee_bps", 30) or 30),
        }
        try:
            lb, nt, reason = le.label_one(rpc, pool, m2, weth, notional, entry_b, exit_b)
            stock_reasons.append(reason)
        except Exception:
            stock_reasons.append("fail_rpc_exception")

    def _is_skip(s: str) -> bool:
        return s.startswith("skip_")

    def _dist(vals: list[float]) -> dict[str, Any]:
        vals = [v for v in vals if v == v]  # drop nan
        if not vals:
            return {"n": 0}
        return {
            "n": len(vals),
            "min": min(vals),
            "max": max(vals),
            "mean": float(statistics.mean(vals)),
        }

    # ---- aggregates ----
    ok_n = sum(1 for s in stock_reasons if s == "executable")
    sclip = [rows_out[i] for i, s in enumerate(stock_reasons) if s == "fail_sanity_clip"]
    qex = [rows_out[i] for i, s in enumerate(stock_reasons) if s == "executable_exit_drained"]
    qen = [rows_out[i] for i, s in enumerate(stock_reasons) if s in ("fail_entry_quote_zero", "fail_v3_entry_zero")]

    def pool_type_c(rows: list[dict]) -> dict[str, int]:
        c = Counter()
        for x in rows:
            pt = (x.get("path") or {}).get("pool_type") or x.get("pool_type_row", "")
            c[pt] += 1
        return dict(c)

    sanity_sub = Counter()
    for x in sclip:
        sd = (x.get("path") or {}).get("sanity_detail", "unknown")
        sanity_sub[sd] += 1

    triage_c = Counter()
    for x in rows_out:
        t = (x.get("path") or {}).get("triage") or x.get("triage")
        if t:
            triage_c[str(t)] += 1

    # success vs fail compare by final_score
    succ_fs = [float(rows_out[i].get("final_score", 0) or 0) for i, s in enumerate(stock_reasons) if s == "executable"]
    fail_fs = [
        float(rows_out[i].get("final_score", 0) or 0)
        for i, s in enumerate(stock_reasons)
        if s != "executable" and not _is_skip(s)
    ]

    def _mean(a: list[float]) -> Optional[float]:
        return sum(a) / len(a) if a else None

    # quote exit forensics
    qex_pools = Counter(x.get("pool_address", "") for x in qex)
    qex_exit_notes = Counter()
    for x in qex:
        pth = x.get("path") or {}
        for k in ("exit_liquidity_note", "exit_note"):
            v = pth.get(k)
            if v:
                qex_exit_notes[v] += 1
        v2l = pth.get("entry_reserves_weth")
        v3e = pth.get("exit_quoter") or {}
        if pth.get("pool_type") == "v2" and v2l is not None and v2l < int(os.environ.get("LABEL_MIN_WETH_WEI", str(int(0.1 * 1e18)))):
            qex_exit_notes["v2_exit_low_weth_proxy"] += 1

    # 20 representative failure rows
    fail_idx = [i for i, s in enumerate(stock_reasons) if s != "executable" and not _is_skip(s)][:20]
    rep20 = []
    for i in fail_idx:
        b = rows_out[i]
        m = meta.get(b["pool_address"], {})
        pth = b.get("path") or {}
        rep20.append({
            "pool": b.get("pool_address"),
            "pool_type": pth.get("pool_type") or b.get("pool_type_row"),
            "t0": m.get("t0"), "t1": m.get("t1"), "weth_leg": pth.get("side_weth_leg") or (
                "token0" if m.get("t0") == weth else "token1" if m.get("t1") == weth else "?"
            ),
            "target_token": pth.get("target_token") or (b.get("token_address") or ""),
            "stock_reason": stock_reasons[i],
            "net_before_sanity": pth.get("net_return_before_sanity"),
            "sanity_detail": pth.get("sanity_detail"),
            "entry_in_weth_wei": notional,
            "entry_out_tokens": pth.get("entry_quote_out_tokens"),
            "exit_out_weth": pth.get("exit_quote_out_weth"),
        })

    def cls_summary(name: str, idxs: list[int]) -> dict[str, Any]:
        dec0 = 0
        for i in idxs:
            b = rows_out[i]
            if int(b.get("t0_decimals", 0) or 0) == 0 or int(b.get("t1_decimals", 0) or 0) == 0:
                dec0 += 1
        return {"n": len(idxs), "mean_final_score": _mean([float(rows_out[i].get("final_score", 0) or 0) for i in idxs]), "rows_with_zero_dec": dec0}

    ok_idx = [i for i, s in enumerate(stock_reasons) if s == "executable"]
    bad_idx = [i for i, s in enumerate(stock_reasons) if s != "executable" and not _is_skip(s)]

    report: dict[str, Any] = {
        "meta": {
            "patch_d2_version": 1,
            "reference_patch_d": {
                "sampled_rows": len(samples),
                "executable_success_rows_this_run": ok_n,
                "executable_success_rate_this_run": round(ok_n / max(len(samples), 1), 6),
                "note": "historical Patch D reported 255/400=0.6375; this_run should match if seeds match",
            },
            "stratify": {"max_per_pool": max_per, "top_pools": top_tier, "max_total": max_total, "seed": seed},
        },
        "A_fail_sanity_clip": {
            "n_stock_labeler": len(sclip),
            "sanity_detail_counts": dict(sanity_sub),
            "by_pool_type": pool_type_c(sclip),
            "top_pools_sanity": [[p, n] for p, n in Counter(x.get("pool_address", "") for x in sclip).most_common(15)],
            "by_token_address": [[a, n] for a, n in Counter(x.get("token_address", "") for x in sclip).most_common(15)],
            "block_distributions": {
                "entry_block": {
                    "min": min((x.get("entry_block", 0) for x in sclip), default=0),
                    "max": max((x.get("entry_block", 0) for x in sclip), default=0),
                },
                "pool_age_blocks_at_entry": {
                    "min": min(
                        (x.get("pool_age_blocks_at_entry") for x in sclip if x.get("pool_age_blocks_at_entry") is not None),
                        default=0,
                    ),
                    "max": max(
                        (x.get("pool_age_blocks_at_entry") for x in sclip if x.get("pool_age_blocks_at_entry") is not None),
                        default=0,
                    ),
                },
            },
            "note": "sanity_detail maps to _sanity_clip: too_high_roi (net>10), too_low_roi (net<-1), nan_roi",
            "gross_return_and_net_for_sanity_rows": {
                "gross_return": _dist([float((x.get("path") or {}).get("gross_return")) for x in sclip if (x.get("path") or {}).get("gross_return") is not None]),
                "net_before_sanity": _dist([float((x.get("path") or {}).get("net_return_before_sanity")) for x in sclip if (x.get("path") or {}).get("net_return_before_sanity") is not None]),
            },
        },
        "B_quote_exit_failed": {
            "n": len(qex),
            "top_pools": [[p, n] for p, n in qex_pools.most_common(15)],
            "exit_path_notes": dict(qex_exit_notes),
            "interpretation": {
                "should_count_as_negative_executable_label": "often_yes_on_true_zero_exit_quote",
                "unlabelable_when": "v3_quoter_revert_may_indicate_unsupported_path_or_toxic_token",
            },
        },
        "C_side_direction_sample_20": rep20,
        "D_decimals": {
            "fail_sample_zero_dec_leg": cls_summary("fail", bad_idx).get("rows_with_zero_dec"),
            "all_fail_t0t1_distributions": {
                "t0": dict(Counter(str(rows_out[i].get("t0_decimals", 0)) for i in bad_idx)),
                "t1": dict(Counter(str(rows_out[i].get("t1_decimals", 0)) for i in bad_idx)),
            },
        },
        "E_success_vs_fail": {
            "success": cls_summary("ok", ok_idx),
            "fail": cls_summary("fail", bad_idx),
            "by_pool_type_stock": {
                "success": dict(Counter(rows_out[i].get("pool_type_row") for i in ok_idx)),
                "fail": dict(Counter(rows_out[i].get("pool_type_row") for i in bad_idx)),
            },
        },
        "F_failure_classification_counts": {
            "LABELER_BUG_SUSPECTED": int(triage_c.get("LABELER_BUG_SUSPECTED", 0)),
            "TRUE_UNEXECUTABLE_OR_TOXIC_SAMPLE": int(triage_c.get("TRUE_UNEXECUTABLE_OR_TOXIC_SAMPLE", 0)),
            "FILTER_RULE_NEEDED": int(triage_c.get("FILTER_RULE_NEEDED", 0)),
            "ok_executable_path": int(triage_c.get("ok", 0)),
        },
        "F_triaging_rules_applied": {
            "per_path_triage": dict(triage_c),
        },
        "G_counts": {
            "stock_label_executable": ok_n,
            "stock_fail_sanity_clip": len(sclip),
            "stock_exit_drained": len(qex),
            "stock_entry_zero": len(qen),
        },
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    with out_json.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    log.info("Wrote %s", out_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
