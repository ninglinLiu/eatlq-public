#!/bin/bash
ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
read -r _ u1 n1 s1 i1 w1 irq1 sirq1 st1 _ < /proc/stat
sleep 1
read -r _ u2 n2 s2 i2 w2 irq2 sirq2 st2 _ < /proc/stat
total=$(( (u2+n2+s2+i2+w2+irq2+sirq2+st2)-(u1+n1+s1+i1+w1+irq1+sirq1+st1) ))
idle=$((i2-i1)); iow=$((w2-w1))
idle_pct=$(awk -v i=$idle -v t=$total 'BEGIN{printf "%.2f",(t>0?100*i/t:0)}')
iow_pct=$(awk -v i=$iow -v t=$total 'BEGIN{printf "%.2f",(t>0?100*i/t:0)}')
swap=$(free -b | awk '/Swap:/{print $3}')
ex=$(docker inspect -f '{{.State.Status}}|{{.RestartCount}}' eatlq-executor-1 2>/dev/null)
sb=$(docker inspect -f '{{.State.Status}}|{{.RestartCount}}' eatlq-signal-bridge-1 2>/dev/null)
echo "$ts idle=$idle_pct iowait=$iow_pct swap=$swap ex=$ex sb=$sb"
