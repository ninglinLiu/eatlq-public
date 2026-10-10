# KEYSTONE R3 Prospective Shadow

## 状态
- 零资金 / 零决策影响
- 禁止 Discovery、禁止改 q50 / 五特征 / HGB
- 冻结来源：`r2.1.0`

## 结论立场（继承审查）
```text
PROSPECTIVE_DIRECTIONAL_META_ALPHA_LEAD
FORWARD_ELIGIBLE = false
DEPLOY_ELIGIBLE = false
```

## 怎么用
1. 对**每一条** KEYSTONE pass（含 F10 fail）在 outcome 前调用 `shadow_recorder.append_preoutcome(...)`
2. 另任务事后写入 atomic/portfolio outcome（不得回改 score/threshold）
3. 到预注册评价点一次性拆盲看 E1/E2/E3

## Primary endpoints
见 `R3_PREREGISTRATION.json`
