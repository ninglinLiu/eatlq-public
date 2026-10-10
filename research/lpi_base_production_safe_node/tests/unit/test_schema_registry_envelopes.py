from __future__ import annotations

import unittest

from launchpad_intelligence.envelopes.standard import EventEnvelope
from launchpad_intelligence.errors import ContractError
from launchpad_intelligence.provenance.source_provenance import SourceProvenance
from launchpad_intelligence.registry.models import ContractVersion, LaunchpadRecord, Registry, bootstrap_base_registry
from launchpad_intelligence.schema.unknown import UNKNOWN, is_unknown
from launchpad_intelligence.versions import ADAPTER_API_VERSION, MANIFEST_VERSION, SCHEMA_VERSION


class SchemaRegistryEnvelopeTests(unittest.TestCase):
    def test_versions_declared(self) -> None:
        self.assertTrue(SCHEMA_VERSION.startswith("lpi.schema."))
        self.assertTrue(ADAPTER_API_VERSION.startswith("lpi.adapter_api."))
        self.assertTrue(MANIFEST_VERSION.startswith("lpi.evidence_manifest."))

    def test_unknown_preserved(self) -> None:
        self.assertTrue(is_unknown(UNKNOWN))
        self.assertFalse(bool(UNKNOWN))

    def test_registry_bootstrap_keeps_unknown_factory(self) -> None:
        reg = bootstrap_base_registry()
        v = reg.get_version("clanker", "v4")
        assert v is not None
        self.assertTrue(is_unknown(v.factory_address))

    def test_registry_rejects_missing_launchpad(self) -> None:
        reg = Registry(launchpads=[LaunchpadRecord("clanker", 8453, "Clanker")])
        with self.assertRaises(ContractError):
            reg.add_version(
                ContractVersion("doppler", "v1", 8453, factory_address="0xabc")
            )

    def test_event_envelope_requires_fields(self) -> None:
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
            event_name="TOKEN_CREATED",
        )
        env.validate()
        self.assertEqual(env.event_name, "TOKEN_CREATED")
        self.assertEqual(len(env.provenance_hash), 64)


if __name__ == "__main__":
    unittest.main()
