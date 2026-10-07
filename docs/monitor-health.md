# Monitor Health reconciler

GAAA-3937. One hourly job that alerts when an org monitor is failed, missing or
under-running, when the delivery gate is unhealthy or serves a stale policy,
and when an open PR has been held by `CODEX_*` gate blockers for too long. It
exists because, in the 2026-10-07 audit (GAAA-967), no org monitor alerted on
its own failure: two were red 7/7 days unnoticed, GitHub dropped about half of
the 10-minute doc sweep's runs, and a missing Codex review blocked silently.

| File | Purpose |
|---|---|
| `.github/workflows/monitor-health.yml` | hourly at :23, plus Monday 13:41 UTC heartbeat |
| `scripts/monitor_health.py` | collection, evaluation, alerting (stdlib + `gh`) |
| `monitor-health/watchlist.json` | what is watched: window, floor, owner, reason per entry |
| `.github/workflows/monitor-health-canary.yml` | hourly no-op: forced-failure target and a direct measure of schedule delivery |
| `tests/monitor-health/` | offline suite (`python3 -m unittest discover -s tests/monitor-health -v`), run by `monitor-health-tests.yml` |
| `docs/monitor-health-forced-failure-test.md` | the forced-failure test runbook (not yet run) |

## What it asserts

For each workflow in the watch list (scheduled runs only, `event=schedule`):

1. **state** is `active` (`disabled_manually` / `disabled_inactivity` stop a cron with no red run at all);
2. **newest scheduled run** is younger than `window_hours`;
3. **newest completed scheduled run** concluded `success` (an in-progress newer run does not hide an older failure);
4. **scheduled runs created in the last 24h** >= `min_runs_24h` when that is above 0.

Bootstrap: within one window of the workflow's `created_at`/`updated_at`
(these move when a workflow is added, enabled/disabled or its file changes) a
missing or old run is not reported, and within 24h the count floor is not
asserted.

Stale reads: the Actions runs API is served from replicas that can lag by
days. On 2026-10-07 about 1 read in 25 to 60 returned a list as it stood
about 3 days earlier (`total_count` 350 vs 369). Any failing workflow result is therefore
re-read up to twice, 5 s apart, keeping the union of runs and the highest
count (a lagging replica can hide runs but never invent them).

**Gate** (`gate_health`): `/health` answers 200 with `status: ok`, and its
`policyRevision` equals `revision` in `aaa-pr-bot@main:gate/policy/gate-policy.json`.
A mismatch is reported only after `deploy_grace_hours` (6) since the last
commit to that file, so a normal merge-then-deploy is quiet.

**Aged Codex blockers** (`codex_blockers`). The source is the newest
`agency-delivery/gate` check run, created by the `agency-delivery-gate` App, on
each open PR's current head. Its `output.summary` lists blockers as
``- `CODE`: message`` lines. A PR is reported when that run is not a success,
lists at least one `CODEX_*` code, and the head is older than `max_age_hours`
(6). The head's age is the earliest `started_at` of any check run on the head
SHA, falling back to the PR's creation. GitHub exposes no push time, and the
gate updates its own run in place, so the gate run's timestamps only show when
it last evaluated. An aged head with no gate check run at all is reported as
`gate_check_missing`, which means the gate is not evaluating. PRs that the pull API shows as closed (the search index lags), drafts, `rmbc`
(an approved gate exception) and PRs labelled `hold`, `on-hold`,
`do-not-merge`, `do not merge` or `blocked` are skipped. The PR list comes from
the search API (`org:Automation-Architecture is:pr is:open archived:false`)
with the same completeness checks as `scripts/rollout_monitor.py`.

## Alerts

- **Issue** in this repository, label `monitor-health`: one rolling,
  bot-authored issue. It is created when problems appear. When the set of
  problems changes, the issue gets a comment listing new and resolved items.
  When the set is unchanged, only the body is refreshed (no comment, no
  Slack). The issue is closed with a comment when all is clear. Its state is
  kept in a hidden marker in the body. Only issues authored by
  `github-actions[bot]` are trusted, because this repository is public.
- **Slack** (#dev-ops) through the `SLACK_DEVOPS_WEBHOOK_URL` repository secret
  of this repository. Posts are sent when the issue is created, changes or
  closes. Delivery means HTTP 200 with the body `ok`; anything else turns the
  run red after the issue has been written. If the secret is unset, the run
  logs a warning and the issue body says that the issue is the only channel.
- **Delivery guarantees**: the two channels are independent. If the alert
  issue cannot be read, Slack still gets this run's findings and the run goes
  red. A state change is written with `slack_pending: true` and cleared only
  after Slack answers `ok` (one retry 5 s later). A failed post is re-sent by
  the next run, and a failed all-clear post leaves the issue open until it is
  delivered.
- **Incomplete scans**: when any read fails, problems that were open before
  and were not seen this run are kept (marked "not re-checked") rather than
  announced as resolved.
- **Weekly heartbeat**: on Monday at 13:41 UTC (or on dispatch with
  `heartbeat=true`), one message saying the reconciler is alive and how many
  problems are open. It goes to Slack, or as a comment on a bot issue labelled
  `monitor-health-heartbeat` while Slack is unset.
- **Run colour**: findings keep the run green. Every GitHub read is retried
  up to 4 times (2, 4, 8 s) on a 5xx or timeout. On 2026-10-07 the check-runs
  endpoint answered HTTP 500 for 11 PRs for a few minutes. A read that still
  fails becomes part of a single `read_error` finding (one key, so a burst
  of 5xx changes the alert once), the alerts are still attempted, and then the run
  exits 1. A failed alert delivery also exits 1. Nothing is swallowed.

Public-repo note: issue bodies name private repositories and workflow files,
as the org secret scan's issue already does here. They carry no titles, logs
or secret material. Private-repo run links in them return 404 to outsiders.

## Who watches the watcher

- `aaa-client-dashboard` `dashboard-watchdog.yml` (daily, 15:00 UTC; companion
  PR, GAAA-3937) requires this workflow's newest scheduled run to be under 6 h
  old and successful, and posts to #dev-ops when it is not. The reconciler
  watches the watchdog back.
- The weekly heartbeat: a Monday without one means the reconciler is down.

## Hosting choice (2026-10-07)

GitHub Actions, in this repository, not a Cloudflare cron Worker. The Worker
would avoid GitHub's cron, but it is materially harder to run safely today:

- **Credentials.** The Worker would need a GitHub credential with
  `actions:read` and `checks:read` on every repository plus `issues:write`.
  The gate's App (`agency-delivery-gate`) has checks write and
  contents/issues/pull_requests read, with no `actions` access and no issue
  writes, so reusing it means widening the gate's own
  private key (an org-owner approval). The alternative is a new App or PAT
  that only Brad can provision. The Slack webhook value would also have to be
  pasted into the Worker by Brad. Here, `AAA_ORG_TOKEN` and `GITHUB_TOKEN`
  already exist. Only the Slack secret is missing, and the issue fallback
  works without it.
- **Cron loss is small at this cadence.** Over 2026-09-30 to 10-07, GitHub
  delivered 154/168 hourly `rollout-monitor` runs: 22 to 24 a day on 6 of 7 days, 13 on 10-03. The
  heavy loss is at 10-minute cadence (80-81 of 144/day). An hourly run at :23
  that misses one hour costs one hour of latency, and the cross-watch and
  heartbeat catch a reconciler that stops.
- **Portability.** The evaluation is pure functions over JSON with injected
  readers. Moving it to a Worker later means re-hosting the same logic on
  `fetch`, plus the credentials above.

The trade-off is that a total GitHub Actions outage silences this reconciler
and the watchdog together. During such an outage every watched workflow is
also down, and the gap shows as a missed Monday heartbeat. Moving to a Worker
is the follow-up if that is not acceptable.

## Operating it

Deploy = merge (Actions picks up new workflow files on the default branch;
`.github` is public, so minutes are free). Then verify:

```bash
R=Automation-Architecture/.github
gh workflow list -R $R --all | grep -i 'monitor health'          # both active
gh workflow run monitor-health.yml -R $R -f dry_run=true          # report only
gh run list -R $R -w monitor-health.yml -L 1 --json databaseId,status,conclusion
gh run view <id> -R $R --log | grep -E 'Monitor Health at|alert plan|::'
gh workflow run monitor-health.yml -R $R                          # real run: opens the issue
gh issue list -R $R --label monitor-health
```

Expected on 2026-10-07's state: 5 findings (`daily-pr-audit` and
`workflow-heartbeat` red until the aaa-internal-dashboard secrets are set,
`au-group` `smoke-e2e` and `fas-portal` `signin-e2e` red, `dashboard-watchdog`
disabled until it is re-enabled). Each is a real problem.

Slack (needs Brad; the value is never read by an agent):
`gh secret set SLACK_DEVOPS_WEBHOOK_URL -R Automation-Architecture/.github`
with the #dev-ops webhook from 1Password, the same one `aaa-client-dashboard`
uses. Marker-verify it as in aaa-runbooks
`integrations/slack-incoming-webhooks.md`, then dispatch with
`heartbeat=true` and read #dev-ops.

Rollback: `gh workflow disable monitor-health.yml -R Automation-Architecture/.github`
(and `monitor-health-canary.yml`). That is reversible with `enable`, and
nothing else reads its output. To remove it, revert the PR. The watchdog then
reports `monitor-health.yml` as not found (a notice) once the file is gone.

Changing the watch list: edit `monitor-health/watchlist.json` in a PR. Windows
and floors come from history, so read the runs before choosing a number:

```bash
gh api "repos/Automation-Architecture/<repo>/actions/workflows/<file>/runs?event=schedule&created=>=<7 days ago>&per_page=100" --paginate --jq '.workflow_runs[].created_at'
```

Snapshot producers (GAAA-3937 question, before any reaper retirement): the
two families the stale-snapshot reaper handles were produced by Claude Code
scheduled agents running as `web3sea`, not by GitHub workflows. The
sprint-progress sync PR body says "Triggered by the scheduled 6h dashboard
sync agent". The last ones were LKID#308 on 2026-08-21 (sprint-progress) and
LKID#309 on 2026-08-24 (husser board sweep). They are not idempotent: each
run made a dated branch and a new PR. This reconciler cannot watch them. Find
where they are scheduled (claude.ai routines or another machine) before
retiring the reaper.
