# Running the agent in a Claude Code cloud session

The original runtime is a macOS LaunchAgent. This document describes the supported alternative: the same code running as a detached background process inside one long-lived Claude Code cloud session, with credentials supplied as environment secrets. Nothing here changes what the agent reviews or how it decides.

## How it works

- `deploy/ensure.sh` is idempotent. It installs dependencies, logs the Codex CLI in with an API key, seeds the Google grant and the configuration from environment secrets, starts `agent.py run` detached if it is not running, and prints a one-line health summary.
- The session's `CLAUDE.md` tells Claude to run that script when asked to deploy or check, and to keep an hourly Routine bound to the session so the container is not reclaimed for inactivity. If the container is ever replaced, the next Routine firing re-runs the script and the agent comes back with a fresh activation time.
- What survives a container replacement: everything in environment secrets and the repository. What does not: `.runtime/` (queue, ledger, logs). Same-head deduplication still holds because the worker checks existing GitHub reviews before spending model tokens; requests posted while the agent was down are not replayed.

## One-time: export a Google grant on a machine with a browser

Google consent needs a browser on the same machine, so do this once on a laptop. The grant file holds the OAuth client plus the refresh token; treat it as a password.

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.lock
```

```bash
REVIEW_AGENT_GRANT_FILE=~/le-review-grant.json REVIEW_AGENT_GOOGLE_ACCOUNT=you@rokt.com \
  .venv/bin/python auth.py authorize --client-file ~/Downloads/client_secret.json
```

```bash
REVIEW_AGENT_GRANT_FILE=~/le-review-grant.json .venv/bin/python auth.py check --force-refresh
```

```bash
REVIEW_AGENT_GRANT_FILE=~/le-review-grant.json .venv/bin/python auth.py whoami
```

`whoami` prints the Chat user resource (`users/…`) of the account you chose; that becomes `REVIEW_AGENT_SELF_USER`. Then paste the contents of `~/le-review-grant.json` into the `GOOGLE_CHAT_GRANT_JSON` secret and delete the file. The Desktop OAuth client must belong to Google Cloud project `le-orbit-review`; if yours is in another project, set `REVIEW_AGENT_GOOGLE_PROJECT` both when exporting and in the cloud environment.

## Cloud environment settings

Open the cloud environment menu in the session title bar, then Edit.

**Secrets / environment variables**

| Name | Required | Value |
| --- | --- | --- |
| `GOOGLE_CHAT_GRANT_JSON` | yes | Contents of the exported grant file. |
| `REVIEW_AGENT_OPENAI_API_KEY` | yes | API key whose organization can use the model pinned in `usage.py`. |
| `REVIEW_AGENT_SPACE` | yes | `spaces/ID` or the Chat space URL. |
| `REVIEW_AGENT_SELF_USER` | yes | `users/…` from `auth.py whoami`. |
| `REVIEW_AGENT_ACTOR` | no | GitHub login that submits reviews. Defaults to the session's GitHub login (see below); a different value holds every review. |
| `REVIEW_AGENT_GH_TOKEN` | no | Only for hosts outside a Claude Code cloud session, where no proxy supplies GitHub credentials. |
| `REVIEW_AGENT_OPERATORS` | no | Comma-separated extra GitHub logins treated as the operator (own PRs skipped, own manual reviews respected). |
| `REVIEW_AGENT_GOOGLE_ACCOUNT` | no | Google account used for login hints. |
| `REVIEW_AGENT_GOOGLE_PROJECT` | no | OAuth client's Google Cloud project, if not `le-orbit-review`. |
| `REVIEW_AGENT_STAMP_EMAILS` | no | Comma-separated STAMP allowlist, written to `.runtime/stamp-permissions.json` once per container. |
| `REVIEW_AGENT_SKILL_FILE` | no | Path to the Falcon `SKILL.md` if it is not committed at `skills/avengers-falcon/SKILL.md`. |

**GitHub identity**: inside a cloud session the proxy authenticates every `api.github.com` call as the GitHub account connected to the session owner's Claude account, and ignores tokens the process sends. Reviews are therefore posted as that account, and that account needs read access to `ROKT/launch-engineering` and permission to submit reviews there. The proxy only authenticates calls for repositories attached to the session, so the deployment session must attach `ROKT/launch-engineering` with push access (Claude's `add_repo` tool with access `push`, which you may be asked to approve). If that attach is refused, an organization owner has to enable the repository for Claude in the GitHub settings the refusal points to.

**Network access**: the default policy blocks OpenAI. Use Limited access with these domains allowed, keeping package managers allowed: `api.openai.com`, `chatgpt.com`, `auth.openai.com`, `api.github.com`, `chat.googleapis.com`, `oauth2.googleapis.com`, `openidconnect.googleapis.com`.

**Falcon skill**: the review refuses to run without it. Commit it at `skills/avengers-falcon/SKILL.md` in the deployment repository (it is not in the public source).

## Recovery timing

| Event | Detected and recovered by | Typical delay |
| --- | --- | --- |
| New request in the Chat space | The worker's own 2-second poll | 2 s |
| Worker process crash | `deploy/supervise.sh`, started by `ensure.sh` | 10 s (60 s if it died within its first minute, which points at configuration) |
| Container replaced or process tree lost | The hourly Routine, or a `/loop 15m` check started in the session | up to 15 min with the loop, up to 1 h without |

Routines cannot fire more often than hourly by default. `/loop` can go as low as one minute but each tick is a model turn and it only exists while the session does, so 15 minutes is the sensible setting.

## Start

Start a new cloud session on the deployment repository after the settings above are saved, and ask Claude to deploy. It attaches `ROKT/launch-engineering` with push access, runs `bash deploy/ensure.sh`, then creates the hourly Routine. Verify with a real request in the Chat space: a 👀 reaction and a threaded acknowledgment, then a GitHub review under the configured account.

## Operate

| Need | Command |
| --- | --- |
| Health | `bash deploy/ensure.sh` (safe to repeat) or `.venv/bin/python agent.py status` |
| Outcomes | `.venv/bin/python agent.py history` |
| Token usage | `.venv/bin/python agent.py costs` |
| Stop | `bash deploy/stop.sh` |
| Logs | `.runtime/activity.log`, `.runtime/agent.out` |
| Resume Chat after a grant repair | `.venv/bin/python agent.py check-chat --resume` |

A paused Google grant (`auth.py status` shows `blocked`) means the refresh token was revoked or the secret is stale: export a new grant, update the secret, delete `.runtime/google-grant.json`, and re-run `ensure.sh`.
