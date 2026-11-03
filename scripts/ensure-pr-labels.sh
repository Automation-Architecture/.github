#!/usr/bin/env bash
# Create (or update) the GAAA-3961 PR-factory labels on allowlisted repos.
# Default is dry-run. Pass --apply to call `gh label create --force`.
# Never patches repository settings (allow_auto_merge stays untouched).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ALLOWLIST="${ALLOWLIST_FILE:-$ROOT/docs/auto-merge-allowlist.txt}"
ORG="${ORG:-Automation-Architecture}"
APPLY=false
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=true ;;
    -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
    *) echo "usage: $0 [--apply]" >&2; exit 2 ;;
  esac
done

[ -f "$ALLOWLIST" ] || { echo "missing $ALLOWLIST" >&2; exit 1; }

labels=(
  # Gate policy v3.1 (2026-11-03): Codex is the only reviewer and merges are done
  # by hand, so review:claude/greptile/coderabbit and auto-merge-ok are no longer
  # published. Existing copies are left in place; they have no effect.
  "review:codex|5319e7|Starts gate review; Codex is the only reviewer"
  "do-not-stale|5319e7|Exempt from org PR stale/close"
  "client-bug|d73a4a|Client-facing bug; exempt from stale/close"
  "security|b60205|Security work; exempt from org PR stale/close"
)

repos=()
while IFS= read -r raw || [ -n "$raw" ]; do
  line="${raw#"${raw%%[![:space:]]*}"}"
  line="${line%"${line##*[![:space:]]}"}"
  [ -z "$line" ] && continue
  [ "${line:0:1}" = "#" ] && continue
  if [ "$line" = '~ALL' ]; then
    listed=$(gh repo list "$ORG" --no-archived --limit 1000 --json nameWithOwner --jq '.[].nameWithOwner')
    [ -n "$listed" ] || { echo "empty org listing" >&2; exit 1; }
    while IFS= read -r name; do repos+=("$name"); done <<<"$listed"
    continue
  fi
  if [[ "$line" == */* ]]; then
    repos+=("$line")
  else
    repos+=("$ORG/$line")
  fi
done < "$ALLOWLIST"

[ "${#repos[@]}" -gt 0 ] || { echo "allowlist is empty: $ALLOWLIST" >&2; exit 1; }

echo "ensure-pr-labels: ${#repos[@]} repos, ${#labels[@]} labels, apply=$APPLY"
failed=0
for repo in "${repos[@]}"; do
  for spec in "${labels[@]}"; do
    IFS='|' read -r name color desc <<<"$spec"
    if [ "$APPLY" != true ]; then
      echo "[dry-run] gh label create $name --repo $repo --color $color --force"
      continue
    fi
    if gh label create "$name" --repo "$repo" --color "$color" --description "$desc" --force >/dev/null; then
      echo "ok $repo $name"
    else
      echo "FAIL $repo $name" >&2
      failed=$((failed + 1))
    fi
  done
done
if [ "$failed" -gt 0 ]; then exit 1; fi
exit 0
