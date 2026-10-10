from __future__ import annotations

import unittest

from launchpad_intelligence.query.metadata_ranking import (
    block_pruning_status,
    map_fields,
    rank_tables,
    schema_fingerprint,
)


class MetadataRankingTests(unittest.TestCase):
    def test_fingerprint_stable(self) -> None:
        a = schema_fingerprint("eatlq", "logs", [("block_number", "UInt64"), ("data", "String")])
        b = schema_fingerprint("eatlq", "logs", [("data", "String"), ("block_number", "UInt64")])
        self.assertEqual(a, b)

    def test_field_mapping_exact_alias_missing(self) -> None:
        cols = {
            "block_number": "UInt64",
            "tx_hash": "FixedString(32)",
            "topic0": "FixedString(32)",
        }
        maps = {m.required: m for m in map_fields(cols)}
        self.assertEqual(maps["block_number"].status, "exact")
        self.assertEqual(maps["transaction_hash"].status, "alias_candidate")
        self.assertEqual(maps["log_index"].status, "missing")

    def test_pruning_statuses(self) -> None:
        cols = {"block_number": "UInt64"}
        maps = map_fields(cols)
        self.assertEqual(
            block_pruning_status(
                columns=cols,
                mappings=maps,
                partition_key="toYYYYMM(block_number)",
                sorting_key="block_number, log_index",
                primary_key="block_number, log_index",
            ),
            "VERIFIED_METADATA_PRUNABLE",
        )
        self.assertEqual(
            block_pruning_status(
                columns=cols,
                mappings=maps,
                partition_key="",
                sorting_key="block_number",
                primary_key="",
            ),
            "PARTIALLY_PRUNABLE",
        )
        self.assertEqual(
            block_pruning_status(
                columns={"x": "UInt8"},
                mappings=map_fields({"x": "UInt8"}),
                partition_key="x",
                sorting_key="x",
                primary_key="x",
            ),
            "NOT_PRUNABLE",
        )

    def test_rank_prefers_logs_prunable(self) -> None:
        tables = [
            {
                "database": "eatlq",
                "name": "chain_logs",
                "engine": "MergeTree",
                "partition_key": "intDiv(block_number, 1000)",
                "sorting_key": "block_number, log_index",
                "primary_key": "block_number, log_index",
                "sampling_key": "",
                "storage_policy": "default",
            },
            {
                "database": "eatlq",
                "name": "misc",
                "engine": "Log",
                "partition_key": "",
                "sorting_key": "",
                "primary_key": "",
                "sampling_key": "",
                "storage_policy": "",
            },
        ]
        columns = [
            {"database": "eatlq", "table": "chain_logs", "name": "block_number", "type": "UInt64"},
            {"database": "eatlq", "table": "chain_logs", "name": "block_hash", "type": "FixedString(32)"},
            {"database": "eatlq", "table": "chain_logs", "name": "block_timestamp", "type": "DateTime"},
            {"database": "eatlq", "table": "chain_logs", "name": "transaction_hash", "type": "FixedString(32)"},
            {"database": "eatlq", "table": "chain_logs", "name": "transaction_index", "type": "UInt32"},
            {"database": "eatlq", "table": "chain_logs", "name": "log_index", "type": "UInt32"},
            {"database": "eatlq", "table": "chain_logs", "name": "address", "type": "FixedString(20)"},
            {"database": "eatlq", "table": "chain_logs", "name": "topic0", "type": "FixedString(32)"},
            {"database": "eatlq", "table": "chain_logs", "name": "topic1", "type": "FixedString(32)"},
            {"database": "eatlq", "table": "chain_logs", "name": "topic2", "type": "FixedString(32)"},
            {"database": "eatlq", "table": "chain_logs", "name": "topic3", "type": "FixedString(32)"},
            {"database": "eatlq", "table": "chain_logs", "name": "data", "type": "String"},
            {"database": "eatlq", "table": "misc", "name": "id", "type": "UInt64"},
        ]
        ranked = rank_tables(tables_rows=tables, columns_rows=columns)
        self.assertEqual(ranked[0].table, "chain_logs")
        self.assertEqual(ranked[0].candidate_role, "logs")
        self.assertEqual(ranked[0].block_pruning_status, "VERIFIED_METADATA_PRUNABLE")
        self.assertEqual(ranked[0].data_content_status, "UNKNOWN")
        self.assertEqual(ranked[0].clanker_presence_status, "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
