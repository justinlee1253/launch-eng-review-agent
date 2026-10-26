# Local Falcon Chat agent

Step 5's Google authorization helper is implemented. The review listener/worker and LaunchAgent are not implemented or running yet. The old MCP monitor stays paused.

This repository stores the source code. Execution remains on the Mac running the agent; pushing code to GitHub does not deploy a cloud service or start reviews.

## Local installation

On macOS with Python 3.10 or later:

    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.lock

The existing local installation is already authorized; cloning the source alone does not copy its credentials. Keep downloaded OAuth client JSON and all runtime data out of Git. The virtual environment, runtime state, activity logs and local progress notes are ignored.

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

Verified on 2026-10-26: initial browser authorization, refresh token present, Keychain persistence, and a successful forced renewal from a separate process without another browser sign-in. Twelve focused tests passed, using Google's real credential-refresh implementation with fake network responses. A naturally elapsed token-expiry cycle and a real Chat space read have not been observed yet. These checks do not prove permanent credentials.

Do not paste client JSON or grants into Chat or logs. The old MCP authentication block is independent of this new direct API setup and has not been cleared.
