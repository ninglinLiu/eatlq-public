# Tick Lifecycle Timeline

Source: `v2/tick-lifecycle-timeline.svg` · kind: `timeline`

**TICK LIFECYCLE · BASE L2 MEASURED PATH**

| Time | Event |
|------|--------|
| T+0 | Swap on-chain |
| +6ms | Redis XADD |
| +15ms | Signal out |
| +20ms | Tx submit |
| +2s | Sequencer confirm |
| after | open_position · slot0() monitor |

