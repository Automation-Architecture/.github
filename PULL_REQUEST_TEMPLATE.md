## Change

Describe the problem and resulting behavior.

Linear: <!-- GAAA-1234 or linked card -->

Owner: <!-- the session that opened this PR: run `aaa-owner` (e.g. claude-code:2af64eae or codex:019a2b3c); a person writes their name. Only the owner merges this PR. -->

## Reviewer

Add the label `review:codex`. Codex is the only reviewer, with no fallback. If Codex posts an availability notice or is silent for about 20 minutes, the gate blocks with `REVIEWERS_EXHAUSTED`: remove and re-add `review:codex` to try again.

## Validation

List tests and acceptance evidence for this head.

Risk: <!-- What could break and how to roll back. Required for risky paths (auth, permissions, billing, database migrations, gate config), e.g. "adds a nullable column to orders; no backfill; roll back by dropping it". -->

## Merge

<!-- Only the PR's Owner merges it; aaa-merge refuses otherwise. Merges are done by hand; never turn on auto-merge. Once agency-delivery/gate passes, every conversation is resolved and the branch is up to date with its base: gh pr merge <number> --squash --match-head-commit <head sha> -->
