from __future__ import annotations

import unittest

from launchpad_intelligence.envelopes.standard import EventEnvelope
from launchpad_intelligence.errors import ContractError
from launchpad_intelligence.provenance.source_provenance import SourceProvenance
from launchpad_intelligence.schema.unknown import is_unknown


class EnvelopeUnknownTests(unittest.TestCase):
    def test_missing_source_table_becomes_unknown(self) -> None:
        prov = SourceProvenance(
            source_name="synthetic",
            source_table=None,
            source_file=None,
            query_hash=None,
            observed_at="2026-07-17T00:00:00Z",
            ingested_at="2026-07-17T00:00:01Z",
            point_in_time_available_at="2026-07-17T00:00:00Z",
            collector="unittest",
        )
        env = EventEnvelope.from_parts(
            common={
                "chain_id": 8453,
                "block_number": 1,
                "block_hash": "0xhash",
                "block_timestamp": "2026-07-17T00:00:00Z",
                "transaction_hash": "0xtx",
                "transaction_index": 0,
                "log_index": 0,
                "contract_address": "0xcontract",
                "topic0": "0xtopic",
                "raw_data": "0x",
            },
            provenance=prov,
        )
        self.assertTrue(is_unknown(env.source_table))

    def test_rejects_empty_required_field(self) -> None:
        prov = SourceProvenance(
            source_name="synthetic",
            source_table="t",
            source_file=None,
            query_hash=None,
            observed_at="2026-07-17T00:00:00Z",
            ingested_at="2026-07-17T00:00:01Z",
            point_in_time_available_at="2026-07-17T00:00:00Z",
            collector="unittest",
        )
        with self.assertRaises(ContractError):
            EventEnvelope.from_parts(
                common={
                    "chain_id": 8453,
                    "block_number": 1,
                    "block_hash": "",
                    "block_timestamp": "2026-07-17T00:00:00Z",
                    "transaction_hash": "0xtx",
                    "transaction_index": 0,
                    "log_index": 0,
                    "contract_address": "0xcontract",
                    "topic0": "0xtopic",
                    "raw_data": "0x",
                },
                provenance=prov,
            )


if __name__ == "__main__":
    unittest.main()
