# PR-factory labels

GAAA-4198, updated for gate policy v3.1 (2026-11-03: Codex only, merges by hand).
All active repos (and any other repo that uses the org stale janitor) should have
these labels. They are ordinary
GitHub labels; creating them does not enable repository settings.

| Label | Purpose |
|---|---|
| `review:codex` | Starts the gate's review. Codex is the only reviewer, with no fallback; `review:claude`, `review:greptile` and `review:coderabbit` no longer choose a reviewer. |
| `auto-merge-ok` | Retired: native auto-merge is not used (merges are done by hand), so this label has no effect. |
| `do-not-stale` | Exempt from [pr-stale.yml](../.github/workflows/pr-stale.yml) (keep open). |
| `security` | Exempt from stale/close. |
| `client-bug` | GitHub PR label for a fix to a **bug a client reported** (which cards count: [software lifecycle](https://github.com/Automation-Architecture/aaa-SOP/blob/main/software-lifecycle.md)). **Exempt** from stale/close while labeled (not a longer window). The fix may wait on the customer; auto-closing it would drop paid work. Remove the label when the wait is over. This is not the Linear `type` label. |

The `client-bug` PR label does not fast-track a merge: every PR is merged by hand
once Codex and `agency-delivery/gate` pass. The client fast lane is defined in aaa-SOP
[software lifecycle](https://github.com/Automation-Architecture/aaa-SOP/blob/main/software-lifecycle.md) and applies
whatever labels the PR carries.

## Create on allowlisted repos

Dry-run (default), then apply:

```bash
scripts/ensure-pr-labels.sh
scripts/ensure-pr-labels.sh --apply
```

Or one repo:

```bash
gh label create review:codex --repo Automation-Architecture/<repo> --color 5319e7 --description "Starts gate review; Codex is the only reviewer" --force
gh label create do-not-stale --repo Automation-Architecture/<repo> --color 5319e7 --description "Exempt from org PR stale/close" --force
gh label create client-bug --repo Automation-Architecture/<repo> --color d73a4a --description "Fix for a client-reported bug; exempt from stale/close" --force
gh label create security --repo Automation-Architecture/<repo> --color b60205 --description "Security work; exempt from org PR stale/close" --force
```

`--force` updates color/description if the label already exists. The helper
script never patches `allow_auto_merge`.
