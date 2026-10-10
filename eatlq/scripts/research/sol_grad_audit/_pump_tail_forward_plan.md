# Forward Paper 标准（T+15→T+90 右尾彩票）

当前分类：**UNTRADABLE_RIGHT_TAIL**

## 模板标准
- 至少 200 笔 paper trades
- 至少 14 自然日
- 每笔 0.1%–0.3% 总资金
- 记录 T+15/T+90 可成交价、sell fail、sparse grid

## 升级条件
- paper mean > 0
- paper bootstrap p5 >= 0
- 右尾频率接近回测
- 真实成交折损 <30%

## 停止条件
- 连续 200 笔无 >+100% 且 mean<0
- sell fail / no liquidity >10%
- grid vs 真实偏差中位 >50%

**当前测量不支持启动 forward paper。**
