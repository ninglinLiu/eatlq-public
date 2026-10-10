#!/usr/bin/env python3
from __future__ import annotations

import math
import sys
from pathlib import Path

import msgpack

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.strategy.src.feature_store import FeatureStore


def assert_close(a: float, b: float, eps: float = 1e-9) -> None:
    if not math.isclose(a, b, rel_tol=eps, abs_tol=eps):
        raise AssertionError(f"expected {b}, got {a}")


def main() -> int:
    # dict payload path
    as_dict = {
        "address": "0xabc",
        "symbol": "TEST",
        "received_at_ms": 1700000000000,
        "net_flow": 40.0,
        "is_buy": True,
    }
    packed_dict = msgpack.packb(as_dict, use_bin_type=True)
    normalized_dict = FeatureStore._normalize_msgpack_payload(packed_dict)
    assert normalized_dict["address"] == "0xabc"

    # list payload path (compact struct order)
    as_list = [1, 8453, "quicknode", None, "eth_subscription", 1700000000001, 1, None, None, 0, 0, "0xdef", None, None, None, None, "0x", False, "{}"]
    packed_list = msgpack.packb(as_list, use_bin_type=True)
    normalized_list = FeatureStore._normalize_msgpack_payload(packed_list)
    assert normalized_list["address"] == "0xdef"

    features = FeatureStore.compute_from_snapshot(
        flow_points=[
            {"net_flow": 40.0, "abs_flow": 40.0},
            {"net_flow": -10.0, "abs_flow": 10.0},
            {"net_flow": 20.0, "abs_flow": 20.0},
        ],
        buy_count=2,
        sell_count=1,
        tick_count=3,
    )
    # net=50, total_abs=70 => 0.714285...
    assert_close(features.price_velocity_5m, 50.0 / 70.0)
    assert_close(features.buy_pressure_ratio, 2.0 / 3.0)
    assert features.activity_heat == 3
    assert 0.0 <= features.token_volatility_5m <= 2.0

    empty = FeatureStore.compute_from_snapshot([], 0, 0, 0)
    assert_close(empty.price_velocity_5m, 0.0)
    assert_close(empty.buy_pressure_ratio, 0.5)
    assert_close(empty.token_volatility_5m, 0.0)
    assert empty.activity_heat == 0

    print("FeatureStore verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
