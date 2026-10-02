"""Read-only rollout report; not a merge eligibility or fleet coverage gate."""
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import quote, urlparse

PATTERNS = {
    "feat/dashboard-sync-workflow": ".github/workflows/dashboard-sync.yml",
    "feat/sentry-release-workflow": ".github/workflows/sentry-release.yml",
    "feat/review-verdict-workflow": ".github/workflows/review-verdict.yml",
}
ROOT = Path(__file__).resolve().parents[1]


class ScanError(Exception):
    pass


def api(endpoint, paginated=False):
    # Explicit GET on every call; no write or merge wrapper exists.
    args = ["gh", "api", "--method", "GET", endpoint]
    if paginated:
        args += ["--paginate", "--slurp"]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=90)
        if result.returncode:
            raise ScanError("GitHub API read failed")
        return json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        # Never echo arbitrary responses, credentials or stderr into the report.
        raise ScanError("API failed, timed out, or returned malformed JSON") from exc


def discover(org, branch, read):
    query = quote(f"org:{org} is:pr is:open head:{branch}", safe="")
    pages = read(f"search/issues?q={query}&per_page=100", paginated=True)
    if not isinstance(pages, list) or not pages:
        raise ScanError("Search did not return a complete page set")
    items, totals = [], []
    for page in pages:
        if (not isinstance(page, dict) or page.get("incomplete_results") is not False
                or type(page.get("total_count")) is not int
                or not isinstance(page.get("items"), list)):
            raise ScanError("Search is malformed or incomplete")
        totals.append(page["total_count"])
        items.extend(page["items"])
    if len(set(totals)) != 1 or totals[0] > 1000 or len(items) != totals[0]:
        raise ScanError("Search coverage changed, was truncated, or exceeds its bound")
    found = set()
    for item in items:
        try:
            parsed = urlparse(item["repository_url"])
            parts = parsed.path.strip("/").split("/")
            number = item["number"]
            if (parsed.scheme != "https" or parsed.netloc != "api.github.com"
                    or len(parts) != 3 or parts[0] != "repos"
                    or parts[1].casefold() != org.casefold()
                    or not re.fullmatch(r"[A-Za-z0-9_.-]+", parts[2])
                    or type(number) is not int or number < 1
                    or "pull_request" not in item):
                raise ValueError()
            found.add((f"{parts[1]}/{parts[2]}", number))
        except (KeyError, TypeError, ValueError) as exc:
            raise ScanError("Invalid PR identity in search") from exc
    if len(found) != len(items):
        raise ScanError("Duplicate candidates; coverage is unreliable")
    return sorted(found)


def scan(org, root=ROOT, read=api, patterns=PATTERNS):
    rows, errors, seen = [], [], set()
    for branch, path in patterns.items():
        try:
            expected = (root / path).read_bytes()
            candidates = discover(org, branch, read)
        except (OSError, ScanError):
            errors.append(f"{branch}: canonical read or complete search failed")
            continue
        for repo, number in candidates:
            try:
                pr = read(f"repos/{repo}/pulls/{number}")
                if pr["state"] != "open" or pr["head"]["ref"] != branch:
                    continue  # Search is fuzzy and can lag closed PRs.
                head = pr["head"]["sha"]
                if not re.fullmatch(r"[0-9a-f]{40}", head):
                    raise ScanError("Invalid head")
                if (repo, number) in seen:
                    continue
                seen.add((repo, number))
                file = read(f"repos/{repo}/contents/{path}?ref={head}")
                if file.get("encoding") != "base64":
                    raise ScanError("Content unavailable")
                actual = base64.b64decode("".join(file["content"].split()), validate=True)
                current = read(f"repos/{repo}/pulls/{number}")
                if (current["head"]["sha"] != head or current["state"] != "open"
                        or current["head"]["ref"] != branch):
                    raise ScanError("Candidate moved during scan")
                rows.append({"repo": repo, "number": number, "head": head,
                             "draft": bool(pr["draft"]), "drift": actual != expected})
            except (ScanError, KeyError, TypeError, ValueError, AttributeError):
                errors.append(f"{repo}#{number}: candidate/content read failed or head changed")
    return rows, errors


def report(rows, errors):
    lines = ["# Rollout PR report", "",
             "Read-only: no branches, reviews, labels, PRs, or protections changed.",
             "Scope: three legacy rollout branch patterns, NOT agency-wide drift coverage.",
             "Not merge readiness: use current-head Codex, CI and protected delivery.", ""]
    for row in rows:
        link = f"https://github.com/{row['repo']}/pull/{row['number']}"
        state = "draft; preserve hold" if row["draft"] else "needs protected delivery evaluation"
        drift = "YES — reviewed resync needed" if row["drift"] else "none"
        lines.append(f"- [{row['repo']}#{row['number']}]({link}) at `{row['head']}`: {state}; canonical drift: {drift}.")
    if errors:
        lines += ["", "## INCOMPLETE — operator action required", ""]
        lines += [f"- {error}" for error in errors]
    else:
        lines += ["", f"Pattern scan complete: {len(rows)} matching open rollout PR(s)."]
    return "\n".join(lines) + "\n"


def main():
    org = os.environ.get("ORG", "Automation-Architecture")
    if not re.fullmatch(r"[A-Za-z0-9-]+", org):
        raise ScanError("Invalid organization")
    rows, errors = scan(org)
    output = report(rows, errors)
    print(output, end="")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write(output)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
