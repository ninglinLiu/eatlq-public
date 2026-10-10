# Launchpad Intelligence OS v0.1

This directory is an isolated, offline-first implementation of the launchpad
registry, normalization, attribution, lifecycle, quality, research-panel and
human-review contracts described in `发射台任务.md`.

Safety defaults are deliberate:

- no network client and no credential loader;
- no private-key, signing, approval, swap or live-execution path;
- only local JSON/JSONL input and SQLite output;
- outputs must remain under `launchpad_intelligence/var/`;
- one worker, bounded input/batch/disk sizes, bounded retries and timeouts;
- synthetic fixtures are labelled `SYNTHETIC` and never promoted as on-chain facts.

Run the offline verification suite:

```bash
cd /root/quant_research_os/web
PYTHONPATH=launchpad_intelligence/src \
  python3 -m unittest discover -s launchpad_intelligence/tests -v
PYTHONPATH=launchpad_intelligence/src \
  python3 -m launchpad_intelligence.cli verify
```

Build an isolated demonstration snapshot from the bundled synthetic Golden
fixtures:

```bash
PYTHONPATH=launchpad_intelligence/src \
  python3 -m launchpad_intelligence.cli demo \
  --output-dir launchpad_intelligence/var/demo
```

This demo does **not** claim real-chain coverage or Golden validation. Public
mainnet discovery, historical backfill and real Golden transaction selection
remain gated until an explicitly approved, isolated data source is supplied.

