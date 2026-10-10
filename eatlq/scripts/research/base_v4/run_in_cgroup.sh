#!/usr/bin/env bash
# Run research scripts under resource limits (never touch live executor cgroup).
set -euo pipefail

MEM_TOTAL_MB=$(free -m | awk '/^Mem:/{print $2}')
MEM_LIMIT_MB=$(( MEM_TOTAL_MB * 30 / 100 ))
if (( MEM_LIMIT_MB > 2048 )); then MEM_LIMIT_MB=2048; fi

CGROUP="/sys/fs/cgroup/v4_research"
CPU_CORES=$(nproc)
CPU_QUOTA=$(( CPU_CORES * 50000 ))  # 50% of all cores

setup_cgroup() {
  if [[ ! -d "$CGROUP" ]]; then
    sudo mkdir -p "$CGROUP" 2>/dev/null || mkdir -p "$CGROUP" 2>/dev/null || true
  fi
  if [[ -w "$CGROUP/memory.max" ]]; then
    echo "${MEM_LIMIT_MB}M" > "$CGROUP/memory.max"
  fi
  if [[ -w "$CGROUP/cpu.max" ]]; then
    echo "${CPU_QUOTA} 100000" > "$CGROUP/cpu.max"
  fi
}

setup_cgroup

echo "[cgroup] mem.max=${MEM_LIMIT_MB}M (total=${MEM_TOTAL_MB}M) cpu.max=${CPU_QUOTA}/100000 (~50%)"

# nice 19 + optional cpulimit; fork-replay scripts should call live_gate first
exec nice -n 19 "$@"
