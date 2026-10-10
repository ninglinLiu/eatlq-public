"""Offline ranking of table candidates from sealed system.* metadata rows.

Never claims data content, Clanker activity, or factory verification.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

LOGS_REQUIRED = (
    "block_number",
    "block_hash",
    "block_timestamp",
    "transaction_hash",
    "transaction_index",
    "log_index",
    "address",
    "topic0",
    "topic1",
    "topic2",
    "topic3",
    "data",
)

ALIASES: dict[str, tuple[str, ...]] = {
    "block_number": ("blockNumber", "block_num", "block", "height"),
    "block_hash": ("blockHash", "block_hash_hex"),
    "block_timestamp": ("blockTimestamp", "timestamp", "block_time", "ts"),
    "transaction_hash": ("transactionHash", "tx_hash", "txid", "hash"),
    "transaction_index": ("transactionIndex", "tx_index", "txIndex"),
    "log_index": ("logIndex", "log_idx", "index"),
    "address": ("contract_address", "contractAddress", "token_address", "emitter"),
    "topic0": ("topic_0", "t0", "event_signature"),
    "topic1": ("topic_1", "t1"),
    "topic2": ("topic_2", "t2"),
    "topic3": ("topic_3", "t3"),
    "data": ("raw_data", "data_hex", "log_data"),
}

ROLE_HINTS: dict[str, tuple[str, ...]] = {
    "logs": ("log", "logs", "event_log", "receipt_log"),
    "transactions": ("transaction", "transactions", "tx", "txs"),
    "events": ("event", "events"),
    "traces": ("trace", "traces", "call_trace"),
    "transfers": ("transfer", "transfers", "erc20_transfer", "token_transfer"),
}


@dataclass
class FieldMapping:
    required: str
    status: str  # exact | alias_candidate | missing | UNKNOWN
    matched_column: str | None = None
    matched_type: str | None = None
    notes: str = ""


SCHEMA_FINGERPRINT_V02 = "lpi.schema_fingerprint.v0.2.0"
UNKNOWN_STR = "UNKNOWN"


@dataclass
class TableCandidate:
    database: str
    table: str
    engine: str
    partition_key: str
    sorting_key: str
    primary_key: str
    sampling_key: str
    storage_policy: str
    schema_fingerprint: str
    candidate_role: str
    field_mappings: list[FieldMapping]
    exact_match_fields: list[str]
    alias_match_fields: list[str]
    missing_fields: list[str]
    unknown_fields: list[str]
    block_pruning_status: str
    bounded_probe_safety_status: str
    rejection_reasons: list[str]
    evidence_refs: list[str]
    score: int = 0
    data_content_status: str = "UNKNOWN"  # system metadata cannot prove content
    clanker_presence_status: str = "UNKNOWN"
    factory_address_status: str = "UNKNOWN"
    activity_window_status: str = "UNKNOWN"
    legacy_schema_fingerprint_v0_1: str = ""
    schema_fingerprint_v0_2: str = ""
    schema_fingerprint_version: str = SCHEMA_FINGERPRINT_V02

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _missing_or(value: str | None) -> str:
    if value is None or value == "":
        return UNKNOWN_STR
    return value


def schema_fingerprint(database: str, table: str, columns: list[tuple[str, str]]) -> str:
    """Legacy v0.1 fingerprint (name/type only). Kept for comparison; do not use for new plans."""
    payload = {
        "database": database,
        "table": table,
        "columns": sorted((n, t) for n, t in columns),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def schema_fingerprint_v02(
    *,
    database: str,
    table: str,
    engine: str | None,
    partition_key: str | None,
    sorting_key: str | None,
    primary_key: str | None,
    sampling_key: str | None,
    storage_policy: str | None,
    columns: list[dict[str, Any]],
) -> str:
    """v0.2 fingerprint including engine/keys and column key-membership flags."""
    col_payload = []
    for c in columns:
        col_payload.append(
            {
                "name": c.get("name") or UNKNOWN_STR,
                "type": c.get("type") or UNKNOWN_STR,
                "is_in_partition_key": _missing_or(str(c["is_in_partition_key"]) if "is_in_partition_key" in c else None),
                "is_in_sorting_key": _missing_or(str(c["is_in_sorting_key"]) if "is_in_sorting_key" in c else None),
                "is_in_primary_key": _missing_or(str(c["is_in_primary_key"]) if "is_in_primary_key" in c else None),
                "is_in_sampling_key": _missing_or(str(c["is_in_sampling_key"]) if "is_in_sampling_key" in c else None),
                "default_kind": _missing_or(c.get("default_kind")),
                "default_expression": _missing_or(c.get("default_expression")),
            }
        )
    col_payload.sort(key=lambda x: x["name"])
    payload = {
        "schema_fingerprint_version": SCHEMA_FINGERPRINT_V02,
        "database": database,
        "table": table,
        "engine": _missing_or(engine),
        "partition_key": _missing_or(partition_key),
        "sorting_key": _missing_or(sorting_key),
        "primary_key": _missing_or(primary_key),
        "sampling_key": _missing_or(sampling_key),
        "storage_policy": _missing_or(storage_policy),
        "columns": col_payload,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

def classify_role(table_name: str) -> str:
    n = table_name.lower()
    for role, hints in ROLE_HINTS.items():
        for h in hints:
            if h in n:
                return role
    return "unknown"


def map_fields(columns: dict[str, str]) -> list[FieldMapping]:
    by_norm = {_norm(k): (k, v) for k, v in columns.items()}
    out: list[FieldMapping] = []
    for req in LOGS_REQUIRED:
        if req in columns:
            out.append(FieldMapping(required=req, status="exact", matched_column=req, matched_type=columns[req]))
            continue
        # exact case-insensitive
        hit = by_norm.get(_norm(req))
        if hit and hit[0] != req:
            out.append(
                FieldMapping(
                    required=req,
                    status="alias_candidate",
                    matched_column=hit[0],
                    matched_type=hit[1],
                    notes="case_variant_unverified_semantics",
                )
            )
            continue
        alias_hit = None
        for alias in ALIASES.get(req, ()):
            alias_hit = by_norm.get(_norm(alias))
            if alias_hit:
                break
        if alias_hit:
            out.append(
                FieldMapping(
                    required=req,
                    status="alias_candidate",
                    matched_column=alias_hit[0],
                    matched_type=alias_hit[1],
                    notes="name_alias_unverified_semantics",
                )
            )
        else:
            out.append(FieldMapping(required=req, status="missing"))
    return out


def block_pruning_status(
    *,
    columns: dict[str, str],
    mappings: list[FieldMapping],
    partition_key: str,
    sorting_key: str,
    primary_key: str,
) -> str:
    block_cols = []
    for m in mappings:
        if m.required == "block_number" and m.status in {"exact", "alias_candidate"} and m.matched_column:
            block_cols.append(m.matched_column)
    if not block_cols:
        # also check raw name presence in keys
        for cand in ("block_number", "blockNumber", "block"):
            if cand in columns:
                block_cols.append(cand)
                break
    if not block_cols:
        return "NOT_PRUNABLE"
    keys = " ".join([partition_key or "", sorting_key or "", primary_key or ""]).lower()
    if not keys.strip():
        return "UNKNOWN"
    matched = [c for c in block_cols if c.lower() in keys]
    if not matched:
        return "NOT_PRUNABLE"
    # VERIFIED_METADATA_PRUNABLE only if exact block_number appears in partition or primary/sort
    exact_block = any(m.required == "block_number" and m.status == "exact" and m.matched_column for m in mappings)
    in_partition = any(c.lower() in (partition_key or "").lower() for c in block_cols)
    in_sort_or_pk = any(
        c.lower() in (sorting_key or "").lower() or c.lower() in (primary_key or "").lower() for c in block_cols
    )
    if exact_block and in_partition and in_sort_or_pk:
        return "VERIFIED_METADATA_PRUNABLE"
    if exact_block and (in_partition or in_sort_or_pk):
        return "PARTIALLY_PRUNABLE"
    if matched:
        return "PARTIALLY_PRUNABLE"
    return "UNKNOWN"


def bounded_probe_safety(pruning: str, role: str, missing: list[str]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if role == "unknown":
        reasons.append("candidate_role_unknown")
    if pruning == "NOT_PRUNABLE":
        reasons.append("block_not_prunable")
    if pruning == "UNKNOWN":
        reasons.append("block_pruning_unknown")
    critical_missing = [f for f in ("block_number", "transaction_hash", "log_index", "address", "topic0", "data") if f in missing]
    if critical_missing:
        reasons.append("critical_fields_missing:" + ",".join(critical_missing))
    reasons.append("data_content_unverified")
    reasons.append("adapter_runtime_transport_unimplemented")
    if pruning == "VERIFIED_METADATA_PRUNABLE" and not critical_missing and role == "logs":
        return "CANDIDATE_UNVERIFIED_DATA", reasons
    if pruning in {"PARTIALLY_PRUNABLE", "VERIFIED_METADATA_PRUNABLE"} and len(critical_missing) <= 2:
        return "WEAK_CANDIDATE_UNVERIFIED_DATA", reasons
    return "REJECT_OR_HOLD", reasons


def rank_tables(
    *,
    tables_rows: Iterable[dict[str, Any]],
    columns_rows: Iterable[dict[str, Any]],
    evidence_refs: list[str] | None = None,
) -> list[TableCandidate]:
    refs = evidence_refs or []
    cols_by_table: dict[tuple[str, str], dict[str, str]] = {}
    col_meta_by_table: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in columns_rows:
        db = str(row.get("database", ""))
        tbl = str(row.get("table", ""))
        name = str(row.get("name", ""))
        typ = str(row.get("type", ""))
        if not db or not tbl or not name:
            continue
        cols_by_table.setdefault((db, tbl), {})[name] = typ
        col_meta_by_table.setdefault((db, tbl), []).append(dict(row))

    candidates: list[TableCandidate] = []
    for row in tables_rows:
        db = str(row.get("database", ""))
        name = str(row.get("name", ""))
        if not db or not name:
            continue
        engine = str(row.get("engine", "") or "")
        partition_key = str(row.get("partition_key", "") or "")
        sorting_key = str(row.get("sorting_key", "") or "")
        primary_key = str(row.get("primary_key", "") or "")
        sampling_key = str(row.get("sampling_key", "") or "")
        storage_policy = str(row.get("storage_policy", "") or "")
        columns = cols_by_table.get((db, name), {})
        col_meta = col_meta_by_table.get((db, name), [])
        mappings = map_fields(columns)
        exact = [m.required for m in mappings if m.status == "exact"]
        alias = [m.required for m in mappings if m.status == "alias_candidate"]
        missing = [m.required for m in mappings if m.status == "missing"]
        unknown = [m.required for m in mappings if m.status == "UNKNOWN"]
        role = classify_role(name)
        pruning = block_pruning_status(
            columns=columns,
            mappings=mappings,
            partition_key=partition_key,
            sorting_key=sorting_key,
            primary_key=primary_key,
        )
        safety, reasons = bounded_probe_safety(pruning, role, missing)
        fp_legacy = schema_fingerprint(db, name, list(columns.items()))
        fp_v2 = schema_fingerprint_v02(
            database=db,
            table=name,
            engine=engine or None,
            partition_key=partition_key or None,
            sorting_key=sorting_key or None,
            primary_key=primary_key or None,
            sampling_key=sampling_key or None,
            storage_policy=storage_policy or None,
            columns=col_meta,
        )
        score = len(exact) * 10 + len(alias) * 3
        if role == "logs":
            score += 20
        if pruning == "VERIFIED_METADATA_PRUNABLE":
            score += 30
        elif pruning == "PARTIALLY_PRUNABLE":
            score += 10
        if "MergeTree" in engine or engine.endswith("MergeTree"):
            score += 5
        candidates.append(
            TableCandidate(
                database=db,
                table=name,
                engine=engine,
                partition_key=partition_key,
                sorting_key=sorting_key,
                primary_key=primary_key,
                sampling_key=sampling_key,
                storage_policy=storage_policy,
                schema_fingerprint=fp_v2,
                candidate_role=role,
                field_mappings=mappings,
                exact_match_fields=exact,
                alias_match_fields=alias,
                missing_fields=missing,
                unknown_fields=unknown,
                block_pruning_status=pruning,
                bounded_probe_safety_status=safety,
                rejection_reasons=reasons,
                evidence_refs=list(refs),
                score=score,
                data_content_status="UNKNOWN",
                legacy_schema_fingerprint_v0_1=fp_legacy,
                schema_fingerprint_v0_2=fp_v2,
                schema_fingerprint_version=SCHEMA_FINGERPRINT_V02,
            )
        )
    candidates.sort(key=lambda c: (-c.score, c.database, c.table))
    return candidates
