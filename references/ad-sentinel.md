# ad-sentinel reference (applies only to changes under `ad-sentinel/`)

ad-sentinel is an aiohttp service on EKS that receives Datadog anomaly webhooks and detector events. It enriches each alert with account data from the Sentinel Postgres database and the lake engines, applies the killswitch, dedupe and suppression rules, and notifies people through Jira tickets, Google Chat and email. It writes its own alert log. Every branch deploys to stage; prod runs on `main` after a manual block step, through an Argo canary.

Its written rules are `ad-sentinel/AGENTS.md` (P0 and P1 review severities), `README.md` and `docs/testing.md`. There is no `ad-sentinel/CLAUDE.md`; judge the trajectory against the shape the code already has. `service_profiles["ad-sentinel"].loaded_docs` lists the context files in `documents`. `deferred_docs` lists files the budget left out. `withheld_docs` lists files that hold credential values: request one only when a finding depends on it, and never quote the value (section 12). Map `AGENTS.md` P0 to `dangerous` and P1 to `should_fix`, blocking. Start the title with `Sentinel rule:`.

## Architecture map

The README diagram is an image, so this is the text map. Prefix paths with `ad-sentinel/ad_sentinel/`. Request a module at `ref=head` to verify a boundary before you judge it.

- Entry points (`app.py`):
  - `POST /webhooks/v1/datadog-webhook`: account anomaly alerts, queued to `process_alerts.process_datadog_alert`.
  - `POST /webhooks/v1/new-datadog-webhook`: SMB and group monitors, to `group_monitors/datadog_webhook.handle_datadog_event`.
  - `POST /detector/notify`: detector events, to `detector/detector_event_controller.handle_detector_event`.
  - `POST /categorize` and `GET /categorize/issues`: issue categorization, in `categorization/`.
  - `GET /killswitch?suppress=N`: sets or clears the killswitch (`models/killswitch.py`).
  - `GET /app_health/v1/__heartbeat__`: health.
- Work runs on one bounded queue: 20 workers, 200 pending jobs (`MAX_WORKERS`, `MAX_PENDING_JOBS`). A handler that does slow work outside the queue blocks the event loop.
- Account alert path, in order, in `controllers/process_alerts.py`:
  1. Validate.
  2. Check the killswitch.
  3. Enrich the account (`models/alert.set_account_values`, using Trino, StarRocks and ClickHouse).
  4. Run `sanity_check`. In order, it checks:
     - monitor and metric exclusions
     - upsell
     - ADMC liveness
     - test account
     - live resource, Rokt-managed account, Aftersell page and SMB segment (these four in `controllers/suppression.py`)
     - genuine trigger
     - superset alarm
     - active page variants or campaigns
     - the hourly cap
  5. Notify (`controllers/notification.py`).
  6. Save to the database (`models/alert.save_to_db`), unless the alert is a trial.
  7. Hand off to `account_alert_service.process_alert_dd` for Jira tickets, tags, product type and auto-close.
- Group monitor path: `group_monitors/datadog_webhook.py` saves the event, then posts one Chat message. It has its own template and space.
- Detector path: `detector/detector_event_controller.py` runs three steps:
  1. Save the event.
  2. Pick the channels (`detector_event_checker.py`).
  3. Dedupe by detector window (`detector_event_suppressor.py`).

  It then sends through `channels/` (Jira and Chat clients, built by `channels/channel_factory.py`). Dev uses mocks.
- Data ownership:
  - ad-sentinel owns the `sentinel` schema in Postgres (alert log, Jira ticket links, killswitch, detector events, recipients), through `controllers/db.py` and `sql/queries/pg_*.sql`.
  - It reads, but does not own, ClickHouse, StarRocks and Trino (`clickhouse.py`, `starrocks.py`, `trino_db.py`, `sql/queries/{ch,sr,trino}_dictionary.sql`).
- Downstream writes:
  - Jira (`controllers/jira_operations.py`, `channels/jira_*`).
  - Google Chat (`controllers/gchat.py`, `channels/gchat_*`).
  - Email through op3-notifications (`notification.py`).
  - Datadog metrics (`controllers/dogstatsd.py`).
  - Reads from the Rokt platform, transactions and supplementals APIs (`controllers/rokt_api.py`, `controllers/supplementals.py`).

A change on one path is checked against the safety steps of that path. A new path is checked against all of them (items 5 to 12).

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
