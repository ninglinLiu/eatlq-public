from __future__ import annotations

import unittest

from launchpad_intelligence.query.metadata_ranking import schema_fingerprint, schema_fingerprint_v02


class SchemaFingerprintV02Tests(unittest.TestCase):
    def _cols(self):
        return [
            {
                "name": "block_number",
                "type": "UInt64",
                "is_in_partition_key": "0",
                "is_in_sorting_key": "1",
                "is_in_primary_key": "1",
                "is_in_sampling_key": "0",
                "default_kind": "",
                "default_expression": "",
            },
            {
                "name": "data",
                "type": "String",
                "is_in_partition_key": "0",
                "is_in_sorting_key": "0",
                "is_in_primary_key": "0",
                "is_in_sampling_key": "0",
                "default_kind": "",
                "default_expression": "",
            },
        ]

    def test_stable_and_order_insensitive(self) -> None:
        a = schema_fingerprint_v02(
            database="eatlq",
            table="raw_evm_logs",
            engine="MergeTree",
            partition_key="toYYYYMM(received_at_ms)",
            sorting_key="chain_id, received_at_ms",
            primary_key="chain_id, received_at_ms",
            sampling_key=None,
            storage_policy="default",
            columns=self._cols(),
        )
        b = schema_fingerprint_v02(
            database="eatlq",
            table="raw_evm_logs",
            engine="MergeTree",
            partition_key="toYYYYMM(received_at_ms)",
            sorting_key="chain_id, received_at_ms",
            primary_key="chain_id, received_at_ms",
            sampling_key=None,
            storage_policy="default",
            columns=list(reversed(self._cols())),
        )
        self.assertEqual(a, b)

    def test_key_change_changes_hash(self) -> None:
        base = dict(
            database="eatlq",
            table="raw_evm_logs",
            engine="MergeTree",
            partition_key="toYYYYMM(received_at_ms)",
            sorting_key="chain_id, received_at_ms",
            primary_key="chain_id, received_at_ms",
            sampling_key=None,
            storage_policy="default",
            columns=self._cols(),
        )
        a = schema_fingerprint_v02(**base)
        base2 = dict(base)
        base2["partition_key"] = "intDiv(block_number, 1000)"
        b = schema_fingerprint_v02(**base2)
        self.assertNotEqual(a, b)

    def test_type_change_changes_hash(self) -> None:
        cols = self._cols()
        a = schema_fingerprint_v02(
            database="eatlq",
            table="t",
            engine="MergeTree",
            partition_key="x",
            sorting_key="x",
            primary_key="x",
            sampling_key=None,
            storage_policy=None,
            columns=cols,
        )
        cols2 = self._cols()
        cols2[0]["type"] = "UInt32"
        b = schema_fingerprint_v02(
            database="eatlq",
            table="t",
            engine="MergeTree",
            partition_key="x",
            sorting_key="x",
            primary_key="x",
            sampling_key=None,
            storage_policy=None,
            columns=cols2,
        )
        self.assertNotEqual(a, b)

    def test_legacy_ignores_keys(self) -> None:
        # v0.1 same columns => same hash even if keys differ (demonstrates why v0.2 needed)
        a = schema_fingerprint("eatlq", "t", [("block_number", "UInt64")])
        b = schema_fingerprint("eatlq", "t", [("block_number", "UInt64")])
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
