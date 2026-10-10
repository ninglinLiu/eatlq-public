"""Read-only replay: external-risk gate OLD vs NEW policy diff.

Runs INSIDE the executor container so module + network egress match live.
NEW policy = current container env (post-change). OLD policy = same object with
only the 3 toggled flags reverted (block_mintable / block_blacklist /
canary_on_missing). This isolates the out-flow impact to exactly those flags.

No writes, no trades. Only outbound GoPlus / Honeypot.is read calls.
"""
import copy
import sys
import time

sys.path.insert(0, "/app/services/executor/src")
sys.path.insert(0, "/app")
import external_risk as er  # noqa: E402

CHAIN = 8453


def state(res) -> str:
    if not res.ok:
        return "BLOCK"
    if res.data_missing:
        return "CANARY"
    return "FULL"


def main(path: str) -> None:
    tokens = [l.strip().lower() for l in open(path) if l.strip()]
    new_pol = er.ExternalRiskPolicy()  # reads live container env
    old_pol = copy.copy(new_pol)
    old_pol.block_mintable = False
    old_pol.block_blacklist = False
    old_pol.canary_on_missing = False

    print(f"providers={new_pol.providers} new(block_mintable={new_pol.block_mintable},"
          f"block_blacklist={new_pol.block_blacklist},canary_on_missing={new_pol.canary_on_missing},"
          f"fail_closed={new_pol.fail_closed},canary_usd={new_pol.canary_usd})")
    print(f"n_tokens={len(tokens)}\n")
    print(f"{'token':42} {'OLD':7} {'NEW':7} {'transition':22} new_reason")

    tally = {}
    blocked_new, canary_new = [], []
    for t in tokens:
        er._CACHE.clear()
        ro = er.assess_external_token_risk(t, CHAIN, old_pol)
        er._CACHE.clear()
        rn = er.assess_external_token_risk(t, CHAIN, new_pol)
        so, sn = state(ro), state(rn)
        trans = f"{so}->{sn}"
        tally[trans] = tally.get(trans, 0) + 1
        if so != "BLOCK" and sn == "BLOCK":
            blocked_new.append((t, rn.reason))
        if so == "FULL" and sn == "CANARY":
            canary_new.append((t, rn.reason))
        print(f"{t:42} {so:7} {sn:7} {trans:22} {rn.reason[:60]}")
        time.sleep(0.3)

    print("\n=== TALLY (state transitions) ===")
    for k, v in sorted(tally.items()):
        print(f"  {k:22} {v}")
    print(f"\nnewly BLOCKED by mintable/blacklist: {len(blocked_new)}")
    for t, r in blocked_new:
        print(f"   {t}  {r}")
    print(f"newly shrunk to CANARY (missing data): {len(canary_new)}")
    for t, r in canary_new:
        print(f"   {t}  {r}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/tokens.txt")
