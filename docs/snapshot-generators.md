# Snapshot generators — stop opening a new PR every run

## The problem

Several scheduled agents/jobs regenerate a **data or status snapshot** on every
run and open a **brand-new dated PR** each time. Nobody merges them (they're
regenerated data, not reviewable changes), so they pile up. On 2026-07-17 the
org had **164 open PRs** — 74 in LKID alone — overwhelmingly from two generators:

| Generator | Opened per run | What it produces |
|---|---|---|
| **husser** board sweep (scheduled cloud routine, 8am ET) | `chore/husser-board-sweep-<date>` + new PR | a Jira board status report |
| **sprint-progress sync** (scheduled) | `chore/sync-sprint-progress-<date>` + new PR | a regenerated `sprint-progress.json` |

A board sweep for today **supersedes** yesterday's — only the newest is ever
useful. Opening a fresh PR daily for it is the bug.

## The fix — pick one, per generator

A regenerated snapshot should never accumulate PRs. In order of preference:

### 1. Don't PR it — commit the data directly
`sprint-progress.json` is generated data, not a change to review. Have the job
commit straight to `main` (or a dedicated `dashboard-data` branch the dashboard
reads). No PR, no pileup. Best option for pure data files.

### 2. Update ONE rolling PR (fixed branch)
If it must stay a PR (e.g. for visibility/approval), use a **stable branch name
with no date** and force-update it. The same branch reuses the same PR instead
of opening a new one. With `peter-evans/create-pull-request` this is automatic:

```yaml
- uses: peter-evans/create-pull-request@v6
  with:
    branch: chore/sprint-progress-sync      # FIXED — no date
    title: "chore: sync sprint-progress.json from Jira"
    commit-message: "chore: sync sprint-progress.json from Jira"
    body: "Rolling data sync. Updated automatically each run."
    delete-branch: false
```

For a **scheduled cloud routine** (husser), the equivalent instruction in the
routine prompt is:

> When you publish the board sweep, do **not** open a new PR. Use the fixed
> branch `chore/husser-board-sweep` (no date in the name). If an open PR from
> that branch already exists, push your update to it; otherwise open one PR and
> reuse it thereafter. Never create a dated per-run branch or PR.

Even better for husser (a status report, not a change): **overwrite
`agents/husser/outputs/latest.md`** on that one branch, or post the sweep as a
comment on a single standing tracking issue — not a PR at all.

## Backstop

`.github/workflows/stale-snapshot-reaper.yml` runs daily and, for the explicit
snapshot families above, keeps the newest open PR and closes the rest. It's a
safety net for when a generator regresses or a new one appears — **not** a
substitute for fixing the generator. Its scope is a narrow allowlist; per-period
deliverables (e.g. the weekly `draft(weekly): funston` PRs, where each week is
distinct) are deliberately excluded.
