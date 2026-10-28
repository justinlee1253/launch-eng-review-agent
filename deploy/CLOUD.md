# Persistent-host deployment and offline handoff

Codex remains the review engine, with the same model, Falcon criteria, repository scope and per-attempt shared twenty-minute budget. Claude can help operate it, but **an ordinary ephemeral Claude Code cloud session is not a supported production daemon host**. A shell supervisor or scheduled model turn cannot preserve SQLite state or establish ownership across replacement containers.

Use the existing macOS deployment or one persistent Linux host with local persistent storage, stable host identity and existing GitHub/Google access. No additional service or dependency is required. Do not run the Mac and a cloud listener for the same Chat space at the same time. SQLite/WAL storage is local to the active host; do not mount one runtime concurrently over a network filesystem.

## Runtime contract

Set `REVIEW_AGENT_RUNTIME` to the absolute existing persistent state directory, for example `/srv/launch-eng-review-agent/state`. The worker, authentication manager and dashboard use that same directory. It contains the original `config.json` (including activation time and `worker_count`), `state.sqlite3` with its WAL files, drafts, context, payloads, outbox/ledger, auth health and existing permission files. Keep private directories/files at their existing restrictive permissions. Credentials in macOS Keychain are not part of a filesystem copy; supply an already-authorized grant securely on Linux.

`ensure.sh` never calls `configure`, creates a fresh queue, resets activation time, or seeds a new STAMP allowlist. If the volume, database, configuration or deployment ownership marker is missing, it fails before starting a listener. Restore the persistent state; never initialize an empty replacement as recovery. Persistent storage must actually be guaranteed by the host/provider. The enrollment confirmation records the operator's verification; it cannot turn ephemeral storage into a persistent volume.

The `deployment.json` marker binds the existing Chat space and GitHub actor to one host. A copied runtime refuses startup on another host until explicit handoff. The worker checks this marker again under its existing process lock before recovery. Legacy local installations without a marker continue to work; supervised deployments require enrollment. This is a single-host cutover protocol, not a distributed lease: never deploy multiple independent copies or deliberately bypass the operator fencing confirmations.

## One-time enrollment on the source host

Use this updated code on the source host before migrating. Do not replace a running checkout or interrupt a review to do so.

1. Wait for active reviews/submissions to finish. Stop the existing supervisor/service so it cannot restart the old binary. Preserve queued work and the full runtime directory.
2. Set `REVIEW_AGENT_RUNTIME` to that **existing** directory. Verify that it includes `config.json` and `state.sqlite3`.
3. After confirming the storage survives host/container replacement and no other listener owns the space:

```bash
.venv/bin/python deployment.py initialize --confirm-persistent-storage --confirm-single-host
```

Enrollment fails while the worker holds its lock and never resets the database. It cannot be repeated over an existing ownership marker. For a genuinely new installation only, perform the repository's explicit configuration/setup first and initialize its database offline with `from agent import Store; Store().db.close()`; never use that step to repair lost state.

## Planned migration

1. On the target persistent host, install the Python environment from `requirements.lock`, a tested Codex CLI, and the same Falcon skill. Run `.venv/bin/python deployment.py host` to obtain its non-secret host ID.
2. On the drained/stopped **source**, with its existing runtime selected:

```bash
.venv/bin/python deployment.py retire --target TARGET_HOST_ID
```

This requires the source process lock to be free and records the target. The source now refuses startup, including automatic supervisor restart. Do not start the target before retirement. Retire using the updated code; an older binary does not understand the marker and must remain stopped.

3. Transfer the **entire stopped runtime** securely, including the retired ownership marker, SQLite files, drafts and configuration, to the target's persistent volume. Preserve permissions. Do not commit the runtime, print secrets, or copy an actively changing database. Keep the retired source copy for recovery.
4. On the target, select the transferred runtime and run:

```bash
.venv/bin/python deployment.py activate
bash deploy/ensure.sh
```

Only the recorded target can activate the retired state. Existing queue/checkpoint/activation values are preserved. First verify with capacity `1` if this is a new host, then drain before enabling the saved capacity `2`. Observe genuine eligible requests; do not manufacture paid reviews as a smoke test.

For rollback, drain/stop the target, retire it toward the original host ID, transfer the **latest** complete state back, then activate there. Never resume an old snapshot while newer GitHub writes or queue history exist.

## Unplanned host replacement

Restore or reattach the persistent runtime first. A changed host ID is intentionally rejected. If the original host is lost, an operator must **fence it at the infrastructure/service level so it cannot restart** before using:

```bash
.venv/bin/python deployment.py activate --confirm-source-fenced
```

This confirmation is never inferred by a script or model. It is required because a filesystem copy cannot prove a remote host has stopped. If state itself is lost, restore a durable backup and reconcile pending writes; do not create a new activation timestamp to hide missing requests.

## Authentication and settings

- Existing Codex login is reused. `REVIEW_AGENT_OPENAI_API_KEY` is needed only if no saved login exists; API-key usage is billed through the API account. CLI installation is explicit and uses a tested version, not an unpinned global install on every replacement container.
- Existing `gh` authentication is reused. `REVIEW_AGENT_GH_TOKEN` is optional for hosts that explicitly supply an existing token. The authenticated login must equal the saved configuration actor; mismatch fails before Codex login or worker startup. No account switching or permission changes are performed.
- macOS retains Keychain. On Linux, an existing owner-only grant file is used, or `GOOGLE_CHAT_GRANT_JSON` securely seeds the already-authorized grant once. `REVIEW_AGENT_GRANT_FILE`, `REVIEW_AGENT_GOOGLE_ACCOUNT` and `REVIEW_AGENT_GOOGLE_PROJECT` retain their documented overrides. No browser consent flow runs during startup.
- Provide Falcon through `REVIEW_AGENT_SKILL_FILE`, a trusted repository copy, or the original macOS path. Do not replace criteria during deployment. Existing STAMP permissions travel with the runtime and are not recreated from environment defaults.
- Permit the endpoints required by the existing clients, including GitHub, Google Chat/OAuth and the selected OpenAI authentication/backend endpoints. A successful cached login is not proof of model entitlement; verify a real eligible request before accepting a new host.

## Operation and health

`bash deploy/ensure.sh` starts one detached supervisor on Linux only after ownership/state/authentication checks. A fresh successful intake heartbeat must appear before startup succeeds. Subsequent checks fail for stale/missing/future heartbeats, paused Chat, unready Google authentication, actor mismatch or unloaded worker capacity. The supervisor retries worker crashes only while this host still owns the same persistent runtime; retirement stops its restart loop.

Use `agent.py status`, `history` and `costs` for operational details. The dashboard supports the same runtime variable. `deploy/stop.sh` is an explicit interrupt, not a drain command; finish active reviews before calling it. Do not schedule additional listeners as a recovery mechanism.
