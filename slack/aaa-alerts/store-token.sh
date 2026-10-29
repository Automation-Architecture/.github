#!/bin/sh
# Slack CLI deploy hook for the AAA Alerts app. The CLI runs it with the
# installed app's bot token in SLACK_BOT_TOKEN; this stores the token in
# 1Password (aaa-APIs / "Slack Bot - AAA Alerts") without printing it, putting
# it on a command line, or writing it to disk: jq reads it from the
# environment and op reads the item template from stdin.
set -eu
ITEM="Slack Bot - AAA Alerts"
[ -n "${SLACK_BOT_TOKEN:-}" ] || { echo "store-token: the CLI did not provide SLACK_BOT_TOKEN" >&2; exit 1; }
case "$SLACK_BOT_TOKEN" in xoxb-*) ;; *) echo "store-token: SLACK_BOT_TOKEN is not a bot token (xoxb-)" >&2; exit 1 ;; esac
if op item get "$ITEM" --vault aaa-APIs >/dev/null 2>&1; then
  echo "store-token: '$ITEM' already exists in aaa-APIs; not overwriting it" >&2
  exit 0
fi
op item template get "API Credential" \
  | jq --arg title "$ITEM" '
      .title = $title
      | .fields |= map(
          if .id == "credential" then .value = env.SLACK_BOT_TOKEN
          elif .id == "notesPlain" then .value = "Bot token of the AAA Alerts Slack app (bot scopes chat:write, channels:join; no token rotation). Used by GitHub Actions alerting via chat.postMessage. Created by the Slack CLI deploy hook in Automation-Architecture/.github slack/aaa-alerts."
          else . end)' \
  | op item create --vault aaa-APIs - >/dev/null
echo "store-token: bot token stored in 1Password (aaa-APIs / $ITEM)"
