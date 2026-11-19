# Authorized task delivery (GAAA-4596)

**There is no separate rule set for authorized or enrolled agent tasks** (Brad,
2026-11-19: approved "keep one standard"). They follow the same delivery policy as
all other work, written once in aaa-SOP:

- [PR review and merge SOP](https://github.com/Automation-Architecture/aaa-SOP/blob/main/pr-review-and-merge-sop.md):
  the gate, two Codex rounds, findings and the follow-up card, the `Risk:` line, merging.
- [Role-separated delivery](https://github.com/Automation-Architecture/aaa-SOP/blob/main/role-separated-delivery.md):
  Builder, Verifier and Merger, and both delivery paths (River and normal development).

This page used to add stricter rules for these tasks: every P2 finding fixed before
merge, a fresh Codex review after every fix, and headed Playwright and CDP evidence
from the working agent. Those no longer apply. P2 findings left after round two go
to the follow-up Linear card, as for any PR, and browser evidence for UI work is the
Verifier's job. The old contract, including its 2026-11-01 enforcement record, is in
this file's git history.

## The delivery CLI

[aaa-hooks `delivery/`](https://github.com/Automation-Architecture/aaa-hooks/tree/main/delivery)
is a convenience tool for the Builder, not a policy. It keeps an enrolled Codex task
moving from a verified commit through a published PR and Codex review, then hands the
green PR to a separate Merger. It never merges the PR itself. How to install, hold,
cancel and uninstall it is in that folder's README.
