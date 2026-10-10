import unittest
from unittest.mock import patch

from scripts import label_dashcam_executable as labeler


class DummyRPC:
    pass


def word(value: int) -> str:
    return f"{value:064x}"


class QuoterV2AbiParseTests(unittest.TestCase):
    def test_strip_0x_preserves_abi_leading_zeros(self) -> None:
        raw = "0x" + word(27) + word(123456789) + word(0) + word(81988)
        stripped = labeler._strip_0x(raw)
        self.assertEqual(len(stripped), 64 * 4)
        self.assertEqual(int(stripped[:64], 16), 27)
        self.assertTrue(stripped.startswith("0" * 62 + "1b"))

    def test_lstrip_would_misalign_leading_zero_words(self) -> None:
        raw = "0x" + word(27) + word(123456789) + word(0) + word(81988)
        unsafe = raw.lstrip("0x")
        safe = labeler._strip_0x(raw)
        self.assertNotEqual(len(unsafe), len(safe))
        self.assertNotEqual(int(unsafe[:64], 16), 27)
        self.assertEqual(int(safe[:64], 16), 27)

    def test_call_quoter_v3_decodes_synthetic_multivalue_return(self) -> None:
        raw = "0x" + word(8827062384) + word(109030668695119570703381686426929542886577490344600530913916461766077380034560) + word(0) + word(73236)
        with patch.object(labeler, "_eth_call", return_value=raw):
            amount_out = labeler.call_quoter_v3(
                DummyRPC(),
                labeler.WETH_BASE,
                "0x391ce4b5b6fa8307437d1b72dc764f1fcbbdd3f8",
                24,
                10000,
                44995009,
            )
        self.assertEqual(amount_out, 8827062384)

    def test_known_v3r3_quote_example_stays_aligned(self) -> None:
        raw = (
            "0x"
            "00000000000000000000000000000000000000000000000000264e42c0e6c988"
            "0000000000000000000000000000000000001f72202f0b568a565f6af3f5f942"
            "0000000000000000000000000000000000000000000000000000000000000000"
            "0000000000000000000000000000000000000000000000000000000000014044"
        )
        with patch.object(labeler, "_eth_call", return_value=raw):
            amount_out = labeler.call_quoter_v3(
                DummyRPC(),
                "0x7f7f1bb097d055e81860d07967ae6eee6cb802d8",
                labeler.WETH_BASE,
                697444958715133216163556,
                100,
                44972674,
            )
        self.assertEqual(amount_out, 10782097726163336)


if __name__ == "__main__":
    unittest.main()
