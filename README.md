# .github
About the Agency

Shared workflow delivery follows [AGENTS.md](AGENTS.md) and the organization
merge policy. Native auto-merge is allowlisted in
[docs/auto-merge-allowlist.txt](docs/auto-merge-allowlist.txt) (GAAA-3961).
Multi-repo work is a [change-set](docs/multi-repo-change-sets.md). PR labels:
[docs/pr-factory-labels.md](docs/pr-factory-labels.md). The
[rollout monitor](docs/rollout-monitor.md) is read-only; canonical differences
are delivered through reviewed pull requests.

[`actions/change-scope`](actions/change-scope/action.yml) lets a gate-required CI
job skip its heavy steps on a docs-only or workflow-only pull request while still
reporting success (Actions cost lever 1).
