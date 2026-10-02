# Shared workflow guidance

Follow the [organization merge policy](https://github.com/Automation-Architecture/aaa-runbooks/blob/main/reference/merge-gate-target-system.md).
No human approving review is required. Code/workflow changes need completed
Codex review on the current head, no open P0/P1 finding (unbadged counts as P1),
and successful CI. P2/P3 receive one fix round, then a follow-up for anything left.
Do not invite or wait for Greptile. Preserve another person's draft/merge hold.
Use normal squash merge pinned to the verified head; never admin bypass or
native auto-merge. Markdown-only PRs use the canonical document merger.

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
