#!/usr/bin/env bash
# Disable history expansion so values containing "!" (e.g. OKX passphrase) do not
# trigger "bash: !0000: event not found" when exported/sourced with double quotes.
set +H
#
# OKX env (must all be `export` — Python only sees exported vars):
#   export OKX_API_KEY='...'
#   export OKX_SECRET_KEY='...'
#   export OKX_API_PASSPHRASE='...'   # use SINGLE quotes if value contains !
# Optional alias: OKX_PASSPHRASE (same value as OKX_API_PASSPHRASE).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "$ROOT/scripts/sprint_s002_base_social_market_data_scout.py" "$@"
