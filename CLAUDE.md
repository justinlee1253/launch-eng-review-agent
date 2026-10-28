# Launch Engineering Review Agent

Codex performs reviews. Claude may help operate the same worker on a persistent host; an ordinary Claude cloud session is not a durable daemon host.

When asked to deploy, start or check:

1. Read `deploy/CLOUD.md`. Use an existing persistent `REVIEW_AGENT_RUNTIME` with its original database, configuration, drafts and outbox. Never recreate activation time or the queue to recover a missing volume.
2. Only run `bash deploy/ensure.sh` on its enrolled host. Missing state, a retired host, different host identity, paused Chat, stale heartbeat or account mismatch must be reported as failures. Never bypass the ownership checks.
3. Do not create Routines or `/loop` tasks to keep a container alive. A replacement session must not automatically create another listener. Use persistent infrastructure and the offline handoff procedure.
4. Before migration, finish active reviews and stop the old supervisor. Retirement and activation are explicit operator actions. Do not run them automatically, change accounts, export credentials into chat, change allowlists, or edit policy.
5. Preserve the one-process lock, configured worker count and twenty-minute shared attempt budget. Never delete `.runtime/` or re-run `agent.py configure` on an existing installation.

`bash deploy/stop.sh` interrupts active work, so drain before using it. No deployment or host handoff is authorized merely by reading these instructions. Run offline tests with `.venv/bin/python -m unittest discover`.
