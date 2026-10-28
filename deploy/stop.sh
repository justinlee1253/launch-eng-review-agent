#!/usr/bin/env bash
# Stop the detached worker gracefully (SIGTERM; the worker finishes its current step and releases its lock).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if pgrep -f "$ROOT/agent.py run" >/dev/null 2>&1; then
  pkill -TERM -f "$ROOT/agent.py run"
  for _ in $(seq 1 30); do pgrep -f "$ROOT/agent.py run" >/dev/null 2>&1 || { echo "stop: worker stopped."; exit 0; }; sleep 1; done
  echo "stop: worker still running after 30s; check .runtime/activity.log" >&2; exit 1
fi
echo "stop: no worker running."
