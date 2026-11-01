# Organization delivery rules

`aaa-delivery-v3.json` is the GAAA-4198 target: every default branch, PR required,
squash only, no force-push/deletion, zero human approvals, resolved conversations,
strict up-to-date status checks and `agency-delivery/gate` pinned to App5021608.
The App checks current-head CI, a linked Linear card, risk disclosure and the
configured two-round reviewer flow. These are three logical requirements in
one trusted check; do not require nonexistent org-wide `tests` or Linear check names.
Keep stronger repository-specific CI rules.

After review, create/read back the new org rule, then disable the superseded
`aaa-default-branch-protection`16125109. The old gate24516917 and merge23649946
are already disabled; leave their definitions available for historical audit.
Never create a gap in default-branch PR/non-force/deletion protection.
Enable native auto-merge settings only after protection is active. The setting
allows authors to opt in; it does not arm any existing PR. Preserve explicit holds.
Enrolled agent tasks still use the stricter delivery CLI and never native auto-merge.

Rollback: restore the saved pre-cutover org definitions/settings deliberately;
do not disable the new rule before restoring equivalent PR/non-force/deletion
protection. Disabling the gate removes mandatory CI/review/Linear enforcement.
The audit reports disabled or weakened enforcement once at organization level.

The four central docs auto-mergers are archived under `archives/gate-v3-2026-11-01`.
Docs-only changes skip review under the hosted policy, but still need CI and Linear.
