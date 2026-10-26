# Launch Engineering review agent

A local five-second listener for the dedicated Google Chat review space. It durably queues coworker PRs, acknowledges in the original thread, runs one sequential Falcon review through Codex, submits a commit-anchored GitHub review, and replies with the result. The old MCP monitor stays paused.

This repository stores the source code. Execution remains on the Mac running the agent; pushing code to GitHub does not deploy a cloud service or start reviews.

## Local installation

On macOS with Python 3.10 or later:

    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.lock

The existing local installation is already authorized; cloning the source alone does not copy its credentials. Keep downloaded OAuth client JSON and all runtime data out of Git. The virtual environment, runtime state, activity logs and local progress notes are ignored.

## Configure and run

This Mac is configured for `spaces/AAQAixK3auw`. On a fresh installation, configure once:

    .venv/bin/python agent.py configure --space https://chat.google.com/u/0/app/chat/AAQAixK3auw

This records a new activation time in `.runtime/config.json`; older messages are excluded. Configuration refuses to overwrite an existing activation/cursor. This pilot reads and writes only this exact space. Teammates post a supported PR link there without needing to tag Justin or authorize the agent themselves. Messages from Justin and bots are excluded.

Verify the existing grant and exact space, then run the foreground pilot:

    .venv/bin/python agent.py check-chat
    .venv/bin/python agent.py run

Stop with Ctrl-C. Restarting preserves requests, outbox messages, pagination and review intents. Do not run another instance; the process lock rejects it. The small listener continues polling and acknowledging while the independent review thread waits for Codex. No model starts on an empty poll. Sleep pauses execution naturally; no keep-awake assertion is used.

Supported review/context repository: `ROKT/launch-engineering` only. GitHub actor: `justinlee1253`, verified from GitHub before each submission; accounts are never switched. PRs authored by either Justin account, closed/merged PRs and drafts do not receive automatic reviews. Drafts or inaccessible evidence pause for attention.

## Review behavior and safety

The host reconstructs full text diffs from verified Git blobs at the captured head and merge base, loads applicable repository instructions, and provides surrounding evidence. Codex can request more files/directories in the approved repository. Related-service `main` reads are pinned to a full SHA for the entire review. Additional repositories require a deliberate future allowlist change; unavailable required context pauses the review.

The model gets the installed Falcon skill and [review-policy.md](review-policy.md). All review lenses run sequentially. Material blockers, vulnerabilities, correctness and consequential design flaws matter; style and preferred abstractions do not. Complete reviews with no blockers approve; substantiated blockers request changes. Tests/builds are never executed on PR code, and CI is not claimed to pass.

GitHub and Chat writes are outside the model. The subprocess uses saved Codex authentication with user configuration, plugins, apps, shell, browser, hooks and agent tools disabled. Structured evidence requests are validated by the host, not executed as commands. Runtime source context is temporary and is not committed. Codex analysis uses the remote model; the program and queue run locally.

Submissions reserve their intended event/head/marker before writing. The worker checks current head/state/actor and intervening Justin reviews, deduplicates by repository/PR/head/actor, and reconciles lost responses against the GitHub timeline. An uncertain write is held for attention, never blindly repeated. A head change requeues a fresh review; an already-submitted result is anchored to its old commit and recorded as stale. GitHub does not provide an atomic latest-head review guarantee.

The pilot pauses rather than approving binary/symlink/submodule changes, files over 250 KB, more than 150 changed files, inaccessible evidence, or context beyond 1.2 million serialized characters. It bounds evidence requests to eight model rounds and each model invocation to ten minutes. These limits are visible failures, not partial approvals.

## Status, history, and recovery

    .venv/bin/python agent.py status
    .venv/bin/python agent.py history

History includes request states, GitHub review IDs/URLs, full SHAs and recent events. SQLite stores message IDs/timestamps/threads, not incoming message bodies. Authored outgoing acknowledgments/reviews are retained for idempotent delivery. Review payloads are in `.runtime/payloads/`; activity logs rotate at 1 MB with three backups. The local runtime directory is owner-only and excluded from Git. Keep its database and WAL files together when backing it up; do not delete it to fix a connection.

A Google authorization/permission failure sets a durable pause. No background path opens OAuth or a browser. After deliberately repairing the actual cause, verify and explicitly resume:

    .venv/bin/python agent.py check-chat --resume

For genuinely revoked grants, use the explicit authorization repair below before this check. Repeating a heartbeat or restarting does not clear the pause. A failed Chat result delivery does not repeat a successful GitHub review. After investigating a paused review:

    .venv/bin/python agent.py retry JOB_ID

A reserved/ambiguous write is retried only as timeline reconciliation, never as another submission. A job without a reserved write receives a fresh review. Repairing auth is separate from retrying a review.

## Background operation

After one real coworker pilot request has completed, stop the foreground process and install the single login service:

    .venv/bin/python launch_agent.py install
    .venv/bin/python launch_agent.py status

The installer refuses startup before a real submitted pilot review is in this runtime's ledger. It creates `~/Library/LaunchAgents/com.jklee.launch-eng-review-agent.plist`, using absolute executable paths, background scheduling, a 30-second restart throttle, and the same process lock. The plist contains no tokens. Logs are written by the rotating application logger; raw subprocess output is not sent to launchd logs. `launch_agent.py render` previews the exact definition without installing it. `launch_agent.py stop` unloads the service.

This runs while the laptop is awake. Automatic login startup is a separate rollout step from building and testing the worker. Do not resume the older MCP heartbeat.

## Authorization and checks

The source and virtual environment live in this directory on Justin's Mac. Google grants are stored in macOS Keychain under service com.rokt.le-orbit-review.google-chat and the account label justin.lee@rokt.com. No token JSON is stored in this project. The account label and browser login hint are configuration, not independent proof of the Google account's identity.

From this directory, inspect non-secret health:

    .venv/bin/python auth.py status

Test renewal without a browser:

    .venv/bin/python auth.py check --force-refresh

Ordinary background credential loading uses CredentialManager.load(). It renews expired access tokens, persists the updated grant and never invokes an interactive flow. Permanent authorization errors persist a blocked flag in .runtime/auth-state.json. Transport failures preserve the grant without labeling it revoked. File locking serializes credential operations across processes.

Initial setup has already succeeded for the downloaded Desktop client associated with project le-orbit-review. The only interactive command is:

    .venv/bin/python auth.py authorize --client-file /absolute/path/to/downloaded-client.json

An existing grant prevents this command from opening another consent page. Deliberate repair after a genuine revocation additionally requires --reauthorize. Do not run repair during normal operation. Authorization requests use exactly the three agreed Chat scopes, offline access, generated PKCE, the OAuth library's state verification and a loopback callback.

Run focused local tests:

    .venv/bin/python -m unittest discover -v

Verified on 2026-10-26: initial browser authorization, refresh token presence, Keychain persistence, repeated successful forced renewal from separate processes without another sign-in, and real reads of the designated Chat space. The focused suite covers expired-token refresh, revocation pause, queue/pagination restart, same-head deduplication, missing evidence, moved heads, ambiguous writes, verified inline findings, and listener responsiveness during a blocked review. A naturally elapsed token-expiry cycle, real teammate acknowledgment timing, and a complete real pilot review still require live acceptance. Initial login alone does not prove unattended renewal or permanent credentials.

Do not paste client JSON or grants into Chat or logs. The old MCP authentication block is independent of this new direct API setup and has not been cleared.
