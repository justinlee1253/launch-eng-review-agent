#!/usr/bin/env bash
# Idempotent: bring the review agent up inside this container, or print one line saying why it cannot.
# Reads only environment secrets; never prints token values.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
RUNTIME="$ROOT/.runtime"
PY="$ROOT/.venv/bin/python"
fail() { echo "ensure: $*" >&2; exit 1; }

# 1. Python dependencies.
[ -x "$PY" ] || python3 -m venv .venv
"$PY" -c 'import google_auth_oauthlib, keyring, requests' 2>/dev/null || "$PY" -m pip install -q -r requirements.lock

# 2. GitHub identity. Inside a Claude Code cloud session the proxy authenticates api.github.com as the session
#    owner's connected GitHub account and ignores any token the process sends, so reviews are posted as that
#    account. REVIEW_AGENT_GH_TOKEN only matters on hosts without that proxy.
if [ -n "${REVIEW_AGENT_GH_TOKEN:-}" ]; then export GH_TOKEN="$REVIEW_AGENT_GH_TOKEN" GITHUB_TOKEN="$REVIEW_AGENT_GH_TOKEN"; fi
login="$(gh api user --jq .login 2>/dev/null || true)"
[ -n "$login" ] || fail "GitHub API is not reachable or not authenticated (gh api user failed)."
gh api "repos/ROKT/launch-engineering/pulls?per_page=1&state=all" >/dev/null 2>&1 \
  || fail "GitHub account '$login' cannot read ROKT/launch-engineering; give this session access to that repository."

# 3. Codex CLI and its login (API key, read from the environment, never echoed).
command -v codex >/dev/null 2>&1 || npm install -g @openai/codex >/dev/null 2>&1 || fail "could not install @openai/codex (network policy?)."
if ! codex login status >/dev/null 2>&1; then
  [ -n "${REVIEW_AGENT_OPENAI_API_KEY:-}" ] || fail "Codex is not logged in and REVIEW_AGENT_OPENAI_API_KEY is not set."
  printenv REVIEW_AGENT_OPENAI_API_KEY | codex login --with-api-key >/dev/null 2>&1 || fail "codex login --with-api-key failed."
fi

# 4. Falcon skill: repository copy, or REVIEW_AGENT_SKILL_FILE.
"$PY" -c 'import review; review.skill_text()' 2>/dev/null \
  || fail "Falcon skill missing: commit skills/avengers-falcon/SKILL.md or set REVIEW_AGENT_SKILL_FILE."

# 5. Google grant: seeded from GOOGLE_CHAT_GRANT_JSON into .runtime/google-grant.json on first use, then renewed there.
"$PY" auth.py check --force-refresh >/dev/null 2>&1 \
  || fail "Google grant unavailable or paused: $("$PY" auth.py status 2>/dev/null | tr -d '\n' | cut -c1-160). See deploy/CLOUD.md."

# 6. One-time configuration for this container (activation time = now; older Chat messages are ignored).
if [ ! -f "$RUNTIME/config.json" ]; then
  [ -n "${REVIEW_AGENT_SPACE:-}" ] || fail "REVIEW_AGENT_SPACE is not set (spaces/ID or the Chat space URL)."
  actor="${REVIEW_AGENT_ACTOR:-$login}"
  self_user="${REVIEW_AGENT_SELF_USER:-}"
  [ -n "$self_user" ] || self_user="$("$PY" auth.py whoami 2>/dev/null)" \
    || fail "could not resolve the Chat user id; set REVIEW_AGENT_SELF_USER or re-authorize with the identity scope."
  args=(configure --space "$REVIEW_AGENT_SPACE" --actor "$actor" --self-user "$self_user")
  IFS=',' read -ra extra <<< "${REVIEW_AGENT_OPERATORS:-}"
  for op in "${extra[@]}"; do [ -n "$op" ] && args+=(--operator "$op"); done
  "$PY" agent.py "${args[@]}" >/dev/null || fail "agent.py configure failed."
fi
actor_cfg="$("$PY" -c 'import json; print(json.load(open(".runtime/config.json"))["actor"])')"
[ "$actor_cfg" = "$login" ] \
  || echo "ensure: warning: configured actor '$actor_cfg' differs from GitHub login '$login'; every review will be held." >&2
if [ ! -f "$RUNTIME/stamp-permissions.json" ] && [ -n "${REVIEW_AGENT_STAMP_EMAILS:-}" ]; then
  REVIEW_AGENT_STAMP_EMAILS="$REVIEW_AGENT_STAMP_EMAILS" "$PY" - <<'PYEOF'
import json, os, pathlib
emails = [e.strip() for e in os.environ['REVIEW_AGENT_STAMP_EMAILS'].split(',') if e.strip()]
path = pathlib.Path('.runtime/stamp-permissions.json')
path.write_text(json.dumps({'allowed_emails': emails}, indent=2) + '\n')
path.chmod(0o600)
PYEOF
fi

# 7. Start the worker detached if it is not already running (the worker also holds a process lock).
if ! pgrep -f "$ROOT/agent.py run" >/dev/null 2>&1; then
  setsid nohup "$PY" "$ROOT/agent.py" run >> "$RUNTIME/agent.out" 2>&1 < /dev/null &
  sleep 4
  pgrep -f "$ROOT/agent.py run" >/dev/null 2>&1 || { tail -n 5 "$RUNTIME/agent.out" >&2; fail "worker exited at startup."; }
  echo "ensure: worker started as GitHub '$login'."
else
  echo "ensure: worker already running as GitHub '$login'."
fi

# 8. Health summary (non-secret).
"$PY" agent.py status | "$PY" -c '
import json, sys
s = json.load(sys.stdin)
print("space:", s["space"], "| heartbeat:", s["heartbeat"], "| google_auth:", s["google_auth"].get("status"),
      "| chat_blocked:", s["chat_blocked"], "| jobs:", s["jobs"], "| delivery:", s["delivery"])
if s["chat_blocked"]:
    print("Chat access is paused; after repairing the grant run: .venv/bin/python agent.py check-chat --resume")
'
