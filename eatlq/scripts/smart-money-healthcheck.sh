#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_DIR="${REPO_ROOT}/infra/compose"

if [[ ! -f "${COMPOSE_DIR}/.env" ]]; then
  echo "Missing ${COMPOSE_DIR}/.env"
  echo "Copy infra/compose/.env.example first and fill real values."
  exit 1
fi

MIN_SMART_MONEY_COUNT="${1:-20}"
MAX_SCORER_SUCCESS_AGE_H="${2:-8}"
MAX_SM_SIGNAL_AGE_H="${3:-24}"
BLACKLIST_FILE="${4:-${REPO_ROOT}/infra/config/mev_blacklist.yaml}"

smart_money_count="$(
  docker compose \
    --env-file "${COMPOSE_DIR}/.env" \
    -f "${COMPOSE_DIR}/compose.yaml" \
    exec -T redis sh -lc 'redis-cli --raw ZCARD wallet:smart_money'
)"
smart_money_count="${smart_money_count:-0}"

scorer_logs="$(
  docker compose \
    --env-file "${COMPOSE_DIR}/.env" \
    -f "${COMPOSE_DIR}/compose.yaml" \
    logs --tail=3000 wallet-scorer
)"

latest_scorer_success_iso="$(
  SCORER_LOGS="${scorer_logs}" python3 - <<'PY'
import os

logs = os.environ.get("SCORER_LOGS", "")
latest = ""
for line in logs.splitlines():
    if "wallet scores updated" not in line:
        continue
    if "| " not in line:
        continue
    after_pipe = line.split("| ", 1)[1].strip()
    if " " not in after_pipe:
        continue
    ts = after_pipe.split(" ", 1)[0]
    if ts > latest:
        latest = ts
print(latest)
PY
)"

latest_scorer_success_age_h="$(
  LATEST_SCORER_SUCCESS_ISO="${latest_scorer_success_iso}" python3 - <<'PY'
import datetime
import os

iso = os.environ.get("LATEST_SCORER_SUCCESS_ISO", "").strip()
if not iso:
    print("999999")
    raise SystemExit(0)

try:
    dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
except Exception:
    print("999999")
    raise SystemExit(0)

now = datetime.datetime.now(datetime.timezone.utc)
age_h = (now - dt).total_seconds() / 3600.0
print(f"{age_h:.2f}")
PY
)"

signals_raw="$(
  docker compose \
    --env-file "${COMPOSE_DIR}/.env" \
    -f "${COMPOSE_DIR}/compose.yaml" \
    exec -T redis sh -lc 'redis-cli --raw XREVRANGE signals + - COUNT 1000'
)"

latest_sm_signal_ts_ms="$(
  SIGNALS_RAW="${signals_raw}" python3 - <<'PY'
import json
import os

raw = os.environ.get("SIGNALS_RAW", "")
latest = 0
for line in raw.splitlines():
    line = line.strip()
    if not line or not line.startswith("{"):
        continue
    try:
        obj = json.loads(line)
    except Exception:
        continue
    if obj.get("signal_source") != "smart_money":
        continue
    ts = obj.get("timestamp")
    if isinstance(ts, int) and ts > latest:
        latest = ts
print(latest)
PY
)"

latest_sm_signal_age_h="$(
  LATEST_SM_SIGNAL_TS_MS="${latest_sm_signal_ts_ms}" python3 - <<'PY'
import datetime
import os

raw = os.environ.get("LATEST_SM_SIGNAL_TS_MS", "0").strip()
try:
    ts_ms = int(raw)
except Exception:
    ts_ms = 0

if ts_ms <= 0:
    print("999999")
    raise SystemExit(0)

ts = datetime.datetime.fromtimestamp(ts_ms / 1000.0, datetime.timezone.utc)
now = datetime.datetime.now(datetime.timezone.utc)
age_h = (now - ts).total_seconds() / 3600.0
print(f"{age_h:.2f}")
PY
)"

latest_sm_signal_iso="$(
  LATEST_SM_SIGNAL_TS_MS="${latest_sm_signal_ts_ms}" python3 - <<'PY'
import datetime
import os

raw = os.environ.get("LATEST_SM_SIGNAL_TS_MS", "0").strip()
try:
    ts_ms = int(raw)
except Exception:
    ts_ms = 0

if ts_ms <= 0:
    print("N/A")
    raise SystemExit(0)

ts = datetime.datetime.fromtimestamp(ts_ms / 1000.0, datetime.timezone.utc)
print(ts.isoformat().replace("+00:00", "Z"))
PY
)"

sm_members="$(
  docker compose \
    --env-file "${COMPOSE_DIR}/.env" \
    -f "${COMPOSE_DIR}/compose.yaml" \
    exec -T redis sh -lc 'redis-cli --raw ZRANGE wallet:smart_money 0 -1'
)"

blacklist_intersections="$(
  BLACKLIST_FILE="${BLACKLIST_FILE}" CHAIN_ID_FROM_ENV="$(awk -F= '/^CHAIN_ID=/{print $2}' "${COMPOSE_DIR}/.env" | tail -n1)" \
  SM_MEMBERS="${sm_members}" python3 - <<'PY'
import json
import os

path = os.environ.get("BLACKLIST_FILE", "")
chain_id = (os.environ.get("CHAIN_ID_FROM_ENV", "8453") or "8453").strip()
sm = {line.strip().lower() for line in os.environ.get("SM_MEMBERS", "").splitlines() if line.strip().startswith("0x")}

try:
    payload = json.loads(open(path, "r", encoding="utf-8").read())
except Exception:
    payload = {}

chain_payload = payload.get(chain_id) or payload.get(int(chain_id) if chain_id.isdigit() else chain_id) or {}
bl = set()
if isinstance(chain_payload, dict):
    for rows in chain_payload.values():
        if isinstance(rows, list):
            for addr in rows:
                if isinstance(addr, str) and addr.lower().startswith("0x"):
                    bl.add(addr.lower().strip())
sm_hit = len(sm & bl)
print(f"{sm_hit},{len(bl)}")
PY
)"

sm_blacklist_hits="$(echo "${blacklist_intersections}" | cut -d, -f1)"
blacklist_size="$(echo "${blacklist_intersections}" | cut -d, -f2)"
sm_blacklist_hits="${sm_blacklist_hits:-0}"
blacklist_size="${blacklist_size:-0}"

verdict="PASS"
if (( smart_money_count < MIN_SMART_MONEY_COUNT )); then
  verdict="BLOCKED"
fi

if ! python3 - "${latest_scorer_success_age_h}" "${MAX_SCORER_SUCCESS_AGE_H}" <<'PY'
import sys
age=float(sys.argv[1]); limit=float(sys.argv[2]); sys.exit(0 if age <= limit else 1)
PY
then
  verdict="BLOCKED"
fi

if ! python3 - "${latest_sm_signal_age_h}" "${MAX_SM_SIGNAL_AGE_H}" <<'PY'
import sys
age=float(sys.argv[1]); limit=float(sys.argv[2]); sys.exit(0 if age <= limit else 1)
PY
then
  verdict="BLOCKED"
fi

if (( sm_blacklist_hits > 0 )); then
  verdict="BLOCKED"
fi

echo "# Smart Money Healthcheck"
echo "min_smart_money_count=${MIN_SMART_MONEY_COUNT} max_scorer_success_age_h=${MAX_SCORER_SUCCESS_AGE_H} max_sm_signal_age_h=${MAX_SM_SIGNAL_AGE_H}"
echo
echo "smart_money_count=${smart_money_count}"
echo "latest_scorer_success_at=${latest_scorer_success_iso:-N/A}"
echo "latest_scorer_success_age_h=${latest_scorer_success_age_h}"
echo "latest_sm_signal_at=${latest_sm_signal_iso}"
echo "latest_sm_signal_age_h=${latest_sm_signal_age_h}"
echo "mev_blacklist_size=${blacklist_size}"
echo "smart_money_blacklist_hits=${sm_blacklist_hits}"
echo
echo "verdict=${verdict}"
