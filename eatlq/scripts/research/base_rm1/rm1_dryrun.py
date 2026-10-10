#!/usr/bin/env python3
"""Pinned archive RM1 v2 dry-run; read-only and structurally unable to send."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path("/opt/EATLQ")
sys.path.insert(0, str(ROOT / "scripts/lib"))
sys.path.insert(0, str(ROOT / "scripts/research/base_rm1"))

from v3_quoterv2_struct import (  # noqa: E402
    WETH_BASE,
    quote_exact_input_single_struct,
)
from rm1_framework import (  # noqa: E402
    ALLOWED_READONLY_RPC_METHODS,
    ArmRuntime,
    BroadcastDisabled,
    DisabledBroadcaster,
    GuardViolation,
    d5_passed,
    file_sha256,
    load_and_validate_prereg,
    observation_flags,
    source_constant_proof,
)


DIAG = ROOT / "mvp/diagnostics"
PREREG = ROOT / "configs/research/base_rm1_v2_preregistration_20260715.json"
OUT = DIAG / "base_rm1_v2_dryrun_20260715.json"
LEDGER = DIAG / "base_rm1_v2_rpc_ledger_20260715.jsonl"
REPORT = ROOT / "reports/RM1_v2_dryrun_20260715.md"
ARM = ROOT / "reports/RM1_v2_human_ARM_checklist_20260715.md"
FROM = "0x000000000000000000000000000000000000a10d"
CHAINLINK_DECIMALS = "0x313ce567"
CHAINLINK_LATEST_ROUND_DATA = "0xfeaf968c"


def canonical_hash(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


class ReadOnlyRpc:
    def __init__(self, url: str) -> None:
        if not url:
            raise RuntimeError("FORK_ARCHIVE_RPC_OR_LABEL_RPC_URL_REQUIRED")
        self._url = url
        self.logical_calls = 0
        self.http_attempts = 0
        self.methods: Counter[str] = Counter()
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        LEDGER.write_text("")

    def call(self, method: str, params: list[object], timeout: int = 90) -> object:
        if method not in ALLOWED_READONLY_RPC_METHODS:
            raise RuntimeError(f"FORBIDDEN_RPC_METHOD:{method}")
        self.logical_calls += 1
        logical_id = self.logical_calls
        request_hash = canonical_hash({"method": method, "params": params})
        last: Exception | None = None
        for retry in range(1, 4):
            self.http_attempts += 1
            self.methods[method] += 1
            started = time.monotonic_ns()
            try:
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": logical_id,
                        "method": method,
                        "params": params,
                    },
                    separators=(",", ":"),
                ).encode()
                req = Request(
                    self._url,
                    data=body,
                    headers={
                        "content-type": "application/json",
                        "user-agent": "EATLQ-RM1-readonly-dryrun/2",
                    },
                )
                with urlopen(req, timeout=timeout) as response:
                    payload = json.load(response)
                if payload.get("error"):
                    raise RuntimeError(str(payload["error"]))
                result = payload.get("result")
                self._append(
                    {
                        "logical_id": logical_id,
                        "attempt": retry,
                        "method": method,
                        "request_sha256": request_hash,
                        "response_sha256": canonical_hash(result),
                        "status": "ok",
                        "elapsed_ms": (time.monotonic_ns() - started) // 1_000_000,
                    }
                )
                return result
            except Exception as exc:  # bounded retry, hashed error only
                last = exc
                self._append(
                    {
                        "logical_id": logical_id,
                        "attempt": retry,
                        "method": method,
                        "request_sha256": request_hash,
                        "error_type": type(exc).__name__,
                        "error_sha256": hashlib.sha256(str(exc).encode()).hexdigest(),
                        "status": "error",
                        "elapsed_ms": (time.monotonic_ns() - started) // 1_000_000,
                    }
                )
                time.sleep(0.2 * retry)
        raise RuntimeError(f"RPC_FAILED:{method}:{type(last).__name__ if last else 'unknown'}")

    @staticmethod
    def _append(row: dict[str, object]) -> None:
        with LEDGER.open("a") as handle:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")

    def summary(self) -> dict[str, object]:
        return {
            "endpoint": "FORK_ARCHIVE_RPC_secret_omitted",
            "logical_calls": self.logical_calls,
            "http_attempts": self.http_attempts,
            "methods": dict(self.methods),
            "all_methods_read_only": set(self.methods).issubset(ALLOWED_READONLY_RPC_METHODS),
            "ledger_path": str(LEDGER.relative_to(ROOT)),
            "ledger_sha256": file_sha256(LEDGER),
        }


def compile_atomic_creation_bytecode() -> str:
    out_dir = Path("/tmp/rm1-forge-out")
    artifact = out_dir / "AtomicRoundTrip.sol/AtomicRoundTripCreateB4.json"
    subprocess.run(
        [
            str(Path.home() / ".foundry/bin/forge"),
            "build",
            "--contracts",
            "scripts/research/base_b4/AtomicRoundTrip.sol",
            "--out",
            str(out_dir),
            "--cache-path",
            "/tmp/rm1-forge-cache",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(artifact.read_text())["bytecode"]["object"].removeprefix("0x")


def atomic_creation_data(bytecode: str, token: str, fee: int, amount_in: int) -> str:
    return (
        "0x"
        + bytecode
        + token.removeprefix("0x").rjust(64, "0")
        + f"{fee:064x}"
        + f"{amount_in:064x}"
    )


def decode_atomic(raw: str) -> dict[str, int]:
    if not raw or len(raw) < 2 + 16 * 64:
        raise ValueError("SHORT_ATOMIC_RESPONSE")
    words = [int(raw[2 + i * 64 : 2 + (i + 1) * 64], 16) for i in range(16)]
    keys = [
        "flags",
        "router_buy_out",
        "target_before",
        "target_after_buy",
        "target_after_sell",
        "router_sell_out",
        "weth_after_deposit",
        "weth_after_buy",
        "weth_after_sell",
        "pin_block",
        "chain_id",
        "parent_block_hash",
        "weth_approve_error_hash",
        "buy_error_hash",
        "target_approve_error_hash",
        "sell_error_hash",
    ]
    return dict(zip(keys, words))


def atomic_at(
    rpc: ReadOnlyRpc, bytecode: str, token: str, fee: int, amount_in: int, block: int
) -> dict[str, object]:
    tx = {
        "from": FROM,
        "data": atomic_creation_data(bytecode, token, fee, amount_in),
        "value": hex(amount_in),
        "gas": hex(20_000_000),
    }
    raw = str(
        rpc.call(
            "eth_call",
            [tx, hex(block), {FROM: {"balance": hex(10**18)}}],
            timeout=150,
        )
    )
    decoded = decode_atomic(raw)
    received = decoded["target_after_buy"] - decoded["target_before"]
    recovered = decoded["weth_after_sell"] - decoded["weth_after_buy"]
    buy_ok = bool(decoded["flags"] & 2) and received > 0
    sell_ok = bool(decoded["flags"] & 8) and recovered > 0
    return {
        "status": "measured",
        "block": block,
        "flags": decoded["flags"],
        "buy_ok": buy_ok,
        "sell_ok": sell_ok,
        "received_target": str(received),
        "recovered_weth": str(recovered),
        "recovery_ratio": recovered / amount_in if amount_in else 0.0,
        "router_buy_out": str(decoded["router_buy_out"]),
        "router_sell_out": str(decoded["router_sell_out"]),
        "buy_integer_equal": buy_ok and decoded["router_buy_out"] == received,
        "sell_integer_equal": sell_ok and decoded["router_sell_out"] == recovered,
        "pin_block_returned": decoded["pin_block"],
        "chain_id_returned": decoded["chain_id"],
        "parent_block_hash_returned": f"0x{decoded['parent_block_hash']:064x}",
        "raw_response_sha256": hashlib.sha256(raw.encode()).hexdigest(),
    }


def address_word(raw: object) -> str:
    value = str(raw)
    if len(value) < 42:
        raise ValueError("SHORT_ADDRESS_RESPONSE")
    return "0x" + value[-40:].lower()


def block_at(rpc: ReadOnlyRpc, block: int) -> dict[str, object]:
    value = rpc.call("eth_getBlockByNumber", [hex(block), False])
    if not isinstance(value, dict):
        raise ValueError("MISSING_BLOCK")
    return {
        "number": int(str(value["number"]), 16),
        "hash": value["hash"],
        "parent_hash": value["parentHash"],
        "timestamp": int(str(value["timestamp"]), 16),
        "response_sha256": canonical_hash(value),
    }


def erc20_balance_at(rpc: ReadOnlyRpc, token: str, owner: str, block: int) -> int:
    data = "0x70a08231" + owner.removeprefix("0x").rjust(64, "0")
    return int(str(rpc.call("eth_call", [{"to": token, "data": data}, hex(block)])), 16)


def chainlink_at(rpc: ReadOnlyRpc, feed: str, block: int) -> dict[str, object]:
    code = str(rpc.call("eth_getCode", [feed, hex(block)]))
    decimals_raw = str(
        rpc.call("eth_call", [{"to": feed, "data": CHAINLINK_DECIMALS}, hex(block)])
    )
    round_raw = str(
        rpc.call(
            "eth_call", [{"to": feed, "data": CHAINLINK_LATEST_ROUND_DATA}, hex(block)]
        )
    )
    words = [round_raw[2 + i * 64 : 2 + (i + 1) * 64] for i in range(5)]
    if len(words) != 5 or any(len(word) != 64 for word in words):
        raise ValueError("BAD_CHAINLINK_ROUND_RESPONSE")
    answer = int(words[1], 16)
    if answer >= 2**255:
        answer -= 2**256
    decimals = int(decimals_raw, 16)
    return {
        "dashboard_only": True,
        "feed": feed,
        "code_present": code != "0x",
        "decimals": decimals,
        "round_id": str(int(words[0], 16)),
        "answer_integer": str(answer),
        "answer_decimal_string": f"{answer / (10**decimals):.{decimals}f}",
        "started_at": int(words[2], 16),
        "updated_at": int(words[3], 16),
        "answered_in_round": str(int(words[4], 16)),
        "raw_response_sha256": hashlib.sha256(round_raw.encode()).hexdigest(),
        "used_in_core_estimands": False,
    }


def kill_switch_proofs(cfg: dict[str, object], good_d5: dict[str, object]) -> dict[str, object]:
    token_out = int(str(good_d5["received_target"]))
    proofs: dict[str, object] = {}

    def caught(name: str, action: object) -> None:
        try:
            action()  # type: ignore[operator]
            proofs[name] = {"triggered": False, "error": "NO_EXCEPTION"}
        except GuardViolation as exc:
            proofs[name] = {"triggered": True, "reason": str(exc)}

    bad_d5 = dict(good_d5)
    bad_d5["sell_ok"] = False
    caught(
        "d5_failure",
        lambda: ArmRuntime(cfg, "dry-d5").begin_entry(
            d5_evidence=bad_d5,
            prebroadcast_simulation_ok=True,
            intended_weth_in_wei=int(cfg["chain"]["notional_weth_wei"]),
            existing_onchain_token_balance_wei=0,
            simulated_token_out=token_out,
        ),
    )
    caught(
        "prebroadcast_revert",
        lambda: ArmRuntime(cfg, "dry-revert").begin_entry(
            d5_evidence=good_d5,
            prebroadcast_simulation_ok=False,
            intended_weth_in_wei=int(cfg["chain"]["notional_weth_wei"]),
            existing_onchain_token_balance_wei=0,
            simulated_token_out=token_out,
        ),
    )
    caught(
        "wrong_notional",
        lambda: ArmRuntime(cfg, "dry-notional").begin_entry(
            d5_evidence=good_d5,
            prebroadcast_simulation_ok=True,
            intended_weth_in_wei=int(cfg["chain"]["notional_weth_wei"]) + 1,
            existing_onchain_token_balance_wei=0,
            simulated_token_out=token_out,
        ),
    )
    caught(
        "external_onchain_position",
        lambda: ArmRuntime(cfg, "dry-external-position").begin_entry(
            d5_evidence=good_d5,
            prebroadcast_simulation_ok=True,
            intended_weth_in_wei=int(cfg["chain"]["notional_weth_wei"]),
            existing_onchain_token_balance_wei=1,
            simulated_token_out=token_out,
        ),
    )

    timeout_runtime = ArmRuntime(cfg, "dry-timeout")
    timeout_runtime.begin_entry(
        d5_evidence=good_d5,
        prebroadcast_simulation_ok=True,
        intended_weth_in_wei=int(cfg["chain"]["notional_weth_wei"]),
        existing_onchain_token_balance_wei=0,
        simulated_token_out=token_out,
    )
    caught("inclusion_timeout", lambda: timeout_runtime.check_inclusion_timeout(30_001))

    residual_runtime = ArmRuntime(cfg, "dry-residual")
    residual_runtime.begin_entry(
        d5_evidence=good_d5,
        prebroadcast_simulation_ok=True,
        intended_weth_in_wei=int(cfg["chain"]["notional_weth_wei"]),
        existing_onchain_token_balance_wei=0,
        simulated_token_out=token_out,
    )
    caught("nonzero_exit_balance", lambda: residual_runtime.complete_exit(remaining_token_wei=1))

    open_runtime = ArmRuntime(cfg, "dry-open")
    open_runtime.begin_entry(
        d5_evidence=good_d5,
        prebroadcast_simulation_ok=True,
        intended_weth_in_wei=int(cfg["chain"]["notional_weth_wei"]),
        existing_onchain_token_balance_wei=0,
        simulated_token_out=token_out,
    )
    caught(
        "existing_open_position",
        lambda: open_runtime.begin_entry(
            d5_evidence=good_d5,
            prebroadcast_simulation_ok=True,
            intended_weth_in_wei=int(cfg["chain"]["notional_weth_wei"]),
            existing_onchain_token_balance_wei=0,
            simulated_token_out=token_out,
        ),
    )

    second_runtime = ArmRuntime(cfg, "dry-second")
    second_runtime.begin_entry(
        d5_evidence=good_d5,
        prebroadcast_simulation_ok=True,
        intended_weth_in_wei=int(cfg["chain"]["notional_weth_wei"]),
        existing_onchain_token_balance_wei=0,
        simulated_token_out=token_out,
    )
    second_runtime.complete_exit(remaining_token_wei=0)
    second_runtime.killed = False
    second_runtime.kill_reason = None
    caught(
        "second_roundtrip_same_arm",
        lambda: second_runtime.begin_entry(
            d5_evidence=good_d5,
            prebroadcast_simulation_ok=True,
            intended_weth_in_wei=int(cfg["chain"]["notional_weth_wei"]),
            existing_onchain_token_balance_wei=0,
            simulated_token_out=token_out,
        ),
    )

    return proofs


def render_report(result: dict[str, object]) -> str:
    c = result["chain_evidence"]  # type: ignore[assignment]
    p = result["pipeline_proof"]  # type: ignore[assignment]
    kills = p["kill_switches"]  # type: ignore[index]
    return f"""---
sop_applied: true
alpha_id: AR-KEYSTONE-RM-CALIB
family: F1/F4/F6/F7
next_gate: HUMAN_REVIEW_DRYRUN__NO_ARM_MADE
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# RM1 v2 禁广播干跑报告

## 结论边界

框架在固定历史浅池上完成了只读模拟链路，D5 与同块精确报价均取得整数证据；所有硬停条件均在状态机中触发，广播接口被结构性拒绝。**没有真实成交、没有真实延迟、没有签名或广播，因此不形成 GO，也不证明 reaudit10 的 +56%。**

## 冻结与姿态

- RM1 预注册 SHA256：`{result['preregistration_sha256']}`
- reaudit10 来源 SHA256：`{result['reaudit10_preregistration_sha256']}`
- `live_broadcast=DISABLED`；`live_allowed=false`；`self_go_allowed=false`
- RPC 方法：`{result['rpc']['methods']}`，全部只读：`{str(result['rpc']['all_methods_read_only']).lower()}`
- 广播尝试被拒：`{str(p['broadcast_rejection']['blocked']).lower()}`；真实 send 调用：`0`

## 固定浅池链上证据

| 项目 | 值 |
|---|---:|
| pool | `{c['fixture']['pool']}` |
| target token | `{c['fixture']['target_token']}` |
| entry block / hash | `{c['entry_block']['number']}` / `{c['entry_block']['hash']}` |
| exit block / hash | `{c['exit_block']['number']}` / `{c['exit_block']['hash']}` |
| pool WETH @ entry | `{c['pool_weth_at_entry_wei']}` wei |
| notional / pool WETH | `{c['notional_share_of_pool_weth']:.8%}` |
| D5 flags | `{c['d5']['flags']}` |
| D5 recovered | `{c['d5']['recovered_weth']}` wei |
| D5 recovery ratio | `{c['d5']['recovery_ratio']:.9f}` |
| entry quote integer | `{c['entry_quote']['amount_out']}` |
| D5 buy integer equal | `{str(c['d5']['buy_integer_equal']).lower()}` |
| exit exact-size quote | `{c['exit_quote']['amount_out']}` wei WETH |
| same-block atomic sell guard | `{str(c['exit_atomic_guard']['sell_ok']).lower()}` |

该池来自既有 reaudit10 healthy golden，但在对应区块只有约 5.146 WETH；本轮按人类最后指令把它作为浅池压力夹具。报价和原子模拟均不是成交证据，`realized_net_weth`、真实滑点、真实 gap 与真实延迟全部保持 `null/not_observed`。

## KEYSTONE 出场规则源码证明

- 来源：`{result['source_constant_proof']['source_path']}`，SHA256 `{result['source_constant_proof']['source_sha256']}`，行 `{result['source_constant_proof']['source_lines']}`。
- 硬止损：`log(0.5)`，等价收益 `−50%`。
- 距峰值回撤：`35%`。
- 最长持有：`7200 blocks`。
- `E-window=1800s`：排除。
- SHAKEDOWN：真实 arm 必须由人类给出整数 `N∈[1,7200]`；本次 dry-run 只用 `N=1`，不得继承为真钱默认。

## 运行时硬停证明

| 条件 | 是否触发 | 原因 |
|---|:---:|---|
""" + "\n".join(
        f"| `{name}` | {str(value['triggered']).lower()} | `{value['reason']}` |"
        for name, value in kills.items()
    ) + f"""

正常虚拟往返最终状态：`roundtrips_started={p['happy_path']['roundtrips_started']}`、`roundtrips_completed={p['happy_path']['roundtrips_completed']}`、`open_position_token_wei={p['happy_path']['open_position_token_wei']}`、`stopped_for_human_review={str(p['happy_path']['stopped_for_human_review']).lower()}`。

## Chainlink（仅看板）

- Base ETH/USD 标准 feed：`{c['chainlink_dashboard_only']['feed']}`。
- 固定 block 读数整数：`{c['chainlink_dashboard_only']['answer_integer']}`，decimals `{c['chainlink_dashboard_only']['decimals']}`。
- 核心 WETH 估计量使用该值：`false`。
- 地址仍须在人类 arm 时复核。

## 未被干跑证明的事项

- 没有真实广播，故无法测真实广播→上链延迟、mempool 竞争或三明治。
- 没有真实成交，故真实滑点、真实 gas、`realized_net_weth` 与 `gap` 均未观测。
- AtomicRoundTrip 是 archive `eth_call` 守卫，不等于未来人类钱包的最终签名交易；最终 unsigned tx 仍必须逐笔做精确 `eth_call`。
- 一键 flatten 在本交付中只生成意图并强制预模拟；真正广播适配器、钱包与授权由人类另行提供。

## 可复现

```bash
cd /opt/EATLQ
export FORK_ARCHIVE_RPC=$(grep -E '^POOL_DISCOVERY_RPC_URL=' infra/compose/.env | cut -d= -f2-)
export LABEL_RPC_URL="$FORK_ARCHIVE_RPC"
python3 -B scripts/research/base_rm1/rm1_dryrun.py
python3 -B -m unittest tests.test_base_rm1
```

RPC ledger：`{result['rpc']['ledger_path']}`，SHA256 `{result['rpc']['ledger_sha256']}`。结果 JSON SHA256 在运行结束后写入旁车哈希文件。GO=`NOT_MADE`。
"""


def render_arm_checklist(result: dict[str, object]) -> str:
    sha = result["preregistration_sha256"]
    return f"""---
sop_applied: true
alpha_id: AR-KEYSTONE-RM-CALIB
family: F4/F6
next_gate: HUMAN_ONLY_ARM_REVIEW
decision_flags:
  training_allowed: false
  live_allowed: false
  self_go_allowed: false
---

# RM1 v2 人类 ARM / 急停清单

当前状态：**未 ARM、不可广播、不含签名器**。预注册 SHA256：`{sha}`。

## 人类逐项确认后才可制作一次性 arm 包

- [ ] 接受预注册 SHA256 完全一致；任何参数变化先生成新预注册、新 SHA256。
- [ ] 指定隔离钱包地址与人工保管的签名方式；不得写入 artifact、仓库或普通环境文件。
- [ ] RPC 身份确认是 prettiest-blue-aura/Base 8453；artifact 只记端点代号，不记 URL。
- [ ] Router 白名单只含 Uniswap SwapRouter02 `0x2626664c2603336E57B271c5C0b26F421741e481`。
- [ ] Chainlink Base ETH/USD 标准 feed 人工复核：`0x71041dDdad3595F9CEd3DcCFBe3D1F4b0a16Bb70`；仅看板使用。
- [ ] 模式选 `KEYSTONE` 或 `SHAKEDOWN`。若 SHAKEDOWN，人工明确填写 `N`，范围 `[1,7200]`；本轮 dry-run 的 `N=1` 不是真钱默认。
- [ ] 标的必须是 WETH 计价池；按人类最新裁定，首笔应选同条件浅池并重新做 D5。
- [ ] unsigned 买单和 unsigned 卖/flatten 单逐笔保存 calldata hash，并在对应 block 做完整 `eth_call`；任何 revert 都不广播。
- [ ] D5 必须 buy/sell/整数一致全绿且 recovery ratio `>0.1`。
- [ ] 链上确认不存在旧仓；本 arm 的计数器为 0；notional 精确等于 `1000000000000000 wei WETH`。
- [ ] 人类明确承认：一次 arm 只允许一笔往返，开仓或平仓后立即耗尽，第二笔需要新 arm。

## 一键 arm 的安全接口约定

本交付**故意不提供可广播命令**。人类后续广播适配器只能接受一个脱离仓库保存的一次性 arm 包，且启动前必须运行：

```text
human-arm-adapter --arm-file /secure/RM1_ONE_SHOT_ARM.json \\
  --require-prereg-sha256 {sha} \\
  --max-roundtrips ==1 --notional-weth-wei ==1000000000000000 \\
  --live-broadcast HUMAN_EXPLICIT
```

适配器必须把 `==` 条件作为运行时相等断言；若尚无经审查的 `human-arm-adapter`，不得用现有 executor 顶替，也不得 ARM。

## 一键急停 / flatten 的接口约定

```text
human-arm-adapter emergency-flatten --arm-file /secure/RM1_ONE_SHOT_ARM.json \\
  --reject-new-entry ==true --simulate-exact-tx ==true --target-balance-after ==0
```

顺序不可改变：立即拒绝新仓 → 读取真实 token 余额 → 生成全量退出 calldata → `eth_call` 精确模拟 → 人类适配器广播 → receipt/log 对账 → token 余额必须 `==0`。若模拟失败，不得假装已平仓；记录 `UNRESOLVED` 并由人类处置。

## 每笔结束后交回人类

- [ ] 保存决策、广播、上链、receipt 时间戳和 block/hash。
- [ ] 真成交数量来自 receipt/log/balance delta，不来自报价。
- [ ] 计算 WETH 原生真实净额；USD 仅用对应块 Chainlink 看板值。
- [ ] 应用 3%、30 秒、−5pp、unresolved 四个旗标。
- [ ] 仓位余额 `==0`，arm 已耗尽，框架停止；GO 仍由人类决定。
"""


def main() -> None:
    cfg = load_and_validate_prereg()
    prereg_sha = file_sha256(PREREG)
    rpc_url = os.environ.get("FORK_ARCHIVE_RPC") or os.environ.get("LABEL_RPC_URL")
    rpc = ReadOnlyRpc(str(rpc_url or ""))
    fixture = cfg["dryrun_fixture"]
    block = int(fixture["entry_block"])
    exit_block_number = int(fixture["exit_block"])
    amount_in = int(cfg["chain"]["notional_weth_wei"])
    pool = str(fixture["pool"])
    token = str(fixture["target_token"])
    expected_fee = int(fixture["fee"])

    head = int(str(rpc.call("eth_blockNumber", [])), 16)
    entry_block = block_at(rpc, block)
    exit_block = block_at(rpc, exit_block_number)
    if entry_block["hash"] != fixture["entry_block_hash"]:
        raise RuntimeError("PINNED_ENTRY_BLOCK_HASH_MISMATCH")

    token0 = address_word(rpc.call("eth_call", [{"to": pool, "data": "0x0dfe1681"}, hex(block)]))
    token1 = address_word(rpc.call("eth_call", [{"to": pool, "data": "0xd21220a7"}, hex(block)]))
    fee = int(str(rpc.call("eth_call", [{"to": pool, "data": "0xddca3f43"}, hex(block)])), 16)
    if fee != expected_fee:
        raise RuntimeError("PINNED_POOL_FEE_MISMATCH")
    if {token0, token1} != {WETH_BASE.lower(), token.lower()}:
        raise RuntimeError("PINNED_POOL_PAIR_MISMATCH")

    pool_weth = erc20_balance_at(rpc, WETH_BASE, pool, block)
    if str(pool_weth) != fixture["pool_weth_at_entry_wei"]:
        raise RuntimeError("PINNED_POOL_WETH_MISMATCH")

    bytecode = compile_atomic_creation_bytecode()
    d5 = atomic_at(rpc, bytecode, token, fee, amount_in, block)
    if not d5_passed(d5, cfg):
        raise RuntimeError("DRYRUN_FIXTURE_D5_FAILED")
    prebroadcast_bundle = atomic_at(rpc, bytecode, token, fee, amount_in, block)
    exit_atomic_guard = atomic_at(rpc, bytecode, token, fee, amount_in, exit_block_number)

    entry_quote = quote_exact_input_single_struct(
        rpc, WETH_BASE, token, amount_in, fee, block_identifier=block
    )
    if not entry_quote["ok"]:
        raise RuntimeError("ENTRY_QUOTE_FAILED")
    entry_qty = int(entry_quote["amount_out"])
    exit_quote = quote_exact_input_single_struct(
        rpc, token, WETH_BASE, entry_qty, fee, block_identifier=exit_block_number
    )
    if not exit_quote["ok"]:
        raise RuntimeError("EXIT_QUOTE_FAILED")

    chainlink = chainlink_at(rpc, cfg["chain"]["chainlink_dashboard_only"]["feed"], block)

    runtime = ArmRuntime(cfg, "RM1-DRYRUN-NO-HUMAN-ARM")
    runtime.begin_entry(
        d5_evidence=d5,
        prebroadcast_simulation_ok=bool(
            prebroadcast_bundle["buy_ok"] and prebroadcast_bundle["sell_ok"]
        ),
        intended_weth_in_wei=amount_in,
        existing_onchain_token_balance_wei=0,
        simulated_token_out=entry_qty,
    )
    runtime.check_inclusion_timeout(2_000)
    runtime.complete_exit(remaining_token_wei=0)

    broadcaster = DisabledBroadcaster()
    broadcast_blocked = False
    broadcast_reason = None
    try:
        broadcaster.submit({"to": cfg["execution"]["router_whitelist"][0], "data": "0x"})
    except BroadcastDisabled as exc:
        broadcast_blocked = True
        broadcast_reason = str(exc)

    kills = kill_switch_proofs(cfg, d5)
    flags = observation_flags(
        entry_slippage=None,
        exit_slippage=None,
        broadcast_to_inclusion_ms=None,
        gap_percentage_points=None,
        unresolved=False,
        cfg=cfg,
    )
    result: dict[str, object] = {
        "schema_version": 2,
        "research_only": True,
        "go": "NOT_MADE",
        "live_broadcast": "DISABLED",
        "live_allowed": False,
        "self_go_allowed": False,
        "signing_performed": False,
        "broadcast_performed": False,
        "real_transaction_hashes": [],
        "preregistration_path": str(PREREG.relative_to(ROOT)),
        "preregistration_sha256": prereg_sha,
        "reaudit10_preregistration_sha256": cfg["source_anchors"]["reaudit10_preregistration"]["sha256"],
        "source_constant_proof": source_constant_proof(cfg),
        "chain_evidence": {
            "archive_head_at_start": head,
            "fixture": fixture,
            "entry_block": entry_block,
            "exit_block": exit_block,
            "pool_token0": token0,
            "pool_token1": token1,
            "pool_fee": fee,
            "pool_weth_at_entry_wei": str(pool_weth),
            "notional_share_of_pool_weth": amount_in / pool_weth,
            "d5": d5,
            "prebroadcast_full_bundle_eth_call": prebroadcast_bundle,
            "entry_quote": entry_quote,
            "exit_quote": exit_quote,
            "exit_atomic_guard": exit_atomic_guard,
            "chainlink_dashboard_only": chainlink,
        },
        "estimands": {
            "accounting_asset": "WETH",
            "entry_slippage_pp": None,
            "exit_slippage_pp": None,
            "realized_net_weth": None,
            "realized_net_return": None,
            "quote_return": int(exit_quote["amount_out"]) / amount_in - 1.0,
            "gap_percentage_points": None,
            "real_broadcast_to_inclusion_ms": None,
            "status": "NOT_OBSERVED_DRYRUN_NO_REAL_FILL",
            "quote_is_fill_evidence": False,
            "observation_flags": flags,
        },
        "pipeline_proof": {
            "happy_path": runtime.snapshot(),
            "broadcast_rejection": {
                "blocked": broadcast_blocked,
                "reason": broadcast_reason,
                "disabled_broadcaster_attempts": broadcaster.attempted_calls,
                "rpc_send_calls": 0,
            },
            "kill_switches": kills,
            "all_kill_switches_triggered": all(v["triggered"] for v in kills.values()),
            "real_latency_not_claimed": True,
        },
    }
    result["rpc"] = rpc.summary()
    OUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    REPORT.write_text(render_report(result))
    ARM.write_text(render_arm_checklist(result))
    manifest_paths = (
        PREREG,
        ROOT / "configs/research/base_reaudit10_seconds_preregistration_20260715.json",
        ROOT / "scripts/research/base_lefttail/keystone_live.py",
        ROOT / "scripts/research/base_b4/AtomicRoundTrip.sol",
        ROOT / "scripts/research/base_rm1/rm1_framework.py",
        ROOT / "scripts/research/base_rm1/rm1_dryrun.py",
        ROOT / "tests/test_base_rm1.py",
        ROOT / "tests/fixtures/base_rm1_v2_golden_20260715.json",
        OUT,
        LEDGER,
        REPORT,
        ARM,
    )
    hash_sidecar = {
        str(path.relative_to(ROOT)): file_sha256(path) for path in manifest_paths
    }
    sidecar = DIAG / "base_rm1_v2_artifact_hashes_20260715.json"
    sidecar.write_text(json.dumps(hash_sidecar, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "status": "DRYRUN_COMPLETE_NO_BROADCAST",
                "preregistration_sha256": prereg_sha,
                "output": str(OUT.relative_to(ROOT)),
                "output_sha256": file_sha256(OUT),
                "rpc": rpc.summary(),
                "all_kill_switches_triggered": result["pipeline_proof"]["all_kill_switches_triggered"],
                "broadcast_performed": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
