# Security policy

This file lives in the org `.github` repo, so it is the default security policy
for every repository in **Automation-Architecture** that doesn't define its own.

## Handling secrets

- **Never commit a secret value** — not to code, and (the way we've actually
  leaked before) not to Markdown docs, deploy checklists, or session transcripts.
  Reference the 1Password item instead, e.g. `op://Vault/Item/field`. This is the
  pattern `opportunity-builder` already follows.
- Real values live only in the platform's secret store (Vercel env vars, Railway
  variables, AWS Secrets Manager) and 1Password — never in git.
- `.env.example` / `.env.sample` hold placeholders only; the real `.env` stays
  git-ignored.

## If a secret is exposed

**Rotate first, scrub second.** Rotating the live credential is what actually
closes the hole — once a value has been committed or pasted anywhere, treat it as
burned, because history-rewriting never reaches copies people already cloned.

1. Rotate the credential at its source (Stripe, Salesforce, Supabase, AWS IAM, …).
2. Update the secret store (Vercel / Railway / AWS SM) and the 1Password item.
3. Only then scrub git history (`git filter-repo` or BFG) and force-push.

## Automated scanning

- **Org-wide, nightly:** [`org-secret-scan.yml`](./.github/workflows/org-secret-scan.yml)
  scans every non-archived repo's full history with [gitleaks](https://github.com/gitleaks/gitleaks)
  and opens a tracking issue here on any finding. Run it on demand from the
  Actions tab (`workflow_dispatch`), optionally against a single repo.
- **Per-repo PR gate (opt-in):** add the **Secret scan (gitleaks)** starter
  workflow from a repo's *Actions → New workflow* page (it calls the reusable
  [`gitleaks.yml`](./.github/workflows/gitleaks.yml)).
- **Local prevention:** the gitleaks pre-commit guard in `aaa-hooks` blocks a
  commit that stages a secret before it ever reaches GitHub.

## Reporting

Found a vulnerability or an exposed secret? Contact **brad@automationarchitecture.ai**
directly — do not open a public issue that references the secret value.
