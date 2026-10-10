import unittest
from datetime import datetime, timezone

from src.backfill_collects import (
    BASE_BLOCK_TIME,
    END_BLOCK,
    START_BLOCK,
    TOPICS,
    block_time,
    decode_log,
    parse_int24,
)


def topic_word(value: int) -> str:
    return "0x" + (value % (1 << 256)).to_bytes(32, "big").hex()


class Int24Tests(unittest.TestCase):
    def test_positive_boundaries(self):
        self.assertEqual(parse_int24(topic_word(0)), 0)
        self.assertEqual(parse_int24(topic_word(1)), 1)
        self.assertEqual(parse_int24(topic_word((1 << 23) - 1)), (1 << 23) - 1)

    def test_negative_sign_extended_values(self):
        self.assertEqual(parse_int24(topic_word(-1)), -1)
        self.assertEqual(parse_int24(topic_word(-200100)), -200100)
        self.assertEqual(parse_int24(topic_word(-(1 << 23))), -(1 << 23))

    def test_negative_compact_value(self):
        self.assertEqual(parse_int24("0xfcf25c"), -200100)


class DecodeTests(unittest.TestCase):
    def test_collect_layout_and_signed_ticks(self):
        address_word = "0" * 24 + "12" * 20
        amount0 = f"{123:064x}"
        amount1 = f"{456:064x}"
        log = {
            "address": "0xd0b53d9277642d899df5c87a3966a349a798f224",
            "blockNumber": hex(START_BLOCK),
            "transactionHash": "0x" + "ab" * 32,
            "logIndex": "0x7",
            "topics": [
                TOPICS["collect"],
                "0x" + "0" * 24 + "34" * 20,
                topic_word(-200100),
                topic_word(200100),
            ],
            "data": "0x" + address_word + amount0 + amount1,
            "removed": False,
        }
        row = decode_log("collect", log)
        self.assertEqual(row["owner"], "0x" + "34" * 20)
        self.assertEqual(row["recipient"], "0x" + "12" * 20)
        self.assertEqual(row["tick_lower"], -200100)
        self.assertEqual(row["tick_upper"], 200100)
        self.assertEqual(row["amount0"], "123")
        self.assertEqual(row["amount1"], "456")
        self.assertEqual(row["chain_id"], 8453)

    def test_base_two_second_block_time_anchors(self):
        self.assertEqual(block_time(START_BLOCK), BASE_BLOCK_TIME)
        self.assertEqual(
            block_time(END_BLOCK),
            datetime(2026, 5, 21, 0, 0, 1, tzinfo=timezone.utc),
        )

    def test_out_of_window_block_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside the authorized window"):
            block_time(START_BLOCK - 1)


if __name__ == "__main__":
    unittest.main()
