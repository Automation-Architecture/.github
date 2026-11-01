# Authorized task delivery (GAAA-4596)

Brad requested a shared path from a completed build through complete PR publication,
Codex and browser review, repairs and eligible merge, without repeated handoff prompts.
This is the policy for explicitly enrolled coding-agent tasks. The implementation is
[aaa-hooks delivery](https://github.com/Automation-Architecture/aaa-hooks/tree/main/delivery)
([implementation PR #80](https://github.com/Automation-Architecture/aaa-hooks/pull/80)).

## Ownership and limits

The working agent owns implementation, acceptance, scoped repairs and Linear updates.
Codex SessionStart supplies enrollment instructions; Stop continues an enrolled task;
Interrupt persists a hold. A private, locked local state file records the worktree,
session, enrollment generation, verified head, PR, packet revision and blockers.
Closing Codex pauses this local runner. It does not wake idle sessions or create a
hosted repair service. One-time native `/hooks` trust is required; do not bypass it.

This path uses a normal expected-head squash merge after fresh evidence checks. It
does not enable GitHub native auto-merge, change its allowlist, create a new GitHub
listener, or revive PR Autopilot. The existing ready/open Codex integration and central
review request sweep remain in place. A fallback request is deduplicated by published
packet revision and current head and requires a user-authenticated GitHub identity.

## Readiness contract

- Verify the committed candidate with relevant build/tests and testable acceptance criteria.
- Publish the problem, resulting behavior, Linear link, criteria, actual checks/results,
  dependencies, rollout risks and a Mermaid diagram where useful. Keep a draft until
  required browser artifacts are available. Preserve human descriptions/holds.
- Frontend acceptance uses headed Playwright and Chrome DevTools Protocol diagnostics,
  with journey assertions, applicable viewports and keyboard paths. Record mock/real
  backend provenance. Publish screenshots/traces from synthetic data. CI reports must
  name the tested head and fresh run nonce; missing capability or evidence holds the task.
- Codex must complete on the current head after the latest packet publication. Fix
  P0/P1/P2 findings, verify again and obtain fresh review. For these enrolled tasks,
  the older one-fix-round-and-defer-P2 policy does **not** permit merge. All unresolved
  review conversations and outstanding change requests conservatively hold merge.
- Required CI and the App-owned `agency-delivery/gate` must succeed. Read live branch
  rules; emitting the gate is insufficient. Pin the required gate to App 5021608
  and require strict up-to-date status checks plus conversation resolution so
  GitHub blocks stale bases and open threads at merge time.
  Optional pending/red checks also hold. Respect fork/queue limitations and dependency
  order; a merged backend is not evidence of a successful runtime deployment.
- Respect draft/label/user holds. Twelve automatic continuations or two hours creates
  a visible hold; record the concrete blocker in Linear. Resume explicitly after a
  genuine interruption. Do not ask Brad to repeat routine task authorization.
- After merge, update Linear and distinguish merged, deployed and verified live.

The hosted gate remains policy v2.17: it accepts one configured reviewer and blocks
P0/P1. The enrolled-task CLI additionally enforces the stricter Codex, packet freshness,
P2/conversation and browser requirements. A successful hosted check alone never
satisfies this task-delivery contract.

## Pilot enforcement record — 2026-11-01 UTC

The org rule `aaa-agency-delivery-gate` (24516917) was **disabled** when inspected;
its last update was 2026-10-16. This rollout preserves that org-wide setting and adds
only repository-scoped pilot requirements. Existing PR/non-force/deletion rules remain.

Production `/health` reports v2.17 and SHA-256
`7e6a346c2008a66000205139fe4950e858b53a401aa7db47dc74d73273033c12`, matching
the tracked production policy. No Worker deployment is part of this rollout.

| Repository | Active ruleset | Required checks (producer) |
| --- | --- | --- |
| aaa-hooks | 26295192 | agency-delivery/gate (5021608); hook tests, shellcheck, delivery browser (GitHub Actions 15368) |
| aios-iq-app | 26295079 | agency-delivery/gate (5021608); Typecheck + Test + Build, E2E (GitHub Actions 15368) |
| aios-cdp | 26295081 | agency-delivery/gate (5021608); Typecheck (all packages), Test aiosiq, Test iq, Build cdp-api, Build cdp-worker (GitHub Actions 15368) |

All three rulesets target the default branch, require review conversation resolution
with zero human approving reviews, have no bypass actors and were read back
through both the ruleset API and effective `rules/branches/main` API. Status checks
are strict against base movement. The CLI invalidates its verification when it
observes a new base; GitHub also rejects an outdated branch atomically if the base
moves after that observation. Reconcile, commit, reverify and publish for fresh
review. The PR REST base SHA can lag the live tip and is not an atomic base lock.

Before activation, real App-owned blocked/clean evidence existed on the same tested
head (base revisions differ and are encoded in the App external IDs):

| PR/head | BLOCK check run | PASS check run |
| --- | --- | --- |
| IQ #100 / `9fcff29b6d8a2d7a47e459db3ab1f05df1fde38a` | 124712745696 | 124715203473 |
| CDP #490 / `1439ea6db31da873416343e32827090ba840ab01` | 124712658964 | 124715181963 |

For aaa-hooks, v2.17 check 124718661205 passed on #80 head
`7f3c378973ebeb189bb8f83b813e2e9365cbe22c`; check 124720557298 blocked head
`38029fae87bda5beafd72ec962a7ff7ecbed6741` before fresh review. The headed
browser check 124720122449 passed from Actions App 15368. This producer/evidence
inspection preceded ruleset 26295192 activation. The earlier App PASS could coexist
with P2 findings under v2.17; the new native conversation-resolution rule and
stricter CLI are both required for this pilot.

The implementation PR is the live delivery pilot; IQ/CDP use the completed GAAA-4552
pair for evidence replay. Replay is not a second merge or a new frontend feature:
current clean evidence passes the local policy with an explicitly simulated open PR;
P2 threads, missing CI, stale heads and an already-merged state block. Hook trust and
a new IQ/CDP task exercising the installed hooks are tracked separately on GAAA-4596.

## Disable and rollback

For a task, `hold --reason TEXT` stops continuation; `cancel` stops local delivery
without changing the PR. `python3 delivery/install.py --uninstall` removes only these
Codex hook definitions, preserving unrelated configuration. Existing sessions retain
state for inspection; restarting with an explicit resume is required after interrupt.

Repository enforcement can be rolled back independently by updating **only** the
pilot ruleset's enforcement to `disabled`, preserving its definition and every other
ruleset. Record the incident/change on GAAA-4596 and read back effective rules. The
CLI then refuses automatic merge because App enforcement is absent. Do not use an
administrator bypass or disable organization-wide protection as a recovery step.
