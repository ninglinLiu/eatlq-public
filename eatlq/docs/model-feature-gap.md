# 模型特征不对齐：现状、决策与后续路线

> 2026-03-26 初稿 / 2026-03-28 Phase 3.5 实施更新

## 问题（已解决）

策略引擎实时推理和离线训练之前使用了**两套完全不同的特征体系**，导致训练产出的 XGBoost 模型无法在线上生效。

此外，FeatureStore 之前无法解析原始 EVM 日志中的交易金额（`data` 字段），
导致 `price_velocity_5m=0`, `buy_pressure_ratio=1.0`, `token_volatility_5m=0`，
只有 `activity_heat`（事件计数）有意义。

| 维度 | 旧离线训练 | 旧线上推理 | **Phase 3.5 对齐后** |
|---|---|---|---|
| 特征数 | 13–17 维 | 4 维（3 个恒为常数） | 4 维（全部有效） |
| 粒度 | 钱包 × 日 | pool × tick | pool × 5min 窗口 |
| 预测目标 | 钱包明天是否活跃 | -- | 未来 10min 买压是否持续 |

## Phase 3.5 修复

### A. FeatureStore 事件解码

`feature_store.py` 现在根据 `topic0` 解码 `data` 字段：

- **V2 Swap / Aerodrome vAMM**：解码 `amount0In, amount1In, amount0Out, amount1Out`
- **V3 Swap**：解码 `int256 amount0, amount1`（有符号）
- **ERC20 Transfer**：仅计入 `activity_heat`

所有 4 个特征现在都能产生有意义的值。

### B. 历史特征 + 标签

`025_realtime_training_views.sql` 新增：
- `eatlq.pool_features_5m`：每个 pool × 5min 窗口的 4 个特征
- `eatlq.pool_realtime_training_v1`：带标签的训练集（未来 10min 买压 > 0.5 → label=1）

### C. 训练管线

- `scripts/extract_realtime_training_data.py`：从 ClickHouse 提取训练数据
- `scripts/train_model.py --features realtime --label label`：4 特征训练模式
- `scripts/run-realtime-training.sh`：一键提取 + 训练

### D. 模型加载优先级

`ModelScorer._candidate_models()` 优先加载 `signal_model_realtime_v*.json`，
验证 4 维兼容性后自动启用。

## 旧决策（已过时）

~~保持 confidence=0.5~~ → 当 `signal_model_realtime_v1.json` 训练完成并部署后，
`confidence` 将由模型实际预测值驱动。

旧的钱包行为模型保留在 `models/` 目录用于离线分析。

## 模型资产清单

| 文件 | 用途 | 特征维度 | 线上可用 |
|---|---|---|---|
| `signal_model_realtime_v1.json` | 实时 pool 信号 | 4 | 训练后自动加载 |
| `signal_model_base_v1.json` | 钱包行为预测 (Base) | 17 | 否（离线分析用） |
| `signal_model_multichain_v1.json` | 钱包行为预测 (多链) | 17 | 否（离线分析用） |
| `signal_model_v1.json` | 钱包行为预测 (Mainnet) | 13 | 否（离线分析用） |

## 2026-06 model_manifest gate (enforced)

Each deployable XGBoost model must ship a sibling `<model_stem>.manifest.json` that locks
`feature_order` and `num_features`. At strategy startup, `ModelScorer` rejects candidates when:

- `feature_order` is not a prefix-compatible match of runtime order
  (`price_velocity_5m`, `buy_pressure_ratio`, `token_volatility_5m`, `activity_heat`,
  optional `unique_buyer_entropy_5m`)
- `num_features` disagrees with the loaded booster

Set `STRATEGY_REQUIRE_MANIFEST=1` to refuse manifest-less models entirely (recommended after
Phase 6 backfill). Generate/backfill manifests with:

```bash
python3 scripts/build_model_manifest.py --glob 'models/signal_model*.json'
```

Training paths (`scripts/train_model.py`, `scripts/retrain_model.py`) emit manifests on save.

---

## 2026-04 Toxic-Liquidity 增强

- `buy_pressure_ratio` 从事件计数比升级为 `$` 加权流量比（线上与离线统一到成交量口径）。
- `price_velocity_5m` 保持流量失衡定义，但新增了同 block 内 `(sender, block)` 净额合并，降低做市商左右手噪音。
- 新增 `unique_buyer_entropy_5m`（买方分布熵）作为第 5 维候选特征，默认向后兼容 4 维模型。
- 增加 `pool_features_5m_sliding` 训练入口视图（当前先做兼容别名，后续可替换为严格 trailing 300s 回放）。
- 新增去毒链路：
  - `(tx_hash, log_index)` 去重；
  - `<60s` 快进快出地址打入 toxic 集；
  - 同 block sandwich 侧地址打入 toxic 集；
  - strategy / wallet scorer / meme scorer 统一读取 `infra/config/mev_blacklist.yaml` 并扣除。
- 在线推理新增可选校准与阈值：
  - `STRATEGY_USE_CALIBRATED=1` 时加载 `*_calibrator.pkl`；
  - 阈值优先读取 `*_threshold.json`，其次 `*_metrics.json` 的 `ev_threshold`。

---

## Whitepaper v2 Feature Store prefix drift (MM-05)

> Documentation-only; static contract. No Redis key renames, no whitepaper alignment claims.

### Prefix mapping

- **`fs:`** — **whitepaper narrative prefix** for “feature store / sorted set” keys in LASZLO whitepaper v2 prose. It is **not** the Redis key prefix used by this codebase’s FeatureStore.
- **`feat:`** — **current implementation prefix** in `services/strategy/src/feature_store.py` (e.g. `feat:flow:{token}`, `feat:buys:{token}`). Treat **`feat:`** as the implementation source of truth for Redis keys.

### Feature cardinality

Whitepaper material often lists **four** core pool features (`price_velocity_5m`, `buy_pressure_ratio`, `token_volatility_5m`, `activity_heat`). The runtime additionally supports **`unique_buyer_entropy_5m`** as an **additive fifth feature** — i.e. it extends the scoring vector for compatible models but is **not** part of the original whitepaper four-feature set. Do not rename keys to `fs:` for cosmetic parity.

**Stable contract phrase:** **`unique_buyer_entropy_5m` is additive fifth feature, not original whitepaper four-feature set.**

### Decision flags

IMPLEMENTATION_ALLOWED=false | TRAINING_ALLOWED=false | LABELING_ALLOWED=false | LIVE_ALLOWED=false | PHASE6_ALLOWED=false | WHITEPAPER_ALIGNMENT_CLAIM_ALLOWED=false
