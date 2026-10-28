# prism-qa reference (applies only to changes under `prism-qa/`)

Prism QA is a macOS SwiftUI app for Launch Engineering QA. It runs a local `mitmdump` proxy, captures Rokt and mParticle traffic, records evidence on the operator's Mac, and shares runs to Google Drive, Sheets, Jira, Google Chat and an n8n webhook. It ships by hand as a signed, notarized build through a Homebrew cask. Telemetry goes to `prism-pulse`.

Its rules live in `prism-qa/CLAUDE.md` (decision log and hygiene) and the topic docs under `prism-qa/.claude/`. `service_profiles["prism-qa"].loaded_docs` lists the docs in `documents`. `deferred_docs` lists topic docs that the budget left out: request one with `kind=file`, `ref=head` when a finding depends on it. `prism-qa/.claude/prism_dev_context.md` is only an index. Cite the doc line when you enforce a rule. Start the title with `Prism rule:`.

## Telemetry and the Pulse token

1. The Pulse token is write-only and is stamped into `Info.plist` at build time. A token value in source, a script argument, a log line or a test fixture: `dangerous`. A change that lets the token read the dashboard or the database: `dangerous`.
2. A telemetry event carries flags, counts and fixed vocabulary. A field that carries notes, mention text, evidence, a URL, a captured customer attribute, an email address, an account id or a session id: `dangerous`. The one business id allowed is a Jira key that matches `OSD-[1-9][0-9]{0,17}`.
3. An error event sends only the sanitized type fingerprint. The full error goes to the local `session.log`. A raw error message or `localizedDescription` in an event: `dangerous`.
4. The queue stays bounded (5,000 events, 5 MiB). A batch is delivered only on a 2xx response whose counts cover the whole batch. A change that marks a batch delivered on another response, or removes a bound: `should_fix`, blocking.
5. A new event kind or field is a one-way door into the Pulse schema. Ask which telemetry doc entry it follows, and look for the contract test in `Resources/tests/test_telemetry_contracts.py`.

## Proxy and capture

6. The proxy streams every response except the documented buffered set (`/v1/experiences`, Fluent `/v1/display`, finite small v2 bodies). A change that buffers another path, or turns a streamed response into a buffered one: `should_fix`, blocking. A buffered stream stalls the operator's browser.
7. Matching is by path plus the `rokt-` header, not by domain. `header_inject.py` loads last. A change to the addon order or to the match rule: `should_fix`, blocking, unless the capture doc at `main` names it.
8. HAR capture is off by default and desktop-only. It drops `Authorization`, `Cookie`, `Set-Cookie`, credential-shaped headers and query values, and URL userinfo, and it keeps its caps (2 MiB per body, 32 MiB of bodies, 64 MiB journal, 10,000 entries). A change that keeps one of those values, removes a cap or turns capture on by default: `dangerous`.

## Files, credentials and sharing

9. A file path read from `output.json` or a sidecar resolves through `URL.appendingContainedFile`: bare filename, no separator, no `..`. A path joined any other way before an upload to Drive, Jira or Chat: `dangerous`.
10. Private files (the ticket registry, `session_meta.json`, run sidecars) are written atomically through an owner-only (0600) temporary file. The registry stores no raw email, account id or session id. A broader mode, a non-atomic write or a raw identifier there: `should_fix`, blocking.
11. OAuth tokens are XOR-obfuscated on disk by a recorded decision; that is not a finding. The `drive` scope is accepted only because the code makes no delete, move or rename call and every query is scoped by `'<parentID>' in parents`. A new Drive mutation or an unscoped query: `dangerous`. A new scope with no reconnect path (`grantedScope`, `hasChatScope`): `should_fix`, blocking. A share flow that starts interactive OAuth: `should_fix`, blocking.
12. Sharing never changes a Jira status and never triggers the Drive, Sheet or n8n export. Jira status changes only through `JiraTicketStatusService`. A share path that writes status or triggers an export is a new write to a system of record (section 7).
13. Run identity is immutable: a `default(-N)` key never renames evidence. A new sidecar does not change the `output.json`, `qa_output.json`, Sheet, webhook or telemetry wire shape. A change to one of those shapes is a one-way door.
14. An account id is a lossless string, never a floating-point number (section 6, item 3). Drive folders match by account id, never by name.

## Local listeners and concurrency

15. `NativeImportServer` binds loopback only. `MobileScreenshotUploadServer` requires the per-run token, keeps its caps (16 images, 64 MB) and closes at End Run. A listener on another interface, a missing token check or a removed cap: `dangerous`.
16. `ProxyManager`, `SessionManager`, `TelemetryService` and `MacIPWatcher` are `@Observable @MainActor`; process stdout is read in detached tasks. Mutation of their state off the main actor: `should_fix`, blocking. ScreenCaptureKit calls are gated on `ensureScreenCaptureAccess()`. Window matching uses an exact PID or `applicationName`, never a bundle id or a title substring.

## Release and distribution

17. The signing key and notarization credentials never enter shared CI. Buildkite runs on Linux and only verifies, bumps the cask and notifies. A step that imports a certificate, unlocks a keychain or notarizes in shared CI: `dangerous`.
18. `verify-artifact.sh` checks the artifact sha256 against `release-manifest.json`. A change that skips or weakens that check: `dangerous`.
19. `latest.json` is published only for the version the cask already names. A change that publishes it before the cask and the artifact agree: `should_fix`, blocking. A stale or early `latest.json` hides or breaks the update banner.
20. The cask has no `depends_on mitmproxy`, and its `zap` stanza never names `~/Desktop/prism_qa`. A `zap` path that deletes audit data: `dangerous`.
21. A version bump changes `project.yml` and the minimum pins in `release-requirements.json` together. One without the other: `should_fix`, non-blocking.

## Background

- The UI follows the approved wording, the Archivo font, spacing and theme tokens. Compilation alone does not prove visual parity. A visual change with no screenshot in the body: a non-blocking `PR:` question.
- `prism-qa/CLAUDE.md` asks that docs change in the same PR as the behaviour they describe (section 8, "Docs ship with code").
- Some docs are stale. `.buildkite/pipeline.yaml` comments still describe a CI release; `RELEASING.md` is right. The in-repo cask lags the tap. `prism_competitor_audits.md` is mostly a pre-build design. When a doc and the source differ, the source is right.
