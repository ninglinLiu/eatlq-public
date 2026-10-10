# KEYSTONE 43笔执行缺口复核

> research-only；不作GO。Hybrid为诊断后定义的探索情景，不是预注册PASS。

## 结果

| 口径 | 窗 | n | mean | median | top1移除 | fatal |
|---|---|---:|---:|---:|---:|---:|
| b5_conservative | W1 | 38 | +16.06% | +9.61% | +0.96% | +39.47% |
| b5_conservative | W2 | 61 | -18.48% | -19.97% | -26.80% | +44.26% |
| b5_conservative | W3 | 17 | -1.02% | -7.45% | -11.46% | +11.76% |
| b5_conservative | pooled | 116 | -4.61% | -14.53% | -13.95% | +37.93% |
| hybrid_trigger_proxy | W1 | 38 | +77.64% | +43.37% | +64.21% | +5.26% |
| hybrid_trigger_proxy | W2 | 61 | +58.68% | +37.93% | +51.65% | +3.28% |
| hybrid_trigger_proxy | W3 | 17 | +9.37% | -7.45% | -0.42% | +0.00% |
| hybrid_trigger_proxy | pooled | 116 | +57.67% | +26.24% | +49.42% | +3.45% |

## 解释

- B5的第三方swap限价证据缺失不等于AMM不能执行我方卖单；43个gap里冻结trigger块有40个同时具备Atomic sell与held-quantity quote。
- 替代情景把73个原limit_filled保持不变，只替换43个gap；若Atomic不可卖或held quote缺失仍按全损。
- 结果强烈说明B5的−100%缺失策略过度保守，但不能反向把hybrid当真实成交，因为历史held-wallet身份无法用纯eth_call完整保存。
- 正确后续是冻结这条新estimand，再做独立OOS/forward shadow；不得用本样本直接宣布PASS。

## Hash

```
execution_evidence_sha256=f9d400b4617dbfbd33f468cb68ebf39163e613193b8f034890a2b685ed297304
```
