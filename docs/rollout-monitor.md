# Read-only rollout monitor

The existing hourly monitor reports open PRs on the three named legacy rollout
branches. It does not update branches, request reviews, alter holds, merge PRs,
or arm native auto-merge. Canonical differences require a reviewed PR and the
[organization merge policy](https://github.com/Automation-Architecture/aaa-runbooks/blob/main/reference/merge-gate-target-system.md).

Each run publishes its report in the Actions job summary. Search/API failures,
incomplete pagination, missing canonical/content files, and a moving PR head
make the scan visibly incomplete and fail the job. Zero candidates is a
successful **pattern scan**, not proof of agency-wide drift coverage. The
existing `AAA_ORG_TOKEN` supplies cross-repository reads; the script uses only
explicit GET calls. This change does not rotate or expand that credential.

Verification: `python3 -m unittest discover -s tests/rollout-monitor -v`.
Preview with `ORG=Automation-Architecture python3 scripts/rollout_monitor.py`
using GitHub CLI authentication; this performs no external writes.

Rollback: fix forward or disable the monitor if reporting fails. Do not restore
the former branch writer/admin-merger as routine rollback. Security, QA,
deployment, snapshot and independent-review workflows are unaffected.
Fleet-wide default-branch workflow/ruleset drift and notification coverage
remain separate acceptance work under GAAA-916/GAAA-965.

## Repository protection cutover

Brad Wilcox is the accountable `.github` repository owner (confirmed 2026-10-02);
Engineer implements the bounded cutover. Baseline main was
`b22f8e1114c78c254dc79e4d2af4f4f01de986d2`, with only organization ruleset
`16125109` required. The delivery App was emitting checks but was not required.

`rulesets/github-delivery-gate.json` is the reviewed desired repository ruleset,
not evidence that it is installed. It adds strict, producer-pinned checks for
App `5021608`'s `agency-delivery/gate` and Actions `15368`'s always-run
`rollout monitor tests`, with no bypass actors. It preserves the organization
PR/force-push/deletion protections. Activate only after both producers have
been observed on the exact candidate and Codex, required CI and clean/blocked
Gate outcomes are verified. Record the created ruleset ID and read it back.
Merge normally, pinned to that reviewed head, then verify the default-branch
monitor and test runs. No other repository's ruleset is changed by this file.

Protection rollback is a deliberate disable of only the newly created
repository ruleset, with Brad owning the decision and an explicit protection-gap
record; keep organization protection and check publishers intact. Do not
automatically restore an admin-merger. Restricting the shared credential for old
refs requires mapping its preserved consumers first; default-branch source
retirement alone does not prove old-ref credential isolation.
