# 路线 C 执行报告（C1 + C2）

> 你选了 C 且 C1、C2 都要做 · **RPC 仍 7,885/20,000（本轮 +0）** · **未动 V3 live**

---

## C1 · 模板接入 Listener ✅

**做了什么**

1. 新建 `hook_template_match.py` — 用 20 个模板 + 本地 bytecode cache 给每个池打标签（0 RPC）
2. `v4_pool_listener.py` 新池自动带 `hook_template` 字段
3. 对已有 **80,793** 条 jsonl 跑完 `--retag-jsonl`（约 5 秒）

**每条池子多了什么**

```json
"hook_template": {
  "template_name": "returns_delta_top_a",
  "template_match_via": "address",
  "hook_bucket": "known_template:returns_delta_top_a",
  "hook_family": "template:returns_delta_top_a"
}
```

**怎么用**

```bash
# 以后新池监听（会自带模板字段）
python3 v4_pool_listener.py --days 14

# 仅重打标签（0 RPC）
python3 v4_pool_listener.py --retag-jsonl
```

---

## C2 · Roundtrip 修复 ✅（部分突破）

**根因（之前以为的「Base fork bug」其实是 approve 对象错了）**

- V4 `PoolSwapTest` 在 `unlockCallback` 里结算时，调用的是  
  `WETH.transferFrom(用户, PoolManager, 数量)`  
- 这里的 **msg.sender 是 Helper 合约**，不是 PoolManager  
- 用户只 `approve` 了 PoolManager → **allowance 不够 → revert**

**修复**

1. 用户必须 **approve Helper 合约**（WETH +  alt token）
2. 卖出一腿用 **用户实际持有的 alt token 余额**，而不是 delta 理论值（避免 rounding revert）

**验证**

| 测试 | 结果 |
|---|---|
| Forge `test_vanilla_roundtrip` | **PASS** |
| Forge `test_pool_swap_test_direct` | **PASS** |
| Python 单池 roundtrip（vanilla WETH） | **buy_ok=True, sell_ok=True** |

**仍要注意**

- 只在少量池上验证过；**hook 池、RD 池需再抽样**
- Python 显示的 slippage 数字对 weird-decimal 池可能不准（计算口径问题，不影响 buy/sell 成败）
- **不等于可上线** — 还需批量 roundtrip 标签 + 与 V3 644 对照

---

## 你现在可以干什么

| 动作 | 谁来做 | 成本 |
|---|---|---|
| 用模板字段做监控/报表 | 团队读 jsonl 里的 `hook_family` | 0 |
| 对 211 池再跑 **roundtrip 强标签**（不仅是 tx replay） | 下一个 agent | 0 RPC，耗 anvil 时间 |
| 把 RD 模板 B 的「86% replay ok」写成内部 caution 规则 | 你拍板（路线 B 政策） | 0 |
| 上 V4 实盘 | **不要** | — |

---

## 给下一个 agent 的一句话

> Roundtrip 根因已修（approve Helper + sell 用余额）；C1 retag 完成。请对 merged 211 池或 stratified 样本跑 roundtrip batch，更新 convexity，**仍禁止动 live**。

---

## 变更文件

- `hook_template_match.py`（新）
- `v4_pool_listener.py`（模板 + `--retag-jsonl`）
- `v4_pool_swap.py`（approve Helper）
- `foundry/src/V4ReplayHelper.sol`（sell 用 balanceOf）
- `foundry/test/V4ReplayFork.t.sol`（测试 approve 修正）

---

*这不是 deployable strategy · 仅研究进展*
