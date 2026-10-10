#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Backfilling normalized event tables via temporary-table rebuild swaps..."
bash "${SCRIPT_DIR}/rebuild-normalized-surface.sh" all
echo "Normalized event backfill finished."
