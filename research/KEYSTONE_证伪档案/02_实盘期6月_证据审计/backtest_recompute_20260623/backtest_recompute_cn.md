# 回测身份核实 + Live 配置重算（只读）

日期: 2026-06-23 | 约束: 只读，未改任何 env / live 配置 | SYSTEM_PANIC 仍 active

重算脚本: `scripts/research/base_lefttail/_recompute_live_config.py`（新增只读工件，复用现有 QuoterV2 缓存，不上链、不花钱）
输出: `scripts/research/base_lefttail/_recompute_live_config.json`

---

## A. 回测"身份"钉死

### A1. 两个数字的来源脚本

| 数字 | 脚本 | 含义 |
|---|---|---|
| **131 笔** | `scripts/research/base_lefttail/risk_rebacktest.py` | 单次 train/holdout 切分（`t0 > quantile(0.6)` 的样本外）经"深度门+左尾模型"后建出的交易集（`build_trades` 实测 len=131） |
| **217 笔** | `scripts/research/base_lefttail/keystone_full_validation.py`（walk-forward 段 178–202 行）与 `exit_upgrade_v1_walkforward.py`（206–244 行，落盘 `mvp/audits/keystone_exit_upgrade/v1_walkforward_cn.md` 明写 n=217） | Feb–Jun 逐月扩窗 walk-forward 的池化样本 |

> 即"131"是**单切分 holdout**，"217"是**逐月 walk-forward 池化**。我本次重算用的是 217（walk-forward，无前视）。

### A2. 各自配置（带文件:行）

**131 / `risk_rebacktest.py`**
- starting_capital: 选单时 `start_equity=10000`（`risk_rebacktest.py:394`）；报表里的"$1000 cap"列与 fatal_stress 用 `start=1000`（`:329`, `:395`）。
- 单笔 sizing: **阶梯** `SIZES=[1000,300,100]`（`:32`），`choose_trade` 取 `min(cap_usd, equity*frac)` 内最大档（`:162-192`）。**不是固定 $25**。实测档位分布 `{300:81, 1000:36, 100:14}`。
- 复利: **是**。`simulate` 的 `stake=min(t.size, cap_usd, equity*frac)`（`:215`）按**当前 equity** 取 frac → equity 涨 stake 涨。
- max_concurrent: 参数存在（`:206,:246`）但**未实现**，注释明说"use time-ordered sequential equity for now"（`:207-208`）→ 实为**顺序串行（=1）**。
- friction 门: `FRIC_THR=-0.05`（`:31`，`choose_trade:177`、`load_inputs:146`）。
- 退出: 沿用 `keystone_live` 的 trailing 35% + hard −50% + ~4h（`keystone_live.py:28-30, 86-98`），净值已缓存在 `_priced_live.json`/`_dynamic_size_realgas.json`。

**217 / `keystone_full_validation.py`（含 walk-forward）**
- starting_capital: `START_EQUITY=1000`（`:28`）。
- 单笔 sizing: `POSITION_FRAC=0.10`（`:27`）、`NOTIONAL_USD=100`（定价口径，`:24`）。
- 复利: **是**。`equity_curve` 的 `stake=eq[-1]*frac`（`:62-67`）。
- max_concurrent: `MAX_CONCURRENT=1`（`:29`，串行）。
- friction 门: `FRIC_THR=-0.05`（`:23`）。
- 退出: trailing 35% + hard −50% + ~4h（docstring `:8` + `keystone_live`）。
- `exit_upgrade_v1_walkforward.py` 只算交易级 returns（不做 equity 模拟），即 217 那张表；`HARD_STOP=log(0.5)`（`:31`）。

### A3. 收益率% 与 MDD% 的分母（带计算行）

- `risk_rebacktest.simulate`：`peak=max(peak,equity); mdd=min(mdd, equity/peak-1.0)`（`:221-223`）→ **MDD 分母 = 运行中的总 equity 峰值**；`total_return = equity/start-1.0`（`:233`）→ **收益率分母 = starting_capital**。
- `keystone_full_validation.max_drawdown`：`peak=np.maximum.accumulate(eq); dd=(eq-peak)/peak`（`:70-74`）→ **MDD 分母 = 运行 equity 峰值**。

> 结论：两套脚本的 MDD 分母都是**总 equity 峰值**，收益率分母是 **starting_capital**。**没有任何一套用"峰值在险资金"当分母** → 这正是 2.6% 这种小数字的来源（被全账户 equity 稀释）。

---

## B. "$2000 起始 + $25/笔 + 6并发"核心疑问

仓库里**没有**用这组参数（$2000/$25/6并发）的脚本；最接近的 `risk_rebacktest.simulate` 是串行、不实现并发。我用并发感知 sim 重算（`_recompute_live_config.py`，walk-forward 217 笔、扣真实 gas + 2% MEV haircut）：

| 指标 | 值 |
|---|---|
| 峰值同时持仓数 | **6 笔**（cap=6 恰好打满，无被挤掉的单） |
| **峰值同时部署资金** | **6 × $25 = $150** |
| MDD（$） | **−$51** |
| MDD / 总 equity 峰值 | **−2.2%**（≈ 你记的 2.6%，差异来自样本集/haircut 版本） |
| **MDD / 峰值在险资金（$150）** | **−34%** |

> **把 2.6% 换算成对在险资金的比例 ≈ −34%。** 即：账面"2.6% 回撤"是被 $2000+ 全账户稀释后的数字；真正在险的那 $150 本金，历史最深回撤约三分之一。

---

## C. 真实 Live 配置专门回测（walk-forward，无前视）

配置: 固定 $100/笔、max_concurrent=10、起始 $1000、**不复利（固定 $100）**、frac=0.10（$100 on $1000）、无日亏熔断、p1 深度门(fric≥−5%)、左尾模型、扣真实 gas + MEV haircut。
样本: walk-forward 池化 **n=217**（2026-02-01 … 2026-06-01，跨度 120 天）。

### C6. 输出（haircut=2% 为基准）

| 指标 | 值 | 说明 |
|---|---|---|
| 总回报（对 $1000） | **+1453%**（end ≈ $15.5k） | 固定 $100/笔 × 217 笔 × 均值 +67%；**被右尾主导**（最佳单笔 +1229%）。见下方告诫 |
| 月化（几何） | +98%/mo | 固定仓位下该数字会误导；本质是 120 天累计 ≈ +$14.5k 已实现盈亏 |
| MDD（对 $1000） | **−$204 = −20.4%**；占 equity 峰值 −15.2%；equity 最低跌破起始 −10.8% | 已实现 equity 曲线 |
| 峰值同时持仓 | **6 笔**（cap=10 从未触发）→ 峰值部署 **$600** | 历史并发上限是 6，不是 10 |
| 单笔胜率 / 致命率(≤−50%) | 67.3% / 5.1% | |
| **最坏单日"合计亏损"**（同日所有亏损仓之和） | **2026-02-01：−$338.5 = −33.8% of $1000** | 但**当天有 24 笔、赢家 +$1080 → 当日净 +$742** |
| 最坏单日**实际净亏** | **2026-04-28：−$57.9 = −5.8% of $1000** | 2 笔、几乎全是亏损、无赢家对冲 |
| 单日 ≥2 笔击穿 −50% | **3 天**：2026-02-01、2026-02-03、2026-03-02 | |
| 单日 ≥3 笔击穿 −50% | **1 天**：2026-02-01（−102%、−57%、−55%） | 当日仍净 +$742 |

MEV haircut 敏感性（对 C 总回报/月化/MDD 影响小）:
- 0%: +1497% / +99.9% / MDD −14.0% / 最坏净日 −$54
- 2%: +1453% / +98.5% / MDD −15.2% / 最坏净日 −$58
- 5%: +1388% / +96.4% / MDD −17.1% / 最坏净日 −$64

### C7. 一句话结论

> 按你定义的口径（同日所有亏损仓之和），历史最坏一天是 **2026-02-01 的 −$338（−34% of $1000）**；但那天 24 笔里赢家 +$1080 把它盖过、**当日净 +$742**。账户**真正净亏最深的一天**是 **2026-04-28 的 −$58 ≈ −5.8% of $1000**。残余尾部 = "某天 rug 集群且**没有赢家对冲**"——在这 217 笔样本内从未发生，但 max_concurrent=10 理论上允许它发生（最坏 10×−100% = 一天 −$1000 = 清零）。

---

## 重要告诫（不得当成净值承诺）

1. **+1453% 不可当预期**：固定 $100/笔放大右尾，且整段被 1 笔 +1229% 主导；去掉该笔总回报塌缩。这是凸性策略的本质——少数赢家扛起全部。
2. **定价仍是"同源" QuoterV2**（被蜜罐骗的同一通道）：出场净值 gross-of-frontrun/抢跑，只用 2% haircut 近似（属审计类别 b "存疑"）。
3. **峰值并发历史=6，但 cap=10**：样本内没打满 10，不等于未来不会；左尾上限仍是 10 仓同日 = 钱包清零。
4. **131 与 217 不是同一回测**：131=单切分 holdout、阶梯 sizing、复利；217=walk-forward、$100 定价、复利。两者的收益/MDD 数字不可直接互比。

## changed files / commands
- 新增（只读工件）: `scripts/research/base_lefttail/_recompute_live_config.py`、`_recompute_live_config.json`
- 新增报告: `mvp/audits/backtest_recompute_20260623/backtest_recompute_cn.md`
- 命令: `python3 _recompute_live_config.py`（复用缓存，不上链）
- 未改: 任何 executor/.env/live 配置；SYSTEM_PANIC 仍 active。
