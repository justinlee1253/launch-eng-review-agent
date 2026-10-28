# ad-sentinel reference (applies only to changes under `ad-sentinel/`)

ad-sentinel is an aiohttp service on EKS that receives Datadog anomaly webhooks and detector events. It enriches each alert with account data from the Sentinel Postgres database and the lake engines, applies the killswitch, dedupe and suppression rules, and notifies people through Jira tickets, Google Chat and email. It writes its own alert log. Every branch deploys to stage; prod runs on `main` after a manual block step, through an Argo canary.

Its written rules are `ad-sentinel/AGENTS.md` (P0 and P1 review severities), `README.md` and `docs/testing.md`. There is no `ad-sentinel/CLAUDE.md`; judge the trajectory against the shape the code already has. `service_profiles["ad-sentinel"].loaded_docs` lists the context files in `documents`. `deferred_docs` lists files the budget left out. `withheld_docs` lists files that hold credential values: request one only when a finding depends on it, and never quote the value (section 12). Map `AGENTS.md` P0 to `dangerous` and P1 to `should_fix`, blocking. Start the title with `Sentinel rule:`.

## Data access

1. SQL takes values as bound parameters: `text(query)` with `:name` placeholders and a params dict, as `alert_baseline.py` does. A value that this PR puts into SQL text through a `{{ }}` template value, an f-string, `%` or `.format`: `dangerous` (`AGENTS.md` P0). Values come from webhook payloads. Unchanged lines are not findings.
2. `db.pg_fetchone` asserts `LIMIT 1` in the query. A new single-row query without it fails at run time: `should_fix`, blocking.
3. Account ids stay strings (section 6, item 3). A lake lookup that casts one to a number: `dangerous`.

## Credentials

4. Credentials come from AWS Secrets Manager by an environment-prefixed secret name. A webhook URL with a `key` or `token` parameter, an API token or a password that this PR adds or changes in source, a values file or a pipeline `env:` block: `dangerous` (`AGENTS.md` P0, section 6 item 1). Fix: a Secrets Manager entry read at start-up.

## Alert flow and safety switches

5. The killswitch runs before any processing (`process_alerts.py`). A new entry path that can notify a person or create a ticket checks it first. One that does not: `should_fix`, blocking.
6. Dedupe and caps are the product. Jira ticket reuse looks back 7 days for partners and 1 day for advertisers; stale-ticket limits are 24 hours for advertisers and 168 hours for partners; detector suppressor windows are per detector; notifications stop at `MAX_NOTIFICATIONS_PER_HOUR`. A change to one of those values or windows with no reason in the body: `should_fix`, blocking. A loosened window floods the alert channels; a tightened one hides real anomalies.
7. Advertiser alerts create no Jira ticket and send no Google Chat message. A change that adds either is a new write to a system of record (section 7).
8. SMB segment suppression runs live in every environment: the code default is dry-run, and `values.yaml` sets `smbSegment.dryRun: "no"`. A change to the SMB rule in `suppression.py` (pod, assigned account manager, revenue threshold) suppresses real prod alerts at merge. Ask for the backtest in the body; with none: `should_fix`, blocking.
9. Auto-close is off in prod (`enabled: 'no'`, `dryRun: 'yes'`). `enabled` wins over `dryRun`. A change that turns it on in prod closes real Jira tickets: `should_fix`, blocking, unless the body names the measurement that approved it. The close transition sets no resolution.
10. A flag reaches the pod as a quoted string through `templates/rollout.yaml` and parses as `yes`, `true`, `on` or `1`. An unquoted Helm boolean, or a new flag parsed another way: `should_fix`, blocking.

## Environments and recipients

11. Stage and prod stay apart: the OSD Jira project and the `Anomaly` issue type only in prod, a test project elsewhere; watchers only in prod; stage email only to the default address; the stage partner Chat space in stage; mocks in dev. A change that lets stage reach a prod project, space or person: `dangerous`.
12. Email goes only to `@rokt.com` recipients (`notification.py`). A change that removes or bypasses that filter: `dangerous`; an alert holds partner and account data.
13. Jinja templates keep `autoescape=True`. A new f-string template that puts a partner, account or campaign name into Jira or Chat markup without escaping: `should_fix`, blocking (`AGENTS.md` P1).
14. Alerts carry partner names, account ids and account manager emails. Those values in a log line beyond the existing `extra` ids, or real values in a test fixture: `dangerous` (section 6, item 2; `AGENTS.md` P1).

## Deploy and CI

15. The Service stays `ClusterIP` and the pod runs as non-root user 2000. Stage skips canary steps; prod does not. A change to either in prod: `should_fix`, blocking.
16. The task role reads its secrets, decrypts with KMS, connects to RDS by IAM and sends SES email. A new action, or a new `Resource: "*"` on an action that accepts an ARN: section 6, item 4.
17. Every branch deploys to stage. A pipeline change that removes the manual block before prod, or lets a branch other than `main` or `hotfix/*` reach prod: `dangerous`.
18. The pytest step pipes to `tee`, so it cannot fail the build; the junit annotate step is the real gate (`docs/testing.md`, "Reading CI test results"). Lint steps soft-fail. A green build is weak evidence. Describe CI exactly as reported and ask for a test only for a dangerous path.
