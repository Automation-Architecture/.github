# Shared workflow guidance

Follow the [organization merge policy](https://github.com/Automation-Architecture/aaa-runbooks/blob/main/reference/merge-gate-target-system.md).
No human approving review is required. Code/workflow changes need completed
Codex review on the current head, no open P0/P1 finding (unbadged counts as P1),
and successful CI (`agency-delivery/gate`). P2/P3 receive one fix round, then a
follow-up for anything left.
Do not invite or wait for Greptile. Preserve another person's draft/merge hold.
Use normal squash merge pinned to the verified head; never admin bypass.
Native auto-merge is allowed on [allowlisted](docs/auto-merge-allowlist.txt)
active repos for hygiene, label `auto-merge-ok`, and client-bug fast-track after
that Codex+gate bar (GAAA-3961). Brad veto still applies for secrets, billing,
destroy, and named production risk. Do not add Autopilot-class GitHub listeners.
Markdown-only PRs use the canonical document merger.

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
