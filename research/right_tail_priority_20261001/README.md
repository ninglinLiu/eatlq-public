---
sop_applied: true
alpha_id: AR-RT-P1-20261001
family: F1/F3/F4/F7
next_gate: COMPLETE_INVENTORY_AND_EXECUTABLE_PATH
decision_flags:
  training_allowed: false
  live_allowed: false
---

# 四项优先右尾研究：2026-10-01 执行包

本目录只做研究，不发交易、不改生产配置，不重复 RH 外部核实。

用户最新调度只推进 RT06 Smart Money；RT07/RT08/RT09 暂时冻结，保留已有成果。下方旧命令是历史复跑清单，不代表授权继续其他三个方向的新采集。

- [结果与限制](REPORT.md)
- [收益测量前的预登记](PREREGISTRATION.md)
- [冻结候选钱包](frozen_wallets.json)：BSC / Base 各 12 个，2026-09-17 保存的 GMGN 现货快照。
- [冻结后续买入](pilot_entries.json)：22 笔 / 19 个代币；供应商标签未验证，不能称为已合格聪明钱。
- [旧钱包资格新审计](legacy_wallet_audit.json)
- [账户现金流分类账](account_cash_ledger.json)
- [延迟与退出代理比较](pilot_comparison.json)
- [全部 441 笔买入候选](expanded_candidate_manifest.json)：扩展冻结清单，账户回执已全量核验；跟单盈利是另一层验证。
- [固定金额历史池报价](amount_quote_result.json)：0.01/0.02 ETH；不是最终净利润。
- [叙事覆盖和重放](narrative_result.json)
- [迁池固定窗口](migration_window.json)和[探针](migration_narrow_probe.json)
- `request_ledger.jsonl` 记录每次新公开请求；`raw/` 保存响应与 SHA-256，不含密钥。
- [Smart Money 范围审计](smartmoney_universe_audit.json)和[完整已保存榜单](smartmoney_full_saved_cohort.json)。
- `smartmoney_receipts.json`、`smartmoney_routes.json`、`smartmoney_cost_ledger.json` 与 `smartmoney_per_trade.csv`：441 个原候选逐笔核查，未把源手续费称为跟单手续费。
- `contracts/ResearchV4Executor.sol`、`src/smartmoney_settlement.py`：仅用历史 `eth_call` 的临时状态覆盖研究转账结算，不部署、不签名。金额报价、结算模拟、费用假设和实盘盈利是不同层次。
- `contracts/ResearchMixedExecutor.sol`、`src/smartmoney_expand_settlement.py`：混合协议、实际余仓与自建成本；非生产交易程序。
- `smartmoney_receipts_extended.json`、`smartmoney_v4_keys_calldata.json`、`smartmoney_routes_extended.json`、`smartmoney_cost_ledger_extended.json`：补齐原交易 calldata、Aerodrome V2 与 Algebra ABI 的扩展证据，原版本保留。
- `smartmoney_route_gap_audit.json`：逐条缺口，不将无法解析当成亏损。

当前 Smart Money 复跑顺序：

```bash
python3 src/smartmoney.py universe
python3 src/smartmoney.py receipts
python3 src/smartmoney_routes.py mapping
python3 src/smartmoney_routes.py metadata
python3 src/smartmoney_routes.py routes
python3 src/smartmoney_routes.py costs
forge test --root /root/right_tail_priority_20261001 --contracts contracts --out sol_out --cache-path sol_cache --use 0.8.26 --optimizer-runs 200 --via-ir
python3 src/smartmoney_settlement.py probe
```

只有结算仪器核验后才运行 `python3 src/smartmoney_settlement.py all`；程序保留混合协议、不支持调用上下文与缺费用项，不当成亏损或零费用。

扩展适配顺序（所有网络命令顺序执行）：

```bash
python3 src/smartmoney_routes.py calldata
python3 src/smartmoney_solidly.py
python3 src/smartmoney_gaps.py
python3 src/smartmoney_settlement.py all
python3 src/smartmoney_expand_settlement.py all
python3 src/smartmoney_funding.py prepare
python3 src/smartmoney_funding.py execute
forge test --offline --contracts contracts --out sol_out_repair --cache-path sol_cache_repair --use 0.8.26 --optimizer-runs 200 --via-ir
python3 src/smartmoney_pancake_repair.py prepare
python3 src/smartmoney_pancake_repair.py run
python3 src/smartmoney_route_failure_probe.py
python3 src/smartmoney_gate_audit.py
python3 src/smartmoney_signal_structure.py
python3 src/smartmoney_fee_diagnostics.py stress
python3 src/smartmoney_execution_diagnostics.py
python3 -c "import sys; sys.path.insert(0,'src'); from smartmoney_expand_settlement import summary; summary('smartmoney_follower_execution_result.json')"
python3 src/smartmoney_cash_schedule.py
python3 src/smartmoney_unclassified_flows.py
python3 src/verify.py
```

最终逐笔执行表为 `smartmoney_execution_per_trade.csv`，含 ETH 计价金额、中文状态、逐腿池路径与原交易链接；条件模型汇总为 `smartmoney_execution_summary.json`。补充诊断后，以 `summary('smartmoney_follower_execution_result.json')` 重建最终汇总和表格，不能用旧主结果覆盖补充。B20 用官方 `base.b20` 命名空间及实际历史哨兵查询确认余额槽；旧适配失败记录保存在 `smartmoney_settlement_prior/`。平台已明确为自建，直接执行不计第三方平台佣金，但 RPC、服务器、部署等尚未摊销。

`smartmoney_follower_execution_result.json` 在扩展主结果之上补两笔固定 USDC/WETH 资金路径及按协议身份限定的 Pancake V2 适配修复，明确区分 439 条来源路径与 2 条自建替代路径，不伪称全部还原。`smartmoney_observation_cutoff.json` 冻结本次退出可观察性链头，未满 24h 的信号保留；以后补成熟窗口必须另存版本。`smartmoney_gate_audit.json` 对一个已出现回滚的代币核实历史购买授权状态，不复用源钱包签名、不修改权限。`smartmoney_signal_structure.json` 是信号重复及固定持有期的计划并发统计，不是资金约束回测。

`smartmoney_cash_schedule.json` 按已核算的买入支出、卖出回款和我方链上费用形成条件现金排程；未知退出不编造回款，余仓未估值，不能称为实盘组合收益或净值回撤。`smartmoney_l2_fee_stress.json` 同时列出完整退出子集与完整加部分退出的现金差，避免高费用诊断悄悄遗漏部分退出亏空。

`smartmoney_route_failure_probe.json` 记录成熟退出回滚的逐腿前缀诊断。其输出只缩小失败路径范围，不作为新的退出、现金回款或永久卖不掉的证明；最终 CSV 附相应诊断字段。

## 复跑

在 `/root/right_tail_priority_20261001` 目录下使用系统 `python3` 运行。依赖 `requests`、`eth_abi`、`eth_utils`；本机已有，无新增安装。所有输出都在本目录，旧项目不覆盖。

只读、无需网络的检查：

```bash
python3 src/test_research.py
python3 src/ledger.py
python3 src/narrative.py analyze
python3 src/pilot.py summary
python3 src/verify.py
```

已执行的采集与核查顺序：

```bash
python3 src/collect.py probe
python3 src/wallets.py transfers
python3 src/wallets.py cash
python3 src/legacy_audit.py
python3 src/narrative.py collect
python3 src/narrative.py analyze
python3 src/migration.py census
python3 src/migration.py narrow
python3 src/ledger.py
python3 src/pilot.py receipts
python3 src/pilot.py metadata
python3 src/v4_paths.py mapping
python3 src/v4_paths.py paths
python3 src/validate_paths.py
python3 src/v4_paths.py repair
python3 src/pilot.py summary
python3 src/narrative.py retry
python3 src/amount_quotes.py
python3 src/repair_internal.py
python3 src/verify.py
```

不要并发运行公开网络采集脚本：节流锁目前是进程内的。已保存的成功响应复用缓存；失败不当作空数据。首次窗口与入场名单已冻结，复跑不能悄悄改窗口或更换钱包。不要为拿到结果删除冻结文件；新试验应另开版本。

`legacy_audit.py` 需要本机 ClickHouse 容器，只读查询已约束账户与块范围，并保存 EXPLAIN。没有导出大表。Python 包 DuckDB / Pandera 在本环境不可用，本轮使用有界 JSON/CSV 和显式单元测试。

## 未决不是否证

源钱包现金流、他人成交、池状态中间价、我方可执行净收益是不同层次。缺少成本、深度、税费、库存或退出时，不提供伪造的净利润、组合回撤或“大概率赚钱”概率。低胜率、靠极少数赢家覆盖亏损本身不是拒绝条件。
