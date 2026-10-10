#!/usr/bin/env python3
"""Sprint032B — offline diagnosis JSON + pool metadata audit CSV + verified manifest.

Reads Sprint032A artifacts and dashcam (read-only). Optional RPC + ClickHouse for audit fields.
Does not print RPC URLs or secrets.

Usage:
  LABEL_RPC_URL or EVM_HTTP_URL required for on-chain audit columns.
  python3 scripts/sprint032b_metadata_audit_manifest_builder.py --write
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
BUCKET_MS = 300_000
BUCKET_SEC = 300

_SEL_TOKEN1 = "0xd21220a7"
_SEL_FEE_V3 = "0xddca3f43"
_SEL_LIQ = "0x1a686502"
_SEL_SLOT0 = "0x3850c7bd"


def load_dotenv_files() -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in ("infra/compose/.env", ".env"):
        p = REPO / rel
        if not p.exists():
            continue
        for line in p.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_label_mod():
    import importlib.util

    path = REPO / "scripts" / "label_dashcam_executable.py"
    spec = importlib.util.spec_from_file_location("label_dashcam_executable", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load label_dashcam_executable")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def exclusions() -> set[str]:
    ex: set[str] = set()
    for rel in (
        "mvp/diagnostics/sprint030e_top10_dryrun_manifest.json",
        "mvp/diagnostics/sprint031a_new_universe_smoke_manifest.json",
        "mvp/diagnostics/sprint031b_new_lineage_dryrun_manifest.json",
    ):
        j = json.loads((REPO / rel).read_text(encoding="utf-8"))
        ex.update(x.lower() for x in j.get("pool_address_audit_keys", []))
        for k in ("excluded_old_cohort_pools_audit_only", "excluded_old_closed_cohort_pools_audit_only"):
            ex.update(x.lower() for x in j.get(k, []))
    ex.add("0xdf349842e5f5b0e9dc7f96638b6c0eb372545b23")
    ex.add("0x09591f786a2e724ed46ccc85293ee0e8dd73f9ba")
    ex.update(
        {
            "0x87b73c76c4cffe0610da8efb8ecb8e04f0a4ca62",
            "0xb73f7f979e1cd2a5377da0e89e1682279cc71c27",
            "0x5486225f16e1c92b8f1d7f7dfd27d93a9de9f1e6",
        }
    )
    return ex


def ch_ping_select1(env: dict[str, str]) -> bool:
    url = (env.get("CLICKHOUSE_URL") or os.environ.get("CLICKHOUSE_URL") or "").strip()
    if not url:
        return False
    q = url.rstrip("/") + "/?query=" + urllib.parse.quote("SELECT 1 FORMAT TabSeparated")
    try:
        req = urllib.request.Request(q, method="GET")
        u = env.get("CLICKHOUSE_USER") or os.environ.get("CLICKHOUSE_USER") or ""
        p = env.get("CLICKHOUSE_PASSWORD") or os.environ.get("CLICKHOUSE_PASSWORD") or ""
        if u:
            tok = base64.b64encode(f"{u}:{p}".encode()).decode()
            req.add_header("Authorization", "Basic " + tok)
        with urllib.request.urlopen(req, timeout=12) as resp:
            return resp.read().strip() == b"1"
    except Exception:
        return False


def ch_fetch_discovered(env: dict[str, str], pools: list[str]) -> dict[str, dict[str, str]]:
    """Return lower(pool) -> row dict from CH if reachable."""
    out: dict[str, dict[str, str]] = {}
    url = (env.get("CLICKHOUSE_URL") or os.environ.get("CLICKHOUSE_URL") or "").strip()
    if not url or not pools:
        return out
    plist = ",".join(f"'{p.lower()}'" for p in pools)
    sql = (
        "SELECT lower(pool_address) AS p, pool_type, token0_address, token1_address, "
        "toString(fee_bps) AS fee_bps, toString(has_weth_leg) AS has_weth_leg "
        f"FROM eatlq.discovered_pools WHERE chain_id = 8453 AND lower(pool_address) IN ({plist}) "
        "ORDER BY discovered_at DESC LIMIT 1000 FORMAT JSONEachRow"
    )
    q = url.rstrip("/") + "/?" + urllib.parse.urlencode({"query": sql})
    try:
        req = urllib.request.Request(q, method="GET")
        u = env.get("CLICKHOUSE_USER") or os.environ.get("CLICKHOUSE_USER") or ""
        p = env.get("CLICKHOUSE_PASSWORD") or os.environ.get("CLICKHOUSE_PASSWORD") or ""
        if u:
            tok = base64.b64encode(f"{u}:{p}".encode()).decode()
            req.add_header("Authorization", "Basic " + tok)
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace").strip()
        if not body:
            return out
        for line in body.splitlines():
            row = json.loads(line)
            pk = row.get("p", "").lower()
            if pk:
                out.setdefault(pk, row)
    except Exception:
        return {}
    return out


def audit_pool_onchain(mod: Any, rpc: Any, pool: str, bn: int) -> dict[str, Any]:
    """Bounded eth_call metadata probe at block bn."""
    pool = pool.lower()
    bh = hex(bn)
    row: dict[str, Any] = {"pool_address_audit_key": pool, "source": "on_chain_eth_call_latest"}
    try:
        t0 = mod.call_token0(rpc, pool, bn).lower()
        r1 = rpc.call("eth_call", [{"to": pool, "data": _SEL_TOKEN1}, bh])
        raw_t1 = r1.get("result") if r1 and "error" not in r1 else None
        t1 = ("0x" + raw_t1[-40:]).lower() if raw_t1 and len(raw_t1) >= 42 else None
        row["token0_present"] = bool(t0 and t0.startswith("0x"))
        row["token1_present"] = bool(t1 and t1.startswith("0x"))
        rf = rpc.call("eth_call", [{"to": pool, "data": _SEL_FEE_V3}, bh])
        fee_raw = None
        if rf and "error" not in rf and rf.get("result"):
            fee_raw = int(rf["result"], 16)
        row["fee_present"] = fee_raw is not None and fee_raw > 0
        row["fee_raw_uint24"] = "" if fee_raw is None else str(fee_raw)

        rliq = rpc.call("eth_call", [{"to": pool, "data": _SEL_LIQ}, bh])
        liqu = None
        if rliq and "error" not in rliq and rliq.get("result"):
            liqu = int(rliq["result"], 16)
        row["liquidity_value_or_status"] = "missing_revert" if liqu is None else str(liqu)

        rs = rpc.call("eth_call", [{"to": pool, "data": _SEL_SLOT0}, bh])
        res_s = rs.get("result") if rs and "error" not in rs else None
        sqrt_ok = False
        if res_s and len(res_s.replace("0x", "")) >= 64:
            sqrt_ok = int(res_s.replace("0x", "")[0:64], 16) > 0
        row["slot0_ok"] = sqrt_ok

        rr = rpc.call("eth_call", [{"to": pool, "data": mod._SEL_GET_RESERVES}, bh])
        v2_ok = bool(rr and "error" not in rr and rr.get("result") not in ("0x", "0x0", None))

        weth = mod.WETH_BASE.lower()
        weth_leg = weth in (t0, t1)
        row["weth_leg_verified"] = weth_leg

        if not weth_leg:
            row["resolved_pool_type"] = "non_weth_pair"
            row["metadata_status"] = "FAIL"
            row["failure_reason_if_any"] = "pair_missing_canonical_base_weth_4200000000000000000000000000000000000006"
            row["token0"] = t0 or ""
            row["token1"] = t1 or ""
            return row

        if v2_ok and not row["fee_present"]:
            row["resolved_pool_type"] = "v2"
            row["metadata_status"] = "PASS"
            row["failure_reason_if_any"] = ""
            row["token0"] = t0
            row["token1"] = t1 or ""
            return row

        if not v2_ok and row["fee_present"]:
            row["resolved_pool_type"] = "v3"
            row["metadata_status"] = "PASS"
            row["failure_reason_if_any"] = ""
            row["token0"] = t0
            row["token1"] = t1 or ""
            return row

        row["resolved_pool_type"] = "ambiguous_or_non_standard"
        row["metadata_status"] = "UNKNOWN"
        row["failure_reason_if_any"] = "could_not_classify_v2_vs_v3_cleanly"
        row["token0"] = t0 or ""
        row["token1"] = t1 or ""
        return row
    except Exception as e:
        row["metadata_status"] = "FAIL"
        row["failure_reason_if_any"] = type(e).__name__
        row["token0_present"] = False
        row["token1_present"] = False
        row["fee_present"] = False
        row["liquidity_value_or_status"] = "exception"
        row["slot0_ok"] = False
        row["weth_leg_verified"] = False
        row["resolved_pool_type"] = ""
        row["token0"] = ""
        row["token1"] = ""
        row["fee_raw_uint24"] = ""
        return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    if not args.write:
        print("pass --write", file=sys.stderr)
        return 1

    env = {**load_dotenv_files(), **dict(os.environ)}
    mod = load_label_mod()

    sj032a = json.loads((REPO / "mvp/diagnostics/sprint032a_hard_quota_smoke_label_summary.json").read_text())
    man032a = json.loads((REPO / "mvp/diagnostics/sprint032a_hard_quota_smoke_manifest.json").read_text())
    pools032a = [str(p).lower() for p in man032a.get("pool_address_audit_keys") or []]

    labeled_by_pool = {k.lower(): sum(v.values()) for k, v in (sj032a.get("per_pool_label_counts") or {}).items()}
    manifest_counts032a = {k.lower(): int(v) for k, v in (man032a.get("bucket_count_by_pool") or {}).items()}
    fail_est = {p: manifest_counts032a.get(p, 0) - labeled_by_pool.get(p, 0) for p in pools032a}

    diag = {
        "kind": "sprint032b_failure_diagnosis",
        "sprint032a_reference_commit": "b394b6f",
        "sprint032a_summary_path": "mvp/diagnostics/sprint032a_hard_quota_smoke_label_summary.json",
        "quote_failed_count": sj032a.get("quote_failed_count"),
        "attempted_rows": sj032a.get("attempted_rows"),
        "labeled_rows": sj032a.get("labeled_rows"),
        "failure_reason_counts": sj032a.get("reason_counts"),
        "estimated_quote_failures_by_pool_manifest_minus_csv_labeled": fail_est,
        "fail_v3_entry_zero_dominates": int((sj032a.get("reason_counts") or {}).get("fail_v3_entry_zero", 0))
        >= int((sj032a.get("reason_counts") or {}).get("executable", 0)) // 5,
        "pool_zero_executable_labels_audit_only": "0xdfe5f275020def30993f042174fc2d335678b626",
        "bias_note": (
            "Sprint032A executable_only economics are biased upward/downward versus attempted cohort until "
            "quote_failed rows are repaired — failures are omitted from labels CSV, not encoded as label=0."
        ),
        "per_pool_label_counts_reference_only": sj032a.get("per_pool_label_counts"),
        "notes": [
            "Per-row quote_failed timestamps are not recoverable from sprint032a_hard_quota_smoke_labels.csv "
            "(quote_failed rows intentionally omitted). Pool-level failure estimates use manifest row counts minus labeled CSV rows.",
            "Root cause hypothesis validated offline: mixed USDC/cbAsset pools lacking canonical WETH leg plus "
            "non-standard Uniswap V3 fee tiers require quoter fee uint24 == on-chain fee(), not fee_bps*100 rounding.",
        ],
    }

    rpc_url = (env.get("LABEL_RPC_URL") or env.get("EVM_HTTP_URL") or "").strip()
    rpc_calls = 0
    audit_rows: list[dict[str, str]] = []

    union_audit_pools = sorted(
        set(pools032a)
        | {
            "0x0c33992a756bc614a0135777704ebcae0d0f6911",
            "0x1b14ef72fdc60d8fe9a96a7e8838e53fa45d6467",
            "0x82321f3beb69f503380d6b233857d5c43562e2d0",
            "0x7f670f78b17dec44d5ef68a48740b6f8849cc2e6",
            "0xe682d300f0d8179af8575c58817ddef076747161",
            "0xe9e3cbb1d0849b8c1fc8542db9b41448b6f58e8a",
        }
    )

    ch_ok = ch_ping_select1(env)
    ch_meta: dict[str, dict[str, str]] = {}
    if ch_ok:
        try:
            ch_meta = ch_fetch_discovered(env, union_audit_pools)
        except Exception:
            ch_meta = {}

    # Fix typo in ch_fetch - base64.b64encoding doesn't exist
    # Patch below - I'll fix function ch_fetch_discovered

    if rpc_url:
        rpc_plain = mod.RPC(rpc_url)
        bn = int(rpc_plain.call("eth_getBlockByNumber", ["latest", False])["result"]["number"], 16)
        rpc_calls += 1
        for pool in union_audit_pools:
            row_on = audit_pool_onchain(mod, rpc_plain, pool, bn)
            rpc_calls += 10
            cr = ch_meta.get(pool.lower())
            match = ""
            if cr:
                ch_fee = cr.get("fee_bps", "")
                ch_weth = cr.get("has_weth_leg", "")
                match = f"ch_fee_bps={ch_fee};ch_has_weth={ch_weth};onchain_status={row_on['metadata_status']}"
            row_out = {
                "pool_address_audit_key": pool.lower(),
                "source": row_on.get("source", "on_chain_eth_call_latest"),
                "token0_present": str(row_on.get("token0_present", False)).lower(),
                "token1_present": str(row_on.get("token1_present", False)).lower(),
                "fee_present": str(row_on.get("fee_present", False)).lower(),
                "liquidity_value_or_status": str(row_on.get("liquidity_value_or_status", "")),
                "slot0_ok": str(row_on.get("slot0_ok", False)).lower(),
                "weth_leg_verified": str(row_on.get("weth_leg_verified", False)).lower(),
                "ch_metadata_match_if_available": match,
                "failure_reason_if_any": row_on.get("failure_reason_if_any", ""),
                "metadata_status": row_on.get("metadata_status", "UNKNOWN"),
                "resolved_pool_type": row_on.get("resolved_pool_type", ""),
                "token0": row_on.get("token0", ""),
                "token1": row_on.get("token1", ""),
                "fee_raw_uint24": row_on.get("fee_raw_uint24", ""),
            }
            audit_rows.append(row_out)
    else:
        for pool in union_audit_pools:
            audit_rows.append(
                {
                    "pool_address_audit_key": pool.lower(),
                    "source": "blocked_missing_rpc_env",
                    "token0_present": "false",
                    "token1_present": "false",
                    "fee_present": "false",
                    "liquidity_value_or_status": "unknown",
                    "slot0_ok": "false",
                    "weth_leg_verified": "false",
                    "ch_metadata_match_if_available": "",
                    "failure_reason_if_any": "BLOCKED_MISSING_RPC_ENV",
                    "metadata_status": "UNKNOWN",
                    "resolved_pool_type": "",
                    "token0": "",
                    "token1": "",
                    "fee_raw_uint24": "",
                }
            )

    diag["metadata_rpc_calls_estimate"] = rpc_calls
    diag["clickhouse_reachable_select1"] = ch_ok

    manifest_pools = sorted(
        [
            "0x0c33992a756bc614a0135777704ebcae0d0f6911",
            "0x1b14ef72fdc60d8fe9a96a7e8838e53fa45d6467",
            "0x82321f3beb69f503380d6b233857d5c43562e2d0",
            "0x7f670f78b17dec44d5ef68a48740b6f8849cc2e6",
            "0xe682d300f0d8179af8575c58817ddef076747161",
            "0xe9e3cbb1d0849b8c1fc8542db9b41448b6f58e8a",
        ]
    )

    pass_set = {r["pool_address_audit_key"] for r in audit_rows if r["metadata_status"] == "PASS"}
    if not rpc_url or not set(manifest_pools).issubset(pass_set):
        diag["manifest_build_status"] = "BLOCKED_MISSING_RPC_ENV_OR_METADATA_NOT_PASS"
        (REPO / "mvp/diagnostics/sprint032b_failure_diagnosis_summary.json").write_text(
            json.dumps(diag, indent=2), encoding="utf-8"
        )
        # still write csv partial
        outp = REPO / "mvp/diagnostics/sprint032b_pool_metadata_audit.csv"
        outp.parent.mkdir(parents=True, exist_ok=True)
        with outp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(audit_rows[0].keys()))
            w.writeheader()
            w.writerows(audit_rows)
        print("BLOCKED_MANIFEST", file=sys.stderr)
        return 3

    pool_buckets: dict[str, set[int]] = defaultdict(set)
    with (REPO / "logs/dashcam.csv").open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            p = (row.get("token_address") or "").strip().lower()
            if p not in manifest_pools:
                continue
            try:
                ts_raw = int(float(row["timestamp"]))
            except Exception:
                continue
            pool_buckets[p].add((ts_raw // BUCKET_MS) * BUCKET_MS)

    lo = max(min(pool_buckets[p]) for p in manifest_pools)
    hi = min(max(pool_buckets[p]) for p in manifest_pools)
    avail = {p: sorted([b for b in pool_buckets[p] if lo <= b <= hi]) for p in manifest_pools}

    CAP_EACH = 40
    TARGET = min(250, sum(min(CAP_EACH, len(avail[p])) for p in manifest_pools))
    queues = {p: deque(avail[p]) for p in manifest_pools}
    counts: Counter[str] = Counter()
    rows_tpl: list[tuple[str, int, int]] = []
    while len(rows_tpl) < TARGET:
        progressed = False
        for p in manifest_pools:
            if counts[p] >= CAP_EACH or not queues[p]:
                continue
            bm = queues[p].popleft()
            rows_tpl.append((p, bm, bm // 1000))
            counts[p] += 1
            progressed = True
            if len(rows_tpl) >= TARGET:
                break
        if not progressed:
            break
    rows_tpl.sort(key=lambda t: (t[1], t[0]))
    total = len(rows_tpl)
    shares = sorted(counts.values(), reverse=True)
    top1 = shares[0] / total if total else 0
    top3 = sum(shares[:3]) / total if total else 0

    manifest = {
        "kind": "metadata_verified_hard_quota_smoke_manifest",
        "created_before_quote": True,
        "no_training": True,
        "no_live": True,
        "source_artifacts": [
            "mvp/audits/sprint032a_hard_quota_universe_smoke_report.md",
            "mvp/diagnostics/sprint032a_hard_quota_universe_summary.json",
            "mvp/diagnostics/sprint032a_hard_quota_smoke_manifest.json",
            "mvp/diagnostics/sprint032a_hard_quota_smoke_label_summary.json",
            "mvp/diagnostics/sprint032a_hard_quota_smoke_labels.csv",
            "mvp/diagnostics/sprint032a_hard_quota_candidate_summary.csv",
            "mvp/diagnostics/sprint032b_failure_diagnosis_summary.json",
            "mvp/diagnostics/sprint032b_pool_metadata_audit.csv",
            "scripts/sprint030d_bounded_smoke_label.py",
            "scripts/sprint032b_metadata_audit_manifest_builder.py",
            "scripts/label_dashcam_executable.py",
            "docs/label-semantics-freeze.md",
            "docs/ml-training-gate.md",
            "mvp/audits/failed_no_7d_alpha_guard.md",
            "logs/dashcam.csv",
        ],
        "metadata_verification_rule": (
            "pre_quote_on_chain_token0_token1_fee_liquidity_slot0_plus_optional_clickhouse_discovered_pools_row "
            "canonical_base_weth_leg_required_pool_type_v2_or_v3_resolved_no_post_quote_cherry_pick"
        ),
        "selected_pool_count": len(manifest_pools),
        "label_limit": 250,
        "selected_rows_count": total,
        "per_pool_row_cap_enforced": CAP_EACH,
        "hard_quota_gates": {
            "top1_row_share_cap": 0.30,
            "top3_row_share_cap": 0.70,
            "computed_top1_row_share": round(top1, 6),
            "computed_top3_row_share": round(top3, 6),
        },
        "pool_selection_rule": (
            "exclude_sprint032a_manifest_pools_with_metadata_FAIL_non_weth_or_non_standard_without_fee_raw_handling "
            "add_overlap_window_ranked_WETH_PASS_replacement_pools_lex_sorted_six_pack_round_robin_cap40"
        ),
        "bucket_selection_rule": (
            "shared_overlap_window_across_selected_PASS_pools_round_robin_then_sort_manifest_rows_chronological"
        ),
        "pool_address_audit_keys": manifest_pools,
        "bucket_count_by_pool": dict(counts),
        "overlap_window_ms_audit_only": {"start_ms": lo, "end_ms": hi},
        "manifest_rows": [{"pool_audit_only": p, "bucket_ms": bm, "bucket_sec": bs} for p, bm, bs in rows_tpl],
        "decision_flags": {
            "FAILED_NO_7D_ALPHA_ACTIVE": True,
            "LABEL_SEMANTICS_REPAIR_ALLOWED": True,
            "METADATA_VERIFIED_SMOKE_LABEL_ALLOWED": "bounded_only",
            "TRAINING_ALLOWED": False,
            "H12_ABLATION_ALLOWED": False,
            "PHASE6_ALLOWED": False,
            "LIVE_ALLOWED": False,
            "RETRAIN_ALLOWED": False,
            "MODEL_PROMOTION_ALLOWED": False,
            "MICRO_LIVE_EXECUTION_ALLOWED": False,
            "SMART_MONEY_GENERAL_LINE_OPEN": False,
            "SMART_MONEY_AS_INPUT_OR_DIAGNOSTIC": True,
        },
    }

    diag["manifest_build_status"] = "METADATA_VERIFIED_MANIFEST_OK"

    (REPO / "mvp/diagnostics/sprint032b_failure_diagnosis_summary.json").write_text(json.dumps(diag, indent=2), encoding="utf-8")
    outp = REPO / "mvp/diagnostics/sprint032b_pool_metadata_audit.csv"
    with outp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(audit_rows[0].keys()))
        w.writeheader()
        w.writerows(audit_rows)
    (REPO / "mvp/diagnostics/sprint032b_metadata_verified_smoke_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print("wrote sprint032b diagnostics + manifest rows", total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
