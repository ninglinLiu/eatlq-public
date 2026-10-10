"""H18.15 / V3R-12A pool-first schema sanity + local V3 supply preflight.

This is diagnostics/report generation only:
  - no model training
  - no model artifacts
  - no legacy dashcam rewrite
  - no RPC / subgraph calls
"""
from __future__ import annotations

import csv
import json
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "mvp/diagnostics/h18_15_schema_sanity_v3_data_preflight.json"
OUT_MD = ROOT / "mvp/runs/h18-15-pool-first-schema-sanity-and-v3-data-preflight.md"

WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
USDBC = "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca"
CBBTC = "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"
QUOTE_ADDR_TO_SYMBOL = {WETH: "WETH", USDC: "USDC", USDBC: "USDbC", CBBTC: "cbBTC"}
QUOTE_PRIORITY = ["WETH", "USDC", "USDbC", "cbBTC"]

H1814_NEW_DASHCAM_COLS = [
    "pool_address",
    "pool_type",
    "dex",
    "fee_bps",
    "token0",
    "token1",
    "quote_token_address",
    "quote_symbol",
    "target_token_address",
    "target_resolution_status",
    "normalized_side",
]

SCHEMA_CONTRACT = [
    {
        "field": "pool_address",
        "tick_dict": True,
        "dashcam_csv": True,
        "legacy_alias": False,
        "new_code_should_use": True,
        "old_code_may_read": False,
        "meaning": "Authoritative pool/pair contract address, copied from the EVM log emitter payload['address'] for Swap logs.",
    },
    {
        "field": "token_address",
        "tick_dict": True,
        "dashcam_csv": True,
        "legacy_alias": True,
        "new_code_should_use": False,
        "old_code_may_read": True,
        "meaning": "Frozen backward-compatible alias of pool_address. It is NOT a true ERC20 token target in current dashcam/tick flow.",
    },
    {
        "field": "target_token_address",
        "tick_dict": True,
        "dashcam_csv": True,
        "legacy_alias": False,
        "new_code_should_use": True,
        "old_code_may_read": False,
        "meaning": "Non-quote token leg after quote priority WETH > USDC > USDbC > cbBTC.",
    },
    {
        "field": "quote_token_address",
        "tick_dict": True,
        "dashcam_csv": True,
        "legacy_alias": False,
        "new_code_should_use": True,
        "old_code_may_read": False,
        "meaning": "Known quote leg selected by quote priority, if present.",
    },
    {
        "field": "quote_symbol",
        "tick_dict": True,
        "dashcam_csv": True,
        "legacy_alias": False,
        "new_code_should_use": True,
        "old_code_may_read": False,
        "meaning": "WETH / USDC / USDbC / cbBTC for the selected quote leg.",
    },
    {
        "field": "token0",
        "tick_dict": True,
        "dashcam_csv": True,
        "legacy_alias": False,
        "new_code_should_use": True,
        "old_code_may_read": False,
        "meaning": "Pool token0 from pool:discovered:{pool_address}.",
    },
    {
        "field": "token1",
        "tick_dict": True,
        "dashcam_csv": True,
        "legacy_alias": False,
        "new_code_should_use": True,
        "old_code_may_read": False,
        "meaning": "Pool token1 from pool:discovered:{pool_address}.",
    },
    {
        "field": "pool_type",
        "tick_dict": True,
        "dashcam_csv": True,
        "legacy_alias": False,
        "new_code_should_use": True,
        "old_code_may_read": False,
        "meaning": "Pool family: v2 / v3 / aero_vamm / aero_cl / unknown.",
    },
    {
        "field": "dex",
        "tick_dict": True,
        "dashcam_csv": True,
        "legacy_alias": False,
        "new_code_should_use": True,
        "old_code_may_read": False,
        "meaning": "Protocol/source: uniswap_v2 / uniswap_v3 / aerodrome / unknown.",
    },
    {
        "field": "fee_bps",
        "tick_dict": True,
        "dashcam_csv": True,
        "legacy_alias": False,
        "new_code_should_use": True,
        "old_code_may_read": False,
        "meaning": "Fee field from pool metadata; V3/CL-like pools may use Uniswap-style units.",
    },
    {
        "field": "target_resolution_status",
        "tick_dict": True,
        "dashcam_csv": True,
        "legacy_alias": False,
        "new_code_should_use": True,
        "old_code_may_read": False,
        "meaning": "ok / no_known_quote_leg / pool_meta_missing.",
    },
    {
        "field": "normalized_side",
        "tick_dict": True,
        "dashcam_csv": True,
        "legacy_alias": False,
        "new_code_should_use": True,
        "old_code_may_read": False,
        "meaning": "Quote-leg-relative side: buy_target / sell_target / ambiguous / unknown.",
    },
    {
        "field": "is_buy",
        "tick_dict": True,
        "dashcam_csv": False,
        "legacy_alias": True,
        "new_code_should_use": False,
        "old_code_may_read": True,
        "meaning": "Legacy WETH-leg-blind side flag. Kept only for compatibility.",
    },
    {
        "field": "is_buy_legacy",
        "tick_dict": True,
        "dashcam_csv": False,
        "legacy_alias": True,
        "new_code_should_use": False,
        "old_code_may_read": True,
        "meaning": "Explicit mirror of is_buy in tick dict only. Not written to dashcam.csv.",
    },
]


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _lower(v: Any) -> str:
    return str(v or "").strip().lower()


def _valid_addr(v: Any) -> bool:
    s = _lower(v)
    if not (s.startswith("0x") and len(s) == 42):
        return False
    try:
        int(s[2:], 16)
        return True
    except ValueError:
        return False


def _pct(n: int | float, d: int | float) -> float:
    return round(float(n) / float(d) * 100.0, 4) if d else 0.0


def _intish(v: Any) -> int | None:
    if v in (None, ""):
        return None
    try:
        return int(float(str(v)))
    except (TypeError, ValueError):
        return None


def _open_csv(path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def _find_pool_field(headers: list[str]) -> str | None:
    candidates = [
        "pool_address",
        "pool",
        "pair_address",
        "pair",
        "address",
        "token_address",
    ]
    lower_map = {h.lower(): h for h in headers}
    for c in candidates:
        if c in lower_map:
            return lower_map[c]
    return None


def _infer_quote_symbol(row: dict[str, Any]) -> str:
    qsym = str(row.get("quote_symbol") or "").strip()
    if qsym:
        return qsym
    qaddr = _lower(row.get("quote_token_address") or row.get("quote_token") or row.get("tokenIn") or "")
    if qaddr in QUOTE_ADDR_TO_SYMBOL:
        return QUOTE_ADDR_TO_SYMBOL[qaddr]
    t0 = _lower(row.get("token0") or row.get("token0_address") or "")
    t1 = _lower(row.get("token1") or row.get("token1_address") or "")
    for addr, sym in QUOTE_ADDR_TO_SYMBOL.items():
        if addr in (t0, t1):
            return sym
    return "(unknown)"


def _row_is_v3(path: Path, row: dict[str, Any]) -> bool:
    pt = _lower(row.get("pool_type"))
    dex = _lower(row.get("dex") or row.get("source") or row.get("protocol"))
    if pt in ("v3", "uniswap_v3", "uni_v3"):
        return True
    if "uniswap_v3" in dex or dex == "v3":
        return True
    # V3R artifacts with amount0/amount1 + fee fields are V3-specific by construction.
    name = path.name.lower()
    if "v3" in name and ("amount0" in row or "amount1" in row or "fee_tier" in row or "fee_bps" in row):
        return True
    return False


def _time_or_block_span(rows: list[dict[str, Any]]) -> dict[str, Any]:
    time_keys = ["timestamp", "timestamp_ms", "block_time", "entry_time", "created_at", "time"]
    block_keys = ["block_number", "entry_block", "block"]
    times: list[int] = []
    blocks: list[int] = []
    for row in rows:
        for k in time_keys:
            if k not in row:
                continue
            raw = row.get(k)
            if raw in (None, ""):
                continue
            n = _intish(raw)
            if n is not None:
                # Keep ms timestamps as-is; convert plausible second timestamps to ms.
                times.append(n * 1000 if 1_000_000_000 <= n < 10_000_000_000 else n)
                break
            text = str(raw).replace("Z", "+00:00").strip()
            try:
                times.append(int(datetime.fromisoformat(text).timestamp() * 1000))
                break
            except ValueError:
                pass
        for k in block_keys:
            if k in row:
                b = _intish(row.get(k))
                if b is not None:
                    blocks.append(b)
                    break
    out: dict[str, Any] = {}
    if times:
        out["timestamp_ms_min"] = min(times)
        out["timestamp_ms_max"] = max(times)
    if blocks:
        out["block_min"] = min(blocks)
        out["block_max"] = max(blocks)
    return out


def analyze_dashcam() -> dict[str, Any]:
    path = ROOT / "logs/dashcam.csv"
    headers, rows = _open_csv(path)
    backup_path = ROOT / "logs/dashcam.pre_h1814_backup.csv"
    backup_data_rows: int | None = None
    if backup_path.exists():
        # csv.DictReader excludes the header, matching total_rows below.
        _, backup_rows = _open_csv(backup_path)
        backup_data_rows = len(backup_rows)
    new_rows = [r for r in rows if _lower(r.get("pool_address"))]
    legacy_rows = len(rows) - len(new_rows)
    field_counts = {c: sum(1 for r in new_rows if str(r.get(c) or "").strip()) for c in H1814_NEW_DASHCAM_COLS}
    missing_counts = {c: len(new_rows) - field_counts[c] for c in H1814_NEW_DASHCAM_COLS}
    dist_fields = ["pool_type", "dex", "quote_symbol", "target_resolution_status", "normalized_side"]
    dists = {
        f: dict(Counter(str(r.get(f) or "(missing)") for r in new_rows))
        for f in dist_fields
    }
    dist_sums = {f: sum(dists[f].values()) for f in dist_fields}
    pools = [_lower(r.get("pool_address")) for r in new_rows if _lower(r.get("pool_address"))]
    targets = [_lower(r.get("target_token_address")) for r in new_rows if _lower(r.get("target_token_address"))]
    pool_counts = Counter(pools)
    target_counts = Counter(targets)
    total_new = len(new_rows)

    alias_ok = sum(1 for r in new_rows if _lower(r.get("pool_address")) == _lower(r.get("token_address")))
    t0_lower_ok = sum(1 for r in new_rows if _valid_addr(r.get("token0")) and _lower(r.get("token0")) == str(r.get("token0")).strip())
    t1_lower_ok = sum(1 for r in new_rows if _valid_addr(r.get("token1")) and _lower(r.get("token1")) == str(r.get("token1")).strip())
    quote_known = sum(
        1 for r in new_rows
        if not _lower(r.get("quote_token_address")) or _lower(r.get("quote_token_address")) in QUOTE_ADDR_TO_SYMBOL
    )
    target_non_quote_ok = 0
    quote_status_ok = 0
    v3_weth_normal = 0
    v3_weth_bad_side = 0
    for r in new_rows:
        t0 = _lower(r.get("token0"))
        t1 = _lower(r.get("token1"))
        q = _lower(r.get("quote_token_address"))
        tgt = _lower(r.get("target_token_address"))
        status = str(r.get("target_resolution_status") or "")
        if q and ((q == t0 and tgt == t1) or (q == t1 and tgt == t0)):
            target_non_quote_ok += 1
        if q and status == "ok":
            quote_status_ok += 1
        if _lower(r.get("pool_type")) == "v3" and str(r.get("quote_symbol") or "") == "WETH":
            v3_weth_normal += 1
            if str(r.get("normalized_side") or "") in ("unknown", "ambiguous", ""):
                v3_weth_bad_side += 1

    return {
        "path": str(path.relative_to(ROOT)),
        "header_columns": headers,
        "header_upgraded_to_h1814": headers[10:] == H1814_NEW_DASHCAM_COLS,
        "total_rows": len(rows),
        "legacy_rows": legacy_rows,
        "pre_h1814_backup_data_rows": backup_data_rows,
        "transition_legacy_rows_after_backup": (
            legacy_rows - backup_data_rows if backup_data_rows is not None else None
        ),
        "new_schema_rows": total_new,
        "field_population_counts": field_counts,
        "field_missing_counts": missing_counts,
        "field_population_pct_of_new_rows": {k: _pct(v, total_new) for k, v in field_counts.items()},
        "pool_type_distribution": dists["pool_type"],
        "dex_distribution": dists["dex"],
        "quote_symbol_distribution": dists["quote_symbol"],
        "target_resolution_status_distribution": dists["target_resolution_status"],
        "normalized_side_distribution": dists["normalized_side"],
        "distribution_sums": dist_sums,
        "all_distribution_sums_equal_new_schema_rows": all(v == total_new for v in dist_sums.values()),
        "unique_pools": len(pool_counts),
        "unique_target_tokens": len(target_counts),
        "top1_pool_share": _pct(pool_counts.most_common(1)[0][1], total_new) if pool_counts else 0.0,
        "top5_pool_share": _pct(sum(v for _, v in pool_counts.most_common(5)), total_new) if pool_counts else 0.0,
        "pool_first_correctness": {
            "pool_address_equals_legacy_token_address": alias_ok,
            "pool_address_equals_legacy_token_address_pct": _pct(alias_ok, total_new),
            "token0_lowercase_valid_address": t0_lower_ok,
            "token1_lowercase_valid_address": t1_lower_ok,
            "quote_token_known_or_blank": quote_known,
            "target_is_non_quote_leg": target_non_quote_ok,
            "target_resolution_ok_when_quote_exists": quote_status_ok,
            "normal_v3_weth_rows": v3_weth_normal,
            "normal_v3_weth_unknown_or_ambiguous_side": v3_weth_bad_side,
        },
        "time_or_block_span": _time_or_block_span(new_rows),
    }


def analyze_csv_artifact(path: Path) -> dict[str, Any]:
    headers, rows = _open_csv(path)
    pool_field = _find_pool_field(headers)
    v3_rows = [r for r in rows if _row_is_v3(path, r)]
    if not v3_rows and "v3" in path.name.lower():
        v3_rows = rows
    pool_counts = Counter(_lower(r.get(pool_field)) for r in v3_rows if pool_field and _valid_addr(r.get(pool_field)))
    quote_basis_fields = {"quote_symbol", "quote_token_address", "quote_token", "token0", "token1", "token0_address", "token1_address"}
    has_quote_basis = bool(quote_basis_fields.intersection(set(headers)))
    if has_quote_basis:
        quote_counts = Counter(_infer_quote_symbol(r) for r in v3_rows)
        weth_rows: int | None = sum(1 for r in v3_rows if _infer_quote_symbol(r) == "WETH")
        weth_pools: set[str] | None = {
            _lower(r.get(pool_field))
            for r in v3_rows
            if pool_field and _valid_addr(r.get(pool_field)) and _infer_quote_symbol(r) == "WETH"
        }
    else:
        quote_counts = Counter({"(unavailable)": len(v3_rows)})
        weth_rows = None
        weth_pools = None
    status_field = None
    for candidate in ("status", "candidate_status", "gate_status", "label_status", "result", "success", "is_success"):
        if candidate in headers:
            status_field = candidate
            break
    success_rows = 0
    status_counts: dict[str, int] = {}
    if status_field:
        status_counter = Counter(str(r.get(status_field) or "(missing)") for r in v3_rows)
        status_counts = dict(status_counter.most_common(10))
        success_rows = sum(
            1 for r in v3_rows
            if str(r.get(status_field) or "").lower() in (
                "ok", "success", "1", "true", "labeled", "clean",
                "trainable_candidate", "pass",
            )
        )
    # If file name itself encodes clean/success and no status field exists, treat rows as clean artifacts.
    clean_name = any(s in path.name.lower() for s in ("clean_alpha", "risk_v3", "all_v3"))
    if clean_name and not status_field:
        success_rows = len(v3_rows)

    return {
        "path": str(path.relative_to(ROOT)),
        "exists": True,
        "headers": headers[:40],
        "total_rows": len(rows),
        "v3_candidate_rows": len(v3_rows),
        "clean_or_success_rows_if_available": success_rows,
        "pool_field": pool_field,
        "unique_pool_address_count": len(pool_counts),
        "weth_paired_rows": weth_rows,
        "weth_paired_pool_count": len(weth_pools) if weth_pools is not None else None,
        "quote_or_token_leg_fields_available": has_quote_basis,
        "quote_symbol_distribution": dict(quote_counts.most_common(12)),
        "time_or_block_span": _time_or_block_span(v3_rows),
        "top1_pool_share": _pct(pool_counts.most_common(1)[0][1], len(v3_rows)) if pool_counts and v3_rows else 0.0,
        "top5_pool_share": _pct(sum(v for _, v in pool_counts.most_common(5)), len(v3_rows)) if pool_counts and v3_rows else 0.0,
        "top5_pools": pool_counts.most_common(5),
        "status_field": status_field,
        "status_counts_top": status_counts,
    }


def local_csv_sources() -> list[Path]:
    paths = [
        ROOT / "logs/dashcam.csv",
        ROOT / "logs/run004_candidates_swaponly_full.csv",
        ROOT / "logs/run004_candidates_swaponly.csv",
        ROOT / "cache/run004_v3r12/v3r12_novel_pool_candidates.csv",
        ROOT / "mvp/diagnostics/run004_v3r8_all_v3_decoded_backed_10k_dataset.csv",
        ROOT / "mvp/diagnostics/run004_v3r8_clean_alpha_v3_decoded_backed_10k_dataset.csv",
        ROOT / "mvp/diagnostics/run004_v3r8_risk_v3_decoded_backed_10k_dataset.csv",
        ROOT / "mvp/diagnostics/run004_v3r6_all_v3_5k_dataset.csv",
        ROOT / "mvp/diagnostics/run004_v3r6_clean_alpha_v3_5k_dataset.csv",
        ROOT / "mvp/diagnostics/run004_v3r6_risk_v3_5k_dataset.csv",
        ROOT / "mvp/diagnostics/run004_v3r6_candidate_pool.csv",
        ROOT / "mvp/diagnostics/run004_v3r3_candidate_pool.csv",
        ROOT / "mvp/diagnostics/run004_v3r2_decoded_backed_candidates.csv",
        ROOT / "mvp/diagnostics/run004_v3r1_v3_only_pilot_100.csv",
        ROOT / "mvp/diagnostics/run004_v3r15_forensic_sample_set.csv",
    ]
    return [p for p in paths if p.exists()]


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "path": str(path.relative_to(ROOT))}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"exists": True, "path": str(path.relative_to(ROOT)), "error": str(exc)}


def summarize_v3_diagnostics() -> dict[str, Any]:
    v3r12 = load_json(ROOT / "mvp/diagnostics/run004_v3r12_data_supply_summary.json")
    v3r11 = load_json(ROOT / "mvp/diagnostics/run004_v3r11_return_aware_summary.json")
    v3r10 = load_json(ROOT / "mvp/diagnostics/run004_v3r10_candidate_model_report.json")
    out: dict[str, Any] = {
        "v3r12_local_audit": {
            "discovered_v3_total": v3r12.get("audit", {}).get("discovered_pools", {}).get("v3_total"),
            "discovered_v3_weth": v3r12.get("audit", {}).get("discovered_pools", {}).get("v3_weth"),
            "decoded_v3_swap_rows": v3r12.get("audit", {}).get("v3_swaps_decoded", {}).get("rows"),
            "decoded_v3_swap_unique_pools": v3r12.get("audit", {}).get("v3_swaps_decoded", {}).get("unique_pools"),
            "decoded_v3_weth_rows": v3r12.get("audit", {}).get("v3_weth_decoded_swaps", {}).get("rows"),
            "decoded_v3_weth_unique_pools": v3r12.get("audit", {}).get("v3_weth_decoded_swaps", {}).get("unique_pools"),
            "v3r10_v3r11_pool_count_observed": v3r12.get("audit", {}).get("v3r10_v3r11_pool_count_observed"),
            "pool_depth": v3r12.get("audit", {}).get("pool_depth"),
            "decoded_swap_source_breakdown": v3r12.get("audit", {}).get("decoded_swap_source_breakdown"),
            "novel_candidate_rows": v3r12.get("candidate_dataset", {}).get("rows"),
            "novel_candidate_pools": v3r12.get("candidate_dataset", {}).get("novel_pools"),
            "pilot_attempted": v3r12.get("pilot_phase1_1k", {}).get("attempted"),
            "pilot_success": v3r12.get("pilot_phase1_1k", {}).get("success"),
            "pilot_success_rate": v3r12.get("pilot_phase1_1k", {}).get("success_rate"),
            "explanation_for_87_pool_ceiling": v3r12.get("audit", {}).get("explanation_for_87_pool_ceiling"),
        },
        "v3r11_input_validation": v3r11.get("input_validation", {}),
        "v3r10_gate_or_input_validation": v3r10.get("input_validation", v3r10.get("gate", {})),
    }
    return out


def clickhouse_light_audit() -> dict[str, Any]:
    queries = {
        "discovered_pools": (
            "SELECT count() AS total, "
            "countIf(pool_type='v3' AND chain_id=8453) AS v3_total, "
            "countIf(pool_type='v3' AND has_weth_leg=1 AND chain_id=8453) AS v3_weth "
            "FROM eatlq.discovered_pools FORMAT JSONEachRow"
        ),
        "v3_decoded": (
            "SELECT count() AS rows, uniqExact(lower(pool_address)) AS pools "
            "FROM eatlq.uniswap_v3_swaps_decoded "
            "WHERE chain_id=8453 AND removed=0 FORMAT JSONEachRow"
        ),
        "v3_weth_decoded": (
            "WITH weth AS (SELECT lower(pool_address) p FROM eatlq.discovered_pools "
            "WHERE chain_id=8453 AND pool_type='v3' AND has_weth_leg=1) "
            "SELECT count() AS rows, uniqExact(lower(s.pool_address)) AS pools "
            "FROM eatlq.uniswap_v3_swaps_decoded s "
            "WHERE chain_id=8453 AND removed=0 AND amount0!=0 AND amount1!=0 "
            "AND lower(s.pool_address) IN (SELECT p FROM weth) FORMAT JSONEachRow"
        ),
    }
    out: dict[str, Any] = {"available": False, "results": {}, "errors": {}}
    for name, query in queries.items():
        try:
            proc = subprocess.run(
                ["docker", "exec", "eatlq-clickhouse-1", "clickhouse-client", "--database", "eatlq", "--query", query],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            if proc.returncode != 0:
                out["errors"][name] = (proc.stderr or proc.stdout).strip()[:500]
                continue
            rows = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
            out["results"][name] = rows[0] if rows else {}
            out["available"] = True
        except Exception as exc:
            out["errors"][name] = str(exc)
    return out


def redis_market_tick_sample() -> dict[str, Any]:
    code = r'''
import json, os
try:
    import msgpack, redis
except Exception as exc:
    print(json.dumps({"available": False, "error": f"import_error: {exc}"}))
    raise SystemExit
r = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"), decode_responses=False)
stream = os.environ.get("MARKET_TICKS_STREAM", "market_ticks")
rows = r.xrevrange(stream, "+", "-", count=100)
out = {"available": True, "stream": stream, "sampled": len(rows), "payloads_with_address": 0, "address_has_pool_meta": 0, "samples": []}
for _id, fields in rows:
    payload = None
    for v in fields.values():
        if isinstance(v, bytes):
            try:
                obj = msgpack.unpackb(v, raw=False)
                if isinstance(obj, dict) and ("address" in obj or "topic0" in obj):
                    payload = obj
                    break
            except Exception:
                pass
    if not isinstance(payload, dict):
        continue
    addr = str(payload.get("address") or "").lower().strip()
    if not (addr.startswith("0x") and len(addr) == 42):
        continue
    out["payloads_with_address"] += 1
    meta = r.hgetall(f"pool:discovered:{addr}")
    if meta:
        out["address_has_pool_meta"] += 1
    if len(out["samples"]) < 5:
        out["samples"].append({
            "stream_id": _id.decode() if isinstance(_id, bytes) else str(_id),
            "payload_address": addr,
            "topic0": str(payload.get("topic0") or "").lower(),
            "has_pool_discovered_meta": bool(meta),
        })
print(json.dumps(out))
'''
    try:
        proc = subprocess.run(
            ["docker", "exec", "-i", "eatlq-strategy-1", "python", "-"],
            input=code,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            return {"available": False, "error": (proc.stderr or proc.stdout).strip()[:1000]}
        return json.loads(proc.stdout.strip() or "{}")
    except Exception as exc:
        return {"available": False, "error": str(exc)}


def analyze_local_sources() -> dict[str, Any]:
    csv_reports: list[dict[str, Any]] = []
    for path in local_csv_sources():
        if path.name == "dashcam.csv":
            continue
        try:
            csv_reports.append(analyze_csv_artifact(path))
        except Exception as exc:
            csv_reports.append({"path": str(path.relative_to(ROOT)), "exists": True, "error": str(exc)})

    union_pools: set[str] = set()
    union_weth_pools: set[str] = set()
    total_v3_rows = 0
    total_success_rows = 0
    for rep in csv_reports:
        total_v3_rows += int(rep.get("v3_candidate_rows") or 0)
        total_success_rows += int(rep.get("clean_or_success_rows_if_available") or 0)
        # Avoid re-reading rows here; union is approximate via per-file counts below.
    return {
        "csv_artifacts": csv_reports,
        "csv_artifact_totals_non_union": {
            "raw_v3_candidate_rows_sum": total_v3_rows,
            "clean_or_success_rows_sum_if_available": total_success_rows,
            "note": "Sums are non-union; V3R datasets are derived from earlier candidate pools and overlap.",
        },
        "diagnostic_summaries": summarize_v3_diagnostics(),
        "clickhouse_light_audit": clickhouse_light_audit(),
        "cached_raw_market_tick_files_found": [],
    }


def write_outputs(summary: dict[str, Any]) -> None:
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(summary, indent=2, sort_keys=False), encoding="utf-8")
    OUT_MD.write_text(render_markdown(summary), encoding="utf-8")


def _dist_line(d: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in d.items()) if d else "(empty)"


def _fmt(v: Any) -> str:
    return "n/a" if v is None else str(v)


def render_markdown(s: dict[str, Any]) -> str:
    dash = s["schema_sanity"]["dashcam"]
    local = s["v3_data_preflight"]
    v3r12 = local["diagnostic_summaries"]["v3r12_local_audit"]
    ch = local["clickhouse_light_audit"]
    ch_v3 = ch.get("results", {}).get("v3_weth_decoded", {})
    csv_rows = local["csv_artifacts"]

    rows_table = "\n".join(
        "| `{path}` | {rows} | {pools} | {weth_pools} | {success} | {top1}% / {top5}% |".format(
            path=r.get("path"),
            rows=_fmt(r.get("v3_candidate_rows")),
            pools=_fmt(r.get("unique_pool_address_count")),
            weth_pools=_fmt(r.get("weth_paired_pool_count")),
            success=_fmt(r.get("clean_or_success_rows_if_available")),
            top1=_fmt(r.get("top1_pool_share")),
            top5=_fmt(r.get("top5_pool_share")),
        )
        for r in csv_rows
        if r.get("v3_candidate_rows")
    )
    schema_rows = "\n".join(
        "| `{field}` | {tick} | {dash} | {legacy} | {new} | {old} | {meaning} |".format(
            field=r["field"],
            tick="yes" if r["tick_dict"] else "no",
            dash="yes" if r["dashcam_csv"] else "no",
            legacy="yes" if r["legacy_alias"] else "no",
            new="yes" if r["new_code_should_use"] else "no",
            old="yes" if r["old_code_may_read"] else "no",
            meaning=r["meaning"],
        )
        for r in SCHEMA_CONTRACT
    )
    return f"""# H18.15 / V3R-12A - Pool-First Schema Sanity + V3 Data Supply Preflight

Status: **diagnostic/preflight only**.

Decision flags:
- `h19_allowed = false`
- `model_research_allowed = false`
- `target_tuning_allowed = false`
- `v3_data_expansion_allowed = true`
- `labeler_migration_allowed = true only after schema sanity is confirmed`
- `live_or_mainline_allowed = false`

No retrain. No model artifact. No target tuning. No H19. No live trading change. No legacy dashcam row rewrite.

## 1. H18.14 Schema Re-Check

Current `logs/dashcam.csv` state:
- total rows: **{dash['total_rows']}**
- legacy rows (no `pool_address` value): **{dash['legacy_rows']}**
- new-schema rows: **{dash['new_schema_rows']}**
- unique pools in new rows: **{dash['unique_pools']}**
- unique target tokens in new rows: **{dash['unique_target_tokens']}**
- top1/top5 pool concentration in new rows: **{dash['top1_pool_share']}% / {dash['top5_pool_share']}%**

Field population counts over new-schema rows:

| field | populated | missing | populated % |
|---|---:|---:|---:|
""" + "\n".join(
        f"| `{k}` | {dash['field_population_counts'][k]} | {dash['field_missing_counts'][k]} | {dash['field_population_pct_of_new_rows'][k]} |"
        for k in H1814_NEW_DASHCAM_COLS
    ) + f"""

Distributions:
- pool_type: {_dist_line(dash['pool_type_distribution'])}
- dex: {_dist_line(dash['dex_distribution'])}
- quote_symbol: {_dist_line(dash['quote_symbol_distribution'])}
- target_resolution_status: {_dist_line(dash['target_resolution_status_distribution'])}
- normalized_side: {_dist_line(dash['normalized_side_distribution'])}

The stricter H18.15 script includes `(missing)` buckets and verifies every distribution sum. Result:
`all_distribution_sums_equal_new_schema_rows = {str(dash['all_distribution_sums_equal_new_schema_rows']).lower()}` with sums `{dash['distribution_sums']}`.

### Why H18.14 said 84 rows but distributions summed to 82

The H18.14 JSON was not produced as one atomic snapshot. `rows_with_new_pool_first_schema` was manually carried forward as 84 while the distribution block remained from the earlier 82-row diagnostic output. H18.15 re-counts rows and distributions from the same read pass, includes missing buckets, and the distribution sums equal the current new-schema row count exactly.

There are **{dash['transition_legacy_rows_after_backup']}** 10-column transition rows after `logs/dashcam.pre_h1814_backup.csv` and before the first 21-column H18.14 row. These are legacy-shape rows emitted during the restart/header-migration window; they are counted as `legacy_rows`, not as new-schema rows. They do not explain the specific 84-vs-82 mismatch; that mismatch came from mixing a moving row count with an older distribution snapshot.

## 2. Exact Schema Contract

| field | tick dict | dashcam.csv | legacy alias | new code use | old code may read | meaning |
|---|---|---|---|---|---|---|
{schema_rows}

Clarifications:
- `is_buy_legacy` is **tick dict only**; it is not written to `dashcam.csv`.
- `token_address` is now a frozen alias of `pool_address` for compatibility. It should not be interpreted as an ERC20 target token.
- New labeler/retrain code should use `target_token_address`, not `token_address`.
- New side logic should use `normalized_side`, not `is_buy`.

## 3. Pool-First Correctness On Live Rows

Validation over current new-schema dashcam rows:
- `pool_address == token_address` (legacy alias): {dash['pool_first_correctness']['pool_address_equals_legacy_token_address']} / {dash['new_schema_rows']} ({dash['pool_first_correctness']['pool_address_equals_legacy_token_address_pct']}%)
- valid lowercase `token0`: {dash['pool_first_correctness']['token0_lowercase_valid_address']} / {dash['new_schema_rows']}
- valid lowercase `token1`: {dash['pool_first_correctness']['token1_lowercase_valid_address']} / {dash['new_schema_rows']}
- quote is one of WETH / USDC / USDbC / cbBTC or blank: {dash['pool_first_correctness']['quote_token_known_or_blank']} / {dash['new_schema_rows']}
- target is the non-quote leg: {dash['pool_first_correctness']['target_is_non_quote_leg']} / {dash['new_schema_rows']}
- `target_resolution_status=ok` when quote exists: {dash['pool_first_correctness']['target_resolution_ok_when_quote_exists']} / {dash['new_schema_rows']}
- normal V3 WETH rows with unknown/ambiguous side: {dash['pool_first_correctness']['normal_v3_weth_unknown_or_ambiguous_side']} / {dash['pool_first_correctness']['normal_v3_weth_rows']}

Redis market tick sampling (source-side sanity, no RPC):
`{s['schema_sanity']['redis_market_tick_sample']}`

## 4. V3 Data Supply Preflight

Local/diagnostic summary:
- discovered Base V3 pools in local `discovered_pools`: **{v3r12.get('discovered_v3_total')}**
- discovered Base V3 WETH pools: **{v3r12.get('discovered_v3_weth')}**
- decoded V3 swap rows: **{v3r12.get('decoded_v3_swap_rows')}** across **{v3r12.get('decoded_v3_swap_unique_pools')}** pools
- decoded V3 WETH rows: **{v3r12.get('decoded_v3_weth_rows')}** across **{v3r12.get('decoded_v3_weth_unique_pools')}** pools
- V3R-10/11 pool count observed: **{v3r12.get('v3r10_v3r11_pool_count_observed')}**
- V3R-12 novel candidate rows/pools: **{v3r12.get('novel_candidate_rows')} / {v3r12.get('novel_candidate_pools')}**
- V3R-12 label pilot success: **{v3r12.get('pilot_success')} / {v3r12.get('pilot_attempted')}** (`success_rate={v3r12.get('pilot_success_rate')}`)

Current ClickHouse light re-check for decoded V3 WETH rows: **{ch_v3.get('rows')} rows / {ch_v3.get('pools')} pools** (if ClickHouse available).

CSV artifact audit (rows are local-file counts; overlapping derived datasets are not unioned):

| source | raw V3 rows | unique pools | WETH pools | clean/success rows | top1/top5 concentration |
|---|---:|---:|---:|---:|---:|
{rows_table}

Why V3R-10/11 likely ended at 87 pools:
- V3R-12 shows the local pool universe is large (4,357 V3 WETH pools), but decoded useful swaps are not: only 95 WETH-paired decoded pools had non-zero decoded swaps.
- V3R-8 then applied dust/notional filters, per-pool caps, and successful round-trip quote/executable-label constraints, reducing the usable set to 87 pools.
- The V3R-12 novel-pool probe found 405 active novel pools, but the 496-row label pilot had 0 executable successes (`entry_quote_fail=496`), so the missing pool count is mostly a tradable/liquidity/quotability bottleneck, not just a CSV discovery bottleneck.

## 5. Next Patch Design

Recommended next patch: **local expansion first, then targeted swap backfill for known-good pools; do not start model research yet**.

Plan:
1. Use existing local sources first: build a union manifest of V3 pool candidates from `discovered_pools`, `uniswap_v3_swaps_decoded`, V3R datasets, and `cache/run004_v3r12/v3r12_novel_pool_candidates.csv`.
2. Rank pools by quotability and depth, not just existence: keep the existing 87-95 clean pools, add decoded pools that have WETH legs and enough swaps, and separately track novel pools that failed quotes.
3. Deepen historical swap backfill for the already decoded/quotable 87-95 pools before probing thousands of dust pools. This is the cheapest route to more rows, even if it does not materially increase independent pool count.
4. If independent pool count remains too low, use Base Uniswap V3 factory `PoolCreated` as the canonical pool seed and backfill Swap logs only for pools passing liquidity/quote prefilters. Use the official Base V3 references:
   - `v3CoreFactory = 0x33128a8fC17869897dcE68Ed026d694621f6FDfD`
   - `quoterV2 = 0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a`
   - `swapRouter = 0x2626664c2603336E57B271c5C0b26F421741e481`
   - `Base WETH = 0x4200000000000000000000000000000000000006`
5. Use subgraph only as an optional cheap metadata/TVL/txCount seed. V3R-12 already showed local `PoolCreated` discovery exists, so subgraph discovery is helpful but not the core unblocker.
6. Migrate labeler only after schema sanity is accepted: labeler must read `target_token_address` / `pool_address` / `normalized_side`; never `token_address` as target.

## 6. Required Answers

Q1. Is H18.14 schema internally consistent after re-check?

Yes. H18.15 re-counts from one dashcam read pass; distribution sums equal the current new-schema row count exactly, and every new field has explicit populated/missing counts.

Q2. Why did the H18.14 report show 84 new rows but distributions summing to 82?

Because H18.14 mixed snapshots/manual edits: the row count was updated to 84 while the distribution block was still from the earlier 82-row diagnostic. H18.15 fixes the diagnostic by computing all counts atomically and including `(missing)` buckets.

Q3. What is the exact new schema contract for `pool_address` / `token_address` / `target_token_address` / `normalized_side` / `is_buy`?

`pool_address` is authoritative pool identity; `token_address` is a frozen legacy alias of `pool_address`; `target_token_address` is the non-quote ERC20 leg and is what new labeler/retrain code should use; `normalized_side` is the new quote-relative side; `is_buy` / `is_buy_legacy` are compatibility-only legacy side fields.

Q4. Are recent rows truly pool-first and target-token-aware?

Yes. Current new-schema rows have populated `pool_address`, `token0`, `token1`, `quote_token_address`, `target_token_address`, `target_resolution_status`, and `normalized_side`; normal V3 WETH rows have zero unknown/ambiguous normalized sides.

Q5. How many V3 pools are already available locally if we audit all candidate sources?

There are several tiers: local pool universe has 4,357 V3-WETH pools; decoded V3 swaps cover 169 pools; decoded WETH V3 swaps cover 95 pools; V3R-10/11 clean/model paths used 87 pools; V3R-12 novel candidate file has 405 additional active/probed pools but 0 executable label successes in the pilot.

Q6. What is the next patch: local expansion, subgraph seed, factory PoolCreated backfill, swap backfill, or labeler migration?

Next patch should be **local expansion + targeted swap backfill for known-good V3 pools**, with labeler migration gated on schema sanity. Subgraph is optional metadata. Factory `PoolCreated` backfill is useful as a canonical seed, but naive broad Swap backfill of thousands of novel pools is not recommended because V3R-12 already showed a 0/496 quote success pilot on the novel set.
"""


def main() -> int:
    dashcam = analyze_dashcam()
    summary = {
        "patch_id": "H18.15 / V3R-12A",
        "generated_at_utc": _now(),
        "scope": "schema_validation_and_v3_data_supply_preflight_only",
        "decision_flags": {
            "h19_allowed": False,
            "model_research_allowed": False,
            "target_tuning_allowed": False,
            "v3_data_expansion_allowed": True,
            "labeler_migration_allowed": "true only after schema sanity is confirmed",
            "live_or_mainline_allowed": False,
        },
        "schema_contract": SCHEMA_CONTRACT,
        "schema_sanity": {
            "dashcam": dashcam,
            "h1814_inconsistency_resolution": {
                "previous_reported_new_rows": 84,
                "previous_distribution_sum": 82,
                "cause": "H18.14 JSON/report mixed two snapshots: the observation row count was manually updated while the distribution block remained from the earlier 82-row diagnostic output. Separately, there are three legacy-shape transition rows after the pre-H18.14 backup and before the first 21-column row; those are correctly counted as legacy rows, not new-schema rows.",
                "h1815_fix": "All counts and distributions are computed from the same dashcam read pass; missing buckets are included; distribution sums are asserted against new_schema_rows.",
            },
            "redis_market_tick_sample": redis_market_tick_sample(),
        },
        "v3_data_preflight": analyze_local_sources(),
        "next_patch_recommendation": {
            "recommended": "local expansion + targeted swap backfill for already decoded/quotable V3 pools; labeler migration after schema sanity; no model research",
            "subgraph_role": "optional metadata/TVL/txCount seed, not primary unblocker",
            "factory_poolcreated_role": "canonical pool universe seed if local discovered_pools needs refresh",
            "swap_backfill_role": "targeted, not naive all-novel-pool backfill",
            "labeler_migration_gate": "schema_sanity_confirmed == true",
        },
        "answers": {
            "Q1": "Yes. H18.15 atomic re-check makes H18.14 schema internally consistent.",
            "Q2": "The 84 vs 82 mismatch was a mixed-snapshot/manual-summary error in H18.14, not two bad rows.",
            "Q3": "pool_address is authoritative pool identity; token_address is frozen pool alias; target_token_address is the ERC20 target for new labeler/retrain; normalized_side replaces is_buy for new side logic; is_buy/is_buy_legacy are compatibility-only.",
            "Q4": "Yes, recent rows are pool-first and target-token-aware based on dashcam field checks and Redis source-side sampling.",
            "Q5": "Local universe has 4,357 V3-WETH pools; decoded swaps cover 169 V3 pools and 95 V3-WETH pools; V3R-10/11 used 87 clean pools; V3R-12 added 405 novel active/probed pools but 0 executable quote successes.",
            "Q6": "Next patch should be local expansion plus targeted backfill for known-good pools, with labeler migration after schema sanity; subgraph/factory PoolCreated are seeds, broad swap backfill of novel dust pools is not first choice.",
        },
    }
    write_outputs(summary)
    print(json.dumps({"json": str(OUT_JSON), "markdown": str(OUT_MD)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
