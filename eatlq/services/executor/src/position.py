"""Redis-backed open position tracking for the executor.

Key schema (aligned to LASZLO conventions):

* ``positions:{pool_address}`` — Redis **Hash** holding per-position fields
  (``fee_tier``, ``entry_eth_wei``, ``peak_eth_value``, ``size_usd``,
  ``entry_time_ms``, ``buy_tx_hash``, ``pending_sell``,
  ``pending_sell_at`` ...).
* ``positions:open_pools`` — Redis SET of currently open pool addresses.
* ``positions:realized_pnl:{YYYY-MM-DD}`` — daily realized PnL (wei).
* ``positions:day_start_balance:{YYYY-MM-DD}`` — wallet ETH baseline (wei).

**EATLQ divergence from LASZLO**: LASZLO keys positions by *token* address;
EATLQ keeps *pool* granularity because Base hosts multiple fee-tier pools per
token (v2 / v3-500 / v3-3000 / v3-10000 / aero_cl / aero_vamm) and we want
independent tracking per pool. ``fee_tier`` is still recorded so SELL routing
can pick the exact pool that was bought.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

import redis


PENDING_SELL_FIELD = "pending_sell"
PENDING_SELL_AT_FIELD = "pending_sell_at"
# Must exceed the executor's worst-case sell wait (~2s replacements + ~86s poll
# + 30s receipt fallback). If this expires too early, the monitor can launch a
# second sell while the first same-nonce exit is still in flight.
PENDING_SELL_TTL_MS = 180_000

_TRY_MARK_PENDING_SELL_LUA = f"""
local key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local ttl_ms = tonumber(ARGV[2])

if redis.call("EXISTS", key) == 0 then
  return {{"missing", "0"}}
end

local pending = redis.call("HGET", key, "{PENDING_SELL_FIELD}")
local pending_at_raw = redis.call("HGET", key, "{PENDING_SELL_AT_FIELD}") or "0"
local pending_at = tonumber(pending_at_raw) or 0

if pending == "1" and pending_at > 0 and (now_ms - pending_at) < ttl_ms then
  return {{"locked", pending_at_raw}}
end

redis.call(
  "HSET",
  key,
  "{PENDING_SELL_FIELD}", "1",
  "{PENDING_SELL_AT_FIELD}", tostring(now_ms)
)

if pending == "1" then
  return {{"stale", pending_at_raw}}
end
return {{"set", "0"}}
"""


@dataclass
class OpenPosition:
    pool_address: str
    pool_type: str
    token_address: str
    token_decimals: int
    token_amount: int
    entry_eth_wei: int
    entry_quote_eth_wei: int
    opened_at_ms: int
    buy_tx_hash: str
    peak_eth_value: int = 0
    # LASZLO-aligned fields (written on BUY, read on SELL / monitoring).
    fee_tier: int = 0  # uniswap V3 fee in 1e-6 units: 500 / 3000 / 10000.
    size_usd: float = 0.0  # USD notional at time of BUY (entry_eth_wei * eth_price).
    entry_time_ms: int = 0  # duplicates opened_at_ms for LASZLO field-name alignment.
    token0_address: str = ""
    token0_symbol: str = "?"
    token0_decimals: int = 18
    token1_address: str = ""
    token1_symbol: str = "?"
    token1_decimals: int = 18

    def __post_init__(self) -> None:
        if not self.entry_time_ms:
            self.entry_time_ms = self.opened_at_ms

    def to_hash(self) -> Dict[str, str]:
        """Serialize to string-valued fields suitable for Redis HSET."""
        return {
            "pool_address": self.pool_address,
            "pool_type": self.pool_type,
            "token_address": self.token_address,
            "token_decimals": str(self.token_decimals),
            "token_amount": str(self.token_amount),
            "entry_eth_wei": str(self.entry_eth_wei),
            "entry_quote_eth_wei": str(self.entry_quote_eth_wei),
            "opened_at_ms": str(self.opened_at_ms),
            "entry_time_ms": str(self.entry_time_ms or self.opened_at_ms),
            "buy_tx_hash": self.buy_tx_hash,
            "peak_eth_value": str(self.peak_eth_value),
            "fee_tier": str(self.fee_tier),
            "size_usd": f"{float(self.size_usd):.6f}",
            "token0_address": self.token0_address.lower(),
            "token0_symbol": self.token0_symbol,
            "token0_decimals": str(self.token0_decimals),
            "token1_address": self.token1_address.lower(),
            "token1_symbol": self.token1_symbol,
            "token1_decimals": str(self.token1_decimals),
        }

    # Backwards-compatible alias used by legacy log_trade calls.
    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["token_amount"] = str(self.token_amount)
        d["entry_eth_wei"] = str(self.entry_eth_wei)
        d["entry_quote_eth_wei"] = str(self.entry_quote_eth_wei)
        d["peak_eth_value"] = str(self.peak_eth_value)
        return d

    @classmethod
    def from_hash(cls, h: Dict[Any, Any]) -> "OpenPosition":
        def _s(key: str, default: str = "") -> str:
            v = h.get(key, default)
            if v == default:
                v = h.get(key.encode(), default)
            if isinstance(v, bytes):
                return v.decode()
            return str(v)

        opened = int(_s("opened_at_ms", "0") or "0")
        entry_time = int(_s("entry_time_ms", str(opened)) or opened)
        return cls(
            pool_address=_s("pool_address").lower(),
            pool_type=_s("pool_type"),
            token_address=_s("token_address").lower(),
            token_decimals=int(_s("token_decimals", "18") or "18"),
            token_amount=int(_s("token_amount", "0") or "0"),
            entry_eth_wei=int(_s("entry_eth_wei", "0") or "0"),
            entry_quote_eth_wei=int(_s("entry_quote_eth_wei", "0") or "0"),
            opened_at_ms=opened,
            buy_tx_hash=_s("buy_tx_hash"),
            peak_eth_value=int(_s("peak_eth_value", "0") or "0"),
            fee_tier=int(_s("fee_tier", "0") or "0"),
            size_usd=float(_s("size_usd", "0") or "0"),
            entry_time_ms=entry_time,
            token0_address=_s("token0_address").lower(),
            token0_symbol=_s("token0_symbol", "?"),
            token0_decimals=int(_s("token0_decimals", "18") or "18"),
            token1_address=_s("token1_address").lower(),
            token1_symbol=_s("token1_symbol", "?"),
            token1_decimals=int(_s("token1_decimals", "18") or "18"),
        )


class PositionStore:
    """Redis-backed store with LASZLO-compatible ``positions:`` key prefix."""

    def __init__(self, r: redis.Redis, key_prefix: str = "positions") -> None:
        self.r = r
        self.prefix = key_prefix

    def _hash_key(self, pool_address: str) -> str:
        return f"{self.prefix}:{pool_address.lower()}"

    def _open_set_key(self) -> str:
        return f"{self.prefix}:open_pools"

    def has_open(self, pool_address: str) -> bool:
        return self.r.sismember(self._open_set_key(), pool_address.lower())

    def count_open(self) -> int:
        return int(self.r.scard(self._open_set_key()))

    def list_open_pools(self) -> List[str]:
        raw = self.r.smembers(self._open_set_key())
        return sorted(p.decode() if isinstance(p, bytes) else p for p in raw)

    def get(self, pool_address: str) -> Optional[OpenPosition]:
        key = self._hash_key(pool_address)
        data = self.r.hgetall(key)
        if not data:
            return None
        return OpenPosition.from_hash(data)

    def open_position(self, pos: OpenPosition) -> None:
        key = self._hash_key(pos.pool_address)
        pipe = self.r.pipeline()
        pipe.delete(key)
        pipe.hset(key, mapping=pos.to_hash())
        pipe.sadd(self._open_set_key(), pos.pool_address.lower())
        pipe.execute()

    def close_position(self, pool_address: str) -> None:
        key = self._hash_key(pool_address)
        pipe = self.r.pipeline()
        pipe.delete(key)
        pipe.srem(self._open_set_key(), pool_address.lower())
        pipe.execute()

    @staticmethod
    def _field_str(data: Dict[Any, Any], key: str, default: str = "") -> str:
        v = data.get(key, default)
        if v == default:
            v = data.get(key.encode(), default)
        if isinstance(v, bytes):
            return v.decode()
        return str(v)

    def pending_sell_age_ms(self, pool_address: str, now_ms: int) -> Optional[int]:
        data = self.r.hgetall(self._hash_key(pool_address))
        if not data:
            return None
        if self._field_str(data, PENDING_SELL_FIELD, "0") != "1":
            return None
        raw_at = self._field_str(data, PENDING_SELL_AT_FIELD, "0")
        try:
            pending_at = int(raw_at)
        except ValueError:
            pending_at = 0
        if pending_at <= 0:
            return None
        return max(0, int(now_ms) - pending_at)

    def is_pending_sell_stale(
        self,
        pool_address: str,
        now_ms: int,
        ttl_ms: int = PENDING_SELL_TTL_MS,
    ) -> bool:
        age = self.pending_sell_age_ms(pool_address, now_ms)
        return age is not None and age >= ttl_ms

    def try_mark_pending_sell(
        self,
        pool_address: str,
        now_ms: int,
        ttl_ms: int = PENDING_SELL_TTL_MS,
    ) -> bool:
        """Atomically acquire or refresh the per-pool SELL lock.

        Returns False when another non-stale sell attempt is already pending.
        Stale locks are refreshed so the executor can retry after the TTL.
        """
        result = self.r.eval(
            _TRY_MARK_PENDING_SELL_LUA,
            1,
            self._hash_key(pool_address),
            str(int(now_ms)),
            str(int(ttl_ms)),
        )
        status_raw = result[0] if isinstance(result, (list, tuple)) and result else result
        status = status_raw.decode() if isinstance(status_raw, bytes) else str(status_raw)
        return status in ("set", "stale")

    def clear_pending_sell(self, pool_address: str) -> None:
        self.r.hdel(
            self._hash_key(pool_address),
            PENDING_SELL_FIELD,
            PENDING_SELL_AT_FIELD,
        )

    def update_peak(self, pool_address: str, peak_wei: int) -> None:
        """Cheap single-field update for the hot monitoring loop."""
        self.r.hset(self._hash_key(pool_address), "peak_eth_value", str(peak_wei))

    def mark_shadow_once(self, pool_address: str, field: str = "shadow_30min_done") -> bool:
        """Atomically claim a one-time shadow snapshot for this position.

        Returns True exactly once (the first caller after the position exists);
        False on every subsequent call. HSETNX is atomic so concurrent monitor
        cycles can never double-record. Observability only — never raises into
        the trade path."""
        try:
            return bool(self.r.hsetnx(self._hash_key(pool_address), field, "1"))
        except Exception:
            return False

    # ── Pending-buy intent (orphan-position防护, P0) ──────────────────────────
    # Written BEFORE a live buy tx is broadcast and cleared once the position is
    # durably opened. If the executor crashes in the window between a successful
    # on-chain buy and store.open_position(), startup reconciliation replays these
    # intents against the wallet's real token balance so a bought-but-unrecorded
    # token can never become an unmonitored orphan.
    def _pending_buy_key(self, pool_address: str) -> str:
        return f"{self.prefix}:pending_buy:{pool_address.lower()}"

    def _pending_buy_set_key(self) -> str:
        return f"{self.prefix}:pending_buy_set"

    def record_pending_buy(self, pool_address: str, fields: Dict[str, str]) -> None:
        key = self._pending_buy_key(pool_address)
        pipe = self.r.pipeline()
        pipe.delete(key)
        pipe.hset(key, mapping={k: str(v) for k, v in fields.items()})
        pipe.sadd(self._pending_buy_set_key(), pool_address.lower())
        pipe.expire(key, 86_400)  # auto-GC stale intents after 24h
        pipe.execute()

    def clear_pending_buy(self, pool_address: str) -> None:
        pipe = self.r.pipeline()
        pipe.delete(self._pending_buy_key(pool_address))
        pipe.srem(self._pending_buy_set_key(), pool_address.lower())
        pipe.execute()

    def list_pending_buys(self) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        raw_set = self.r.smembers(self._pending_buy_set_key()) or set()
        for member in raw_set:
            pool = member.decode() if isinstance(member, bytes) else member
            data = self.r.hgetall(self._pending_buy_key(pool))
            if not data:
                # Hash expired or was deleted but the set member remained.
                self.r.srem(self._pending_buy_set_key(), pool)
                continue
            rec: Dict[str, str] = {}
            for k, v in (data or {}).items():
                dk = k.decode() if isinstance(k, bytes) else str(k)
                dv = v.decode() if isinstance(v, bytes) else str(v)
                rec[dk] = dv
            rec.setdefault("pool_address", pool)
            out.append(rec)
        return out

    def add_realized_pnl_wei(self, day_key: str, delta_wei: int) -> int:
        k = f"{self.prefix}:realized_pnl:{day_key}"
        return int(self.r.incrby(k, delta_wei))

    def get_realized_pnl_wei(self, day_key: str) -> int:
        k = f"{self.prefix}:realized_pnl:{day_key}"
        v = self.r.get(k)
        if v is None:
            return 0
        return int(v.decode() if isinstance(v, bytes) else v)

    def get_day_start_balance_wei(self, day_key: str) -> Optional[int]:
        k = f"{self.prefix}:day_start_balance:{day_key}"
        v = self.r.get(k)
        if v is None:
            return None
        return int(v.decode() if isinstance(v, bytes) else v)

    def set_day_start_balance_wei(self, day_key: str, wei: int) -> None:
        k = f"{self.prefix}:day_start_balance:{day_key}"
        self.r.set(k, str(wei))

    def migrate_legacy_exec_keys(self) -> int:
        """One-time migration from legacy ``exec:pos:*`` JSON storage.

        Copies any existing legacy open positions, realized-PnL counters, and
        day-start balances into the LASZLO-aligned ``positions:*`` namespace.
        Safe to call on every startup — idempotent once legacy keys are gone.
        Returns the number of positions migrated.
        """
        import json

        migrated = 0
        legacy_open = self.r.smembers("exec:open_pools") or set()
        for member in legacy_open:
            pool = member.decode() if isinstance(member, bytes) else member
            pool_l = pool.lower()
            new_key = self._hash_key(pool_l)
            if self.r.exists(new_key):
                continue
            raw = self.r.get(f"exec:pos:{pool_l}")
            if not raw:
                continue
            try:
                s = raw.decode() if isinstance(raw, bytes) else raw
                d = json.loads(s)
                pos = OpenPosition(
                    pool_address=str(d.get("pool_address", pool_l)).lower(),
                    pool_type=str(d.get("pool_type", "")),
                    token_address=str(d.get("token_address", "")).lower(),
                    token_decimals=int(d.get("token_decimals", 18)),
                    token_amount=int(d.get("token_amount", 0) or 0),
                    entry_eth_wei=int(d.get("entry_eth_wei", 0) or 0),
                    entry_quote_eth_wei=int(d.get("entry_quote_eth_wei", 0) or 0),
                    opened_at_ms=int(d.get("opened_at_ms", 0) or 0),
                    buy_tx_hash=str(d.get("buy_tx_hash", "")),
                    peak_eth_value=int(d.get("peak_eth_value", 0) or 0),
                    fee_tier=int(d.get("fee_tier", 0) or 0),
                    size_usd=float(d.get("size_usd", 0) or 0),
                )
                self.open_position(pos)
                migrated += 1
            except Exception:
                continue

        for day_key_raw in list(self.r.scan_iter(match="exec:realized_pnl:*")) + \
                           list(self.r.scan_iter(match="exec:day_start_balance:*")):
            legacy = day_key_raw.decode() if isinstance(day_key_raw, bytes) else day_key_raw
            suffix = legacy.split(":", 1)[1]
            new_key = f"{self.prefix}:{suffix}"
            if self.r.exists(new_key):
                continue
            v = self.r.get(legacy)
            if v is not None:
                self.r.set(new_key, v)

        return migrated
