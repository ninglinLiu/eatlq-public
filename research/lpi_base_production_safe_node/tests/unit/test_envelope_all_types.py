from __future__ import annotations

import unittest

from launchpad_intelligence.envelopes.standard import EventEnvelope, LogEnvelope, TransactionEnvelope
from launchpad_intelligence.errors import ContractError
from launchpad_intelligence.schema.unknown import UNKNOWN, is_unknown


class EnvelopeAllTypesTests(unittest.TestCase):
    def _common(self, **overrides):
        base = dict(
            chain_id=8453,
            block_number=1,
            block_hash="0xh",
            block_timestamp="2026-07-17T00:00:00Z",
            transaction_hash="0xtx",
            transaction_index=0,
            log_index=0,
            contract_address="0xc",
            topic0="0xt",
            raw_data="0x",
            source_name="synthetic",
            source_table=UNKNOWN.__class__("TABLE"),
            observed_at="2026-07-17T00:00:00Z",
            ingested_at="2026-07-17T00:00:01Z",
            point_in_time_available_at="2026-07-17T00:00:00Z",
            provenance_hash="a" * 64,
        )
        base.update(overrides)
        return base

    def test_event_direct_rejects_empty_and_accepts_unknown(self) -> None:
        with self.assertRaises(ContractError):
            EventEnvelope(**self._common(block_hash=""))
        env = EventEnvelope(**self._common(block_hash=UNKNOWN.__class__("MISSING_HASH")))
        self.assertTrue(is_unknown(env.block_hash))

    def test_transaction_direct_validation(self) -> None:
        with self.assertRaises(ContractError):
            TransactionEnvelope(
                chain_id=8453,
                block_number=1,
                block_hash="0xh",
                block_timestamp="2026-07-17T00:00:00Z",
                transaction_hash="0xtx",
                transaction_index=0,
                from_address="",
                to_address="0xto",
                status="1",
                source_name="s",
                source_table=UNKNOWN.__class__("T"),
                observed_at="2026-07-17T00:00:00Z",
                ingested_at="2026-07-17T00:00:01Z",
                point_in_time_available_at="2026-07-17T00:00:00Z",
                provenance_hash="b" * 64,
            )
        tx = TransactionEnvelope(
            chain_id=8453,
            block_number=1,
            block_hash="0xh",
            block_timestamp="2026-07-17T00:00:00Z",
            transaction_hash="0xtx",
            transaction_index=0,
            from_address=UNKNOWN.__class__("FROM"),
            to_address="0xto",
            status="1",
            source_name="s",
            source_table=UNKNOWN.__class__("T"),
            observed_at="2026-07-17T00:00:00Z",
            ingested_at="2026-07-17T00:00:01Z",
            point_in_time_available_at="2026-07-17T00:00:00Z",
            provenance_hash="b" * 64,
        )
        self.assertTrue(is_unknown(tx.from_address))

    def test_log_direct_validation(self) -> None:
        with self.assertRaises(ContractError):
            LogEnvelope(
                **self._common(topic0=None),
                topics=("0xt",),
            )
        log = LogEnvelope(**self._common(topic0=UNKNOWN.__class__("TOPIC")), topics=("0xt",))
        self.assertTrue(is_unknown(log.topic0))


if __name__ == "__main__":
    unittest.main()
