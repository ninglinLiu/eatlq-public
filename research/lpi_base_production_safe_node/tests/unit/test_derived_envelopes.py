from __future__ import annotations

import unittest

from launchpad_intelligence.envelopes.standard import (
    EventEnvelope,
    LiquidityEnvelope,
    PoolEnvelope,
    SwapEnvelope,
    TransferEnvelope,
)
from launchpad_intelligence.errors import ContractError
from launchpad_intelligence.schema.unknown import UNKNOWN, Unknown, is_unknown


class DerivedEnvelopeTests(unittest.TestCase):
    def _event(self) -> EventEnvelope:
        return EventEnvelope(
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
            source_table=Unknown("T"),
            observed_at="2026-07-17T00:00:00Z",
            ingested_at="2026-07-17T00:00:01Z",
            point_in_time_available_at="2026-07-17T00:00:00Z",
            provenance_hash="a" * 64,
        )

    def test_transfer_positive_unknown_none_empty(self) -> None:
        ev = self._event()
        ok = TransferEnvelope(ev, "0xtoken", "0xfrom", "0xto", "0")
        self.assertEqual(ok.amount_raw, "0")  # real zero string allowed? amount_raw "0" is non-empty
        unk = TransferEnvelope(ev, Unknown("TOKEN"), "0xfrom", "0xto", Unknown("AMT"))
        self.assertTrue(is_unknown(unk.token))
        with self.assertRaises(ContractError):
            TransferEnvelope(ev, None, "0xfrom", "0xto", "1")  # type: ignore[arg-type]
        with self.assertRaises(ContractError):
            TransferEnvelope(ev, "", "0xfrom", "0xto", "1")

    def test_swap_pool_liquidity(self) -> None:
        ev = self._event()
        SwapEnvelope(ev, "0xp", "0xin", "0xout", "1", "2", Unknown("TRADER"))
        PoolEnvelope(ev, "0xp", "0xa", "0xb", Unknown("FEE"))
        LiquidityEnvelope(ev, "0xp", "0xo", "add", "0", "0")
        with self.assertRaises(ContractError):
            SwapEnvelope(ev, "0xp", "0xin", "0xout", "1", "2", None)  # type: ignore[arg-type]
        with self.assertRaises(ContractError):
            PoolEnvelope(ev, "", "0xa", "0xb", "fee")
        with self.assertRaises(ContractError):
            LiquidityEnvelope(ev, "0xp", "0xo", None, "1", "1")  # type: ignore[arg-type]
        with self.assertRaises(ContractError):
            TransferEnvelope("not-event", "t", "f", "to", "1")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
