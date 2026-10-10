#!/usr/bin/env bash
# Install dual-track validation cron entries (engineering + readout + track B bridge).
set -euo pipefail
REPO="/opt/EATLQ"
TMP="$(mktemp)"
crontab -l 2>/dev/null | grep -v "sprint086-engineering" | grep -v "dual-track-readout" | grep -v "track_b_bridge" > "$TMP" || true

cat >> "$TMP" <<'CRON'
# Dual-track validation (engineering shadow — NOT SOP promotion)
4-59/5 * * * * cd /opt/EATLQ && python3 scripts/sprint086_engineering_shadow_harness.py --once >> logs/sprint086/harness_cron.log 2>&1 # sprint086-engineering
*/5 * * * * /opt/EATLQ/scripts/run_track_b_fee500.sh bridge-once >> /opt/EATLQ/logs/pipeline/track_b_bridge.log 2>&1 # track_b_bridge
*/10 * * * * cd /opt/EATLQ && python3 scripts/dual_track_alpha_readout.py >> logs/pipeline/dual_track_readout.log 2>&1 # dual-track-readout
CRON

# Replace sprint069 cron with script-internal flock (no double-lock)
sed -i '/sprint069_forward_dryrun.py/d' "$TMP"
echo '1-56/5 * * * * cd /opt/EATLQ && python3 scripts/sprint069_forward_dryrun.py --once --quiet >> logs/sprint069_forward_dryrun.log 2>&1' >> "$TMP"

crontab "$TMP"
rm -f "$TMP"
mkdir -p "$REPO/logs/sprint086" "$REPO/logs/pipeline"
echo "Dual-track cron installed."
crontab -l | grep -E "sprint086|dual-track|track_b|sprint069_forward"
