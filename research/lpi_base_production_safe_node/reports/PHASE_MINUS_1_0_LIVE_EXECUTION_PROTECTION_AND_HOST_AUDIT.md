# Launchpad Intelligence Base Production-Safe Node

## Phase -1–0: Live Execution Protection and Host Audit

Audit date: 2026-07-17 UTC

```text
overall_status = PAUSED_LIVE_PRIORITY
SAFE_TO_PROCEED = false
blocking_statuses = BLOCKED_RESOURCE_RISK, BLOCKED_UNKNOWN_PRODUCTION_BOUNDARY, BLOCKED_MISSING_CODE_BASELINE
```

1. **本轮目标**  
   仅完成实盘链路识别、被动健康基线、Git/历史资产位置审计、隔离路径确认和 Phase -1–0 报告。不进入 Bootstrap、Adapter、回填或平台研究。

2. **实盘 execution 健康**  
   EATLQ 的 12 个已观察容器前后均存活；archiver、ingestor、ClickHouse、Postgres、Redis、autoheal 保持 Docker `healthy`。executor 容器/PID 存活，但无 healthcheck，短窗口日志未推进且 mtime 约早两小时；未找到安全可读的订单、风控、仓位或 checkpoint 心跳。因此不能声明 execution 全链路健康。

3. **本任务资源使用**  
   仅运行短时 `ps`/`ss`/`vmstat`/`iostat`/`mpstat`、Docker 元数据、systemd 状态、Git 元数据和文件名级检查。没有常驻进程。任务输出为少量 Markdown。主机自身在最终 3 秒样本中平均仅 2.01% CPU idle，触发停止门。

4. **实际完成**  
   识别 EATLQ Compose 项目、生产容器、数据卷、监听端口、systemd 服务、crontab 数量、3 个相关 Git 仓库、生产仓库脏状态、候选历史资产和一个无 Git provenance 的 LPI 原型。保留并创建审计专用隔离路径。

5. **修改文件**  
   仅新增本报告以及 `docs/BASE_PRODUCTION_EXECUTION_INVENTORY.md`、`BASE_EXECUTION_SAFETY_BASELINE.md`、`BASE_NODE_CURRENT_STATE.md`、`BASE_NODE_SECURITY.md`、`BASE_NODE_REPOSITORY_AUDIT.md`、`BASE_EXISTING_RESEARCH_ASSETS.md`。所有文件均位于 `/root/lpi_base_production_safe_node/`。

6. **查询的数据源**  
   无数据库、Redis、RPC、WS、HTTP、交易所或链上数据源查询。只读取 Linux `/proc`/系统元数据、Docker daemon 元数据、systemd 状态和 Git 元数据。

7. **执行的只读查询**  
   `uptime`, `nproc`, `free`, `swapon --show`, `df`, `lsblk`, `vmstat`, `iostat`, `mpstat`, `ps`, `ss`; targeted `docker ps/compose ls/inspect`; `systemctl list-units`; Supervisor/PM2 status presence checks; redacted-count-only crontab inspection; bounded `find`; `git rev-parse/status/worktree/ls-files/ls-tree/log`; file `stat`. 未读取进程参数、环境、日志正文或配置正文。

8. **测试结果**  
   未运行代码测试。Phase 0 审计检查通过的是“路径存在性/仓库身份/容器连续存活”；安全门测试失败：CPU headroom 不足，execution heartbeat 不可证。

9. **Golden 结果**  
   未开始；`NOT_RUN_PHASE_GATED`。

10. **数据质量**  
    未访问研究数据；`NOT_ASSESSED_PHASE_GATED`。现有资产仅做 filename-level inventory。

11. **UNKNOWN 和缺失**  
    风控/订单管理/仓位同步的确切进程边界；executor 正常心跳；checkpoint 路径；生产 RPC/WS（有意不读）；数据库依赖图；查询预算；所有 Launchpad 合约版本与事件语义；共享代码 provenance；Golden/Adapter/Bundle 版本。

12. **是否触发健康守卫**  
    是。初始 Load 1m 7.28（8 核），最终 6.46；最终 `mpstat` 3 秒平均 CPU idle 2.01%，单个区间 0%；Swap 已使用约 2.2 GiB。触发 `BLOCKED_RESOURCE_RISK` 并停止。

13. **是否修改生产状态**  
    否，按本轮已执行命令范围可验证：生产 HEAD 保持 `90077ef020315e2490833c328d46941d6a24d996`；审计后 tracked-status hash 为 `8d1870eba8d153a970722b9185c5da8ff48ee5ea171129e5ea048a3281fe3415`；所有生产容器 ID 前后一致且保持 `Up`；没有执行 Docker/service/database/Redis/RPC/交易写操作。限制：没有全盘 pre-hash，不能证明其他并发主体未发生变化。

14. **当前阻塞**  
    `BLOCKED_RESOURCE_RISK`：CPU 无安全余量。`BLOCKED_UNKNOWN_PRODUCTION_BOUNDARY`：execution/risk/order/position heartbeat 与 checkpoint 未确认。`BLOCKED_MISSING_CODE_BASELINE`：发现的 19 文件 LPI 目录未被 Git 跟踪，无 commit/tree hash/test provenance。

15. **下一步最小安全任务**  
    暂停。待主机出现持续 CPU 余量后，仅重复更长的被动健康窗口，并由生产 owner 指认 canonical executor/risk/order/position health signal 与 checkpoint 路径；随后再决定能否给出 `SAFE_TO_PROCEED`。在此之前禁止共享代码 Bootstrap、数据源映射、数据库查询和 Clanker 工作。
