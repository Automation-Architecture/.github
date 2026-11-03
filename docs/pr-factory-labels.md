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
| `client-bug` | Client-facing bug; **exempt** from stale/close while labeled (not a longer window). A client bug may wait on the customer; auto-closing it would drop paid work. Remove the label when the wait is over. |

`client-bug` no longer fast-tracks a merge: every PR is merged by hand once Codex
and `agency-delivery/gate` pass.

## Create on allowlisted repos

Dry-run (default), then apply:

```bash
scripts/ensure-pr-labels.sh
scripts/ensure-pr-labels.sh --apply
```

Or one repo:

```bash
gh label create auto-merge-ok --repo Automation-Architecture/<repo> --color 0e8a16 --description "Hygiene/fast-track: native auto-merge ok after Codex+gate" --force
gh label create do-not-stale --repo Automation-Architecture/<repo> --color 5319e7 --description "Exempt from org PR stale/close" --force
gh label create client-bug --repo Automation-Architecture/<repo> --color d73a4a --description "Client-facing bug; exempt from stale; auto-merge fast-track" --force
gh label create security --repo Automation-Architecture/<repo> --color b60205 --description "Security work; exempt from org PR stale/close" --force
```

`--force` updates color/description if the label already exists. The helper
script never patches `allow_auto_merge`.
