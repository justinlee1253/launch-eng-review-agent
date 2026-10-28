#!/usr/bin/env bash
# Start only an existing, explicitly enrolled persistent runtime. Never reset its queue.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
fail() { echo "ensure: $*" >&2; exit 1; }
[ -n "${REVIEW_AGENT_RUNTIME:-}" ] || fail "Set REVIEW_AGENT_RUNTIME to the existing persistent runtime; ephemeral session storage is unsupported."
RUNTIME="$REVIEW_AGENT_RUNTIME"
python3 deployment.py verify --runtime "$RUNTIME" || fail "Restore or hand off the runtime before startup; no new activation or queue was created."

[ -x "$PY" ] || fail "Install the Python environment and requirements.lock before deployment."
"$PY" -c 'import google_auth_oauthlib, keyring, requests' 2>/dev/null || fail "Install requirements.lock in .venv."
if [ -n "${REVIEW_AGENT_GH_TOKEN:-}" ]; then export GH_TOKEN="$REVIEW_AGENT_GH_TOKEN" GITHUB_TOKEN="$REVIEW_AGENT_GH_TOKEN"; fi
login="$(gh api user --jq .login 2>/dev/null)" || fail "Existing GitHub authentication is unavailable."
actor_cfg="$("$PY" -c 'import json,os,pathlib; print(json.loads((pathlib.Path(os.environ["REVIEW_AGENT_RUNTIME"])/"config.json").read_text())["actor"])')"
[ "$actor_cfg" = "$login" ] || fail "Configured actor differs from the authenticated GitHub account; no worker was started."
gh api "repos/ROKT/launch-engineering/pulls?per_page=1&state=all" >/dev/null 2>&1 || fail "GitHub repository access is unavailable."

command -v codex >/dev/null 2>&1 || fail "Install a tested Codex CLI version before deployment."
if ! codex login status >/dev/null 2>&1; then
  [ -n "${REVIEW_AGENT_OPENAI_API_KEY:-}" ] || fail "Codex has no saved login or configured API key."
  printenv REVIEW_AGENT_OPENAI_API_KEY | codex login --with-api-key >/dev/null 2>&1 || fail "Codex API-key login failed."
fi
"$PY" -c 'import review; review.skill_text()' 2>/dev/null || fail "Falcon skill unavailable; configure REVIEW_AGENT_SKILL_FILE."
"$PY" auth.py check >/dev/null 2>&1 || fail "Google grant unavailable or paused; repair it explicitly."

started=0
if ! pgrep -f "$ROOT/deploy/supervise.sh" >/dev/null 2>&1; then
  command -v setsid >/dev/null 2>&1 || fail "Detached supervision requires Linux; use the existing LaunchAgent on macOS."
  setsid nohup bash "$ROOT/deploy/supervise.sh" >/dev/null 2>&1 < /dev/null &
  started=1
fi
# A running PID is insufficient: successful intake must have produced a fresh heartbeat.
if [ "$started" = 1 ]; then
  for _ in $(seq 1 30); do
    "$PY" deployment.py health --runtime "$RUNTIME" --actor "$login" >/dev/null 2>&1 && break
    sleep 1
  done
fi
"$PY" deployment.py health --runtime "$RUNTIME" --actor "$login"
