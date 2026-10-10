# R2 第三方审查采纳书

采纳时间：2026-07-20T04:53:56Z

## 新结论标签

```text
PROSPECTIVE_DIRECTIONAL_META_ALPHA_LEAD
STRONG_HISTORICAL_META_ALPHA_LEAD
CONFIRMATION_PENDING
SHADOW_DATA_COLLECTION_ELIGIBLE
FORWARD_ELIGIBLE = false
DEPLOY_ELIGIBLE = false
```

原 `PURE_DIRECTIONAL_META_ALPHA` **作废（过重）**。

## 关键事实（审查方指出，本方核对后接受）

1. R2 主表 308 = F10 训练开发集；在无 row-level OOF provenance 前，`+158.8%` 不能当确认证据。
2. R1 未持久化模型 → 仅有 `FROZEN_RECIPE`；R2 重建是新 operational artifact。
3. `SELL_SUCCESS` 上 atomic ≡ QV2；R2 增量主要是 unsellable→-100% override。
4. 校准 3+5 仅为 smoke test，不是 external validity。
5. F10 提高 per-accepted EV，但降低 per-signal（skip=0）总机会收益 → 更像资金约束下的优先级/sizing，不是无脑硬砍。
6. unsellable：全量 KS live-pass **8**（7 revert + 1 economic）；core fatal **7**；此前表述歧义已对账。

## 下一步

只做 **R3 prospective Shadow**（零资金、零决策影响、全量留痕）。不再 Discovery，不改 5 特征 / HGB / q50。
