# GL13 Rug Dynamics Backtest (644 universe, CH-only)

Generated: 2026-07-17T07:29:04.284747+00:00

## 方法
- 冻结 644 labeled universe + 当前实盘 gate 标签（离线回放，**零 RPC，不影响实盘**）
- 链上事件来自 ClickHouse：`swaps_decoded` / `mints_decoded` / `burns_decoded` / `tx_from_by_hash`

## 实盘 gate 覆盖
- 会通过 live_full（模型+深度+非F）: **120** 池
- F闸拦截（骨架或部署者）: **248** 池
- 其中 rug 被 F 拦: **246** / 262 rugs
- 若无 F 闸会买到的 rug（反事实）: **219**

## 1. Rug 速度（首 Swap → 首次 Burn）
- 全部 rug: fast(≤300blk) **1**, slow **261**
- 中位速度: **70.0 分钟**
- 被 F 拦的 rug: fast **1**, slow **245**, 中位 **249.5 min**

## 2. 信号 → Rug（entry_block → flip_block，仅 live_traded rugs）
- n=0, 中位 **Nones**

## 3. 买卖不对称（W=600s 预窗口，WETH 进/出池笔数比）
- rug 中位 sell/buy: **0.5**
- clean 中位 sell/buy: **0.67**

## 4. 部署者 = Burn 发起者
- 全部 rug: **0.0%**

## 5. 流动性 cliff（flip 前 50blk 有深度 → flip 时 ≈0）
- 全部 rug: **0.0%**

## 6. E 窗深度坍塌（1800s / 0.5 ETH，反事实：F 闸关闭时会被买到的 rug）
- 会触发: **5** / 219 (2.3%)
- 当前实盘 live_traded rug 数: **0**（F 闸全开时 rug 进不去）

## 7. 同骨架 Top clusters（rug 数 ≥2）

| skeleton | n_rug | F拦 | fast% | 中位(min) | deployer=burn | cliff% | sell/buy |
|---|---:|---:|---:|---:|---:|---:|---:|
| `0xc0da9a5bd00e…` | 30 | 30 | 0% | 70.0 | 0% | 0% | 0.48 |
| `0x2e7f2922ec03…` | 25 | 25 | 0% | 249.8 | 0% | 0% | 0.31 |
| `0x88ea17ea4d47…` | 14 | 14 | 0% | 70.0 | 0% | 0% | 0.52 |
| `0x25a4417966ef…` | 13 | 13 | 0% | 250.0 | 0% | 0% | 0.76 |
| `0xd4e7eae43daa…` | 12 | 12 | 0% | 69.7 | 0% | 0% | 0.55 |
| `0x64899f76a13b…` | 11 | 11 | 0% | 250.0 | 0% | 0% | 0.53 |
| `0xe787f8c21740…` | 10 | 10 | 0% | 249.9 | 0% | 0% | 0.31 |
| `0x7698b0f40d67…` | 10 | 10 | 0% | 249.9 | 0% | 0% | 0.62 |
| `0xf1bd84e96014…` | 9 | 9 | 0% | 250.0 | 0% | 0% | 0.49 |
| `0x55079814c80e…` | 8 | 0 | 0% | 69.8 | 0% | 0% | 0.0 |
| `0x368e825a6b95…` | 7 | 7 | 0% | 69.8 | 0% | 0% | 0.51 |
| `0xb0faced336bf…` | 5 | 5 | 0% | 250.0 | 0% | 0% | 0.55 |
| `0x8b98958f532a…` | 5 | 5 | 0% | 250.0 | 0% | 0% | 0.63 |
| `0x574970a9db87…` | 5 | 5 | 0% | 70.0 | 0% | 0% | 0.53 |
| `0x5f6b905d5e05…` | 4 | 4 | 0% | 160.0 | 0% | 0% | 0.53 |

## CUBEMAN（实盘案例，不在 644 内）
- 同骨架在 644 中 rug 数: 3（链上 live 案例为慢 rug ~26min）

Artifacts: `scripts/research/base_gl13/_gl13_*.json`
