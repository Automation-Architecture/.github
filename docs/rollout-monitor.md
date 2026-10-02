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
