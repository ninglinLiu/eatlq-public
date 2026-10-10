"""Regression tests for the honeypot.is v2 parser rewrite + GoPlus downgrade.

Uses REAL captured API responses (2026-06-23) as fixtures:
  - two PARADAENGI honeypots that slipped through the OLD parser
  - benign WETH/USDC (must still clear)

Offline only: network calls are monkeypatched to return the fixtures.
"""
import json
import os
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "services" / "executor" / "src"
sys.path.insert(0, str(SRC))

import external_risk as er  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "honeypot"
HONEYPOT_1 = "0x6c8a7e7f4574dca6f0e5ca8e6772d27cc53ead38"
HONEYPOT_2 = "0x56c6ee3498ca94b7cd2fc363e465b75828f12738"
BENIGN_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"


def _load(prefix: str, token: str) -> dict:
    return json.load(open(FIX / f"{prefix}_{token}.json"))


def _assess_with_fixtures(token: str, *, providers=("goplus", "honeypot"), block_proxy=True):
    """Run assess_external_token_risk against captured fixtures (no network)."""
    er._CACHE.clear()
    policy = er.ExternalRiskPolicy(
        enabled=True, fail_closed=True, providers=tuple(providers),
        canary_on_missing=True, canary_usd=25.0, block_proxy=block_proxy,
    )
    orig_gp, orig_hp = er._get_goplus, er._get_honeypot
    er._get_goplus = lambda t, c, to: _load("gp", token)
    er._get_honeypot = lambda t, c, to: _load("hp", token)
    try:
        return er.assess_external_token_risk(token, policy=policy)
    finally:
        er._get_goplus, er._get_honeypot = orig_gp, orig_hp


class HoneypotParserTests(unittest.TestCase):
    def test_paradaengi_1_rejected(self):
        res = _assess_with_fixtures(HONEYPOT_1)
        self.assertFalse(res.ok, f"PARADAENGI #1 should be REJECTED, got reason={res.reason}")
        self.assertTrue(res.critical, "expected at least one critical flag")

    def test_paradaengi_2_rejected(self):
        res = _assess_with_fixtures(HONEYPOT_2)
        self.assertFalse(res.ok, f"PARADAENGI #2 should be REJECTED, got reason={res.reason}")
        self.assertTrue(res.critical)

    def test_benign_usdc_cleared(self):
        # USDC is a legit proxy (FiatToken); block_proxy is a SEPARATE policy that
        # would reject it independently of honeypot detection. Disable it here to
        # isolate the honeypot.is clearance path (the focus of this fix).
        res = _assess_with_fixtures(BENIGN_USDC, block_proxy=False)
        self.assertTrue(res.ok, f"WETH/USDC should clear, got reason={res.reason}")
        self.assertFalse(res.data_missing, "benign deep pool should be full-size, not canary")
        self.assertEqual(res.reason, "external_risk_ok")

    def test_usdc_proxy_still_blocked_by_proxy_policy(self):
        # Documents that with block_proxy on (default), USDC is rejected for proxy,
        # not for honeypot — confirms proxy policy is independent and intact.
        res = _assess_with_fixtures(BENIGN_USDC, block_proxy=True)
        self.assertFalse(res.ok)
        self.assertTrue(any("proxy" in x for x in res.critical))

    def test_honeypot_parser_picks_critical_flag(self):
        c, w, m, cleared = er._assess_honeypot(_load("hp", HONEYPOT_1), er.ExternalRiskPolicy())
        self.assertFalse(cleared)
        self.assertTrue(any("high_fail_rate" in x for x in c), f"critical={c}")
        self.assertTrue(any("simulation_failed" in x for x in c), f"critical={c}")

    def test_benign_honeypot_parser_clears(self):
        c, w, m, cleared = er._assess_honeypot(_load("hp", BENIGN_USDC), er.ExternalRiskPolicy())
        self.assertTrue(cleared, f"benign should clear; critical={c} missing={m}")
        self.assertEqual(c, [])

    def test_goplus_clean_alone_does_not_clear(self):
        """GoPlus says is_honeypot=0 for PARADAENGI; honeypot.is missing -> must NOT clear."""
        er._CACHE.clear()
        policy = er.ExternalRiskPolicy(enabled=True, fail_closed=True,
                                       providers=("goplus", "honeypot"),
                                       canary_on_missing=False)
        orig_gp, orig_hp = er._get_goplus, er._get_honeypot
        # GoPlus returns clean-ish; honeypot.is unreachable (raises)
        er._get_goplus = lambda t, c, to: _load("gp", HONEYPOT_1)
        def _raise(*a, **k):
            raise TimeoutError("simulated honeypot.is timeout")
        er._get_honeypot = _raise
        try:
            res = er.assess_external_token_risk(HONEYPOT_1, policy=policy)
        finally:
            er._get_goplus, er._get_honeypot = orig_gp, orig_hp
        # GoPlus clean must not clear; honeypot.is missing + fail_closed -> reject
        self.assertFalse(res.ok, f"GoPlus-clean alone must not clear; reason={res.reason}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
