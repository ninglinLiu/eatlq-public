"""Run-004 Patch V3R-13C — QuoterV2 struct ABI fix + 405-pool rerun.

Research-only/read-only. No model artifacts, no live/mainline writes.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from threading import Lock
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

assert os.environ.get("LIVE_TRADING_MODE", "").strip().lower() not in (
    "1", "true", "yes", "on"
), "LIVE_TRADING_MODE must not be active"

WETH_BASE = "0x4200000000000000000000000000000000000006"
V3_FACTORY = "0x33128a8fc17869897dce68ed026d694621f6fdfd"
QUOTER_V2 = "0x3d4e44eb1374240ce5f1b871ab261cd16335b76a"
STRUCT_SELECTOR = "c6a5026a"

KNOWN_GOOD_CSV = ROOT / "mvp/diagnostics/run004_v3r8_clean_alpha_v3_decoded_backed_10k_dataset.csv"
NOVEL_CSV = ROOT / "cache/run004_v3r12/v3r12_novel_pool_candidates.csv"
OUT_JSON = ROOT / "mvp/diagnostics/run004_v3r13c_quoter_v2_abi_rerun_summary.json"
OUT_MD = ROOT / "mvp/runs/run-004-patch-v3r13c-quoter-v2-abi-rerun.md"

FIXED_WETH_SIZES = [0.0001, 0.0005, 0.001]
FIXED_WETH_RAW = {str(s): int(s * 10**18) for s in FIXED_WETH_SIZES}
KNOWN_GOOD_LIMIT = int(os.environ.get("V3R13C_KNOWN_GOOD_LIMIT", "30"))
BLOCK_SEMANTICS_LIMIT = int(os.environ.get("V3R13C_BLOCK_SEMANTICS_LIMIT", "20"))
POSITIVE_CONTROL_MIN_BUY_SUCCESS_RATE = float(os.environ.get("V3R13C_POS_CTRL_MIN_BUY_RATE", "0.30"))
POSITIVE_CONTROL_MIN_ROUNDTRIP_POOLS = int(os.environ.get("V3R13C_POS_CTRL_MIN_RTT", "1"))

TOKEN0_SELECTOR = "0dfe1681"
TOKEN1_SELECTOR = "d21220a7"
FEE_SELECTOR = "ddca3f43"
LIQUIDITY_SELECTOR = "1a686502"
BALANCE_OF_SELECTOR = "70a08231"


def _load_env_file() -> None:
    env_path = ROOT / "infra/compose/.env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _rpc_url() -> str:
    _load_env_file()
    url = os.environ.get("LABEL_RPC_URL") or os.environ.get("EVM_HTTP_URL", "")
    if not url:
        raise RuntimeError("RPC URL must be in LABEL_RPC_URL or EVM_HTTP_URL")
    return url


class RPC:
    def __init__(self, url: str) -> None:
        self.url = url
        self.session = requests.Session()
        self._id = 0
        self._calls = 0
        self._lock = Lock()

    def call(self, method: str, params: list[Any], timeout: int = 30) -> Any:
        last_error: Exception | None = None
        for attempt in range(6):
            with self._lock:
                self._id += 1
                req_id = self._id
                self._calls += 1
            try:
                time.sleep(0.04)
                r = self.session.post(
                    self.url,
                    json={"jsonrpc": "2.0", "id": req_id, "method": method, "params": params},
                    timeout=timeout,
                )
                if r.status_code == 429:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                r.raise_for_status()
                j = r.json()
                if "error" in j:
                    raise RuntimeError(f"rpc[{method}]: {j['error']}")
                return j.get("result")
            except Exception as exc:
                last_error = exc
                if "429" in str(exc) or "Too Many Requests" in str(exc):
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise
        raise RuntimeError(str(last_error) if last_error else "rpc_failed")

    @property
    def calls(self) -> int:
        return self._calls


def _lower(v: Any) -> str:
    return str(v or "").strip().lower()


def _pad_addr(addr: str) -> str:
    return _lower(addr).replace("0x", "").rjust(64, "0")


def _pad_uint(n: int) -> str:
    return hex(int(n))[2:].rjust(64, "0")


def _decode_uint(res: str | None, default: int = 0) -> int:
    if not res or res == "0x":
        return default
    raw = res[2:] if res.startswith("0x") else res
    return int(raw[:64], 16) if len(raw) >= 64 else default


def _eth_call(rpc: RPC, to: str, data: str, block: int | str = "latest", timeout: int = 30) -> str:
    block_tag = hex(block) if isinstance(block, int) else block
    return rpc.call("eth_call", [{"to": to, "data": data}, block_tag], timeout=timeout)


def quote_v3_struct(
    rpc: RPC,
    token_in: str,
    token_out: str,
    amount_in_raw: int,
    fee: int,
    block: int | str = "latest",
) -> dict[str, Any]:
    data = (
        "0x" + STRUCT_SELECTOR
        + _pad_addr(token_in)
        + _pad_addr(token_out)
        + _pad_uint(amount_in_raw)
        + _pad_uint(fee)
        + _pad_uint(0)
    )
    try:
        res = _eth_call(rpc, QUOTER_V2, data, block=block, timeout=25)
    except Exception as exc:
        return {"ok": False, "err": str(exc)[:220], "amount_out": 0}
    if not res or res == "0x":
        return {"ok": False, "err": "empty_result", "amount_out": 0}
    raw = res[2:] if res.startswith("0x") else res
    words = [raw[i:i + 64] for i in range(0, len(raw), 64)]
    amount_out = int(words[0], 16) if words and words[0] else 0
    return {"ok": amount_out > 0, "err": "" if amount_out > 0 else "zero_amount_out", "amount_out": amount_out}


def quote_roundtrip(rpc: RPC, target: str, fee: int, amount_weth_raw: int, block: int | str = "latest") -> dict[str, Any]:
    buy = quote_v3_struct(rpc, WETH_BASE, target, amount_weth_raw, fee, block)
    if not buy["ok"]:
        return {"status": "buy_fail", "buy": buy}
    sell = quote_v3_struct(rpc, target, WETH_BASE, int(buy["amount_out"]), fee, block)
    if not sell["ok"]:
        return {"status": "buy_success_sell_fail", "buy": buy, "sell": sell}
    return {
        "status": "quote_success",
        "buy": buy,
        "sell": sell,
        "gross_return": round(int(sell["amount_out"]) / amount_weth_raw - 1.0, 8),
    }


def load_known_good() -> list[dict[str, Any]]:
    rows = list(csv.DictReader(KNOWN_GOOD_CSV.open("r", encoding="utf-8")))
    by_pool: dict[str, dict[str, Any]] = {}
    for row in rows:
        if _lower(row.get("label_status")) != "success":
            continue
        pool = _lower(row.get("pool_address"))
        t0 = _lower(row.get("token0"))
        t1 = _lower(row.get("token1"))
        if not pool or WETH_BASE not in (t0, t1):
            continue
        weth_amt = int(float(row.get("weth_side_amount_raw") or 0))
        if pool not in by_pool or weth_amt > by_pool[pool].get("_weth_amt", 0):
            by_pool[pool] = {
                "pool_address": pool,
                "token0": t0,
                "token1": t1,
                "target": t1 if t0 == WETH_BASE else t0,
                "fee": int(row.get("fee_tier") or row.get("fee_bps") or 0),
                "entry_block": int(row.get("entry_block") or row.get("block_number") or 0),
                "_weth_amt": weth_amt,
            }
    return list(by_pool.values())[:KNOWN_GOOD_LIMIT]


def load_novel_405() -> list[dict[str, Any]]:
    rows = list(csv.DictReader(NOVEL_CSV.open("r", encoding="utf-8")))
    by_pool: dict[str, dict[str, Any]] = {}
    for row in rows:
        pool = _lower(row.get("pool_address"))
        t0 = _lower(row.get("token0"))
        t1 = _lower(row.get("token1"))
        if not pool or WETH_BASE not in (t0, t1):
            continue
        weth_amt = int(float(row.get("weth_side_amount_raw") or 0))
        if pool not in by_pool or weth_amt > by_pool[pool].get("_weth_amt", 0):
            by_pool[pool] = {
                "pool_address": pool,
                "token0": t0,
                "token1": t1,
                "target": t1 if t0 == WETH_BASE else t0,
                "fee": int(row.get("fee_tier") or row.get("fee_bps") or 0),
                "entry_block": int(row.get("entry_block") or row.get("block_number") or 0),
                "_weth_amt": weth_amt,
            }
    return list(by_pool.values())


def run_pool_quotes(rpc: RPC, pools: list[dict[str, Any]], block: int | str = "latest") -> list[dict[str, Any]]:
    out = []
    for i, pool in enumerate(pools, start=1):
        sizes = {
            size: quote_roundtrip(rpc, pool["target"], int(pool["fee"]), raw, block=block)
            for size, raw in FIXED_WETH_RAW.items()
        }
        out.append({**pool, "sizes": sizes})
        if i % 50 == 0:
            print(f"[quote] {i}/{len(pools)} pools")
    return out


def summarize_quote_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_size: dict[str, dict[str, Any]] = {}
    for size in FIXED_WETH_RAW:
        buy_ok = sum(1 for r in rows if r["sizes"][size]["status"] in ("quote_success", "buy_success_sell_fail"))
        roundtrip_ok = sum(1 for r in rows if r["sizes"][size]["status"] == "quote_success")
        sell_fail = sum(1 for r in rows if r["sizes"][size]["status"] == "buy_success_sell_fail")
        by_size[size] = {
            "pools": len(rows),
            "buy_quotable": buy_ok,
            "buy_quotable_rate": round(buy_ok / max(1, len(rows)), 4),
            "roundtrip_success": roundtrip_ok,
            "roundtrip_success_rate": round(roundtrip_ok / max(1, len(rows)), 4),
            "buy_success_sell_fail": sell_fail,
            "status_counts": dict(Counter(r["sizes"][size]["status"] for r in rows)),
        }
    any_buy = sum(
        1 for r in rows
        if any(v["status"] in ("quote_success", "buy_success_sell_fail") for v in r["sizes"].values())
    )
    any_roundtrip = sum(1 for r in rows if any(v["status"] == "quote_success" for v in r["sizes"].values()))
    fee_tier_success = {}
    for fee in sorted({int(r["fee"]) for r in rows}):
        sub = [r for r in rows if int(r["fee"]) == fee]
        fee_tier_success[str(fee)] = {
            "pools": len(sub),
            "buy_any": sum(
                1 for r in sub
                if any(v["status"] in ("quote_success", "buy_success_sell_fail") for v in r["sizes"].values())
            ),
            "roundtrip_any": sum(1 for r in sub if any(v["status"] == "quote_success" for v in r["sizes"].values())),
        }
    return {
        "pools": len(rows),
        "by_size": by_size,
        "pools_buy_quotable_any_size": any_buy,
        "pools_roundtrip_success_any_size": any_roundtrip,
        "fee_tier_success": fee_tier_success,
    }


def read_pool_basic_state(rpc: RPC, row: dict[str, Any]) -> dict[str, Any]:
    p = row["pool_address"]
    try:
        liquidity = _decode_uint(_eth_call(rpc, p, "0x" + LIQUIDITY_SELECTOR))
        weth_bal = _decode_uint(_eth_call(rpc, WETH_BASE, "0x" + BALANCE_OF_SELECTOR + _pad_addr(p)))
        target_bal = _decode_uint(_eth_call(rpc, row["target"], "0x" + BALANCE_OF_SELECTOR + _pad_addr(p)))
        return {"liquidity": liquidity, "weth_balance": weth_bal, "target_balance": target_bal}
    except Exception as exc:
        return {"error": str(exc)[:220]}


def classify_pool_failure(rpc: RPC, row: dict[str, Any]) -> str:
    if any(v["status"] == "quote_success" for v in row["sizes"].values()):
        return "quote_success"
    if any(v["status"] == "buy_success_sell_fail" for v in row["sizes"].values()):
        return "buy_success_sell_fail"
    state = read_pool_basic_state(rpc, row)
    if state.get("error"):
        return "revert_unknown"
    if int(state.get("liquidity") or 0) == 0:
        return "zero_liquidity"
    if int(state.get("weth_balance") or 0) == 0:
        return "zero_weth_balance"
    if int(state.get("target_balance") or 0) == 0:
        return "zero_target_balance"
    return "revert_unknown"


def run_block_semantics(rpc: RPC, pools: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for pool in pools[:BLOCK_SEMANTICS_LIMIT]:
        tests = {}
        for label, block in {
            "entry_block_minus_1": max(0, int(pool["entry_block"]) - 1),
            "entry_block": int(pool["entry_block"]),
            "head": "latest",
        }.items():
            tests[label] = quote_v3_struct(
                rpc, WETH_BASE, pool["target"], FIXED_WETH_RAW["0.0001"], int(pool["fee"]), block=block
            )
        rows.append({**pool, "tests": tests})
    return {
        "sample_size": len(rows),
        "entry_minus_1_buy_success": sum(1 for r in rows if r["tests"]["entry_block_minus_1"]["ok"]),
        "entry_block_buy_success": sum(1 for r in rows if r["tests"]["entry_block"]["ok"]),
        "head_buy_success": sum(1 for r in rows if r["tests"]["head"]["ok"]),
        "rows": rows,
    }


def render_markdown(summary: dict[str, Any]) -> str:
    pos = summary["positive_control"]
    novel = summary["novel_405_rerun"]
    block = summary["block_semantics"]
    by_size = novel["summary"].get("by_size", {})
    size_lines = "\n".join(
        f"| {size} | {stats['buy_quotable']} / {stats['pools']} | {stats['roundtrip_success']} / {stats['pools']} | {stats['buy_success_sell_fail']} |"
        for size, stats in by_size.items()
    )
    failure_lines = "\n".join(f"- `{k}`: {v}" for k, v in novel["failure_mode_counts"].items())
    return f"""# Run-004 Patch V3R-13C — QuoterV2 Struct ABI 修复与 405 Pools Sanity 重跑

Status: **research-only / read-only**. No retrain. No promote. No H19. No live trading.

## 1. Quote Path Fix

Broken manual ABI paths patched:
- `scripts/run004_v3r12_v3_data_supply.py`: `f7729d43` -> `c6a5026a`
- `scripts/run004_v3r13a_fixed_notional_quote_sanity.py`: `f7729d43` -> `c6a5026a`

Already-correct paths:
- `scripts/label_dashcam_executable.py`: already uses `0xc6a5026a`
- `scripts/label_dashcam.py`: Web3 tuple ABI
- `services/strategy/src/main.py`: Web3 tuple ABI; not modified
- `services/executor/src/swap.py`: Web3 tuple ABI; not modified

`scripts/run004_v3r13b_tradability_falsification.py` intentionally retains the broken path as a comparison/control artifact.

## 2. Positive-Control Gate

- known-good pools tested: **{pos['pools']}**
- head block: **{summary['head_block']}**
- buy-quotable any size: **{pos['pools_buy_quotable_any_size']} / {pos['pools']}**
- buy+sell round-trip any size: **{pos['pools_roundtrip_success_any_size']} / {pos['pools']}**
- gate passed: **{pos['gate_passed']}**

## 3. 405 Novel Pool Rerun

| WETH input | Buy quotable | Buy + sell-back round trip | Buy success / sell fail |
|---|---:|---:|---:|
{size_lines}

Any-size totals:
- buy-quotable pools: **{novel['summary'].get('pools_buy_quotable_any_size')} / {novel['summary'].get('pools')}**
- buy+sell round-trip pools: **{novel['summary'].get('pools_roundtrip_success_any_size')} / {novel['summary'].get('pools')}**

Failure/success mode counts:
{failure_lines}

## 4. Block Semantics Subset

Sample size: **{block['sample_size']}**

0.0001 WETH WETH→target quote successes:
- `entry_block - 1`: {block['entry_minus_1_buy_success']} / {block['sample_size']}
- `entry_block`: {block['entry_block_buy_success']} / {block['sample_size']}
- `head/latest`: {block['head_buy_success']} / {block['sample_size']}

## 5. 必答问题（中文）

Q1. 哪些 quote call site 用了旧错误路径？

{summary['answers']['Q1']}

Q2. 修正后的 QuoterV2 struct ABI 在 known-good pools 上成功率是多少？

{summary['answers']['Q2']}

Q3. 405 novel pools 修正后 quote 成功率是多少？

{summary['answers']['Q3']}

Q4. V3R-13A 的 0/405 结论是否作废？

{summary['answers']['Q4']}

Q5. Base Uni V3 novel expansion 是否恢复？

{summary['answers']['Q5']}

Q6. 下一步是 labeler bridge、数据扩容，还是仍需修 quote？

{summary['answers']['Q6']}

## 6. Decision Flags

- `v3r13a_dead_pool_conclusion_valid = {str(summary['decision_flags']['v3r13a_dead_pool_conclusion_valid']).lower()}`
- `quote_path_fixed = {str(summary['decision_flags']['quote_path_fixed']).lower()}`
- `allow_base_univ3_novel_expansion = {str(summary['decision_flags']['allow_base_univ3_novel_expansion']).lower()}`
- `allow_model_research = {str(summary['decision_flags']['allow_model_research']).lower()}`
- `allow_mainline = {str(summary['decision_flags']['allow_mainline']).lower()}`

## Safety

- `rpc_calls_total = {summary['rpc_calls_total']}`
- `elapsed_sec = {summary['elapsed_sec']}`
- no large CSVs written
- no model artifacts written
"""


def main() -> int:
    t0 = time.time()
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    rpc = RPC(_rpc_url())
    head_block = int(rpc.call("eth_blockNumber", []), 16)
    known = load_known_good()
    novel = load_novel_405()

    print(f"[positive] testing {len(known)} known-good pools at head {head_block}")
    positive_rows = run_pool_quotes(rpc, known, block=head_block)
    positive_summary = summarize_quote_rows(positive_rows)
    pos_buy_rate = positive_summary["pools_buy_quotable_any_size"] / max(1, positive_summary["pools"])
    gate_passed = (
        pos_buy_rate >= POSITIVE_CONTROL_MIN_BUY_SUCCESS_RATE
        and positive_summary["pools_roundtrip_success_any_size"] >= POSITIVE_CONTROL_MIN_ROUNDTRIP_POOLS
    )
    positive_control = {
        **positive_summary,
        "gate_thresholds": {
            "min_buy_success_rate": POSITIVE_CONTROL_MIN_BUY_SUCCESS_RATE,
            "min_roundtrip_pools": POSITIVE_CONTROL_MIN_ROUNDTRIP_POOLS,
        },
        "gate_passed": gate_passed,
    }

    if gate_passed:
        print(f"[novel] rerunning {len(novel)} unique V3R-12 novel pools")
        novel_rows = run_pool_quotes(rpc, novel, block=head_block)
        novel_summary = summarize_quote_rows(novel_rows)
        classifications = Counter(classify_pool_failure(rpc, row) for row in novel_rows)
        failure_counts = dict(classifications)
        print(f"[block] testing {min(BLOCK_SEMANTICS_LIMIT, len(novel))} pools")
        block = run_block_semantics(rpc, novel)
    else:
        novel_rows = []
        novel_summary = {"aborted": True, "reason": "quote_path_failed_positive_control"}
        failure_counts = {"quote_path_failed_positive_control": len(novel)}
        block = {"sample_size": 0, "entry_minus_1_buy_success": 0, "entry_block_buy_success": 0, "head_buy_success": 0, "rows": []}

    by_size = novel_summary.get("by_size", {}) if isinstance(novel_summary, dict) else {}
    q3 = ", ".join(
        f"{size} WETH: {stats['buy_quotable']} / {stats['pools']}"
        for size, stats in by_size.items()
    ) if by_size else "aborted due to positive-control failure"
    q4 = ", ".join(
        f"{size} WETH: {stats['roundtrip_success']} / {stats['pools']}"
        for size, stats in by_size.items()
    ) if by_size else "aborted due to positive-control failure"

    novel_buy_any = int(novel_summary.get("pools_buy_quotable_any_size", 0)) if isinstance(novel_summary, dict) else 0
    novel_roundtrip_any = int(novel_summary.get("pools_roundtrip_success_any_size", 0)) if isinstance(novel_summary, dict) else 0
    quote_path_fixed = bool(gate_passed)
    # V3R-13A's original 0/405 result came from the broken manual ABI path and is invalid regardless of rerun outcome.
    v3r13a_dead_pool_conclusion_valid = False
    allow_expansion = gate_passed and novel_roundtrip_any > 0
    summary = {
        "run": "run004_v3r13c_quoter_v2_abi_rerun",
        "research_only": True,
        "head_block": head_block,
        "quote_selector": STRUCT_SELECTOR,
        "fixed_weth_sizes": FIXED_WETH_SIZES,
        "quote_path_audit": {
            "broken_paths_patched": [
                "scripts/run004_v3r12_v3_data_supply.py::_call_quoter",
                "scripts/run004_v3r13a_fixed_notional_quote_sanity.py::_quote",
            ],
            "already_struct_or_web3_tuple": [
                "scripts/label_dashcam_executable.py::call_quoter_v3",
                "scripts/label_dashcam.py::_quote_v3_buy/_quote_v3_sell",
                "services/strategy/src/main.py::_estimate_entry_out/_honeypot_check",
                "services/executor/src/swap.py::V3 quote helpers",
            ],
            "intentionally_retained_comparison_path": [
                "scripts/run004_v3r13b_tradability_falsification.py::v3r13a_exact"
            ],
        },
        "positive_control": positive_control,
        "novel_405_rerun": {
            "source": str(NOVEL_CSV.relative_to(ROOT)),
            "unique_pools_loaded": len(novel),
            "summary": novel_summary,
            "failure_mode_counts": failure_counts,
            "sample_successes": [
                {
                    "pool_address": r["pool_address"],
                    "fee": r["fee"],
                    "successful_sizes": [s for s, v in r["sizes"].items() if v["status"] == "quote_success"],
                    "buy_only_sizes": [s for s, v in r["sizes"].items() if v["status"] == "buy_success_sell_fail"],
                }
                for r in novel_rows
                if any(v["status"] in ("quote_success", "buy_success_sell_fail") for v in r["sizes"].values())
            ][:20],
        },
        "block_semantics": block,
        "decision_flags": {
            "v3r13a_dead_pool_conclusion_valid": v3r13a_dead_pool_conclusion_valid,
            "quote_path_fixed": quote_path_fixed,
            "allow_base_univ3_novel_expansion": allow_expansion,
            "allow_model_research": False,
            "allow_mainline": False,
        },
        "answers": {
            "Q1": (
                "旧错误路径是手工拼 ABI 且使用 `f7729d43` 的 call site："
                "`scripts/run004_v3r13b_tradability_falsification.py` 中 `v3r13a_exact` 与 `legacy_fee_amount_order` 两条路径。"
                "历史上 `scripts/run004_v3r12_v3_data_supply.py::_call_quoter` 与 "
                "`scripts/run004_v3r13a_fixed_notional_quote_sanity.py::_quote` 也曾是同类旧路径，现已迁移到 `c6a5026a`。"
            ),
            "Q2": (
                f"known-good positive control 结果：buy-quotable(any size) = "
                f"{positive_summary['pools_buy_quotable_any_size']} / {positive_summary['pools']} "
                f"({round(positive_summary['pools_buy_quotable_any_size'] / max(1, positive_summary['pools']) * 100, 2)}%), "
                f"round-trip(any size) = {positive_summary['pools_roundtrip_success_any_size']} / {positive_summary['pools']} "
                f"({round(positive_summary['pools_roundtrip_success_any_size'] / max(1, positive_summary['pools']) * 100, 2)}%), "
                f"gate_passed = {gate_passed}。"
            ),
            "Q3": q3,
            "Q4": "是。V3R-13A 的 0/405 结论建立在已证伪的旧 ABI quote path 上，必须作废。",
            "Q5": (
                "恢复（有条件）。positive control 已通过且 405 rerun 出现可交易样本，允许受控恢复 Base Uni V3 novel expansion。"
                if allow_expansion else
                "暂不恢复。虽然可能存在 buy quote，但 full round-trip 成功不足或 positive control 未通过。"
            ),
            "Q6": (
                "下一步应进入 labeler bridge（统一 struct ABI）+ 小步数据扩容；"
                "quote path 已修复，不应继续停留在“修 quote”阶段。"
                if quote_path_fixed else
                "仍需先修 quote path 并通过 positive control，禁止直接做数据扩容或策略结论。"
            ),
        },
        "rpc_calls_total": rpc.calls,
        "elapsed_sec": round(time.time() - t0, 2),
    }
    OUT_JSON.write_text(json.dumps(summary, indent=2, sort_keys=False), encoding="utf-8")
    OUT_MD.write_text(render_markdown(summary), encoding="utf-8")
    print(json.dumps({"json": str(OUT_JSON), "markdown": str(OUT_MD)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
