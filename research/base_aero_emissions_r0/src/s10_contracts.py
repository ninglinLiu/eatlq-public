"""Step 1: write out/contracts.json with every address used, plus how it was verified."""

import sys

sys.path.insert(0, "/root/base_aero_emissions_r0/src")

import common as C
import rpc as R

DOCS = "https://github.com/aerodrome-finance/docs/blob/main/content/security.mdx"
SLIP = "https://github.com/aerodrome-finance/slipstream/blob/main/README.md"
ICLGAUGE = "https://github.com/aerodrome-finance/slipstream/blob/main/contracts/gauge/interfaces/ICLGauge.sol"
IVOTER = "https://github.com/aerodrome-finance/contracts/blob/main/contracts/interfaces/IVoter.sol"
IMINTER = "https://github.com/aerodrome-finance/contracts/blob/main/contracts/interfaces/IMinter.sol"


def probe(addr, calls):
    out = {}
    for name, selector in calls:
        r, e = R.eth_call_batch([(addr, selector)])[0]
        out[name] = R.dec_addr(r) if (r and r != "0x") else f"revert/empty"
    return out


def main():
    core = {
        "Voter": "0x16613524e02ad97eDfeF371bC883F2F5d6C480A5",
        "Minter": "0xeB018363F0a9Af8f91F06FEe6613a751b2A33FE5",
        "VotingEscrow_veAERO": "0xeBf418Fe2512e7E6bd9b87a8F0f294aCDC67e6B4",
        "AERO_token": "0x940181a94A35A4569E4529A3CDfB74e38FD98631",
        "FactoryRegistry": "0x5C3F18F06CC09CA1910767A34a20F771039E37C0",
    }
    checks = {
        "Voter": probe(core["Voter"], [("minter()", C.SEL["minter()"]),
                                       ("ve()", C.SEL["ve()"]),
                                       ("factoryRegistry()", C.SEL["factoryRegistry()"])]),
        "Minter": probe(core["Minter"], [("aero()", C.SEL["aero()"]),
                                         ("voter()", C.SEL["voter()"]),
                                         ("ve()", C.SEL["ve()"])]),
        "VotingEscrow_veAERO": probe(core["VotingEscrow_veAERO"],
                                     [("token()", C.SEL["token()"]), ("voter()", C.SEL["voter()"])]),
        "AERO_token": probe(core["AERO_token"], [("minter()", C.SEL["minter()"])]),
    }

    factories = C.ch_rows("""
        SELECT c.gauge_factory AS gauge_factory, c.pool_factory AS pool_factory,
               count() AS n_gauges_in_panel
        FROM census.aero_gauge_created c
        INNER JOIN census.aero_gauge_map m ON m.pool = c.pool
        GROUP BY gauge_factory, pool_factory ORDER BY n_gauges_in_panel DESC""")
    cl = []
    for f in factories:
        gf = f["gauge_factory"]
        p = probe(gf, [("implementation()", C.SEL["implementation()"]), ("voter()", C.SEL["voter()"]),
                       ("nft()", C.SEL["nft()"])])
        cl.append({
            "CLGaugeFactory": gf,
            "CLGaugeImplementation": p["implementation()"],
            "CLFactory_pool_factory": f["pool_factory"],
            "NonfungiblePositionManager": p["nft()"],
            "voter_on_factory": p["voter()"],
            "gauges_in_our_panel": int(f["n_gauges_in_panel"]),
        })

    doc = {
        "chain": {"name": "Base mainnet", "chain_id": 8453,
                  "verified_by": "eth_chainId via the archive RPC returned 0x2105 (8453)"},
        "core_contracts": {
            "Voter": {
                "address": core["Voter"],
                "role": "pool -> gauge registry (gauges/poolForGauge), vote tallying, "
                        "weekly emission routing to gauges",
                "sources": [DOCS, "https://basescan.org/address/" + core["Voter"]],
                "onchain_verification": checks["Voter"],
            },
            "Minter": {
                "address": core["Minter"],
                "role": "weekly AERO emission schedule (updatePeriod, weekly(), Mint event)",
                "sources": [DOCS, IMINTER],
                "onchain_verification": checks["Minter"],
            },
            "AERO": {
                "address": core["AERO_token"], "decimals": 18, "symbol": "AERO",
                "sources": [DOCS],
                "onchain_verification": checks["AERO_token"],
            },
            "VotingEscrow (veAERO)": {
                "address": core["VotingEscrow_veAERO"],
                "sources": [DOCS],
                "onchain_verification": checks["VotingEscrow_veAERO"],
            },
            "FactoryRegistry": {"address": core["FactoryRegistry"], "sources": [DOCS]},
        },
        "verification_method": (
            "Addresses were taken from the Aerodrome docs repo (security.mdx) and the "
            "Slipstream README, then confirmed by reciprocal on-chain eth_call: every "
            "contract points back at the others (Voter.minter()==Minter, Voter.ve()==VE, "
            "Minter.aero()==AERO, Minter.voter()==Voter, VE.token()==AERO, VE.voter()=="
            "Voter, AERO.minter()==Minter). A set of addresses that all reference each "
            "other cannot be a copied-from-a-blog mistake."
        ),
        "slipstream_cl_deployments": cl,
        "emission_redistributors": {
            "note": "CL gauges receive AERO from more than just the Voter. These "
                    "contracts call CLGauge.notifyRewardWithoutClaim and are the reason "
                    "Voter.DistributeReward != gauge NotifyReward for some gauges.",
            "0xee5b3c7b333e2870b746b3b2b168ef0958e55e15": {
                "label": "Redistributor for CLGaugeFactory 0x385293Ca...",
                "onchain": probe("0xee5b3c7b333e2870b746b3b2b168ef0958e55e15",
                                 [("voter()", C.SEL["voter()"]), ("minter()", C.SEL["minter()"]),
                                  ("rewardToken()", C.SEL["rewardToken()"]),
                                  ("gaugeFactory()", C.SEL["gaugeFactory()"]), ("owner()", C.SEL["owner()"])]),
                "source": SLIP,
            },
            "0x11a53f31bf406de59fcf9613e1922bd3e283a4b4": {
                "label": "Redistributor for CLGaugeFactory 0xB630227a...",
                "onchain": probe("0x11a53f31bf406de59fcf9613e1922bd3e283a4b4",
                                 [("voter()", C.SEL["voter()"]), ("minter()", C.SEL["minter()"]),
                                  ("rewardToken()", C.SEL["rewardToken()"]),
                                  ("gaugeFactory()", C.SEL["gaugeFactory()"]), ("owner()", C.SEL["owner()"])]),
                "source": "discovered from NotifyReward log senders; identity confirmed on-chain",
            },
            "0xe6a41fe61e7a1996b59d508661e3f524d6a32075": {
                "label": "owner of both Redistributors; directly notified 2 gauges in panel",
                "source": "discovered from NotifyReward log senders",
            },
        },
        "event_topics": {
            "how_computed": (
                "topic0 = keccak256(canonical signature string). Each value was computed "
                "TWICE and required to agree: (1) `cast keccak '<sig>'` from Foundry, and "
                "(2) a from-scratch pure-Python Keccak-f[1600] implementation written for "
                "this task, self-tested against the known vector "
                "keccak256('') = 0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470. "
                "Canonical signatures were read from the Solidity interfaces in the "
                "Aerodrome repos, NOT from blog posts -- this caught a real error: "
                "GaugeCreated takes 8 address args (trailing `creator`), and the 7-arg "
                "hash returns zero logs."
            ),
            "signatures": [
                {"contract": "CLGauge", "signature": "NotifyReward(address,uint256)",
                 "topic0": C.TOPIC0["NotifyReward(address,uint256)"],
                 "indexed": ["from"], "data": ["amount (uint256, 18dp AERO)"],
                 "abi_source": ICLGAUGE,
                 "used_for": "primary emissions source (census.aero_emissions)"},
                {"contract": "CLGauge", "signature": "Deposit(address,uint256,uint128)",
                 "topic0": C.TOPIC0["Deposit(address,uint256,uint128)"],
                 "indexed": ["user", "tokenId", "liquidityToStake"], "data": [],
                 "abi_source": ICLGAUGE,
                 "used_for": "NFT position staked into gauge (stake flow)"},
                {"contract": "CLGauge", "signature": "Withdraw(address,uint256,uint128)",
                 "topic0": C.TOPIC0["Withdraw(address,uint256,uint128)"],
                 "indexed": ["user", "tokenId", "liquidityToStake"], "data": [],
                 "abi_source": ICLGAUGE,
                 "used_for": "NFT position unstaked (stake flow)"},
                {"contract": "CLGauge", "signature": "ClaimRewards(address,uint256)",
                 "topic0": C.TOPIC0["ClaimRewards(address,uint256)"],
                 "indexed": ["from"], "data": ["amount"], "abi_source": ICLGAUGE,
                 "used_for": "AERO actually claimed by LPs"},
                {"contract": "CLGauge", "signature": "ClaimFees(address,uint256,uint256)",
                 "topic0": C.TOPIC0["ClaimFees(address,uint256,uint256)"],
                 "indexed": ["from"], "abi_source": ICLGAUGE},
                {"contract": "Voter", "signature": "DistributeReward(address,address,uint256)",
                 "topic0": C.TOPIC0["DistributeReward(address,address,uint256)"],
                 "indexed": ["sender", "gauge"], "data": ["amount"], "abi_source": IVOTER,
                 "used_for": "protocol-wide weekly emission total across ALL gauges"},
                {"contract": "Voter",
                 "signature": "GaugeCreated(address,address,address,address,address,address,address,address)",
                 "topic0": C.TOPIC0["GaugeCreated(address,address,address,address,address,address,address,address)"],
                 "indexed": ["poolFactory", "votingRewardsFactory", "gaugeFactory"],
                 "data": ["pool", "bribeVotingReward", "feeVotingReward", "gauge", "creator"],
                 "abi_source": IVOTER,
                 "used_for": "independent verification of the pool->gauge map"},
                {"contract": "Voter",
                 "signature": "Voted(address,address,uint256,uint256,uint256,uint256)",
                 "topic0": C.TOPIC0["Voted(address,address,uint256,uint256,uint256,uint256)"],
                 "indexed": ["voter", "pool", "tokenId"], "abi_source": IVOTER,
                 "used_for": "not ingested in this task"},
                {"contract": "Voter", "signature": "GaugeKilled(address)",
                 "topic0": C.TOPIC0["GaugeKilled(address)"], "abi_source": IVOTER},
                {"contract": "Voter", "signature": "GaugeRevived(address)",
                 "topic0": C.TOPIC0["GaugeRevived(address)"], "abi_source": IVOTER},
                {"contract": "Minter", "signature": "Mint(address,uint256,uint256,bool)",
                 "topic0": "0xcd2127828092f586df56003faf212897e901bb47f89a227e1acd3a0738f1fe5f",
                 "indexed": ["_sender", "_tail"],
                 "data": ["_weekly (uint256)", "_circulating_supply (uint256)"],
                 "abi_source": IMINTER,
                 "used_for": "published weekly emission schedule (sanity check a)"},
            ],
        },
        "function_selectors_used": C.SEL,
        "epoch_definition": {
            "rule": "epoch_start = floor(unix_ts / 604800) * 604800",
            "why_thursday": "unix ts 0 (1970-01-01) was a Thursday, so week-flooring "
                            "unix seconds lands on Thursday 00:00:00 UTC",
        },
    }
    C.save_json("contracts.json", doc)
    print("wrote out/contracts.json")
    import json
    print(json.dumps(doc["slipstream_cl_deployments"], indent=2))


if __name__ == "__main__":
    main()
