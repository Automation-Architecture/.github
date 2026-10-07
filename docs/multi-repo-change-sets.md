# Multi-repo change-sets

GAAA-3961: run a factory, not a parking lot. A change that would otherwise
open the same PR in N repositories is a **change-set**, not N orphan pull
requests.

## Prefer one shared change

This repository is the canonical source for copied review/document/request
workflows. Prefer:

1. A reviewed PR here in `.github` (org-wide workflow, reusable workflow, or
   shared action).
2. A [reusable workflow](https://docs.github.com/en/actions/sharing-automations/reusing-workflows)
   under `.github/workflows/` that other repos `uses:` — one definition, N
   callers, no fanout.
3. Only then a per-repo fanout, and only when the shared path cannot express
   the change.

Do not open N copies of the same workflow edit when one central file already
covers the org (the auto-merge setting guard, org secret scan, PR stale
janitor, and the central sweeps are this kind of file).

## Fanout rule (N/N)

When a fanout is unavoidable:

1. **One Linear tracking issue** for the whole program. The issue body is a
   repo checklist (one box per repository).
2. **Open the PRs only on a day when native auto-merge will be enabled** on
   those allowlisted repos (see [auto-merge-allowlist.txt](auto-merge-allowlist.txt))
   so each PR can merge the same day after Codex + `agency-delivery/gate`.
   Do not park a fanout over a weekend.
3. **Program Done = N/N** — every box is merged, or that repo's PR is
   explicitly abandoned on the tracking issue (reason in the comment). A
   leftover draft is not Done.

Residual closeout of an older fanout uses the same checklist: merge, replace,
or abandon each sibling under the WIP/age SLA (ready PRs within 48h; escalate
past 7d; drafts follow [pr-stale.yml](../.github/workflows/pr-stale.yml)).

## What this is not

- Not an epic PR as the delivery unit.
- Not Brad as the sole merger for hygiene.
- Not a new Autopilot-class GitHub listener.
- Not a change to org rulesets (the App Gate already requires
  `agency-delivery/gate`).
