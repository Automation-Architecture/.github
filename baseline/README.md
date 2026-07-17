# Secret-scan baselines

One file per repo: `<repo>.json`. Each holds gitleaks findings that were
**individually triaged and confirmed benign** on 2026-07-17, so the nightly
`org-secret-scan` stops reporting them.

## Why this exists

The first full run found **268 findings across 35 repos**. Almost all were noise
(vendor documentation mirrors, `YOUR_API_KEY` placeholders, fabricated test
fixtures, public-by-design values like Supabase `anon` keys and Stripe `pk_`
publishable keys). A control that reports 268 things — of which 9 matter — gets
muted within a week, and then it protects nothing. Baselining the confirmed-benign
findings is what keeps the signal readable.

## How it works (and why it's safe)

- Matching is **commit-pinned**: a fingerprint is `commit:file:rule:line`. Git
  history is immutable, so a baselined finding stays quiet forever — but the
  same secret re-committed in a NEW commit produces a NEW fingerprint and
  **still fires**. Baselining does not create a blind spot for new leaks.
- Files are **redacted** (`Secret` / `Match` are `REDACTED`). Verified: gitleaks
  matches redacted baselines against a `--redact` scan, so these commit safely.
- A repo with no baseline file is simply scanned with no baseline.

## What is deliberately NOT here

The 9 findings triaged **REAL** are not baselined — they must be rotated. Nor did
we blanket-allowlist `tests/` or `docs/`: the org's two worst real leaks lived in
committed `.md` docs, so path-based suppression would recreate exactly the
blindness this scanner exists to remove. Every entry here was judged on the
**value**, not its path.

## Maintaining it

- **A finding is a false positive →** re-triage it, then add that finding object
  to the repo's file (copy it verbatim from the run's `gitleaks-reports`
  artifact). Prefer fixing the *config* (`../.gitleaks.toml`) when the noise is
  categorical rather than a one-off.
- **A baselined finding turns out to be real →** delete it from the file and
  rotate the credential.
- **Never** add a finding here to silence something you haven't actually looked at.
