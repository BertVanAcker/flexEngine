#!/bin/sh
# Stand-in for a generated flexNode: serves /healthz on :8080, emits a heartbeat line every second.
# FLEX_FAIL_HEALTH=1 simulates a release whose readiness never comes up (scenario d).
# Deployment parameters arrive as FLEX_PARAM_* env vars:
#   FLEX_PARAM_STARTUP_DELAY=<s>  readiness comes up after <s> seconds (default 0.3)
#   FLEX_PARAM_CRASH_AFTER=<s>    exit with an error after <s> seconds (supervision test)
set -u
term() { echo "[$FLEX_APP $FLEX_VERSION] SIGTERM, stopping"; kill "$NC" 2>/dev/null; exit 0; }
trap term TERM INT
echo "[$FLEX_APP $FLEX_VERSION] starting"
nc -lk -p 8080 -e /app/health.sh &
NC=$!
sleep "${FLEX_PARAM_STARTUP_DELAY:-0.3}"
if [ "${FLEX_FAIL_HEALTH:-0}" = "1" ]; then
  echo "[$FLEX_APP $FLEX_VERSION] model load failed (simulated): readiness stays down"
else
  touch /tmp/healthy
fi
i=0
while true; do
  i=$((i+1))
  if [ -n "${FLEX_PARAM_CRASH_AFTER:-}" ] && [ "$i" -ge "${FLEX_PARAM_CRASH_AFTER}" ]; then
    echo "[$FLEX_APP $FLEX_VERSION] crashing (simulated)"; exit 3
  fi
  sleep 1 & wait $!
done
