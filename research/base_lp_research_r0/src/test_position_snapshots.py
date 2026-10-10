import unittest

from src.fetch_position_snapshots import position_key


class PositionKeyTests(unittest.TestCase):
    def test_owner_and_signed_int24_are_packed(self):
        owner = "0x" + "12" * 20
        key = position_key(owner, -1, 1)
        from eth_utils import keccak

        expected = keccak(bytes.fromhex("12" * 20 + "ffffff" + "000001"))
        self.assertEqual(key, expected)


if __name__ == "__main__":
    unittest.main()
