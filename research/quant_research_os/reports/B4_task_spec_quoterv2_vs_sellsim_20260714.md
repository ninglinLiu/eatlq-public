# B4(v2)· 任务书:QuoterV2 的两类盲区 —— 入场谎报 vs 时间性 LP-pull

> 审查 agent 下发 / codex 执行。**工作区 = `/opt/EATLQ`**(不是 /root/quant_research_os)。
> 纯只读模拟(`eth_call` / fork-replay),不下单/签名/广播/改 live。研究只证伪,不 GO。

## 0. 修正与翻案(审查方自纠)

- v1 派错工作区(/root/quant_research_os 无基建),codex 拒绝造数是**对的**(CHA:不拿占位冒充链上)。基建全在 `/opt/EATLQ`。
- 读 `/opt/EATLQ/scripts/research/base_lefttail/_labeled_universe.json` 后**部分翻我自己的结论**:该 644 池 gated 候选宇宙里 `n_instant_hp=0`、`transfer_toggle=2`、`lp_pull=260`。即 Base 左尾几乎全是**时间性 LP-pull**,不是 transfer-hook 蜂窝。→ QuoterV2 的 ~99.5% **入场时**一致很可能是**真的**;审计不全错。我先前"QuoterV2 结构性撒谎"是**瞄错了失败模式**。
- 因此 B4 不再是"证明 QuoterV2 撒谎",而是**把两类盲区分开、各自量化到底**,数据落哪边报哪边,无立场要护。

## 1. 复用既有基建(禁止重造)

- QuoterV2 客户端:`scripts/lib/v3_quoterv2_struct.py`
- fork-replay 买后卖 ground-truth:`scripts/research/base_lefttail/fork_replay_backtest.py`(+ `replay_realgas_sellsim.py`)
- 已标注宇宙:`_labeled_universe.json`(644,label∈clean/delayed_rug/dead,含 flip_block/flip_offset/mechanism/in_traded)、`_set131_forkreplay.json`、`_set217_forkreplay.json`(逐笔 `qv2_net` vs `real_buy_ok/real_sell_ok/qv2_missed_honeypot`)、`_universe_blocks.json`
- 宇宙构建器:`build_labeled_universe.py`(注释已自陈幸存者结构 1275→644→131)
- 风险缓存:`external_risk_cache.py`、`_external_risk_current.json`
- RPC env:`FORK_ARCHIVE_RPC` / `LABEL_RPC_URL`(B3 同款 archive)

## 2. 两个零假设(预注册,数据落哪报哪,禁止看结果改)

- **H0a 入场结构谎报**:在**入场块**,QuoterV2 卖腿报价 == fork-replay 买后即卖真值。
  拒绝判据:在 instant-honeypot / transfer-hook 子集上,入场分歧率(QuoterV2 报可按合理价卖、fork-replay revert/收回≈0)Wilson 95% 下界 > 5%。
- **H0b 时间性盲区**:入场点可卖 ≠ 持有期可卖;LP-pull 在 `flip_block` 之后使可卖性坍塌,QuoterV2 与任何**点态** sell-sim(含 D5 原子)均**无法预见**。
  量化:`flip_offset`(rug 距入场块)分布 vs 策略持有到出场的块数 → 有多少 rug 落在出场前(真损)vs 出场后(已离场)。

## 3. 执行(分两臂,先便宜后按需)

**臂 A(零新 RPC,先做):** 只用既有 `_labeled_universe.json` + `_setNNN_forkreplay.json` 复算:
1. 按 ground-truth label 分层的**入场一致率表**(QuoterV2 vs fork-replay),报 instant_hp / delayed_rug / clean 各自分歧率 + Wilson CI。
2. **时间性表**:`flip_offset` 分布 × `flip_timing_vs_our_exit`(before/at/after exit),量化"点态 sim 看不见的未来 rug"占左尾多少、其中多少真造成损失。
3. **幸存者结构**:traded_survivor(384,rug 62.5%)vs hidden_by_quoterv2_friction(260,rug 8.46%)vs 全候选(644)—— 讲清 99.5% 从何而来、它掩盖/未掩盖了什么。

**臂 B(有界新 RPC,仅当臂 A 留有缺口):**
4. **去 p_fatal 门的无偏 instant-hp 探针**:`n_instant_hp=0` 可能是 p_fatal 上游门的幸存假象。抽全新池样本(可交叉 B3 的 3,302 fresh 宇宙),**关掉 p_fatal 门**,fork-replay 逐个标 instant_hp,给无偏 instant-honeypot 患病率 + CI。若仍≈0,则 Base 入场谎报罕见的结论坐实。
5. **同一 delayed_rug 的 pull 前/后**双块:QuoterV2 与 sell-sim 在 flip 前/后各跑一次,证其**无时间/状态感知**(点态盲区的直接演示)。

## 4. 必须产出

- 分层入场一致率表 + 时间性 flip 表 + 幸存者结构表(臂 A);无偏 instant-hp 患病率 + pull 前后双块(臂 B,若做)。
- 一句**明确翻案/坐实**:Base 上 QuoterV2 入场谎报是否罕见?左尾主因是否为时间性 LP-pull?—— 直接喂 B5 的防 rug 结论(防 rug 手段应是**快出场/forward 监控**,而非更好的点态 sell-sim)。
- golden:抽 2–3 池链上独立核对(整数 ==);回归测试断言 ==。

## 5. 铁律

pin archive block、只读 `eth_call`/fork-replay、不下单/签名/广播、不启动/重启服务、不改 live;不自判 GO;结论可复现(记 RPC/block/hash)。任何"数据缺失/未测"一律保守,不作有利推断;n 不足照实报 n 与 CI 宽。
