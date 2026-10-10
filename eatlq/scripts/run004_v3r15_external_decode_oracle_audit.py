from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run004_research_utils import optional_float, read_rows, rounded, write_csv_rows, write_json, write_markdown  # noqa: E402

WETH_BASE = "0x4200000000000000000000000000000000000006"
NOTIONAL_WEI = 500000000000000
ROUND_TRIP_COST = 0.006
SANITY_THRESHOLD = "[-1.0,10.0]"
QUOTER_FUNCTION = "Uniswap V3 QuoterV2.quoteExactInputSingle((address,address,uint256,uint24,uint160))"
QUOTER_ADDRESS_BASE = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"

H165_PILOT = ROOT / "mvp/diagnostics/run004_h165_pilot_labeled_dataset.csv"
H165_CHECKPOINT = ROOT / "mvp/diagnostics/run004_h165_pilot_checkpoint_report.json"
H165_PATCH = ROOT / "mvp/diagnostics/run004_patch_h165_rpc_candidate_repair_labeling_pilot.json"
H166_V3_PLAN = ROOT / "mvp/diagnostics/run004_h166_v3_repair_branch_plan.json"
V3R1_FORENSIC = ROOT / "mvp/diagnostics/run004_v3r1_h165_failure_forensic.csv"

OUT_DIR = ROOT / "mvp/diagnostics"
RUN_DIR = ROOT / "mvp/runs"
QUERY_DIR = ROOT / "queries"


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def _sql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _dt_from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC)


def _ch_query(query: str, timeout: int = 70) -> list[dict[str, Any]]:
    q = query.rstrip(";")
    if " format " not in q.lower():
        q = f"{q} FORMAT JSONEachRow"
    cmd = [
        "docker",
        "exec",
        "eatlq-clickhouse-1",
        "clickhouse-client",
        "--database",
        "eatlq",
        "--query",
        q,
    ]
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def _weth_leg(token0: str, token1: str) -> str:
    if _lower(token0) == WETH_BASE:
        return "token0"
    if _lower(token1) == WETH_BASE:
        return "token1"
    return "missing"


def _token_paths(token0: str, token1: str) -> dict[str, str]:
    leg = _weth_leg(token0, token1)
    token = _lower(token1 if leg == "token0" else token0)
    return {
        "weth_leg": leg,
        "current_entry_token_in": WETH_BASE,
        "current_entry_token_out": token,
        "current_exit_token_in": token,
        "current_exit_token_out": WETH_BASE,
    }


def _sample_bucket(row: dict[str, str]) -> str:
    label_type = str(row.get("label_type") or row.get("failure_reason") or "")
    if label_type == "executable":
        return "v3_control_success"
    if label_type == "executable_exit_drained":
        return "executable_exit_drained"
    if label_type in {"fail_sanity_clip", "fail_v3_entry_zero"}:
        return label_type
    if str(row.get("label_status")) == "success":
        return "v3_control_success"
    return label_type or "unknown"


def _load_v3r1_lookup() -> dict[tuple[str, str, str], dict[str, str]]:
    if not V3R1_FORENSIC.exists():
        return {}
    out: dict[tuple[str, str, str], dict[str, str]] = {}
    for row in read_rows(V3R1_FORENSIC):
        key = (_lower(row.get("pool_address")), str(row.get("timestamp_ms") or ""), str(row.get("entry_block") or ""))
        out[key] = row
    return out


def _select_samples() -> list[dict[str, Any]]:
    rows = [r for r in read_rows(H165_PILOT) if str(r.get("pool_class")) == "v3"]
    by_bucket: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_bucket[_sample_bucket(row)].append(row)
    selected: list[dict[str, str]] = []
    for bucket in ["fail_sanity_clip", "fail_v3_entry_zero", "executable_exit_drained", "v3_control_success"]:
        selected.extend(by_bucket.get(bucket, [])[:20])
    # Keep all H16.5 V3 rows when the sample is small.
    if len(rows) <= 120:
        selected = rows
    lookup = _load_v3r1_lookup()
    out: list[dict[str, Any]] = []
    for idx, row in enumerate(selected):
        token0 = _lower(row.get("token0_address"))
        token1 = _lower(row.get("token1_address"))
        paths = _token_paths(token0, token1)
        key = (_lower(row.get("pool_address")), str(row.get("timestamp_ms") or ""), str(row.get("entry_block") or ""))
        q = lookup.get(key, {})
        out.append(
            {
                "row_id": idx,
                "h165_pilot_row_id": row.get("pilot_row_id", ""),
                "candidate_id": row.get("candidate_id", ""),
                "sample_bucket": _sample_bucket(row),
                "pool_address": _lower(row.get("pool_address")),
                "timestamp": row.get("entry_time", ""),
                "timestamp_ms": row.get("timestamp_ms", ""),
                "block_number": row.get("entry_block", ""),
                "entry_block": row.get("entry_block", ""),
                "exit_block": row.get("exit_block", ""),
                "tx_hash": row.get("tx_hash", ""),
                "log_index": row.get("log_index", ""),
                "token0": token0,
                "token1": token1,
                "token0_decimals": row.get("token0_decimals", ""),
                "token1_decimals": row.get("token1_decimals", ""),
                "weth_leg": paths["weth_leg"],
                "fee_tier": row.get("fee_bps", ""),
                "current_tokenIn_entry": paths["current_entry_token_in"],
                "current_tokenOut_entry": paths["current_entry_token_out"],
                "current_tokenIn_exit": paths["current_exit_token_in"],
                "current_tokenOut_exit": paths["current_exit_token_out"],
                "amountIn_before_decimals_conversion": str(NOTIONAL_WEI),
                "amountIn_after_decimals_conversion": NOTIONAL_WEI / 1e18,
                "current_quoter_function": QUOTER_FUNCTION,
                "current_quoter_address": QUOTER_ADDRESS_BASE,
                "raw_quote_result": q.get("raw_quote_result") or q.get("entry_quote_raw", ""),
                "entry_quote_amount_out": q.get("entry_quote_amount_out", ""),
                "exit_quote_amount_out": q.get("exit_quote_amount_out", ""),
                "net_return_before_sanity": q.get("net_return_before_sanity", ""),
                "sanity_clip_threshold": SANITY_THRESHOLD,
                "current_failure_reason": row.get("failure_reason") or ("" if row.get("label_type") == "executable" else row.get("label_type", "")),
                "current_label_type": row.get("label_type", ""),
                "decoded_event_count": row.get("decoded_event_count", ""),
                "decoded_join_status": row.get("decoded_join_status", ""),
                "decoded_source_row": "",
                "discovered_pools_metadata_row": "",
            }
        )
    return out


def _enrich_local_oracle(samples: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    pools = sorted({str(r["pool_address"]) for r in samples if r.get("pool_address")})
    if not pools:
        return samples, {"metadata_coverage": 0, "decoded_window_coverage": 0}
    pool_sql = ",".join(_sql_string(p) for p in pools)
    meta_rows = _ch_query(
        f"""
        SELECT
          lower(pool_address) AS pool_address,
          any(lower(pool_type)) AS pool_type,
          any(lower(token0_address)) AS token0_address,
          any(lower(token1_address)) AS token1_address,
          any(token0_decimals) AS token0_decimals,
          any(token1_decimals) AS token1_decimals,
          any(fee_bps) AS fee_bps,
          any(has_weth_leg) AS has_weth_leg,
          any(created_block) AS created_block,
          any(toString(discovered_at)) AS discovered_at
        FROM eatlq.discovered_pools
        WHERE chain_id = 8453 AND lower(pool_address) IN ({pool_sql})
        GROUP BY pool_address
        """
    )
    meta = {r["pool_address"]: r for r in meta_rows}
    ts_values = [_int(r.get("timestamp_ms")) for r in samples if _int(r.get("timestamp_ms"))]
    decoded: list[dict[str, Any]] = []
    if ts_values:
        start = min(ts_values) - 3600_000
        end = max(ts_values) + 3600_000
        decoded = _ch_query(
            f"""
            SELECT
              lower(pool_address) AS pool_address,
              tx_hash,
              log_index,
              block_number,
              toString(block_time) AS block_time,
              toUnixTimestamp64Milli(parseDateTime64BestEffort(block_time)) AS ts_ms,
              lower(sender) AS sender,
              lower(recipient) AS recipient,
              toString(amount0) AS amount0,
              toString(amount1) AS amount1,
              toString(sqrt_price_x96) AS sqrt_price_x96,
              toString(liquidity) AS liquidity,
              tick
            FROM eatlq.uniswap_v3_swaps_decoded
            WHERE chain_id = 8453
              AND lower(pool_address) IN ({pool_sql})
              AND toUnixTimestamp64Milli(parseDateTime64BestEffort(block_time)) >= {start}
              AND toUnixTimestamp64Milli(parseDateTime64BestEffort(block_time)) <= {end}
            ORDER BY pool_address, abs(ts_ms - {int(sum(ts_values) / len(ts_values))})
            """
        )
    by_pool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in decoded:
        by_pool[_lower(row.get("pool_address"))].append(row)
    matched = 0
    for sample in samples:
        pool = str(sample["pool_address"])
        m = meta.get(pool, {})
        sample["discovered_pools_metadata_row"] = json.dumps(m, sort_keys=True) if m else ""
        ts = _int(sample.get("timestamp_ms"))
        candidates = by_pool.get(pool, [])
        nearest = None
        if candidates and ts:
            nearest = min(candidates, key=lambda r: abs(_int(r.get("ts_ms")) - ts))
            if abs(_int(nearest.get("ts_ms")) - ts) > 1800_000:
                nearest = None
        if nearest:
            matched += 1
            sample["decoded_source_row"] = json.dumps(nearest, sort_keys=True)
            sample["local_decoded_time_delta_sec"] = rounded(abs(_int(nearest.get("ts_ms")) - ts) / 1000.0, 3)
        else:
            sample["local_decoded_time_delta_sec"] = ""
    coverage = {
        "sample_rows": len(samples),
        "unique_pools": len(pools),
        "metadata_matched_rows": sum(1 for s in samples if s.get("discovered_pools_metadata_row")),
        "decoded_window_matched_rows": matched,
        "metadata_coverage": rounded(sum(1 for s in samples if s.get("discovered_pools_metadata_row")) / len(samples), 8) if samples else 0,
        "decoded_window_coverage": rounded(matched / len(samples), 8) if samples else 0,
        "decoded_events_loaded": len(decoded),
    }
    return samples, coverage


def _hex_pool(pool: str) -> str:
    return pool if pool.startswith("0x") else f"0x{pool}"


def _dune_sql(samples: list[dict[str, Any]]) -> str:
    values = []
    for s in samples:
        ts = _dt_from_ms(_int(s["timestamp_ms"]))
        values.append(f"    ({int(s['row_id'])}, {_hex_pool(s['pool_address'])}, TIMESTAMP '{ts.isoformat().replace('+00:00', '')}')")
    return f"""-- Run-004 V3R-1.5 Dune oracle query for Base dex.trades
-- Paste into Dune. Uses pool + +/- 30 minute window because H16.5 V3 rows are metadata time-offset rows without tx_hash/log_index.
WITH samples(row_id, pool_address, sample_time) AS (
  VALUES
{',\n'.join(values)}
)
SELECT
  s.row_id,
  dt.blockchain,
  dt.project,
  dt.version,
  dt.block_time,
  dt.block_number,
  dt.project_contract_address AS pool_or_project_contract_address,
  dt.tx_hash,
  dt.evt_index,
  dt.token_bought_address,
  dt.token_bought_symbol,
  dt.token_bought_amount,
  dt.token_sold_address,
  dt.token_sold_symbol,
  dt.token_sold_amount,
  dt.amount_usd,
  dt.taker,
  dt.maker
FROM samples s
LEFT JOIN dex.trades dt
  ON dt.blockchain = 'base'
 AND dt.project_contract_address = s.pool_address
 AND dt.block_time >= s.sample_time - INTERVAL '30' minute
 AND dt.block_time <  s.sample_time + INTERVAL '30' minute
ORDER BY s.row_id, abs(date_diff('second', s.sample_time, dt.block_time))
LIMIT 5000;
"""


def _bitquery_graphql(samples: list[dict[str, Any]]) -> str:
    first = samples[0]
    ts = _dt_from_ms(_int(first["timestamp_ms"]))
    start = (ts - timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    end = (ts + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    pool = first["pool_address"]
    return f"""# Run-004 V3R-1.5 Bitquery Base DEXTrades template
# Repeat with variables from run004_v3r15_external_oracle_query_pack.json samples[].
query BaseDexTradesForV3Sample {{
  EVM(network: base) {{
    DEXTrades(
      where: {{
        Trade: {{ Dex: {{ SmartContract: {{ is: "{pool}" }} }} }}
        Block: {{ Time: {{ since: "{start}", till: "{end}" }} }}
      }}
      orderBy: {{ ascending: Block_Time }}
      limit: {{ count: 100 }}
    ) {{
      Block {{ Time Number }}
      Transaction {{ Hash From To }}
      Trade {{
        Sender
        Dex {{ ProtocolName ProtocolVersion SmartContract }}
        Buy {{
          Buyer Seller Amount AmountInUSD Price PriceInUSD
          Currency {{ Symbol Name SmartContract Decimals }}
        }}
        Sell {{
          Buyer Seller Amount AmountInUSD Price PriceInUSD
          Currency {{ Symbol Name SmartContract Decimals }}
        }}
      }}
    }}
  }}
}}
"""


def _subgraph_graphql(samples: list[dict[str, Any]]) -> str:
    first = samples[0]
    ts = _dt_from_ms(_int(first["timestamp_ms"]))
    start = int((ts - timedelta(minutes=30)).timestamp())
    end = int((ts + timedelta(minutes=30)).timestamp())
    pool = first["pool_address"]
    return f"""# Run-004 V3R-1.5 Uniswap V3 subgraph Swap entity template
# Endpoint depends on hosted/gateway availability for Base; use the current Uniswap V3 Base subgraph endpoint.
query V3SwapsForSample {{
  swaps(
    first: 100
    orderBy: timestamp
    orderDirection: asc
    where: {{
      pool: "{pool}"
      timestamp_gte: {start}
      timestamp_lt: {end}
    }}
  ) {{
    id
    timestamp
    transaction {{ id blockNumber timestamp }}
    pool {{ id token0 {{ id symbol decimals }} token1 {{ id symbol decimals }} feeTier }}
    sender
    recipient
    origin
    amount0
    amount1
    amountUSD
    sqrtPriceX96
    tick
    logIndex
  }}
}}
"""


def _query_pack(samples: list[dict[str, Any]]) -> dict[str, Any]:
    QUERY_DIR.joinpath("dune_sql").mkdir(parents=True, exist_ok=True)
    QUERY_DIR.joinpath("bitquery_graphql").mkdir(parents=True, exist_ok=True)
    QUERY_DIR.joinpath("uniswap_v3_subgraph").mkdir(parents=True, exist_ok=True)
    dune = _dune_sql(samples)
    bitquery = _bitquery_graphql(samples)
    subgraph = _subgraph_graphql(samples)
    (QUERY_DIR / "dune_sql/run004_v3r15_base_dex_trades.sql").write_text(dune)
    (QUERY_DIR / "bitquery_graphql/run004_v3r15_base_dex_trades.graphql").write_text(bitquery)
    (QUERY_DIR / "uniswap_v3_subgraph/run004_v3r15_v3_swaps.graphql").write_text(subgraph)
    api_tokens = {
        "DUNE_API_KEY": bool(os.environ.get("DUNE_API_KEY") or os.environ.get("DUNE_API_TOKEN")),
        "BITQUERY_API_KEY": bool(os.environ.get("BITQUERY_API_KEY") or os.environ.get("BITQUERY_TOKEN")),
        "THEGRAPH_API_KEY": bool(os.environ.get("GRAPH_API_KEY") or os.environ.get("THEGRAPH_API_KEY")),
    }
    sample_vars = []
    for s in samples:
        ts = _dt_from_ms(_int(s["timestamp_ms"]))
        sample_vars.append(
            {
                "row_id": s["row_id"],
                "pool_address": s["pool_address"],
                "sample_time": ts.isoformat(),
                "window_start": (ts - timedelta(minutes=30)).isoformat(),
                "window_end": (ts + timedelta(minutes=30)).isoformat(),
                "token0": s["token0"],
                "token1": s["token1"],
                "fee_tier": s["fee_tier"],
            }
        )
    return {
        "oracle_sources": {
            "dune": {
                "official_docs": "https://docs.dune.com/data-catalog/curated/dex-trades/evm/dex-trades",
                "query_file": "queries/dune_sql/run004_v3r15_base_dex_trades.sql",
                "status": "query_pack_generated_api_token_missing" if not api_tokens["DUNE_API_KEY"] else "token_present_manual_or_api_query_possible",
            },
            "bitquery": {
                "official_docs": "https://docs.bitquery.io/docs/blockchain/Base/base-dextrades/",
                "query_file": "queries/bitquery_graphql/run004_v3r15_base_dex_trades.graphql",
                "status": "query_pack_generated_api_token_missing" if not api_tokens["BITQUERY_API_KEY"] else "token_present_manual_or_api_query_possible",
            },
            "uniswap_v3_subgraph": {
                "official_schema": "https://docs.uniswap.org/api/subgraph/subgraphs-devs/schemas/swap",
                "query_file": "queries/uniswap_v3_subgraph/run004_v3r15_v3_swaps.graphql",
                "status": "query_pack_generated_api_token_missing" if not api_tokens["THEGRAPH_API_KEY"] else "token_present_manual_or_api_query_possible",
            },
            "open_source_decoders": {
                "eth_defi_decode_swap": "https://web3-ethereum-defi.readthedocs.io/api/uniswap_v3/_autosummary_uniswap_v3/eth_defi.uniswap_v3.events.decode_swap.html",
                "dextrades": "https://github.com/elyase/dextrades",
                "degenbot": "https://github.com/BowTiedDevil/degenbot",
            },
        },
        "api_token_status": api_tokens,
        "samples": sample_vars,
        "dune_sql": dune,
        "bitquery_graphql_template": bitquery,
        "uniswap_v3_subgraph_template": subgraph,
        "local_decoder_reproduction_plan": [
            "Fetch raw_evm_logs by pool address and Swap event topic in the sample time window.",
            "Decode topics[1]/topics[2] as indexed sender/recipient.",
            "Decode data as int256 amount0, int256 amount1, uint160 sqrtPriceX96, uint128 liquidity, int24 tick.",
            "Map token0/token1 from discovered_pools or pool.token0()/token1() at entry block.",
            "Interpret signed amounts: positive means token enters pool, negative means token exits pool.",
            "Normalize abs(amount0) / 10^token0_decimals and abs(amount1) / 10^token1_decimals.",
            "Infer WETH-side trade: if WETH leg amount is positive, trader sold WETH to buy token; if negative, trader bought WETH by selling token.",
            "Compare inferred direction and normalized amounts against Dune dex.trades / Bitquery DEXTrades / Subgraph Swap.",
        ],
    }


def _comparison(samples: list[dict[str, Any]], coverage: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for s in samples:
        labels: list[str] = []
        if not s.get("decoded_source_row"):
            labels.append("oracle_unavailable")
        if not s.get("discovered_pools_metadata_row"):
            labels.append("pool_metadata_mismatch")
        if s.get("weth_leg") == "missing":
            labels.append("weth_leg_mismatch")
            labels.append("direction_mismatch")
        net = optional_float(s.get("net_return_before_sanity"))
        if s.get("current_failure_reason") == "fail_sanity_clip":
            # H16.5 has no oracle rows; classify using current quote symptoms only.
            if net is not None and (net < -1.0 or net > 10.0):
                labels.append("quoter_path_bug")
                labels.append("amount_scale_mismatch")
            else:
                labels.append("sanity_clip_too_strict")
        if s.get("current_failure_reason") == "fail_v3_entry_zero":
            labels.append("quoter_path_bug")
        if s.get("current_failure_reason") == "executable_exit_drained":
            labels.append("true_market_failure")
        if s.get("fee_tier") in {"", "0"}:
            labels.append("fee_tier_mismatch")
        rows.append(
            {
                "row_id": s["row_id"],
                "pool_address": s["pool_address"],
                "sample_bucket": s["sample_bucket"],
                "current_failure_reason": s["current_failure_reason"],
                "oracle_sources_checked": ["local_clickhouse_decoded", "discovered_pools"],
                "external_oracle_status": "query_pack_generated_not_executed_no_api_token",
                "identity_match": "pool_time_window_only" if s.get("decoded_source_row") else "no_decoded_row_in_local_window",
                "metadata_match": bool(s.get("discovered_pools_metadata_row")),
                "direction_match": None,
                "amount_match": None,
                "discrepancy_labels": sorted(set(labels)),
            }
        )
    return {
        "summary": {
            "rows": len(rows),
            "external_oracle_rows_queried": 0,
            "external_oracle_reason": "Dune/Bitquery/The Graph API tokens are not present; generated executable query pack instead.",
            "local_decoded_window_coverage": coverage.get("decoded_window_coverage"),
            "metadata_coverage": coverage.get("metadata_coverage"),
            "discrepancy_label_counts": dict(Counter(label for row in rows for label in row["discrepancy_labels"]).most_common()),
        },
        "rows": rows,
    }


def _root_cause(samples: list[dict[str, Any]], comparison: dict[str, Any], checkpoint: dict[str, Any]) -> dict[str, Any]:
    buckets = Counter(str(s["sample_bucket"]) for s in samples)
    decoded_count = sum(1 for s in samples if _int(s.get("decoded_event_count")) > 0)
    return {
        "ranking": [
            {
                "rank": 1,
                "root_cause": "V3 candidate rows are metadata time-offset rows, not swap-backed trade rows",
                "evidence": f"H16.5 V3 sample decoded_event_count>0 rows: {decoded_count}/{len(samples)}; local decoded window coverage: {comparison['summary'].get('local_decoded_window_coverage')}",
                "impact": "Quoter is asked to replay arbitrary post-discovery windows rather than known executable trade contexts.",
            },
            {
                "rank": 2,
                "root_cause": "V3 quote outputs are not validated against decoded signed amount0/amount1 or external trade direction",
                "evidence": "H16.5 stores no raw V3 swap tx/log and no oracle bought/sold side; fail_sanity_clip dominates.",
                "impact": "Amount scale/direction errors collapse into fail_sanity_clip instead of a precise bug label.",
            },
            {
                "rank": 3,
                "root_cause": "Entry-zero combines several causes",
                "evidence": "fail_v3_entry_zero rows have no local decoded event identity; causes may include no initialized liquidity at historical block, wrong fee, dead pool, or no pool at computed fee.",
                "impact": "Direct cause cannot be separated without pool.slot0/liquidity and decoded oracle trade identity.",
            },
            {
                "rank": 4,
                "root_cause": "Metadata is necessary but not sufficient",
                "evidence": "discovered_pools metadata coverage is high, but decoded coverage is low and sample rows lack tx_hash/log_index.",
                "impact": "token0/token1/WETH leg are usually available, yet execution context remains missing.",
            },
        ],
        "answers": {
            "why_v3_success_only_5pct": "Because H16.5 V3 rows are metadata-backed time offsets with almost no decoded trade identity, so historical Quoter calls often hit arbitrary/illiquid/unvalidated V3 states; the apparent 5% success rows are executable_exit_drained, not clean executable controls.",
            "fail_v3_entry_zero_most_likely_direct_reason": "Entry quote returned zero/reverted at the historical block for the chosen WETH->non-WETH path and fee tier; without oracle tx/log, the most likely direct causes are no initialized liquidity at that block or fee/pool state mismatch, not V2 mainline behavior.",
            "fail_sanity_clip_market_or_bug": "More likely labeler/sample-construction/quote-path bug until external oracle proves otherwise; sanity clip is catching unphysical quote paths, not a trainable market label.",
            "amount0_amount1_signed_handling": "The local decoded convention should be positive=token enters pool, negative=token exits pool. H16.5 labeler does not consume amount0/amount1 for entry/exit reconstruction, so signed handling is unvalidated rather than proven wrong.",
            "tokenIn_tokenOut_often_reversed": "Not proven from metadata; WETH-leg inference is deterministic for WETH pools, but external oracle is required because H16.5 has no bought/sold side.",
            "fee_tier_correct": "Usually plausible from discovered_pools fee_bps * 100, but not oracle-confirmed; fee mismatch remains a suspect for entry-zero rows.",
            "amount_scale_decimals_correct": "WETH input scale is correct at 0.0005 ETH, but token output normalization is not validated against decimals/oracle amounts in H16.5.",
            "historical_block_quoter_normal": "QuoterV2 address/function are standard, but historical calls can revert or return pathological paths when pool state/liquidity is unsuitable.",
            "discovered_pools_metadata_sufficient": "No. Metadata can identify WETH leg and fee, but it does not provide trade identity, decoded direction, amountUSD, or reliable execution context.",
            "sanity_clip_too_strict": "No evidence it is too strict. It is acting as a guardrail; relaxing it would admit untrusted V3 labels.",
        },
        "h165_context": {
            "checkpoint_v3_success_rate": checkpoint.get("v2_v3_success_rate", {}).get("v3"),
            "failure_reasons": checkpoint.get("failure_reasons"),
            "sample_buckets": dict(buckets.most_common()),
        },
    }


def _repair_plan() -> dict[str, Any]:
    return {
        "v3r2_scope": "small V3-only repair; no mixed dataset, no V2 mainline changes",
        "items": [
            {
                "priority": 1,
                "item": "token direction / WETH leg inference",
                "affected_file_function": "scripts/label_dashcam_executable.py::label_v3 and V3R helper",
                "expected_impact": "Prevent WETH->token and token->WETH inversion before quote.",
                "risk": "Low if restricted to V3 path.",
                "test_sample": "H16.5 V3 rows with both token0=WETH and token1=WETH.",
                "pass_condition": "Oracle bought/sold side agrees with current_tokenIn/tokenOut for >=95% matched rows.",
            },
            {
                "priority": 2,
                "item": "amount scale / decimals normalization",
                "affected_file_function": "new V3R decoded bridge helper before label_v3",
                "expected_impact": "Separate raw uint256 quote scale from normalized token amounts and USD sanity.",
                "risk": "Medium; decimals mistakes silently poison labels.",
                "test_sample": "Rows with token decimals != 18 and V3 Swap amount0/amount1 available.",
                "pass_condition": "Normalized amounts match Dune/Bitquery/Subgraph within tolerance.",
            },
            {
                "priority": 3,
                "item": "fee tier lookup",
                "affected_file_function": "scripts/label_dashcam_executable.py::label_v3 fee_bps conversion",
                "expected_impact": "Avoid no-pool-at-fee entry zeros.",
                "risk": "Low; can validate against pool.fee()/subgraph feeTier.",
                "test_sample": "fail_v3_entry_zero rows with fee_bps 3/30/100.",
                "pass_condition": "fee_param equals on-chain/subgraph feeTier for every matched row.",
            },
            {
                "priority": 4,
                "item": "Quoter version / function params",
                "affected_file_function": "scripts/label_dashcam_executable.py::call_quoter_v3",
                "expected_impact": "Confirm Base QuoterV2 tuple ABI and selector are correct.",
                "risk": "Low; official selector/address already match.",
                "test_sample": "Known liquid Base WETH/USDC V3 pool control.",
                "pass_condition": "Raw eth_call decodes amountOut,sqrtPriceX96After,ticksCrossed,gasEstimate.",
            },
            {
                "priority": 5,
                "item": "historical block eth_call handling",
                "affected_file_function": "scripts/label_dashcam_executable.py::_eth_call/call_quoter_v3",
                "expected_impact": "Classify missing state/revert separately from zero executable quote.",
                "risk": "Low.",
                "test_sample": "Entry-zero samples before/after created_block.",
                "pass_condition": "fail_v3_entry_zero split into no_code, zero_liquidity, quoter_revert, no_pool_at_fee.",
            },
            {
                "priority": 6,
                "item": "sanity clip thresholds",
                "affected_file_function": "scripts/label_dashcam_executable.py::_sanity_clip",
                "expected_impact": "Keep guardrail; add diagnostics instead of relaxing threshold.",
                "risk": "High if relaxed.",
                "test_sample": "fail_sanity_clip rows with oracle amountUSD.",
                "pass_condition": "No row with external-oracle mismatch enters executable labels.",
            },
            {
                "priority": 7,
                "item": "zero-entry quote handling",
                "affected_file_function": "scripts/label_dashcam_executable.py::label_v3",
                "expected_impact": "Better root-cause labels for repair metrics.",
                "risk": "Low.",
                "test_sample": "All fail_v3_entry_zero rows.",
                "pass_condition": "Every zero entry row has a non-generic reason.",
            },
            {
                "priority": 8,
                "item": "decoded bridge enrichment compatibility",
                "affected_file_function": "scripts/run004_research_harness.py V3 candidate selection / decoded join",
                "expected_impact": "V3 pilots use swap-backed rows with tx/log identity.",
                "risk": "Medium; shared harness changes need patch plan before edit.",
                "test_sample": "V3-only decoded-backed 100-row pilot.",
                "pass_condition": "decoded coverage >=90% and no mixed V2/V3 output.",
            },
        ],
    }


def _markdown(patch: dict[str, Any]) -> str:
    sample = patch["sample_summary"]
    oracle = patch["oracle_comparison"]["summary"]
    root = patch["root_cause_ranking"]["ranking"]
    return f"""# Run-004 Patch V3R-1.5 - External Decode Oracle Audit

## Scope
- V3-only forensic / oracle query pack / repair plan.
- Mixed V2/V3 dataset created: `False`
- V2-only H16.6/H17 affected: `False`
- Production model / active model / runtime trading code affected: `False`

## Samples
- V3 sample rows: {sample['rows']}
- Buckets: {sample['buckets']}
- Clean V3 executable controls: {sample['clean_executable_control_rows']}
- Note: {sample['control_note']}
- Local metadata coverage: {patch['local_coverage']['metadata_coverage']}
- Local decoded window coverage: {patch['local_coverage']['decoded_window_coverage']}

## Oracle Status
- Dune / Bitquery / Uniswap V3 subgraph API calls executed: `0`
- Reason: {oracle['external_oracle_reason']}
- Query pack: `mvp/diagnostics/run004_v3r15_external_oracle_query_pack.json`
- SQL/GraphQL files written under `queries/`

## Root Cause Ranking
1. {root[0]['root_cause']}
2. {root[1]['root_cause']}
3. {root[2]['root_cause']}
4. {root[3]['root_cause']}

## Gate
- V3 mainline allowed: `False`
- Larger V3 repair pilot allowed only if V3-only success is 60%-80%.
- Mixed dataset reconsideration only if V3-only success >=80% and decoded coverage >=90%.
- V2-only H16/H17 do not wait for V3.
"""


def main() -> None:
    samples = _select_samples()
    samples, local_coverage = _enrich_local_oracle(samples)
    write_csv_rows(OUT_DIR / "run004_v3r15_forensic_sample_set.csv", samples)

    query_pack = _query_pack(samples)
    write_json(OUT_DIR / "run004_v3r15_external_oracle_query_pack.json", query_pack)

    comparison = _comparison(samples, local_coverage)
    write_json(OUT_DIR / "run004_v3r15_oracle_comparison.json", comparison)

    checkpoint = json.loads(H165_CHECKPOINT.read_text()) if H165_CHECKPOINT.exists() else {}
    root_cause = _root_cause(samples, comparison, checkpoint)
    write_json(OUT_DIR / "run004_v3r15_root_cause_ranking.json", root_cause)

    repair_plan = _repair_plan()
    write_json(OUT_DIR / "run004_v3r15_v3r2_repair_plan.json", repair_plan)

    sample_summary = {
        "rows": len(samples),
        "buckets": dict(Counter(str(s.get("sample_bucket")) for s in samples).most_common()),
        "clean_executable_control_rows": sum(1 for s in samples if s.get("current_label_type") == "executable"),
        "label_status_success_rows": sum(1 for s in samples if s.get("current_label_type") == "executable_exit_drained"),
        "control_note": "H16.5 contains no clean V3 label_type=executable control rows; the 3 label_status success rows are executable_exit_drained and remain quarantined.",
        "sources": {
            "h165_pilot": str(H165_PILOT),
            "h165_checkpoint": str(H165_CHECKPOINT),
            "h165_patch": str(H165_PATCH),
            "h166_v3_plan": str(H166_V3_PLAN),
            "v3r1_forensic_optional": str(V3R1_FORENSIC) if V3R1_FORENSIC.exists() else "",
        },
    }
    gate = {
        "allow_v3_mainline": False,
        "allow_mixed_v2_v3_dataset": False,
        "v3_rejoin_rule": "No mainline unless V3-only pilot success >= 80% and decoded coverage >= 90%; 60%-80% permits only larger V3 repair pilot; <60% continue repair.",
        "v2_mainline_waits_for_v3": False,
    }
    patch = {
        "patch": "run-004-v3r15-external-decode-oracle-audit",
        "generated_at": datetime.now(UTC).isoformat(),
        "sample_summary": sample_summary,
        "local_coverage": local_coverage,
        "oracle_sources_used": ["local_clickhouse_discovered_pools", "local_clickhouse_uniswap_v3_swaps_decoded", "official_docs_query_pack"],
        "external_oracle_sources_unavailable": ["dune_api_token_missing", "bitquery_api_token_missing", "thegraph_api_token_missing"],
        "oracle_comparison": comparison,
        "root_cause_ranking": root_cause,
        "v3r2_repair_plan": repair_plan,
        "gate": gate,
        "outputs": {
            "patch_json": str(OUT_DIR / "run004_patch_v3r15_external_decode_oracle_audit.json"),
            "sample_csv": str(OUT_DIR / "run004_v3r15_forensic_sample_set.csv"),
            "query_pack_json": str(OUT_DIR / "run004_v3r15_external_oracle_query_pack.json"),
            "comparison_json": str(OUT_DIR / "run004_v3r15_oracle_comparison.json"),
            "root_cause_json": str(OUT_DIR / "run004_v3r15_root_cause_ranking.json"),
            "repair_plan_json": str(OUT_DIR / "run004_v3r15_v3r2_repair_plan.json"),
            "markdown": str(RUN_DIR / "run-004-patch-v3r15-external-decode-oracle-audit.md"),
        },
        "safety_assertions": {
            "diagnostics_only": True,
            "v3_only": True,
            "no_v2_only_h166_outputs_modified": True,
            "no_mixed_v2_v3_dataset": True,
            "no_training": True,
            "no_production_retrain": True,
            "no_promote": True,
            "no_live": True,
            "no_production_model_write": True,
            "no_active_model_overwrite": True,
        },
    }
    write_json(OUT_DIR / "run004_patch_v3r15_external_decode_oracle_audit.json", patch)
    write_markdown(RUN_DIR / "run-004-patch-v3r15-external-decode-oracle-audit.md", _markdown(patch))
    print(json.dumps({"sample_summary": sample_summary, "local_coverage": local_coverage, "gate": gate}, indent=2))


if __name__ == "__main__":
    main()
