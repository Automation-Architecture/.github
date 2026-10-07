# Monitor Health: forced-failure test runbook

Steps 2 to 6 of the forced-failure test plan in the GAAA-967 final audit, adapted
to the reconciler that was built (GAAA-3937). **Not yet run.** Brad approved
the test once the reconciler exists (2026-10-07). It touches production
monitors, so run it deliberately, one step at a time, and revert each step
before starting the next.

Step 1 of the plan (build the reconciler) is `docs/monitor-health.md`.

## Before you start

- `monitor-health.yml` and `monitor-health-canary.yml` are merged, active, and
  have had at least one scheduled run each. Both are also active in
  `gh workflow list -R Automation-Architecture/.github --all`.
- Record the baseline. Note the open `monitor-health` issue (number and the
  findings it lists) so you can tell the test's finding apart from standing
  ones.

```bash
R=Automation-Architecture/.github
gh issue list -R $R --label monitor-health --state open --json number,title,url
gh run list -R $R -w monitor-health.yml -L 3 --json databaseId,event,conclusion,createdAt
```

- Know the timings. The reconciler runs at :23 every hour and the canary at
  :53. Any step can be sped up by dispatching the reconciler:
  `gh workflow run monitor-health.yml -R $R`. Only **scheduled** runs of
  watched workflows count. A dispatched run of a watched workflow is ignored
  by design.
- Each step passes when **both** happen: the finding appears in the issue (a
  comment listing it under "New"), and a #dev-ops message arrives if
  `SLACK_DEVOPS_WEBHOOK_URL` is set in `.github`. Read the destination; the
  run log alone does not prove delivery.

## Step 2: completed-but-failed (canary)

The plan said "dispatch codex-review-sweep with a FORCE_FAIL input", but that
sweep has no such input, and a dispatched run is not a scheduled run. The
canary gives a scheduled run that fails on demand, without breaking a real
monitor.

```bash
gh variable set MONITOR_HEALTH_CANARY_FAIL --body true -R $R
# wait for the next scheduled canary run (:53) and confirm it failed:
gh run list -R $R -w monitor-health-canary.yml --event schedule -L 1 --json conclusion,createdAt
# then wait for :23 or dispatch the reconciler:
gh workflow run monitor-health.yml -R $R
```

**Expect** a `last_run_failed` finding for `.github/monitor-health-canary.yml`
within one reconciler run of the failed canary run (at most about 1.5 h).

**Revert:**
`gh variable delete MONITOR_HEALTH_CANARY_FAIL -R $R`. The next scheduled
canary run succeeds, and the reconciler run after it lists the finding as
resolved. The issue closes if nothing else is open.

## Step 3: monitor not running

```bash
gh workflow disable rollout-monitor.yml -R $R
gh workflow run monitor-health.yml -R $R      # or wait for :23
```

**Expect** a `workflow_not_active` finding for `.github/rollout-monitor.yml`
("state is `disabled_manually`") on the next reconciler run, well inside the
2 h the plan allows. `rollout-monitor` is a read-only report, so an hour off
costs nothing.

**Revert:**
`gh workflow enable rollout-monitor.yml -R $R`. Enabling moves the workflow's
`updated_at`, so the next reconciler run treats it as freshly enabled (one
4 h window) and the finding resolves.

## Step 4: provider absence (aged CODEX_* blocker)

Withholding Codex for real takes hours. The reconciler's threshold can be
lowered for one dispatched run instead. The detection path is the same; only
the age limit changes.

1. Open a throwaway, non-Markdown PR in the sandbox repository, so that the
   gate requires Codex:

   ```bash
   S=Automation-Architecture/aaa-pr-bot-sandbox
   # in a worktree of $S: change one non-.md file on a new branch, push, then
   gh pr create -R $S --title "Monitor Health forced-failure test: aged Codex blocker (close me)" --body "GAAA-3937 step 4. Throwaway, close without merging."
   ```

2. Within the first minutes, before Codex reviews (3 to 6 min observed), confirm
   that the gate check lists `CODEX_REVIEW_MISSING`. Then dispatch the
   reconciler with a tiny threshold:

   ```bash
   gh workflow run monitor-health.yml -R $R -f codex_max_age_hours=0.02
   ```

**Expect** a `codex_blocker_aged` finding for `aaa-pr-bot-sandbox#<n>` that
names `CODEX_REVIEW_MISSING`.

It is a race: a dispatched run spends about 3 minutes on the workflow checks before it scans PRs. If Codex reviews the PR first, the blocker clears and nothing is reported. In that case, repeat the step, or use the bot-authored variant below.

**Revert:**
Close the PR and delete its branch. The next normal run (6 h threshold) no
longer lists it, because closed PRs are not searched.

To test real Codex absence, which takes longer: leave the PR open as a draft
for 6 h, mark it ready, and stop Codex from reviewing it (for example, open
it from a bot account, which Codex does not review). Expect the finding on
the first run after 6 h.

## Step 5: dead alert channel

```bash
gh workflow run monitor-health.yml -R $R -f test_dead_slack=true
```

The run posts to a syntactically valid but nonexistent webhook. **Expect:**

- the run is **red**, with `::error::alert delivery failed: Slack answered HTTP 4xx ...`;
- the `monitor-health` issue was still written or refreshed in the same run.
  It is the fallback channel.
- the Dashboard Watchdog's next run (15:00 UTC) reports
  `monitor-health.yml: last scheduled run ... concluded failure` only if a
  *scheduled* reconciler run was red. A dispatched test run does not count,
  so for the end-to-end path wait for the next scheduled run, which should be
  green again.

**Revert:**
Nothing to revert. The real secret is untouched.

To prove a revoked real webhook turns runs red, the closer equivalent, Brad
can point `SLACK_DEVOPS_WEBHOOK_URL` in `.github` at a webhook he deletes in
the Slack app settings. Restore the real value afterwards and marker-verify
it.

## Step 6: heartbeat path (safe, today)

The aaa-internal-dashboard heartbeat (AAAID-55) is mute while that repository
has no `SLACK_WEBHOOK_URL`:

```bash
gh workflow run workflow-heartbeat.yml -R Automation-Architecture/aaa-internal-dashboard
gh run list -R Automation-Architecture/aaa-internal-dashboard -w workflow-heartbeat.yml -L 1 --json conclusion
```

**Expect** a red run (`daily-pr-audit` has no successful run, and
`SLACK_WEBHOOK_URL is not set`). This proves that path is mute until Brad sets
the secrets. The reconciler already reports the scheduled `workflow-heartbeat`
and `daily-pr-audit` runs as `last_run_failed`, so this gap is covered while
it lasts. This dispatched run does not change the reconciler's result.

Then send the reconciler's own heartbeat:

```bash
gh workflow run monitor-health.yml -R $R -f heartbeat=true
```

**Expect** "Monitor Health weekly heartbeat: alive, watching N scheduled
workflows ..." in #dev-ops, or as a comment on the `monitor-health-heartbeat`
issue while Slack is unset.

## Record the result

Add a row per step to the GAAA-967 audit's evidence matrix ("Forced-failure
test"): the time, the run ID that detected it, the issue comment URL, and
whether Slack received the message. Note anything that did not alert within
its window as a finding on GAAA-3937.
