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
  "auto-merge-ok|0e8a16|Hygiene/fast-track: native auto-merge ok after Codex+gate"
  "do-not-stale|5319e7|Exempt from org PR stale/close"
  "client-bug|d73a4a|Client-facing bug; exempt from stale; auto-merge fast-track"
  "security|b60205|Security work; exempt from org PR stale/close"
)

repos=()
while IFS= read -r raw || [ -n "$raw" ]; do
  line="${raw#"${raw%%[![:space:]]*}"}"
  line="${line%"${line##*[![:space:]]}"}"
  [ -z "$line" ] && continue
  [ "${line:0:1}" = "#" ] && continue
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
exit "$failed"
