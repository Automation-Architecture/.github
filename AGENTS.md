# Shared workflow guidance

Follow the [organization merge policy](https://github.com/Automation-Architecture/aaa-runbooks/blob/main/reference/merge-gate-target-system.md).
No human approving review is required. The hosted gate (policy v3.1, live
2026-11-03) asks Codex only, started by the `review:codex` label, with no fallback
and at most two rounds, and requires current-head CI, Linear and risk disclosure.
It blocks P0/P1; repository rules also require resolved conversations and a branch
up to date with its base. If Codex is unavailable the gate blocks with
`REVIEWERS_EXHAUSTED` until the label is re-added.
Preserve another person's draft or merge hold. Use normal squash merges pinned
to verified evidence; never admin bypass. Enrolled tasks have the stricter
Codex/P2/browser contract below.
Merges are done by hand (CEO, 2026-11-03): native auto-merge is not used on any
repo, and the gate blocks an armed one with `AUTO_MERGE_ENABLED`. Only the PR's
owner merges it: every PR carries an `Owner:` line (the session that opened it,
from `aaa-owner`), and a passing gate is not permission to merge someone else's
PR. `aaa-merge` refuses (exit 5) when the line names another session. Risky paths require a useful
Risk line, not another human approval. Do not add Autopilot-class listeners.
Explicitly enrolled coding-agent tasks use the stricter
[authorized task delivery contract](docs/agent-delivery.md) (GAAA-4596): current-head
Codex review, zero unresolved P0/P1/P2/conversations, required browser evidence and
App-pinned branch enforcement. This is a local continuation adapter, not a new
GitHub listener or a change to the native auto-merge allowlist.
Docs-only PRs use the same App gate and skip review only under its narrow docs rule.

Multi-repo fanouts are [change-sets](docs/multi-repo-change-sets.md): prefer a
shared `.github` / reusable workflow over N orphan PRs; any fanout needs one
Linear tracking issue with an N/N repo checklist and is Done only when every
PR is merged or explicitly abandoned. Required labels:
[pr-factory-labels.md](docs/pr-factory-labels.md).

This repository is the canonical source for copied review/document/request
workflows. Change their behavior here through a reviewed PR, not in downstream
copies. Inspect live rulesets and check producers before changing enforcement;
an emitted App Gate is not proof that GitHub requires it. Keep security,
deployment and independent-review coverage intact.

The rollout monitor is read-only. See [its contract](docs/rollout-monitor.md).
Do not restore its retired branch writer/admin-merger or treat its three-pattern
report as fleet-wide coverage. Complete GAAA-983's staged control, ownership,
negative/clean validation and old-ref/credential checks before declaring this
repository's protected cutover complete.
