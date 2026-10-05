#!/usr/bin/env python3
"""Classify a pull request as code or docs/workflow-only. See action.yml.

Skippable: a `.md`/`.markdown` file, or a file under `.github/` other than
`.github/actions/**` and the workflow file running this job. A file matching a
CODE_PATHS glob is never skippable. A rename counts both its names. Anything
uncertain (not a pull request, API failure, the 3,000-file API cap) is code.
"""
import json
import os
import re
import subprocess
import sys

FILE_CAP = 3000  # GET /pulls/{n}/files returns at most this many


def glob_to_regex(glob):
    """`**` spans directories, `*` and `?` stay inside one path segment."""
    out = []
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def own_workflow():
    """`owner/repo/.github/workflows/ci.yml@refs/...` -> `.github/workflows/ci.yml`."""
    ref = os.environ.get("GITHUB_WORKFLOW_REF", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    path = ref.split("@", 1)[0]
    if repo and path.startswith(repo + "/"):
        return path[len(repo) + 1:]
    return None


def skippable(path, code_globs, own):
    if path == own or any(g.match(path) for g in code_globs):
        return False
    if re.search(r"\.(md|markdown)\Z", path, re.IGNORECASE):
        return True
    return path.startswith(".github/") and not path.startswith(".github/actions/")


def classify(event, files, code_globs, own):
    """Return (code, reason). `files` is the API list, or None if it failed."""
    if event not in ("pull_request", "pull_request_target"):
        return True, f"event {event or 'unknown'} always runs in full"
    if files is None:
        return True, "could not list the pull request's files; running in full"
    if len(files) == 0 or len(files) >= FILE_CAP:
        return True, f"{len(files)} changed files reported; running in full"
    for f in files:
        for path in (f.get("filename"), f.get("previous_filename")):
            if path is None:
                continue
            if not skippable(path, code_globs, own):
                return True, f"{path} is code"
    return False, f"all {len(files)} changed files are docs or other workflows"


def list_files():
    number = os.environ.get("PR_NUMBER", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not number.isdigit() or not repo:
        return None
    try:
        out = subprocess.run(
            ["gh", "api", "--paginate", f"repos/{repo}/pulls/{number}/files?per_page=100",
             "--jq", ".[] | {filename, previous_filename}"],
            check=True, capture_output=True, text=True, timeout=120,
        ).stdout
        return [json.loads(line) for line in out.splitlines() if line.strip()]
    except (subprocess.SubprocessError, OSError, ValueError) as err:
        detail = getattr(err, "stderr", "") or str(err)
        print(f"::warning title=change-scope::listing PR files failed: {detail.strip()[:300]}")
        return None


def main():
    code_globs = [glob_to_regex(g.strip()) for g in os.environ.get("CODE_PATHS", "").splitlines()
                  if g.strip() and not g.strip().startswith("#")]
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    files = list_files() if event in ("pull_request", "pull_request_target") else []
    code, reason = classify(event, files, code_globs, own_workflow())
    # Filenames come from the PR: drop control characters (a newline could
    # inject a second `code=` line into GITHUB_OUTPUT) and write `code` last,
    # so nothing in `reason` can override it.
    reason = "".join(ch if ch.isprintable() else "?" for ch in reason)[:300]
    print(f"change-scope: code={str(code).lower()} ({reason})")
    if not code:
        print("::notice title=change-scope::Docs/workflow-only change: heavy steps skipped, check reports success.")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
        out.write(f"reason={reason}\ncode={str(code).lower()}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
