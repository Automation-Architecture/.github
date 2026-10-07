#!/usr/bin/env python3
"""Offline tests for stale-snapshot-reaper.yml.

Extracts the job's one step script, puts a stub `gh` first on PATH that serves
canned `gh repo list` / `gh pr list` output and records every call, and runs
the script with bash. Checks that read failures turn the run red (never
"nothing to reap") and that the close behaviour is unchanged. Needs python3 +
PyYAML, bash and jq.

    uv run --with pyyaml python tests/stale-snapshot-reaper/test_stale_snapshot_reaper.py
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/stale-snapshot-reaper.yml"
ORG = "Automation-Architecture"

# Stub gh. Fixtures: "repos" (list of names, or "__error"), "prs" {repo: list
# | "__error" | "__raw" | "__empty" | any JSON}, "close_fail" [repo#num, ...]. Every call
# is appended to STATE_DIR/calls as JSON.
STUB = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
fx = json.load(open(os.environ["FIXTURES"]))
args = sys.argv[1:]
with open(os.path.join(os.environ["STATE_DIR"], "calls"), "a") as f:
    f.write(json.dumps(args) + "\n")
def opt(name):
    return args[args.index(name) + 1] if name in args else None
def fail(msg="gh: Server Error (HTTP 502)"):
    sys.stderr.write(msg + "\n"); sys.exit(1)
if args[:2] == ["repo", "list"]:
    v = fx["repos"]
    if v == "__error": fail()
    data = json.dumps([{"nameWithOwner": f"%s/{r}" % args[2]} for r in v])
    jq = opt("--jq")
    if jq:
        data = subprocess.run(["jq", "-r", jq], input=data, capture_output=True, text=True, check=True).stdout
    sys.stdout.write(data); sys.exit(0)
if args[:2] == ["pr", "list"]:
    v = fx["prs"].get(opt("--repo"), [])
    if v == "__error": fail()
    if v == "__raw": sys.stdout.write("<html>bad gateway</html>"); sys.exit(0)
    if v == "__empty": sys.exit(0)
    sys.stdout.write(json.dumps(v)); sys.exit(0)
if args[:2] == ["pr", "close"]:
    if f"{opt('--repo')}#{args[2]}" in fx.get("close_fail", []): fail("HTTP 403")
    sys.exit(0)
if args[:1] == ["api"] and opt("-X") == "DELETE":
    sys.exit(0)
fail("stub gh: unexpected call " + " ".join(args))
'''


def step_script():
    wf = yaml.safe_load(WORKFLOW.read_text())
    steps = wf["jobs"]["reap"]["steps"]
    assert len(steps) == 1, "the reaper job is expected to have one step"
    return steps[0]["run"]


def pr(n, title="chore: sync sprint-progress.json", author="aaa-dashboard-bot", created=None, branch=None):
    return {"number": n, "title": title, "author": {"login": author},
            "createdAt": created or f"2026-10-{n:02d}T12:00:00Z", "headRefName": branch or f"sync/{n}"}


def run(fx, dry_run="false"):
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        (tmp / "bin").mkdir()
        gh = tmp / "bin/gh"
        gh.write_text(STUB)
        gh.chmod(0o755)
        (tmp / "fx.json").write_text(json.dumps(fx))
        (tmp / "step.sh").write_text(step_script())
        work = tmp / "work"
        work.mkdir()
        env = dict(os.environ, PATH=f"{tmp / 'bin'}:{os.environ['PATH']}", FIXTURES=str(tmp / "fx.json"),
                   STATE_DIR=str(tmp), DRY_RUN=dry_run, GH_TOKEN="x")
        p = subprocess.run(["bash", str(tmp / "step.sh")], cwd=work, env=env, capture_output=True, text=True,
                           timeout=60)
        calls = [json.loads(l) for l in (tmp / "calls").read_text().splitlines()] if (tmp / "calls").exists() else []
        return p.returncode, p.stdout + p.stderr, calls


def closes(calls):
    return [f"{c[c.index('--repo') + 1]}#{c[2]}" for c in calls if c[:2] == ["pr", "close"]]


def deletes(calls):
    return [c[-1] for c in calls if c[:1] == ["api"] and "DELETE" in c]


R = f"{ORG}/a"
S = f"{ORG}/b"
FAMILY = [pr(1), pr(2), pr(3)]  # one family; #3 newest

CASES = []


def case(name, fx, *, ok, closed=(), says=(), not_says=(), dry_run="false", check=None):
    CASES.append(dict(name=name, fx=fx, ok=ok, closed=list(closed), says=says, not_says=not_says,
                      dry_run=dry_run, check=check))


# ── Close behaviour (unchanged) ─────────────────────────────────────────────
case("nothing open: green, no closes", {"repos": ["a", "b"], "prs": {}}, ok=True,
     says=("repos=2  unreadable=0  closed=0  failed=0", "(no actions)"))
case("family of 3: closes the 2 older, keeps the newest", {"repos": ["a"], "prs": {R: FAMILY}}, ok=True,
     closed=[f"{R}#1", f"{R}#2"], says=("closed=2  failed=0", "superseded by #3"),
     check=lambda c: deletes(c) == [f"repos/{R}/git/refs/heads/sync/1", f"repos/{R}/git/refs/heads/sync/2"])
case("human author and non-allowlisted title are never touched",
     {"repos": ["a"], "prs": {R: [pr(1, author="brad"), pr(2, author="brad"), pr(3, title="feat: x"),
                                  pr(4, title="feat: x")]}}, ok=True, closed=[])
case("husser board sweep family: older closed",
     {"repos": ["a"], "prs": {R: [pr(5, title="chore(husser): daily board sweep 2026-10-05", author="web3sea"),
                                  pr(6, title="chore(husser): daily board sweep 2026-10-06", author="web3sea")]}},
     ok=True, closed=[f"{R}#5"])
case("dry run: lists, closes nothing", {"repos": ["a"], "prs": {R: FAMILY}}, ok=True, dry_run="true", closed=[],
     says=("dry_run_would_close=2", "[dry-run]"))
case("close fails: red, other closes still done", {"repos": ["a"], "prs": {R: FAMILY}, "close_fail": [f"{R}#1"]},
     ok=False, closed=[f"{R}#1", f"{R}#2"], says=("failed=1", "Reaper failed to close 1 PR(s)"))

# ── Read failures fail the run ──────────────────────────────────────────────
case("gh repo list fails: red before any PR read", {"repos": "__error", "prs": {}}, ok=False,
     says=("gh repo list failed",), check=lambda c: not any(x[:2] == ["pr", "list"] for x in c))
case("gh repo list returns nothing: red", {"repos": [], "prs": {}}, ok=False,
     says=("returned no repositories",))
case("gh pr list fails: red, named, not 'nothing to reap'", {"repos": ["a", "b"], "prs": {S: "__error"}}, ok=False,
     says=("unreadable=1", f"READ-FAILED {S}", "Could not read open PRs in 1 of 2 repo(s)"))
case("gh pr list fails in one repo: the other repo is still reaped",
     {"repos": ["a", "b"], "prs": {R: FAMILY, S: "__error"}}, ok=False, closed=[f"{R}#1", f"{R}#2"],
     says=("closed=2", f"READ-FAILED {S}"))
case("gh pr list returns non-JSON: red", {"repos": ["a"], "prs": {R: "__raw"}}, ok=False,
     says=(f"READ-FAILED {R}",))
case("gh pr list returns a JSON object, not an array: red", {"repos": ["a"], "prs": {R: {"message": "x"}}},
     ok=False, says=(f"READ-FAILED {R}",))
case("gh pr list exits 0 with no output: red", {"repos": ["a"], "prs": {R: "__empty"}}, ok=False,
     says=(f"READ-FAILED {R} (gh pr list returned no output)",))
case("read failure in a dry run is still red", {"repos": ["a"], "prs": {R: "__error"}}, ok=False, dry_run="true")


def main():
    failures = 0
    for c in CASES:
        code, out, calls = run(c["fx"], c["dry_run"])
        problems = []
        if (code == 0) != c["ok"]:
            problems.append(f"exit {code}, expected {'green' if c['ok'] else 'red'}")
        if closes(calls) != c["closed"]:
            problems.append(f"closed {closes(calls)}, expected {c['closed']}")
        for s in c["says"]:
            if s not in out:
                problems.append(f"output lacks {s!r}")
        for s in c["not_says"]:
            if s in out:
                problems.append(f"output has {s!r}")
        if c["check"] and not c["check"](calls):
            problems.append("extra check failed")
        if problems:
            failures += 1
            print(f"FAIL {c['name']}: " + "; ".join(problems))
            print("  " + out.replace("\n", "\n  "))
        else:
            print(f"ok   {c['name']}")
    print(f"{len(CASES) - failures}/{len(CASES)} passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
