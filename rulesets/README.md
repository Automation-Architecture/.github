# Org rulesets

Source of truth for the org rulesets this repo manages. GitHub holds the live
copy; this file is what it should match.

| File | Ruleset | State |
|---|---|---|
| `aaa-merge-gates.json` | `aaa-merge-gates` | **Not yet created.** Pilot on aaa-client-dashboard and opportunity-builder. |

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

Rollback: set `enforcement` to `disabled` (`gh api -X PUT orgs/Automation-Architecture/rulesets/<ID> -f enforcement=disabled`), and re-enable `auto-merge.yml`.
