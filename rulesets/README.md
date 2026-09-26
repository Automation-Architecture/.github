# Org rulesets

> **Coffee rename audit (2026-09-26):** The historical JSON and rollback command
> below still name `aaa-coffee`. GitHub now names the repository `aios-coffee`,
> and its effective `main` rulesets are `aaa-default-branch-protection`
> (`16125109`) and the repository-specific `agency-delivery-gate-pilot`
> (`23773361`). Do not apply this JSON or use the Coffee rollback command
> until the live organization ruleset and this file have been reconciled.

Intended configuration for the org rulesets this repo manages. GitHub holds the live
copy; the Coffee target in this file requires reconciliation before reuse.

| File | Ruleset | State |
|---|---|---|
| `aaa-merge-gates.json` | `aaa-merge-gates` | **Historical configuration, id `23649946`.** Coffee was added as `aaa-coffee` on 2026-09-20; its current effective rulesets are listed above. Confirm live org configuration before applying this file. |

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

Rollback: disable enforcement, then re-enable the workflow gate on every repo whose
`auto-merge.yml` was disabled FOR this ruleset. Disabling enforcement alone leaves those
repos with no gate at all: the ruleset stops requiring `review/verdict`, and the workflow
that would otherwise hold the PR is still off.

```bash
gh api -X PUT orgs/Automation-Architecture/rulesets/23649946 -f enforcement=disabled
gh workflow enable auto-merge.yml -R Automation-Architecture/opportunity-builder
gh workflow enable auto-merge.yml -R Automation-Architecture/aaa-coffee
```

aaa-client-dashboard is deliberately absent: its copy was already disabled before the
pilot, so re-enabling it would restore a state this ruleset never took away. Add a line
here whenever a repo joins, in the same change that disables its workflow.

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
