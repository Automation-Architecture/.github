# Org rulesets

Source of truth for the org rulesets this repo manages. GitHub holds the live
copy; this file is what it should match.

| File | Ruleset | State |
|---|---|---|
| `aaa-merge-gates.json` | `aaa-merge-gates` | **Active since 2026-09-18, id `23649946`.** Pilot on aaa-client-dashboard and opportunity-builder; `auto-merge.yml` disabled on both. |

## aaa-merge-gates

Moves merge enforcement from `auto-merge.yml` into GitHub, which checks these
rules at the moment of merge. Merging is GitHub's native auto-merge, opted into
per PR with `gh pr merge --auto --squash`.

- `review/verdict` must pass, from GitHub Actions (`integration_id` 15368). It
  is published by `.github/workflows/review-verdict.yml`.
- Every review conversation must be resolved.
- Squash only.
- A PR touching `.github/**` or a reviewer-config path needs one approval from
  `dev-ops` (team id 17698930). Every workflow posts checks as the same Actions
  app, so without this a PR editing a workflow could publish its own verdict.
- Org admins may bypass on a PR.

**Create it only after** `review-verdict.yml` is on `main` in every targeted
repo and has been seen publishing on real PRs. Team has no dry-run mode: a
required check a repo never produces blocks every PR there immediately. Also
disable `auto-merge.yml` in the targeted repos first, because it revokes
native auto-merge.

```bash
gh api -X POST orgs/Automation-Architecture/rulesets --input rulesets/aaa-merge-gates.json
```

Rollback: `gh api -X PUT orgs/Automation-Architecture/rulesets/23649946 -f enforcement=disabled`,
then `gh workflow enable auto-merge.yml -R Automation-Architecture/opportunity-builder`
(aaa-client-dashboard's copy was already disabled before the pilot).

**Keep the live ruleset and this file in step.** An edit made in the GitHub UI is not
reflected here; after any change, compare:

```bash
gh api orgs/Automation-Architecture/rulesets/23649946 \
  | jq -S '{name, target, enforcement, conditions, bypass_actors, rules}' > /tmp/live.json
jq -S . rulesets/aaa-merge-gates.json > /tmp/repo.json && diff /tmp/repo.json /tmp/live.json
```

**Widening:** add repos to `conditions.repository_name.include`, or replace the list with
`~ALL`, only after `review-verdict.yml` is on the default branch of every repo added.

**Operating procedure** for people working in these repos: `aaa-SOP`
`pr-review-and-merge-sop.md`, section "Ruleset-pilot repos".
