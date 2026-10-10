# Triple Stop Loss

Source: `v2/triple-stop-loss.svg` · kind: `params`

**POSITION MANAGER · TRIPLE STOP**

| Stop | Param | Rule |
|------|-------|------|
| Trailing Stop | 5% | from peak · while in profit → auto SELL signal |
| Time Stop | 30m | flat ±2% range |
| Hard Stop | 15% | unconditional loss cap |

