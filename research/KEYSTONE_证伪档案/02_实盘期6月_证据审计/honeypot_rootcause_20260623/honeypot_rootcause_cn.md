# 蜜罐事件根因回查（只读，SYSTEM_PANIC 保持 active）

事件: 2026-06-23 两笔 live 策略单（PARADAENGI，`0x6c8a7e7f…` / `0x56c6ee34…`）均 pretrade PASS / sell_sim=ok，买入后 28–46s `honeypot_detected` → ABANDON −100%，合计 **−0.114 ETH**。
约束: 只读，未改任何 env/逻辑，未恢复买入。

---

## A. 蜜罐机制取证

### A1. 机制判定 = 闭源「全员禁卖」型（非定时冷却）
- honeypot.is 全量返回（`curl` 实测）：
  - `summary.flags` 含 **`high_fail_rate`（severity=critical，"A very high amount of users cannot sell"）** + `closed_source`(high) + `all_snipers_honeypot`
  - `simulationSuccess=false`，`simulationError="execution reverted: \"revert: HP: BUY_FAILED\""` ← 合约里硬编码 `HP:`（honeypot）revert
  - `holderAnalysis`: holders=16, **successful=0, failed=14** → 历史上**没有任何普通持有者卖出成功**
  - `contractCode.openSource=false`（GoPlus 却报 `is_open_source=1`，两家冲突）
- 判定：**闭源合约，卖出路径对非特权地址一律 revert**（黑名单全员 / sells-disabled / sniper-trap）。不是「买后 N 块冷却」，是**从出生起除部署者外都卖不掉**。

### A2. 时间线（证明买前模拟过、真卖失败）
| 事件 | 笔1 | 笔2 |
|---|---|---|
| bridge emit | 04:12:06Z | 04:51:30Z |
| 买入落地块/ts | block 47699292 / 1782187931 | block 47700474 / 1782190295 |
| pretrade | PASS sell_sim=ok friction=-0.0277 | PASS sell_sim=ok friction=-0.0321 |
| honeypot_detected | 04:12:57Z（买后 ~46s） | 04:52:03Z（买后 ~28s） |
| 买入 tx | `84078a4b…`(status=1) | `8ceef47a…`(status=1) |

买入成功拿到 token（链上 status=1，钱包至今仍持有 6.07e21 / 6.80e21 单位），但卖不掉。

### A1 真正根因（决定性，代码级）
`swap.py:1146-1151` `simulate_real_sell` 用 `eth_call` **state-override 强行给探针地址写入 balance + allowance slot** 再模拟卖出：
```
overrides = { token: {"stateDiff": {bkey: grant_hex, akey: grant_hex}}, probe: {...} }
```
→ 这会**绕过合约真实的"卖出授权"分支**（黑名单 / 「只有真实买家能卖」/ sells-disabled）。模拟里探针凭空有余额、swap 数学成立 → `ok`；但真实钱包走真实卖出时命中 anti-sell 分支 → revert → `honeypot_detected`。
**这是审计 (b) 类共享盲区的教科书命中：state-override/eth_call/QuoterV2 看得见 swap 数学，看不见 token 的转账钩子/授权逻辑。**

---

## B. 外部风险门为什么没拦（关键）

### B3. 门确实接了真·聚合器（已实现且启用）
`external_risk.py:113-122` 真调 **GoPlus**(`api.gopluslabs.io`) + **honeypot.is**(`api.honeypot.is/v2/IsHoneypot`)；`preflight_trade.py:94-120 external_risk_gate` 在买入前第一道跑。不是仅内部 blacklist/mintable。`providers=goplus,honeypot`，`fail_closed=1`，`canary_on_missing=1`。

### B4. 放行原因 = 两家在进场那刻都没给出可判蜜罐信号 + 解析器漏读关键字段
- 日志 `canary=None` + PASS ⇒ `external_risk_gate` 走的是末尾干净 `external_risk_ok`（无 critical、无 missing），**没走 canary 缩仓分支**（`preflight_trade.py:114-120`）。即进场那刻两家都「看起来没问题」。
- **即便现在**复查（`curl` 实测）两家也抓不干净：
  - GoPlus：`is_honeypot=0`、`cannot_sell_all=None`、无税、`transfer_pausable=0`、`trading_cooldown=0`、`honeypot_with_same_creator=0`。**GoPlus 判它「不是蜜罐」。** 唯一红旗 `creator_percent=0.93`（93%），按 `max_creator_pct=50` 现在会触发 critical——但进场那刻 token 刚出生，creator% 很可能尚未计算/为 0。
  - honeypot.is：**`honeypotResult.isHoneypot` 字段根本不存在**（=None，非 True）→ 代码 `_assess_honeypot`(`external_risk.py:196`) 判不出 critical；`simulationResult` 也不存在 → 仅记 `honeypot_no_simulation_result`(missing)。
- **解析器 bug（真漏洞）**：honeypot.is v2 实际把决定性信号放在 `summary.flags[].severity=critical (high_fail_rate)`、`simulationSuccess=false`、`holderAnalysis.successful=0/failed=14`，而代码只读 `honeypotResult.isHoneypot` 和 `simulationResult` 这两个**该响应里不存在的字段** → 把一个「14/14 卖出失败、闭源、HP revert」的明确蜜罐解析成了「无数据」。

> 结论：门没拦不是「没查」，是 **(1)进场那刻聚合器数据未成熟 + (2)honeypot.is 解析器读错 schema，把 critical 级 flags/holderAnalysis 全丢了**。后者是可立即修的代码缺陷。

---

## C. 回测毁灭率是否被系统性低估

### C5. 回测卖出端与今天失效的工具**完全同源**
- `keystone_live.py:113-139 price_all/quote` 用 **QuoterV2** 算买/卖 net；`risk_rebacktest.py` 复用该缓存。
- QuoterV2 只模拟池子 swap 数学，**不执行 token transfer 钩子/授权** → 对「全员禁卖」型蜜罐，QuoterV2 会算出**正常可卖、正常 friction**，回测里这类币显示为普通收益，**不会变 −100%**。
- 回测的 `fatal` 标签 `keystone_live.py:82` = `(ns_fwd<=2) | (tick跌破-50%)`，基于**前向 swap 数/tick 路径**，与「能否真卖」无关。

### C6. 量化
- 历史样本用「同源尺子」纳入：**131/131 与 217/217 全部**的卖出端都是 QuoterV2 判定（无一例用过 GoPlus/honeypot.is 历史数据）。
- 用外部聚合器重过滤（`blacklist_mintable_validation_cn.md`，n=131）：**删 0 笔，毁灭率 3.8%→3.8% 不变，月化 +57.1% 不变**——在历史样本上是 **no-op**（历史选中的微盘里正向命中拉黑/增发=0）。
- 关键 UNKNOWN（该文件已预警，今天被现实证实）：探针是 current-state；**真进场时 token 刚出生，聚合器覆盖率远低于 77%，最该起作用的瞬间是瞎的**。今天两笔正是此情形。
- 因此「回测 5.1% 毁灭率」**系统性低估了这一类**：QuoterV2 既给回测定价、又给 live 把关，二者共享同一盲区，回测无法看见「QuoterV2 可卖但真卖 revert」的蜜罐。真实左尾 > 回测左尾，幅度无法用现有数据闭式量化（需 forward 记录每笔进场那刻的真卖结果）。

### C7. 信号来源 = keystone bridge（**不是** S074 legacy）
- bridge 日志：`04:12:06 BUY signal emitted … WETH/PARADAENGI p_fatal=0.069`、`04:51:30 … p_fatal=0.054`，均 < `BRIDGE_P_CUT_MAX=0.27` → keystone 模型主动选中。
- exec:trades 里 `dry_run=true` 的 S074 是 shadow，与这两笔无关。
- 旁证：05:24 / 05:39 bridge 又收到同名 PARADAENGI 池但 `p_fatal=0.613/0.632 > 0.27` 被 **REJECT** → 同部署者连环铸蜜罐，模型对其中低分的两个没识别出风险。

---

## D. 候选防御方案（仅方案 + 代价，未实现）

| 方案 | 能否挡住今天这类 | 对出单频率/收益代价 | 备注 |
|---|---|---|---|
| (a) 买后极小额真实试卖确认再补仓 | **能**（真卖 revert 即停，损失=试卖额而非全仓） | 中：每单多 1 笔 gas + 延迟；仓位分两段 | 唯一能挡「真卖 revert」型的真·闭环；试卖额建议 ~$3–5 |
| (b) 强依赖 GoPlus/honeypot.is + fail-closed | **部分**（今天 GoPlus 判 clean；honeypot.is 修解析后能凭 high_fail_rate/holderAnalysis 挡，但进场那刻数据未成熟仍漏） | **高**：进场瞬间多数无数据→fail-closed 会把大量真 alpha 也拒掉（历史曾 131→6 量级） | 必须先修 honeypot.is 解析 bug；单靠它不够 |
| (c) 买后冷却期复检 + 异常立即清仓 | 对「全员禁卖」**无效**（清仓本身就卖不掉） | 低 | 只对「买后才 toggle 坏」型有用 |
| (d) 同名/同部署者批量蜜罐聚类黑名单 | **能挡连环**（同 deployer 的后续池） | 低、且几乎不伤 alpha | 今天同名 PARADAENGI 反复出现，deployer 维度黑名单性价比最高 |

### D9. 恢复买入的最小必要新增防线（建议）
**最小组合 = (a) + (d) + 修 honeypot.is 解析**：
1. **(a) 买后小额试卖确认**：把「先全仓买→发现卖不掉=全损」改成「小额买/试卖→确认可卖→补满」。这是对本次根因（state-override 看不见真卖授权）唯一直接有效的闭环。预估代价：每单 +1 笔 gas（~$0.003）+ 数秒延迟；若做两段建仓，单笔有效 size 暂减半。
2. **(d) deployer/同名聚类黑名单**：低成本挡住连环蜜罐，几乎不损 alpha。
3. **修 honeypot.is 解析**（读 `summary.flags severity=critical`、`simulationSuccess`、`holderAnalysis.successful/failed`），让 (b) 这道门对「已被 14 人验证卖不掉」的币真正生效。

**频率/收益影响**：主要来自 (a) 的两段建仓与延迟——预估月化频率因每单多一步确认而下降有限（信号本就稀疏），但能把「单笔 −100% 全损」改成「−试卖额」。(d)/(b) 对历史 alpha 近乎零损（C6 已证 131 样本 0 删单）。**净权衡：用很小的频率/操作成本，换掉今天这类 −100% 左尾。**

---

## 总结

- **根因**：闭源「全员禁卖」蜜罐；我方 pre-buy `simulate_real_sell` 用 state-override 写余额槽，绕过了合约真实卖出授权分支（(b) 类共享盲区）。买成功、卖 revert。
- **外部门失效**：进场那刻聚合器数据未成熟 + honeypot.is 解析器读错 schema（漏掉 critical 级 high_fail_rate / holderAnalysis / simulationSuccess=false）。GoPlus 至今仍判其非蜜罐。
- **回测低估**：回测卖出端=QuoterV2，与 live 把关同源，结构上看不见此类蜜罐 → 5.1% 毁灭率是低估；外部聚合器历史重过滤在 n=131 上为 no-op（删 0 笔），无法弥补进场瞬间的数据空窗。
- **信号源**：keystone bridge 模型主动选中（p_fatal 0.069/0.054），非 legacy。
- **恢复前置**：至少落地 (a) 买后试卖确认 + (d) deployer 聚类黑名单 + 修 honeypot.is 解析；否则会继续踩同类雷。

## changed files / commands（全只读）
- 取证命令：`curl` GoPlus/honeypot.is（两 token）、`docker exec` web3 读 tx/block/balance、`docker logs` executor/bridge、读 `external_risk.py`/`preflight_trade.py`/`swap.py`/`keystone_live.py`
- 未改任何 env / 逻辑 / 实盘；SYSTEM_PANIC 仍 active（`manual_pause_20260623_after_2_honeypots`）
- 报告：`mvp/audits/honeypot_rootcause_20260623/honeypot_rootcause_cn.md`
