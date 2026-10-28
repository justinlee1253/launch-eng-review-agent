#!/usr/bin/env bash
# Stop the detached worker gracefully (SIGTERM; the worker finishes its current step and releases its lock).
set -euo pipefail
# This interrupts an active review; drain before invoking it. It never enables a retired host.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
running() { pgrep -f "$ROOT/deploy/supervise.sh" >/dev/null 2>&1 || pgrep -f "$ROOT/agent.py run" >/dev/null 2>&1; }
if running; then
  pkill -TERM -f "$ROOT/deploy/supervise.sh" 2>/dev/null || true   # the supervisor forwards TERM to the worker
  pkill -TERM -f "$ROOT/agent.py run" 2>/dev/null || true
  for _ in $(seq 1 30); do running || { echo "stop: supervisor and worker stopped."; exit 0; }; sleep 1; done
  echo "stop: still running after 30s; check .runtime/activity.log" >&2; exit 1
fi
echo "stop: nothing running."
