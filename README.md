# EATLQ 公开快照

这是 2026-10-10 从运行中的研究机器拷出来的一份源码和文档，没有旧的 Git 历史。私有仓库 `ninglinLiu/EATLQ` 和 `ninglinLiu/eATLQ-research-archive` 仍是私有的，也不在这份快照里。

公开这份快照不等于放弃版权。这里没有加开源许可证。

## 目录

- `eatlq/`：交易栈的服务、crate、测试、配置、Compose 文件、ClickHouse 和 Postgres 初始化脚本，以及脚本和白皮书。`scripts/research/` 只保留程序和说明（`.py`、`.rs`、`.sql`、`.sh`、`.md`）。
- `research/`：各轮 Base / KEYSTONE 研究的脚本和结论文档。

`infra/compose/.env.example` 里的数据库密码是占位符 `change-me`。私钥、Telegram token、Dashboard 口令是空的。带账号密码的 URL 已去掉账号密码。

## 原作者文档和后来叠加上去的笔记

Franklin Nexus / LASZLO 写的是这些文件：

- `eatlq/Architecture.md`
- `eatlq/Architecture-Simplified.md`
- `eatlq/Whitepaper-v1-continued.md`
- `eatlq/Operations.md`
- `eatlq/Whitepaper-v2-continued.md`
- `eatlq/Home.md`
- `eatlq/whitepaper-v2.md`
- `eatlq/Code-Review.md`
- `eatlq/原作者白皮书 2 外宣版.txt`
- `eatlq/docs/author-whitepapers/` 下的外宣稿

其余文档，包括 `docs/` 里后来的决策笔记、`核查背景.md`、`前问题核查.md`，以及 `research/` 下的脚本和结论，是在原作者文本之后追加的，不代表原作者的设计。

## 没有放进来的东西

- 钱包私钥、`.env`、API token、SSH 密钥
- 个人笔记、聊天导出、书签
- 依赖目录、虚拟环境、构建产物、日志、数据库文件
- 大于 1MB 的数据文件，以及 parquet、压缩包、HTTP 原文
- Morpho 官方仓库的本地克隆。那些代码在 `morpho-org`。

链上地址如果出现在研究笔记里，只是地址，不是私钥。
