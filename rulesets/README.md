# Org rulesets

> **Policy (Brad, 2026-09-29):** no human approval is required anywhere in the org, and Codex is the one
> required reviewer. Greptile is outside the merge policy: it runs only on repos Brad enables in its
> dashboard, nothing requires or waits on it, and nobody invites it (`@greptileai`). `aaa-merge-gates.json` below is the live
> ruleset `23649946` as read back on 2026-09-29, after that policy was applied. `aios-coffee` is not in it;
> it uses its own `agency-delivery-gate-pilot` ruleset (`23773361`).

Intended configuration for the org rulesets this repo manages. GitHub holds the live
copy; compare after any change (below). `aaa-default-branch-protection` (`16125109`,
all repos: PR required, no force-push, no deletion, no approvals) is not stored here.

| File | Ruleset | State |
|---|---|---|
| `aaa-merge-gates.json` | `aaa-merge-gates` | **Active ruleset, id `23649946`.** Targets aaa-client-dashboard and opportunity-builder. Matches the live ruleset as of 2026-09-29. |

## aaa-merge-gates

Moves merge enforcement from `auto-merge.yml` into GitHub, which checks these
rules at the moment of merge. Merging is GitHub's native auto-merge, opted into
per PR with `gh pr merge --auto --squash`.

- `review/verdict` must pass, from GitHub Actions (`integration_id` 15368). It
  is published by `.github/workflows/review-verdict.yml`.
- Squash only.
- No human approval of any kind: no approving reviews, no team or code-owner
  reviewers, no path-based approval, and no extra approval for unattributed
  changes. Review threads do not have to be resolved. (Removed 2026-09-29,
  Brad's org-wide policy.)
- **Known trade-off:** every workflow posts checks as the same Actions app, so a
  PR that edits `.github/workflows/review-verdict.yml` could publish its own
  `review/verdict`. The removed `dev-ops` approval on `.github/**` used to cover
  that. Codex still reviews such a PR; the gap is accepted, not closed.
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
```

`aaa-client-dashboard` is deliberately absent: its copy was already disabled before the
pilot, so re-enabling it would restore a state this ruleset never took away.
`aios-coffee` now uses its own `agency-delivery-gate-pilot` ruleset; do not
re-enable its old auto-merge workflow as part of this rollback. Add a line
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
