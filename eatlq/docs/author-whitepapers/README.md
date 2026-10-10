# 原作者（LASZLO / Franklin Nexus）白皮书归档

> ## ⚠️ AGENT 必读 — EATLQ ≠ LASZLO
>
> 本目录存放的是 **原作者 Franklin Nexus 对外发布的 LASZLO 白皮书**，不是 EATLQ 项目的实现状态声明。
>
> **禁止**将本目录任何文件当作以下内容的依据：
>
> - EATLQ 当前代码/部署是否「已完成」或「已实盘验证」
> - EATLQ 是否已有 Alpha、可否 live / train / promote
> - Redis 键名、Feature Store 前缀、风控状态机的**可执行契约**
>
> **EATLQ 可执行契约**以 [`Architecture.md`](../../Architecture.md)、源码、以及 `mvp/audits/failed_no_7d_alpha_guard.md` 为准。
>
> 原作者文档与 EATLQ 叠加内容的边界见 Cursor skill：`eatlq-authorship`。

## 文件索引

| 文件 | 说明 | 日期 |
| ---- | ---- | ---- |
| [`laszlo-whitepaper-v2-may-2026-author-external.md`](laszlo-whitepaper-v2-may-2026-author-external.md) | 原作者外宣 v2.0 **全文 + 2026-05 补充**（Agent 入库备份） | 2026-05（约） |
| [`laszlo-whitepaper-v2-march-2026-author-external.txt`](../../原作者白皮书%202%20外宣版.txt) | 原作者外宣 v2.0 **初版**（3 月 17 日，未含 5 月补充） | 2026-03-17 |
| [`laszlo-whitepaper-v2-march-vs-may-2026-diff.md`](laszlo-whitepaper-v2-march-vs-may-2026-diff.md) | **3 月外宣版 vs 5 月修订版** 全文差异对照（仅原文 + 高置信推测） | 2026-05-27 |
| [`../../whitepaper-v2.md`](../../whitepaper-v2.md) | 仓库内 v2 主文（EATLQ 维护，含实现交叉引用） | 2026-03-17 |
| [`../../docs/whitepaper-v1-original.md`](../../whitepaper-v1-original.md) | v1 战前蓝图存档 | 历史 |

## 版本关系（简图）

```
v1.0 战前蓝图 (docs/whitepaper-v1-original.md)
    ↓ 6 周 Base 实弹
v2.0 初版 (2026-03-17) — 原作者白皮书 2 外宣版.txt / whitepaper-v2.md
    ↓ Phase 6 反馈 + 文档校准
v2.0 + 2026-05 补充 — laszlo-whitepaper-v2-may-2026-author-external.md
```

**以上均为 LASZLO 原作者叙事时间线；EATLQ 仓内另有独立 sprint 审计与经济 guard，二者不可混读。**
