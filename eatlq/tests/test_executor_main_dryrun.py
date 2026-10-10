"""End-to-end executor dry-run test — no real transactions."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
EXECUTOR_SRC = ROOT / "services" / "executor" / "src"


def _stub_executor_deps() -> None:
    import types

    if "eth_account" not in sys.modules:
        eth_m = types.ModuleType("eth_account")

        class _Account:
            @staticmethod
            def from_key(_pk):
                m = MagicMock()
                m.address = "0x" + "ab" * 20
                return m

        eth_m.Account = _Account
        sys.modules["eth_account"] = eth_m

    if "eth_abi" not in sys.modules:
        abi_m = types.ModuleType("eth_abi")
        abi_m.encode = lambda *a, **k: b""
        sys.modules["eth_abi"] = abi_m

    if "web3" not in sys.modules:
        web3_m = types.ModuleType("web3")

        class Web3:
            @staticmethod
            def HTTPProvider(*_a, **_k):
                return None

            def __init__(self, *_a, **_k):
                pass

            @property
            def eth(self):
                eth = MagicMock()
                eth.get_transaction_count.return_value = 0
                return eth

        web3_m.Web3 = Web3
        sys.modules["web3"] = web3_m

    if "web3.types" not in sys.modules:
        wt = types.ModuleType("web3.types")
        wt.TxReceipt = dict
        sys.modules["web3.types"] = wt


def load_executor_main():
    _stub_executor_deps()
    if str(EXECUTOR_SRC) not in sys.path:
        sys.path.insert(0, str(EXECUTOR_SRC))
    spec = importlib.util.spec_from_file_location("executor_main_dryrun_test", EXECUTOR_SRC / "main.py")
    if spec is None or spec.loader is None:
        raise AssertionError("main.py missing")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class FakePipeline:
    def __init__(self, r: "FakeRedis"):
        self.r = r
        self.ops: list = []

    def delete(self, key):
        self.ops.append(("delete", (key,), {}))
        return self

    def hset(self, key, mapping=None, **kwargs):
        self.ops.append(("hset", (key,), {"mapping": mapping}))
        return self

    def sadd(self, key, *members):
        self.ops.append(("sadd_immediate", (key,) + members, {}))
        return self

    def execute(self):
        for op, args, kwargs in self.ops:
            if op == "sadd_immediate":
                self.r.sadd(*args)
            else:
                getattr(self.r, op)(*args, **kwargs)


class FakeRedis:
    def __init__(self):
        self.data: dict = {}
        self.hashes: dict = {}
        self.streams: dict[str, list] = {}
        self.groups: dict[str, set] = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, val, **kwargs):
        self.data[key] = val

    def xgroup_create(self, stream, group, id="0", mkstream=True):
        self.groups.setdefault(stream, set()).add(group)

    def xadd(self, stream, fields, **kwargs):
        self.streams.setdefault(stream, []).append(fields)
        return f"{len(self.streams[stream])}-0"

    def xreadgroup(self, group, consumer, streams, count=10, block=2000):
        stream = list(streams.keys())[0]
        pending = self.streams.get(stream, [])
        if not pending:
            return []
        msg = pending.pop(0)
        return [(stream.encode() if isinstance(stream, str) else stream, [(b"1-0", {b"payload": msg[b"payload"]})])]

    def xack(self, stream, group, msg_id):
        pass

    def xlen(self, stream):
        return len(self.streams.get(stream, []))

    def pipeline(self):
        return FakePipeline(self)

    def delete(self, key):
        self.hashes.pop(key, None)
        self.data.pop(key, None)

    def hset(self, key, mapping=None, **kwargs):
        h = self.hashes.setdefault(key, {})
        if mapping:
            h.update(mapping)

    def hgetall(self, key):
        return self.hashes.get(key, {})

    def hdel(self, key, *fields):
        h = self.hashes.get(key, {})
        for f in fields:
            h.pop(f, None)

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def sadd(self, key, *members):
        self.data.setdefault(key, set()).update(members)

    def srem(self, key, member):
        s = self.data.get(key, set())
        s.discard(member)

    def sismember(self, key, member):
        return member in self.data.get(key, set())

    def scard(self, key):
        return len(self.data.get(key, set()))

    def smembers(self, key):
        return self.data.get(key, set())

    def exists(self, key):
        return key in self.hashes or key in self.data

    def eval(self, *a, **k):
        return [b"set", b"0"]

    def incrby(self, key, delta):
        self.data[key] = int(self.data.get(key, 0)) + delta
        return self.data[key]


class ExecutorMainDryRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_executor_main()

    def test_preflight_dry_run_no_pk(self):
        with patch.dict(os.environ, {"EXECUTOR_DRY_RUN": "1", "LIVE_TRADING_MODE": ""}, clear=False):
            flags = self.m.preflight.run_preflight_checks(panic_active=False)
            self.assertEqual(flags["mode"], "DRY_RUN")

    def test_dry_run_handle_signal_opens_position(self):
        os.environ["EXECUTOR_DRY_RUN"] = "1"
        os.environ["LIVE_TRADING_MODE"] = ""
        cfg = self.m.load_config()
        cfg.dry_run = True
        cfg.live_trading_mode = False

        fake_r = FakeRedis()
        store = self.m.PositionStore(fake_r)
        flags = {"LIVE_TRADING_MODE": False, "REAL_TX_SENT": False}

        pool = "0xbe00ff35af70e8415d0eb605a286d8a45466a4c1"
        sig = {
            "action": "BUY",
            "pool_address": pool,
            "confidence": 0.75,
            "signal_source": "smart_money",
            "chain_id": 8453,
        }
        with patch.object(self.m, "lookup") as mock_lookup:
            from registry import POOL_REGISTRY

            info = POOL_REGISTRY.get(pool.lower())
            self.assertIsNotNone(info, "test pool must exist in registry")
            mock_lookup.return_value = info
            self.m._handle_signal(None, None, fake_r, store, cfg, flags, sig, "test-1")

        self.assertTrue(store.has_open(pool.lower()))
        pos = store.get(pool.lower())
        self.assertIsNotNone(pos)
        self.assertTrue(pos.buy_tx_hash.startswith("0x"))

    def test_bridge_signal_parse_and_malformed_gate(self):
        payload = {"action": "BUY", "pool_address": "0x" + "ab" * 20, "confidence": 0.8}
        raw = {b"payload": json.dumps(payload).encode()}
        parsed = self.m._parse_signal(raw)
        self.assertEqual(parsed, payload)
        self.assertIsNone(self.m._malformed_buy_signal_reason(parsed))


if __name__ == "__main__":
    unittest.main()
