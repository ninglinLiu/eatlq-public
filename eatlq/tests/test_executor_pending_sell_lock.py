import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXECUTOR_SRC = ROOT / "services/executor/src"


def load_position_module():
    sys.modules.setdefault("redis", types.SimpleNamespace())
    spec = importlib.util.spec_from_file_location(
        "executor_position_under_pending_sell_test",
        EXECUTOR_SRC / "position.py",
    )
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load position.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


position = load_position_module()


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.sets = {}

    def pipeline(self):
        return self

    def execute(self):
        return []

    def delete(self, key):
        self.hashes.pop(key, None)

    def hset(self, key, mapping=None, *args):
        self.hashes.setdefault(key, {})
        if mapping is not None:
            self.hashes[key].update({str(k): str(v) for k, v in mapping.items()})
            return len(mapping)
        if len(args) % 2 != 0:
            raise AssertionError("hset args must be pairs")
        for i in range(0, len(args), 2):
            self.hashes[key][str(args[i])] = str(args[i + 1])
        return len(args) // 2

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hdel(self, key, *fields):
        h = self.hashes.get(key, {})
        removed = 0
        for field in fields:
            if field in h:
                removed += 1
                del h[field]
        return removed

    def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(str(value))

    def srem(self, key, value):
        self.sets.setdefault(key, set()).discard(str(value))

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def sismember(self, key, value):
        return str(value) in self.sets.get(key, set())

    def scard(self, key):
        return len(self.sets.get(key, set()))

    def eval(self, _script, _numkeys, key, now_ms, ttl_ms):
        if key not in self.hashes:
            return ["missing", "0"]

        now = int(now_ms)
        ttl = int(ttl_ms)
        pending = self.hashes[key].get(position.PENDING_SELL_FIELD)
        pending_at_raw = self.hashes[key].get(position.PENDING_SELL_AT_FIELD, "0")
        try:
            pending_at = int(pending_at_raw)
        except ValueError:
            pending_at = 0

        if pending == "1" and pending_at > 0 and (now - pending_at) < ttl:
            return ["locked", pending_at_raw]

        self.hashes[key][position.PENDING_SELL_FIELD] = "1"
        self.hashes[key][position.PENDING_SELL_AT_FIELD] = str(now)
        if pending == "1":
            return ["stale", pending_at_raw]
        return ["set", "0"]


def sample_position(pool="0xPool"):
    return position.OpenPosition(
        pool_address=pool,
        pool_type="v2",
        token_address="0xToken",
        token_decimals=18,
        token_amount=100,
        entry_eth_wei=1000,
        entry_quote_eth_wei=1000,
        opened_at_ms=1,
        buy_tx_hash="0xbuy",
    )


class ExecutorPendingSellLockTests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.store = position.PositionStore(self.redis)
        self.pool = "0xPool"
        self.store.open_position(sample_position(self.pool))

    def test_pending_sell_fields_and_ttl_constant_exist(self):
        self.assertEqual(position.PENDING_SELL_FIELD, "pending_sell")
        self.assertEqual(position.PENDING_SELL_AT_FIELD, "pending_sell_at")
        self.assertEqual(position.PENDING_SELL_TTL_MS, 60_000)

    def test_buy_open_position_does_not_create_pending_sell_lock(self):
        data = self.redis.hgetall("positions:0xpool")
        self.assertIn("pool_address", data)
        self.assertNotIn(position.PENDING_SELL_FIELD, data)
        self.assertNotIn(position.PENDING_SELL_AT_FIELD, data)

    def test_first_sell_attempt_sets_lock(self):
        self.assertTrue(self.store.try_mark_pending_sell(self.pool, now_ms=1_000))
        data = self.redis.hgetall("positions:0xpool")
        self.assertEqual(data[position.PENDING_SELL_FIELD], "1")
        self.assertEqual(data[position.PENDING_SELL_AT_FIELD], "1000")

    def test_second_sell_attempt_within_ttl_skips_duplicate(self):
        self.assertTrue(self.store.try_mark_pending_sell(self.pool, now_ms=1_000))
        self.assertFalse(self.store.try_mark_pending_sell(self.pool, now_ms=2_000))
        self.assertEqual(self.store.pending_sell_age_ms(self.pool, now_ms=2_000), 1_000)
        data = self.redis.hgetall("positions:0xpool")
        self.assertEqual(data[position.PENDING_SELL_AT_FIELD], "1000")

    def test_stale_sell_lock_refreshes_after_ttl(self):
        self.assertTrue(self.store.try_mark_pending_sell(self.pool, now_ms=1_000))
        self.assertTrue(
            self.store.try_mark_pending_sell(
                self.pool,
                now_ms=1_000 + position.PENDING_SELL_TTL_MS,
            )
        )
        data = self.redis.hgetall("positions:0xpool")
        self.assertEqual(data[position.PENDING_SELL_AT_FIELD], str(1_000 + position.PENDING_SELL_TTL_MS))

    def test_sell_failure_policy_clears_lock_and_keeps_position(self):
        self.assertTrue(self.store.try_mark_pending_sell(self.pool, now_ms=1_000))
        self.store.clear_pending_sell(self.pool)
        data = self.redis.hgetall("positions:0xpool")
        self.assertIn("pool_address", data)
        self.assertNotIn(position.PENDING_SELL_FIELD, data)
        self.assertNotIn(position.PENDING_SELL_AT_FIELD, data)

    def test_sell_success_closes_position_and_removes_lock(self):
        self.assertTrue(self.store.try_mark_pending_sell(self.pool, now_ms=1_000))
        self.store.close_position(self.pool)
        self.assertEqual(self.redis.hgetall("positions:0xpool"), {})
        self.assertFalse(self.store.has_open(self.pool))

    def test_monitor_source_wraps_execute_sell_with_lock_lifecycle(self):
        main_src = (EXECUTOR_SRC / "main.py").read_text(encoding="utf-8")
        lock_idx = main_src.index("store.try_mark_pending_sell(pool_addr, now_ms)")
        sell_idx = main_src.index("res = execute_sell(w3, account, pool, amt, reason=reason)")
        clear_idx = main_src.index("store.clear_pending_sell(pool_addr)")
        close_idx = main_src.index("store.close_position(pool_addr)", sell_idx)

        self.assertLess(lock_idx, sell_idx)
        self.assertGreater(clear_idx, sell_idx)
        self.assertGreater(close_idx, sell_idx)
        self.assertIn('"event": "sell_lock_active"', main_src)


if __name__ == "__main__":
    unittest.main()
