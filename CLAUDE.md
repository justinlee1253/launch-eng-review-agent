# Launch Engineering Review Agent

This repository runs as a long-lived background process inside a Claude Code cloud session. Credentials come only from environment secrets; never ask for them in chat and never print token values.

When asked to deploy, start, check, or keep the agent running:

1. Make sure `ROKT/launch-engineering` is attached to this session with push access (the `add_repo` tool, access `push`; the person may need to approve it). The session proxy authenticates GitHub API calls only for attached repositories, and it posts as the GitHub account connected to the session owner.
2. Run `bash deploy/ensure.sh`. It is idempotent: it installs dependencies, logs Codex in, seeds configuration and the Google grant from environment secrets, starts `agent.py run` detached if it is not running, and prints a one-line health summary.
3. If it fails, report the single line it printed and stop. The fix is almost always a missing secret, a blocked domain, or the missing Falcon skill; `deploy/CLOUD.md` lists each.
4. Keep it alive at three speeds. The worker runs under `deploy/supervise.sh`, which restarts it within seconds of a crash with no Claude turn involved. One hourly Routine bound to this session (create it only if none exists) with the prompt "Run `bash deploy/ensure.sh` and reply with one line: healthy, or the error." is the durable backstop that also brings a replaced container back. When the person wants closer monitoring, also start `/loop 15m` with that same prompt; it lasts for this session only, and the Routine still fires in a container the loop no longer exists in.
5. Do not change code, review policy, or allowlists unless explicitly asked. Never run `agent.py configure` a second time and never delete `.runtime/`.

`bash deploy/stop.sh` stops the worker. Logs are in `.runtime/activity.log` and `.runtime/agent.out`. Run tests with `.venv/bin/python -m unittest discover`.
