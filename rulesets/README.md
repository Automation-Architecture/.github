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
| `github-delivery-gate.json` | `.github App-owned delivery Gate` | Desired repository protection for `.github`; verify live installation and ID in the [cutover record](../docs/rollout-monitor.md). Adds App-owned Gate and always-run monitor tests, no bypass actors. |

## aaa-merge-gates

Moves merge enforcement from the old `auto-merge.yml` workflow (retired
2026-09-29) into GitHub, which checks these
rules at the moment of merge. Do not use native auto-merge (`gh pr merge --auto`). Markdown-only PRs
are left to the canonical document merger. A code/workflow PR merges with a normal squash pinned to the
verified head, `gh pr merge <n> --squash --match-head-commit <head-sha>`, once Codex has reviewed that
head, no P0/P1 is open, any P2/P3 findings have had their one fix round (leftovers go to a follow-up
issue) and CI is green.

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
- This ruleset lists org admins as bypass actors, but that no longer opens a way round the gate: the org
  ruleset `aaa-agency-delivery-gate` (`24516917`) requires `agency-delivery/gate` on every default branch with
  no bypass actors. Never use `gh pr merge --admin`; merge as described at the top of this section.

**Create it only after** `review-verdict.yml` is on `main` in every targeted
repo and has been seen publishing on real PRs. Team has no dry-run mode: a
required check a repo never produces blocks every PR there immediately.

```bash
gh api -X POST orgs/Automation-Architecture/rulesets --input rulesets/aaa-merge-gates.json
```

Rollback: disable enforcement.

```bash
gh api -X PUT orgs/Automation-Architecture/rulesets/23649946 -f enforcement=disabled
```

There is no workflow gate to fall back to: `auto-merge.yml` was retired
org-wide on 2026-09-29 and is gone from both targeted repos. With enforcement
disabled, `review/verdict` still publishes but nothing requires it, so code PRs
in the targeted repos lose their merge gate until the ruleset is re-enabled.
Markdown-only PRs are unaffected (`doc-auto-merge.yml`).

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
