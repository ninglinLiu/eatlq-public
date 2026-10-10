# Clanker Quote-Path v0.2.1 Offline Test Result

Run date (UTC): 2026-07-17

Source implementation commit: `9c10f2f946741e263c3f8251778007761fc715f9`

Result: **OK — 133/133 tests**

Command class: single-process, low-priority, socket-blocked `unittest discover`.

- interpreter: `/usr/bin/python3` (CPython 3.12);
- dependency source: system-installed `eth_abi 5.2.0` and `eth_hash`;
- network guard: `tests/offline_guard/sitecustomize.py`;
- maximum RSS: 46,380 KiB;
- socket messages sent/received: 0/0;
- exit status: 0;
- new v0.2.1 tests: 10/10.

The original `runtime/venv` remains intentionally stdlib-only and cannot import
the Clanker modules introduced after the 79a7060 baseline because those modules
depend on `eth_abi` and `eth_hash`. It was not mutated and its older 117-test
result is not presented as this round's validation. Dependency isolation and
locking remain a correction item before this calibration becomes a reusable
production-adjacent service.

The tests exercised no public RPC, production database, Redis, Docker, wallet,
signing, order, or backfill path.
