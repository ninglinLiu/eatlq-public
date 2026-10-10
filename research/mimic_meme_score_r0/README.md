# Mimic meme-score R0

Read-only charter + 22-event point-in-time pilot.

- **Source of truth:** [`GOAL.md`](GOAL.md)
- **Locked universe:** [`events/EVENT_UNIVERSE.md`](events/EVENT_UNIVERSE.md)
- **Instrument notes:** [`reports/R0_INSTRUMENT.md`](reports/R0_INSTRUMENT.md)
- **Pilot report:** [`reports/R0_PILOT.md`](reports/R0_PILOT.md)
- **R1 clocks:** [`GOAL_R1.md`](GOAL_R1.md) · [`reports/R1_PILOT.md`](reports/R1_PILOT.md)
- **Live trading:** forbidden while [`NO_LIVE.lock`](NO_LIVE.lock) exists

```bash
python3 src/no_live.py
python3 src/collect_search.py --smoke
python3 src/collect_search.py
python3 src/score_and_label.py
```

`contracts_all.csv` is the clone table. `contracts.csv` is the earliest-10 score set. Attach is computed on the primary chain at collect time, not on the score set.
