#!/usr/bin/env bash
# Keep one worker alive without any Claude turn: restart agent.py run shortly after it exits.
# Started detached by ensure.sh; stopped by stop.sh (SIGTERM stops the worker too).
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
LOG="$ROOT/.runtime/agent.out"
child=""
trap 'if [ -n "$child" ]; then kill -TERM "$child" 2>/dev/null; wait "$child" 2>/dev/null; fi; exit 0' TERM INT
while true; do
  started=$(date +%s)
  echo "$(date -u +%FT%TZ) supervise: starting worker" >> "$LOG"
  "$PY" "$ROOT/agent.py" run >> "$LOG" 2>&1 &
  child=$!
  wait "$child"
  code=$?
  child=""
  # A worker that dies within a minute is usually misconfigured; back off so the log stays readable.
  if [ $(( $(date +%s) - started )) -lt 60 ]; then delay=60; else delay=10; fi
  echo "$(date -u +%FT%TZ) supervise: worker exited with status $code; restarting in ${delay}s" >> "$LOG"
  sleep "$delay"
done
