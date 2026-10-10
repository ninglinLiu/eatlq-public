# B/D 修复 + 3 历史 bug 回归测试 changelog（2026-06-22）

约束遵守：SYSTEM_PANIC 保持 active（`manual_left_tail_config_change_20260622_pause_buys`），未恢复买入；
未动 TP/trailing/stop/sizing/凸性参数；未动选池/信号逻辑；每项配离线测试，全程不花钱、不上链。

## 改动与测试（逐项）

| 项 | 文件:行 | 改了什么 | 测试 | 结果 |
|---|---|---|---|---|
| P0-① panic fail-closed | `executor/src/main.py` `_panic_active`(~201) | 读 panic key 异常 → `return True`(按暂停) + 限频 `send_telegram` 告警 | `PanicFailClosedTests`×4 | PASS |
| P0-② RPC 告警 | `executor/src/tx_sender.py` `_rpc_alert`(新增)、`_ReadFallbackHTTPProvider.make_request`(~57)、`send_raw_transaction`(~140) | 主用失败→限频告警；主备双挂→escalate(绕过限频)告警 | `RpcAlertTests`×8 | PASS |
| Bug1 0-token 到账 | `executor/src/swap.py` `_extract_tokens_from_receipt`(已存在,锁定) | 用真实 receipt(0xea6a3062 BOT 买入) 从 ERC20 Transfer 日志恢复 token_out | `ReceiptTokenRecoveryTests`×2，断言=90146692587553330993631(≈90146.69 BOT，非 0) | PASS |
| Bug3 from_hash bytes | `executor/src/position.py` `from_hash`(已含 bytes fallback,锁定) | bytes 键值 redis hash 正确解码 token_amount | `FromHashBytesTests`×2 | PASS |
| Bug2 0-token 告警 | `executor/src/main.py` `_handle_signal`(~810) | live 模式 0-token 终态发 CRITICAL Telegram(报告 B 已判 PASS) | `ZeroTokenAlertLockTests`×1(源结构锁) | PASS |
| P1-③ monitor 异常告警 | `executor/src/main.py` `_update_monitor_failure_state`(新增)+loop(~1330) | 连续异常计数，≥3 且过限频窗口→告警；干净轮重置 | `MonitorFailureStateTests`×4 | PASS |
| P1-④ 启动 TG 校验 | `executor/src/notify.py` `validate_telegram_or_raise`(新增) + `main.run()`(~1214) | live 启动校验 token/chat_id 且发测试消息；缺失/失败→`TelegramConfigError` 拒启 | `TelegramStartupValidationTests`×4 | PASS |
| P1-⑤ 组合敞口闸 | `executor/src/main.py` `_portfolio_exposure_exceeded`/`_open_exposure_wei`(新增)+开仓路径(~660)；config `max_portfolio_fraction` | Σ未平仓 stake ≥ 钱包×fraction → 跳过新买；新 env `EXECUTOR_MAX_PORTFOLIO_FRACTION` 默认 0.30；评估不了则 fail-open | `PortfolioExposureGateTests`×5 | PASS |
| P2-⑥ 训练时间 holdout | `signal_bridge/train_model.py` `time_ordered_split`(新增)+`main`(~127) | 按 t0 时间序划分；fit+p_cut 仅用 train；holdout 指标入 params；确认无 shuffle split | `TrainModelHoldoutTests`×3 | PASS |

## 新增 env（默认值，已生效需重启才读；本次未重启、未恢复买入）
- `EXECUTOR_MAX_PORTFOLIO_FRACTION=0.30`（组合敞口上限）
- `EXECUTOR_RPC_ALERT_INTERVAL_SEC=300`（RPC 告警限频）
- `EXECUTOR_MONITOR_FAIL_ALERT_THRESHOLD=3` / `EXECUTOR_MONITOR_FAIL_ALERT_INTERVAL_SEC=300`

## 测试命令
```
cd /opt/EATLQ
python3 -m unittest tests.test_executor_alert_exposure_fixes -v   # 29 tests OK
python3 -m unittest tests.test_train_model_time_holdout -v         # 3 tests OK
```

## changed files
- services/executor/src/main.py
- services/executor/src/tx_sender.py
- services/executor/src/notify.py
- services/signal_bridge/train_model.py
- tests/test_executor_alert_exposure_fixes.py（新）
- tests/test_train_model_time_holdout.py（新）
- tests/fixtures/bot_buy_receipt_0xea6a3062.json（新，离线 receipt fixture）

## 仍 FAIL / 注意
- 无 FAIL。32/32 测试通过。
- Bug2 是「源结构锁」而非全链路功能测试（离线无法驱动真实 live 买入到 0-token 终态）；功能已由审计 B 在源码 `main.py:799-806` 验证，本测试锁住「该分支含 live_trading_mode 守卫 + send_telegram + MANUAL CHECK」防回归。
- 新逻辑需 executor 重启才生效；按约束**未重启、未恢复买入**，等确认。
