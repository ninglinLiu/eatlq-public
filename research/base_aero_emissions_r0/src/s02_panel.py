"""Resolve panel block bounds and the epoch -> block grid via binary search on the archive RPC."""

import sys

sys.path.insert(0, "/root/base_aero_emissions_r0/src")

import common as C
import rpc as R


def main():
    head = int(R.rpc("eth_blockNumber", []), 16)
    b_from = R.block_at_timestamp(C.PANEL_TS_FROM, 1, head)
    b_to = R.block_at_timestamp(C.PANEL_TS_TO, 1, head)
    print(f"head={head}")
    print(f"panel from block {b_from} ({R.iso(R.block_timestamp(b_from))})")
    print(f"panel to   block {b_to} ({R.iso(R.block_timestamp(b_to))})")

    epochs = R.epochs_between(C.PANEL_TS_FROM, C.PANEL_TS_TO)
    print(f"epochs in panel: {len(epochs)}  first={R.iso(epochs[0])} last={R.iso(epochs[-1])}")

    grid = []
    for e in epochs:
        # block at the epoch flip, and block just before the epoch ends
        b_start = R.block_at_timestamp(e, 1, head)
        b_end = R.block_at_timestamp(min(e + R.WEEK - 1, C.PANEL_TS_TO), 1, head)
        wd = R.datetime.fromtimestamp(e, tz=R.timezone.utc).strftime("%A %H:%M:%S")
        grid.append({"epoch_start": e, "epoch_start_iso": R.iso(e), "weekday": wd,
                     "block_start": b_start, "block_start_ts": R.block_timestamp(b_start),
                     "block_end": b_end, "block_end_ts": R.block_timestamp(b_end)})
        print(f"  {R.iso(e)}  {wd:<18} blocks {b_start}..{b_end}")

    C.save_json("panel.json", {
        "head": head, "block_from": b_from, "block_to": b_to,
        "ts_from": C.PANEL_TS_FROM, "ts_to": C.PANEL_TS_TO,
        "ts_from_iso": R.iso(C.PANEL_TS_FROM), "ts_to_iso": R.iso(C.PANEL_TS_TO),
        "epochs": grid,
    })
    weekdays = {g["weekday"].split()[0] for g in grid}
    print("distinct weekdays of epoch starts:", weekdays)
    print("rpc stats:", R.STATS)


if __name__ == "__main__":
    main()
