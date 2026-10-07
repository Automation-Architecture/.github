# PR-factory labels

GAAA-3961. Allowlisted repos (and any other repo that uses the org stale
janitor or native auto-merge) should have these labels. They are ordinary
GitHub labels; creating them does not enable repository settings.

| Label | Purpose |
|---|---|
| `auto-merge-ok` | Hygiene / fast-track: native auto-merge may arm after Codex + `agency-delivery/gate` on an [allowlisted](auto-merge-allowlist.txt) repo. |
| `do-not-stale` | Exempt from [pr-stale.yml](../.github/workflows/pr-stale.yml) (keep open). |
| `security` | Exempt from stale/close. |
| `client-bug` | Client-facing bug; **exempt** from stale/close while labeled (not a longer window). A client bug may wait on the customer; auto-closing it would drop paid work. Remove the label when the wait is over. |

`client-bug` is also the fast-track label in [AGENTS.md](../AGENTS.md): on an
allowlisted repo, after Codex + gate, native auto-merge may proceed.

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
