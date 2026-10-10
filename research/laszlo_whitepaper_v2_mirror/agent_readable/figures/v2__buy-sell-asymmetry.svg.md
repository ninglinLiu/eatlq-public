# BUY vs SELL Asymmetry

Source: `v2/buy-sell-asymmetry.svg` · kind: `comparison`

**EXECUTION ASYMMETRY · BUY CONSERVATIVE / SELL FAIL-OPEN**

| BUY path (fail-closed triggers) | SELL path (fail-open) |
|--------------------------------|------------------------|
| stale RPC head | intent_id dedup (SET NX) |
| daily drawdown breach | entry_token_amount cap |
| fire_thr < max(model, 0.68) | not blocked by stale head |
| balance RPC failure | pending_sell TTL self-heal |

Shared: single on-chain exit = `rust_executor` only · PM emits signal → executor swaps

