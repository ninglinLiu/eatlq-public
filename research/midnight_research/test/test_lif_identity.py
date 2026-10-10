#!/usr/bin/env python3
"""Lock the liquidation-incentive identity used in the report.

These asserts check arithmetic against numbers already read from receipts
and from one onchain totalUnits/withdrawable pair. They do not simulate a
liquidation and they do not count as captured profit.
"""
WAD = 10**18


def max_lif(lltv: int, cursor: int) -> int:
    inner = (cursor * (WAD - lltv)) // WAD
    return (WAD * WAD) // (WAD - inner)


def ramp_bonus_raw(repaid_raw: int, lif: int, elapsed_s: int) -> int:
    elapsed_s = min(elapsed_s, 3600)
    return repaid_raw * (lif - WAD) * elapsed_s // WAD // 3600


def main() -> None:
    lif98 = max_lif(980_000_000_000_000_000, 300_000_000_000_000_000)
    lif86 = max_lif(860_000_000_000_000_000, 300_000_000_000_000_000)
    assert lif98 == 1_006_036_217_303_822_937
    assert lif86 == 1_043_841_336_116_910_229

    # 23.62 minutes = 1417.2s. Use 1417s. Receipt cash was 946.433217 USDC.
    bonus = ramp_bonus_raw(398_259_832_936, lif98, 1417)
    assert abs(bonus - 946_433_217) < 2_000_000  # within $2 of the receipt

    # 7 seconds on the 180,476.259735 USDC repayment. Receipt cash 2.210428 USDC.
    bonus7 = ramp_bonus_raw(180_476_259_735, lif98, 7)
    assert abs(bonus7 - 2_210_428) < 200_000  # within $0.20

    # totalUnits = summed debt + withdrawable on the checked market.
    total_units = 1_112_582_904_662_105
    withdrawable = 995_981_889_769_698
    summed_debt = 116_601_014_892_407
    assert total_units - withdrawable == summed_debt
    print("ok")


if __name__ == "__main__":
    main()
