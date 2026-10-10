"""
Run-004 Patch D3: V3 fee-tier and Uniswap QuoterV2 path audit (read-only).

Does not modify label_dashcam_executable.py. Reuses Patch D / D2 stratified sample;
filters to pool_type=v3 trainable rows only.

Env: RUN004_CANDIDATES_CSV, RUN004_D3_OUT_JSON, CLICKHOUSE_*, REDIS_URL, CHAIN_ID,
     LABEL_RPC_URL or EVM_HTTP_URL, same RUN004_D_* stratify vars as validate.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import label_dashcam_executable as le
import validate_run004_executable_labels as vr

logging.basicConfig(level=logging.INFO, format="%(asctime)sZ %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
log = logging.getLogger("audit_v3")

WETH = le.WETH_BASE
ROUND_TRIP = le.ROUND_TRIP_COST
QUOTER = le.QUOTER_V2_BASE
_SEL = "0xc6a5026a"


def _pad32(h: str) -> str:
    return h.lstrip("0x").lower().rjust(64, "0")


def _addr(a: str) -> str:
    return _pad32(a.lower().lstrip("0x"))


def _u(n: int) -> str:
    return _pad32(hex(n)[2:])


def quoter_call(
    rpc: le.RPC, token_in: str, token_out: str, amount_in: int, fee_param: int, block: int,
) -> dict[str, Any]:
    data = _SEL + _addr(token_in) + _addr(token_out) + _u(amount_in) + _u(fee_param) + _u(0)
    out: dict[str, Any] = {"ok": False, "amount": 0, "error": None, "fee_param": fee_param}
    try:
        r = rpc.call("eth_call", [{"to": QUOTER, "data": data}, hex(int(block))])
    except Exception as e:
        out["error"] = str(e)[:300]
        return out
    if isinstance(r, dict) and r.get("error"):
        err = r["error"]
        out["error"] = (err.get("message", str(err)) if isinstance(err, dict) else str(err))[:300]
        return out
    raw = (r.get("result") or "0x").lstrip("0x")
    if len(raw) < 64:
        out["error"] = "short_return"
        return out
    out["amount"] = int(raw[0:64], 16)
    out["ok"] = True
    return out


def classify_fee_storage(fee_bps: int) -> str:
    if fee_bps in (500, 3000, 10000):
        return "stored_as_uniswap_fee_tier_uint24"
    if fee_bps in (1, 5, 10, 30, 60, 100, 500):
        return "stored_as_bps_style_small_int"
    if 100 <= fee_bps < 500 and fee_bps not in (100,):
        return "ambiguous_mid_range"
    if fee_bps > 10000:
        return "suspicious_gt_10000"
    return "other"


def amount_flags(amount: int) -> list[str]:
    flags: list[str] = []
    if amount > 2**200:
        flags.append("amount_out_uint256_extreme")
    if amount > 10**35:
        flags.append("amount_out_very_large")
    return flags


def implied_gross_net(
    weth_in: int, weth_out: int, token_dec: int,
) -> tuple[float, float, float, list[str]]:
    """Rough implied: WETH per 1 token unit (notional pricing). Flags if unphysical."""
    flags: list[str] = []
    if weth_in <= 0:
        return 0.0, 0.0, 0.0, ["zero_in"]
    gross = weth_out / weth_in - 1.0
    net = gross - ROUND_TRIP
    if abs(gross) > 1e6 or (weth_out > 0 and weth_out > weth_in * 10**15):
        flags.append("price_ratio_unphysical")
    return gross, net, (weth_out / weth_in) if weth_in else 0.0, flags


def load_factory_ch(
    ch_url: str, ch_db: str, ch_user: str, ch_pw: str, chain_id: int, pools: set[str],
) -> dict[str, str]:
    if not pools:
        return {}
    pl = ",".join(f"'{p}'" for p in sorted(pools))
    sql = f"""
        SELECT lower(pool_address) AS pool, lower(argMax(factory_address, discovered_at)) AS fac
        FROM eatlq.discovered_pools
        WHERE chain_id = {int(chain_id)} AND lower(pool_address) IN ({pl})
        GROUP BY pool
    """
    rows = vr.ch_query(ch_url, ch_db, ch_user, ch_pw, sql)
    return {r["pool"]: (r.get("fac") or "") for r in rows}


def build_fee_candidates(fb: int) -> list[tuple[str, int]]:
    raw: list[tuple[str, int]] = [
        ("tier_500", 500),
        ("tier_3000", 3000),
        ("tier_10000", 10000),
        ("labeler_fee_bps_times_100", fb * 100),
        ("metadata_fee_bps_raw_as_param", fb),
    ]
    seen: set[int] = set()
    out: list[tuple[str, int]] = []
    for name, v in raw:
        if v < 0 or v > 2**24 - 1:
            continue
        if v not in seen:
            seen.add(v)
            out.append((name, v))
    return out


def main() -> int:
    out_path = Path(os.environ.get("RUN004_D3_OUT_JSON", str(REPO / "mvp" / "diagnostics" / "run004_patch_d3_v3_quoter_audit.json")))
    csv_path = Path(os.environ.get("RUN004_CANDIDATES_CSV", str(REPO / "logs" / "run004_candidates_swaponly_full.csv")))
    ch_url = os.environ.get("CLICKHOUSE_URL", "http://clickhouse:8123")
    ch_db = os.environ.get("CLICKHOUSE_DB", "eatlq")
    ch_u = os.environ.get("CLICKHOUSE_USER", "default")
    ch_pw = os.environ.get("CLICKHOUSE_PASSWORD", "")
    chain_id = int(os.environ.get("CHAIN_ID", "8453"))
    redis_url = (os.environ.get("REDIS_URL") or "").strip()
    rpc_url = (os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL") or "").strip()
    if not rpc_url:
        log.error("RPC required")
        return 1

    if not csv_path.is_file():
        log.error("missing %s", csv_path)
        return 1

    max_per = int(os.environ.get("RUN004_D_MAX_PER_POOL", "5") or 5)
    top_tier = int(os.environ.get("RUN004_D_TOP_POOLS", "60") or 60)
    max_total = int(os.environ.get("RUN004_D_MAX_SAMPLE_ROWS", "400") or 400)
    seed = int(os.environ.get("RUN004_D_STRATIFY_SEED", "42") or 42)
    exit_horizon = int(os.environ.get("LABEL_EXIT_HORIZON_SEC", "1800"))
    notional = int(os.environ.get("LABEL_NOTIONAL_WEI", str(int(0.0005e18))))

    # Same pool as Patch D / D2: stratify on ALL trainable v2+v3, then take v3-only rows
    trainable: list[dict[str, Any]] = []
    with csv_path.open("r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("candidate_status") or "") != "trainable_candidate":
                continue
            if (row.get("pool_type") or "").lower() not in ("v2", "v3"):
                continue
            trainable.append(row)
    if not trainable:
        log.error("no trainable v2/v3 rows")
        return 1

    all_sampled = vr.stratified_sample(trainable, max_per, top_tier, max_total, seed)
    samples = [r for r in all_sampled if (r.get("pool_type") or "").lower() == "v3"]
    log.info("D2-matched v3 rows in sample: %d (from %d total stratified)", len(samples), len(all_sampled))
    if not samples:
        log.error("no v3 rows in stratified sample")
        return 1
    pools = {r.get("pool_address", "").lower() for r in samples}
    chm = vr.load_pool_meta_full(ch_url, ch_db, ch_u, ch_pw, chain_id, pools)
    rm = vr.load_pool_meta_redis(redis_url, pools)
    meta = vr.merge_meta(chm, rm)
    factories = load_factory_ch(ch_url, ch_db, ch_u, ch_pw, chain_id, pools)

    ts_lo = min(int(r.get("timestamp", 0) or 0) for r in samples) // 1000
    ts_hi = max(int(r.get("timestamp", 0) or 0) for r in samples) // 1000 + exit_horizon
    rpc = le.RPC(rpc_url)
    bn, ts_a = le.build_ts_grid(rpc, ts_lo, ts_hi)

    per_row: list[dict[str, Any]] = []
    fee_tier_wins = Counter()
    conclusion_votes = Counter()

    for r in samples:
        pool = (r.get("pool_address") or "").lower()
        m = meta.get(pool) or {}
        fb = int(m.get("fee_bps", 0) or 0)
        t0, t1 = (m.get("t0") or "").lower(), (m.get("t1") or "").lower()
        pt = (m.get("pool_type") or "v3").lower()
        ts = int(r.get("timestamp", 0) or 0)
        entry_b = le.ts_to_block(bn, ts_a, ts // 1000)
        exit_b = le.ts_to_block(bn, ts_a, ts // 1000 + exit_horizon)
        token = t1 if t0 == WETH else t0
        td = int(m.get("t1_dec", 0) or 0) if token == t1 else int(m.get("t0_dec", 0) or 0)
        if td <= 0:
            td = 18

        fee_q_labeler = fb * 100
        row_a: dict[str, Any] = {
            "pool_address": pool,
            "factory_address": factories.get(pool, ""),
            "pool_type_metadata": pt,
            "fee_bps_metadata": fb,
            "fee_storage_class": classify_fee_storage(fb),
            "fee_q_passed_by_current_labeler": fee_q_labeler,
            "entry_block": entry_b,
            "exit_block": exit_b,
            "token0": t0,
            "token1": t1,
            "target_token": token,
        }

        cand_results: dict[str, Any] = {}
        best_reasonable: str | None = None
        for name, fee_p in build_fee_candidates(fb):
            e = quoter_call(rpc, WETH, token, notional, fee_p, entry_b)
            if not e["ok"] or e["amount"] <= 0:
                cand_results[name] = {
                    "fee_param": fee_p, "entry_ok": False, "entry_error": e.get("error"),
                }
                continue
            tok_amt = e["amount"]
            x = quoter_call(rpc, token, WETH, tok_amt, fee_p, exit_b)
            ent_fl = amount_flags(tok_amt)
            ex_fl = amount_flags(x["amount"]) if x["ok"] else []
            gross, net, ratio, pfl = implied_gross_net(notional, x["amount"] if x["ok"] else 0, td)
            all_fl = ent_fl + ex_fl + pfl
            ok_phys = x["ok"] and x["amount"] > 0 and "price_ratio_unphysical" not in all_fl and not ent_fl and not ex_fl
            cand_results[name] = {
                "fee_param": fee_p,
                "entry_ok": True,
                "entry_amount_out": tok_amt,
                "exit_ok": x["ok"],
                "exit_amount_out": x["amount"] if x["ok"] else 0,
                "exit_error": x.get("error"),
                "gross_return": gross,
                "net_return_before_sanity": net,
                "flags": all_fl,
                "fee_candidate_passed": ok_phys and -1.0 <= net <= 10.0,
            }
            if ok_phys and -1.0 <= net <= 10.0 and best_reasonable is None:
                best_reasonable = name

        row_a["fee_candidate_results"] = cand_results
        row_a["first_reasonable_fee_candidate"] = best_reasonable

        lab = cand_results.get("labeler_fee_bps_times_100") or {}
        t3 = cand_results.get("tier_3000") or {}
        l_pass = bool(lab.get("fee_candidate_passed"))
        t3_pass = bool(t3.get("fee_candidate_passed"))
        if l_pass:
            row_a["row_conclusion_e"] = "V3_OK_WITH_LABELER_FEE"
            conclusion_votes["V3_OK_OR_PHYSICAL_WITH_LABELER"] += 1
        elif t3_pass and not l_pass:
            row_a["row_conclusion_e"] = "V3_FEE_UNIT_BUG_LIKELY"
            conclusion_votes["V3_FEE_UNIT_BUG_LIKELY"] += 1
        elif not l_pass and not t3_pass:
            std_names = ("tier_500", "tier_3000", "tier_10000")
            if all(
                not (cand_results.get(tn) or {}).get("exit_ok")
                for tn in std_names
            ):
                row_a["row_conclusion_e"] = "V3_QUOTER_INCOMPATIBLE"
                conclusion_votes["V3_QUOTER_INCOMPATIBLE"] += 1
            elif (lab.get("entry_amount_out") or 0) > 2**200:
                row_a["row_conclusion_e"] = "V3_TRUE_UNEXECUTABLE"
                conclusion_votes["V3_TRUE_UNEXECUTABLE"] += 1
            else:
                row_a["row_conclusion_e"] = "INCONCLUSIVE"
                conclusion_votes["INCONCLUSIVE"] += 1

        if pt != "v3" and pt:
            row_a["misclass_note"] = f"metadata_pool_type={pt}_expected_v3_row"
            conclusion_votes["V3_POOL_METADATA_MISCLASSIFIED"] += 1

        if best_reasonable:
            fee_tier_wins[best_reasonable] += 1

        per_row.append(row_a)

    # aggregate
    pt_dist = Counter(r.get("pool_type_metadata", "") for r in per_row)
    fac_dist = Counter(r.get("factory_address", "")[:12] for r in per_row if r.get("factory_address"))

    std_tier_keys = ("tier_500", "tier_3000", "tier_10000")
    labeler_vs_tier = {
        "labeler_works_physical": 0,
        "tier_500_works_physical": 0,
        "tier_3000_works_physical": 0,
        "tier_10000_works_physical": 0,
        "only_tier_works": 0,
    }
    for r in per_row:
        cr = r.get("fee_candidate_results", {})
        lq = cr.get("labeler_fee_bps_times_100") or {}
        t5 = cr.get("tier_500") or {}
        t3 = cr.get("tier_3000") or {}
        t10 = cr.get("tier_10000") or {}
        l_ok = bool(lq.get("fee_candidate_passed"))
        ok5 = bool(t5.get("fee_candidate_passed"))
        ok3 = bool(t3.get("fee_candidate_passed"))
        ok10 = bool(t10.get("fee_candidate_passed"))
        if l_ok:
            labeler_vs_tier["labeler_works_physical"] += 1
        if ok5:
            labeler_vs_tier["tier_500_works_physical"] += 1
        if ok3:
            labeler_vs_tier["tier_3000_works_physical"] += 1
        if ok10:
            labeler_vs_tier["tier_10000_works_physical"] += 1
        if (ok5 or ok3 or ok10) and not l_ok:
            labeler_vs_tier["only_tier_works"] += 1

    n_rows = len(per_row)
    rows_any_std_exit = 0
    rows_all_std_exit_fail = 0
    rows_any_full_pass = 0
    cands = (*std_tier_keys, "labeler_fee_bps_times_100", "metadata_fee_bps_raw_as_param")
    for r in per_row:
        cr = r.get("fee_candidate_results", {})
        if any((cr.get(tn) or {}).get("exit_ok") for tn in std_tier_keys):
            rows_any_std_exit += 1
        if all(not (cr.get(tn) or {}).get("exit_ok") for tn in std_tier_keys):
            rows_all_std_exit_fail += 1
        if any((cr.get(n) or {}).get("fee_candidate_passed") for n in cands):
            rows_any_full_pass += 1

    e_agg = {
        "rows_n": n_rows,
        "rows_with_any_std_tier_exit_ok": rows_any_std_exit,
        "rows_with_all_std_tier_exit_fail": rows_all_std_exit_fail,
        "rows_with_any_fee_candidate_passed": rows_any_full_pass,
    }

    # Top-level E: FEE if many "only a std tier" passes; MISCLASS for aero; TRUE_UN if many full-pass;
    # QUOTER only if <20% of rows ever get a Uniswap 500/3000/10000 exit (QuoterV2 does not return exit);
    # if many std exits but 0 full-pass — INCONCLUSIVE (QuoterV2 can work, label path/sanity is wrong).
    global_conclusion: str = "INCONCLUSIVE"
    if n_rows and labeler_vs_tier["only_tier_works"] * 3 > n_rows:
        global_conclusion = "V3_FEE_UNIT_BUG_LIKELY"
    elif n_rows and (pt_dist.get("aero_cl", 0) + pt_dist.get("slipstream", 0)) / n_rows > 0.2:
        global_conclusion = "V3_POOL_METADATA_MISCLASSIFIED"
    elif n_rows and rows_any_full_pass * 2 > n_rows:
        global_conclusion = "V3_TRUE_UNEXECUTABLE"
    elif n_rows and rows_any_std_exit * 5 < n_rows and not rows_any_full_pass:
        global_conclusion = "V3_QUOTER_INCOMPATIBLE"

    e_agg["global_rationale"] = (
        f"{rows_all_std_exit_fail} rows: all 500/3000/10000 exit failed; {rows_any_std_exit} rows: "
        f"at least one std exit ok; {rows_any_full_pass} rows: any fee_candidate_passed; "
        "per-row E tags in E_row_conclusion_votes."
    )

    c_hits: dict[str, dict[str, int]] = {}
    for r in per_row:
        for k, v in (r.get("fee_candidate_results") or {}).items():
            if not isinstance(v, dict):
                continue
            d = c_hits.setdefault(k, {"entry_ok": 0, "exit_ok": 0})
            d["entry_ok"] += 1 if v.get("entry_ok") else 0
            d["exit_ok"] += 1 if v.get("exit_ok") else 0

    report: dict[str, Any] = {
        "meta": {
            "quoter_v2_base": QUOTER,
            "chain_id": chain_id,
            "v3_sample_rows": len(per_row),
            "stratify": {"max_per_pool": max_per, "top_pools": top_tier, "max_total": max_total, "seed": seed},
            "patch_d2_reference": {
                "v2_executable_success_rate_approx": 0.951,
                "v3_executable_success_0_of_125_note": "from forensics doc; D3 only audits v3 subset of same 400-row design",
            },
        },
        "A_fee_audit": {
            "per_row": per_row,
            "fee_storage_class_distribution": dict(Counter(r.get("fee_storage_class") for r in per_row)),
            "first_reasonable_fee_candidate_wins": dict(fee_tier_wins),
            "labeler_vs_standard_tiers": labeler_vs_tier,
        },
        "B_quoter_compatibility": {
            "pool_type_distribution": dict(pt_dist),
            "factory_prefix_top": dict(fac_dist.most_common(12)),
            "note": "QUOTER_V2_BASE is Uniswap V3 QuoterV2 on Base; non-Uni factories may need a different quoter or path",
        },
        "C_amount_summary": {
            "per_candidate_entry_exit_hits": c_hits,
            "per_row": "gross_return net_return flags fee_candidate_passed in fee_candidate_results",
        },
        "D_v2_control": {
            "patch_d2_stated_v2_success_fraction_of_v2_rows": "~0.95",
            "no_v2_rerun": True,
        },
        "E_global_conclusion": global_conclusion,
        "E_aggregate": e_agg,
        "E_row_conclusion_votes": dict(conclusion_votes),
        "F_recommendations_text": [
            "If V3_FEE_UNIT_BUG_LIKELY: normalize fee — store Uniswap fee tier (500/3000/10000) OR bps*100 consistently; align with on-chain pool.fee()",
            "If V3_QUOTER_INCOMPATIBLE: do not use Uni QuoterV2 for those factories; pool-specific quoter or simulation",
            "Consider v2-only Patch D rerun before v3 labeler investment",
        ],
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    log.info("Wrote %s rows=%s conclusion=%s", out_path, len(per_row), global_conclusion)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
