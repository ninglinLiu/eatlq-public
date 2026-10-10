# EVENT_UNIVERSE — Mimic R0（已锁定）

**状态：LOCKED 2026-08-15**  
**规则：** 未解锁不得增删事件。收数与打分只针对本表。  
**口径：** [`../GOAL.md`](../GOAL.md)

## 配额核对

| 规则 | 要求 | 本表 |
|---|---|---|
| Web2 / 文化渗漏 | ≥5，须含牛来 | 6（含 E01 牛来） |
| 链原生梗 | ≥5 | 6 |
| 近失 / 未粘住 | ≥5 | 6 |
| 政治 / 身份 | ≥3 | 4 |
| 事件总数 | 15–30 | 22 |
| 已知超级赢家占比 | ≤40% | 6/22 = 27.3% |

`is_known_superwinner=1` 表示抽样时已知名的超级赢家叙事（事后标签风险），不是结果字段。

## 事件表

| event_id | search_term | category | chain_primary | t0_anchor | is_known_superwinner | t0_rationale |
|---|---|---|---|---|---|---|
| E01_niulai | 牛来 | web2_culture | bsc | 2026-08 院线梗进入中文社媒热议 | 0 | 国产电影角色/口号在微博抖音形成可搜词「牛来」；链上合约尚未成为统一焦点时，词本身已可被陌生人复述。 |
| E02_moodeng | moodeng | web2_culture | solana | 2024-09 泰国小河马出圈 | 1 | 动物园幼崽照片与短视频跨语言传播，英文拼写 Moodeng 成为可搜索专名。 |
| E03_chillguy | chillguy | web2_culture | solana | 2024 漫画小人「Just a chill guy」梗图扩散 | 1 | 固定姿势插画在 X/Reddit 被当作情绪反应图大量转发，专名可检索。 |
| E04_fartcoin | fartcoin | web2_culture | solana | 2024 粗口幽默名作为独立笑话传播 | 1 | 「屁/放屁」类冒犯幽默在英文 meme 圈可独立成词，无需项目说明即可被复述。 |
| E05_neiro | neiro | web2_culture | ethereum | 2024 Kabosu 继任狗相关报道 | 0 | 柴犬纪念叙事下出现继任狗名字 Neiro，新闻与社媒同时给出可搜专名。 |
| E06_pnutt | pnutt | web2_culture | solana | 2024 纽约松鼠安乐死新闻 | 0 | 动物园/宠物松鼠新闻短周期冲上美区社媒，Peanut/Pnutt 拼写变体成为搜索簇。 |
| E07_clanker | clanker | chain_native | base | Clanker bot 在 Base/Farcaster 被当作发币入口讨论 | 0 | 链内工具名本身成为「在 Base 上发梗币」的口语标签，搜索词先于任一具体代币。 |
| E08_virtuals | virtuals | chain_native | base | Virtuals 代理叙事在 Base 社群被反复提 | 0 | 「AI agent + Base」短语集群中，Virtuals 作为协议/叙事标签被搜索，早于单一代币焦点争议。 |
| E09_pumpfun | pump | chain_native | solana | pump.fun 作为发射器名进入口语 | 0 | 发射器品牌名成为 Solana 土狗场的元标签；本事件盯「工具名被搜」而非某个毕业币。 |
| E10_bonk | bonk | chain_native | solana | Solana 狗系吉祥物口语化 | 1 | 链内狗形象 + 单词 BONK 在 Sol 社群成为默契符号，可脱离具体合约被喊。 |
| E11_brett | brett | chain_native | base | Base 蓝娃 Brett 作为链吉祥物被讨论 | 1 | Matt Furie 宇宙角色名在 Base 叙事里被当作「这条链的人设」，专名可搜。 |
| E12_toshi | toshi | chain_native | base | Base 猫/吉祥物名在社群传播 | 0 | Coinbase 关联吉祥物形象带来可搜专名，讨论发生在具体合约收敛之前即可观测。 |
| E13_trump_fragment | trump | political_identity | solana | 2024 选举季人名作为通用搜索词 | 1 | 政治人物姓名本身是全球搜索词；链上在同一窗口出现大量同名合约，焦点未先验唯一。 |
| E14_harris | harris | political_identity | solana | 2024 选举季另一候选人名 | 0 | 与 E13 同季的对称人名搜索；用于对照「身份词是否自动变成谢林合约」。 |
| E15_maga | maga | political_identity | ethereum | 口号词长期存在于政治话语 | 0 | 四字母口号先于任何代币存在；链上同名密度预期高，适合测焦点唯一性。 |
| E16_biden | biden | political_identity | ethereum | 在任/卸任周期人名搜索 | 0 | 人名政治搜索词，用作身份类近失/碎片化对照，不预设粘住。 |
| E17_goatseus | goatseus | near_miss | solana | 「goatseus maxmus」拼写玩笑传播 | 0 | 刻意难拼的拉丁化玩笑词出现；预期同名变体多、焦点易碎，用作近失结构。 |
| E18_hawktuah | hawktuah | near_miss | solana | 短视频口头禅瞬间出圈 | 0 | 语音梗转写为无标准拼写的搜索簇；多合约抢同一语音记忆时，粘住条件苛刻。 |
| E19_luguan | 撸官 | near_miss | bsc | 中文职场/官场梗短周期热搜 | 0 | 中文双关热词可搜，但缺统一视觉锚与英文 ticker；预期命名赛碎裂。 |
| E20_shandong | 山东学 | near_miss | bsc | 中文社媒对 BSC 刻板印象标签 | 0 | 标签是对整条链的嘲讽词，不是单一角色；预期「梗」强、「合约谢林点」弱。 |
| E21_夫妻文学 | 夫妻文学 | near_miss | bsc | 中文圈对特定土狗叙事的戏称 | 0 | 元叙事标签（讲故事的方式）而非单一 IP；用于测「有热度词但不可发行权」的近失。 |
| E22_wen | wen | near_miss | solana | 币圈口头禅「wen」长期存在 | 0 | 愿望/催促口头禅人人会用，焦点唯一性极低；用作「名字即欲望但无法粘住」对照。 |

## 类别计数

- web2_culture: E01–E06 (6)
- chain_native: E07–E12 (6)
- political_identity: E13–E16 (4)
- near_miss: E17–E22 (6)

## 锁定声明

本文件锁定后：

1. 不得为了「结果更好看」替换近失事件。
2. 不得追加「前一百榜」上的新赢家来提高右尾比例。
3. 收集脚本必须以 `event_id` + `search_term` 为键，不得按市值排序选样。

搜索别名（不增删事件）锁在 [`aliases.csv`](aliases.csv)。`pump` 收紧为 `pumpfun`；`夫妻文学` 不扩成「夫妻」。

**UNLOCK 条件：** 仅当 R0 报告写明抽样失败（例如某类链零命中）且发起人书面同意时，可开新文件 `EVENT_UNIVERSE_v1.md`，本文件保留只读。
