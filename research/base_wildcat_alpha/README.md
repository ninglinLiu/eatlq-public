# Base Wildcat Alpha

Independent, greenfield research workspace for the mandate in
`/root/BASE_WILDCAT_ALPHA_GREENFIELD_v1.md`.

The workspace consumes historical facts through read-only ClickHouse queries.
It contains no wallet, signer, transaction builder, executor, or order API.

## Reproduce

```bash
cd /root/base_wildcat_alpha
python3 src/run_all.py
python3 -m unittest discover -s tests -v
```

All Python entry points require `NO_LIVE.lock`. The query layer accepts only a
single `SELECT` statement and rejects mutation keywords, external table
functions, and secret-like environment access.

LIVE_TRADING = FORBIDDEN

