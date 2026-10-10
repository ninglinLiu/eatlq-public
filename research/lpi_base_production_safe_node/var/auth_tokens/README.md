# Capability token directory

Default: empty / unauthorized.

To authorize a capability (rarely, and only after owner/Codex approval):

1. Create a non-symlink file named `<capability>.authorized`
2. Contents must be exactly `AUTHORIZED:<capability>`
3. Also pass the matching CLI `--authorize-...` flag

Ambient `ENABLE_*` / `ALLOW_*` environment variables are ignored by design.
