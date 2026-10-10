# midnight_research

只读。原始响应在 `/root/midnight-research`，本目录不修改它，也不包含私钥或发送交易的入口。

依赖：Python 3.12 标准库。本轮没有 pip 安装。链上抽查用的是本机 Foundry `cast` 1.7.1 和 `https://mainnet.base.org`，只调用了 `eth_call` 和 `eth_getBlockByNumber`。

## 实际跑过的命令

```bash
python3 src/build_tables.py
python3 test/test_lif_identity.py
python3 src/receipt_nets.py
```

三步都在 2026-10-07 成功。`build_tables.py` 写出 `data/*.csv` 和 `data/summary.json`。测试打印 `ok`。`receipt_nets.py` 打印 `987805151`，即五笔收据里清算人留下的 USDC 最小单位之和。

同一天早些时候的链上读取，区块 52288300，时间 2026-10-07 09:39:07 UTC：

```bash
cast call 0xAdedD8ab6dE832766Fedf0FaC4992E5C4D3EA18A \
  "totalUnits(bytes32)(uint128)" \
  0x0b8fe70a0597e371bdcdbf1bbca019826f819d9a1a434e5d54a6793c3393c5ab \
  --rpc-url https://mainnet.base.org
cast call 0xAdedD8ab6dE832766Fedf0FaC4992E5C4D3EA18A \
  "withdrawable(bytes32)(uint128)" \
  0x0b8fe70a0597e371bdcdbf1bbca019826f819d9a1a434e5d54a6793c3393c5ab \
  --rpc-url https://mainnet.base.org
```

返回值分别是 1112582904662105 和 995981889769698。差额等于该市场 API 债务之和。

## 文件

- `facts.md`：部署、权限、常数、口径。
- `report.md`：结论、现金流、资金表和被推翻的说法。
- `handoff.md`：断点和下一次只读观察的停止条件。
- `data/manifest.json`：源文件和派生表的 SHA-256、覆盖范围和缺口。
