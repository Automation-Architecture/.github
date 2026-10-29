# AAA Alerts (Slack app)

The bot that posts monitoring alerts into Slack for Automation-Architecture
automations, replacing per-channel incoming webhooks (Brad, 2026-10-29). One
bot token works for any public channel: the bot joins with
`conversations.join`, then posts with `chat.postMessage`. No human step per
channel.

- App ID `A0CD4RDRBQS`, workspace Automation Architects (`T091Z8KB068`), bot
  user `aaa_alerts`.
- Bot scopes: `chat:write`, `channels:join` (see `manifest.json`). It cannot
  read messages or list channels; pass a channel ID (Slack search finds it).
- Token: 1Password `aaa-APIs` / `Slack Bot - AAA Alerts` (field `credential`),
  set in a repo with
  `v=$(op read 'op://aaa-APIs/Slack Bot - AAA Alerts/credential') && [ -n "$v" ] && printf '%s' "$v" | gh secret set SLACK_ALERTS_BOT_TOKEN -R <owner>/<repo>`.

## Changing the app

Edit `manifest.json`, then from this folder:

```bash
slack app install --team T091Z8KB068 -E deployed   # applies the manifest
slack deploy --team T091Z8KB068                     # runs store-token.sh
```

`store-token.sh` (the deploy hook) stores the bot token in 1Password on first
deploy and never prints it; it leaves an existing item alone. Adding a scope
needs a reinstall, which the first command does.

Used by: `.github` monitor-health (#dev-ops).
