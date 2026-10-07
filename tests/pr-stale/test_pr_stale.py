#!/usr/bin/env python3
"""Offline tests for pr-stale.yml.

Extracts the job's one step script, puts a stub `gh` first on PATH, and
checks that every write names the target repo (`--repo` or repos/owner/name).
No token, no network. Needs python3 + PyYAML, bash, jq, and GNU date.

    uv run --with pyyaml python tests/pr-stale/test_pr_stale.py
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/pr-stale.yml"
ORG = "acme"
NOW = "2026-10-07T16:00:00Z"
# 8 days before NOW: idle enough to stale. 6 days: too fresh.
IDLE = "2026-09-29T16:00:00Z"
FRESH = "2026-10-01T16:00:00Z"
LABELED_OLD = "2026-09-29T16:00:00Z"   # 8 days before NOW → close
LABELED_NEW = "2026-10-05T16:00:00Z"   # 2 days before NOW → wait
ACTIVITY_AFTER = "2026-10-06T16:00:00Z"

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
    data = json.dumps([{"nameWithOwner": f"{args[2]}/{r}"} for r in v])
    jq = opt("--jq")
    if jq:
        data = subprocess.run(["jq", "-r", jq], input=data, capture_output=True, text=True, check=True).stdout
    sys.stdout.write(data); sys.exit(0)
if args[:2] == ["pr", "list"]:
    v = fx.get("prs", {}).get(opt("--repo"), [])
    if v == "__error": fail()
    if v == "__raw": sys.stdout.write("<html>bad</html>"); sys.exit(0)
    if v == "__empty": sys.exit(0)
    sys.stdout.write(json.dumps(v)); sys.exit(0)
if args[:2] == ["pr", "close"]:
    key = f"{opt('--repo')}#{args[2]}"
    if key in fx.get("close_fail", []): fail("HTTP 403")
    sys.exit(0)
if args[:1] == ["api"]:
    method = opt("-X") or "GET"
    path = next((a for a in args if a.startswith("repos/")), "")
    if "--paginate" in args and path.endswith("/events"):
        v = fx.get("events", {}).get(path, [])
        if v == "__error": fail()
        sys.stdout.write(json.dumps(v)); sys.exit(0)
    if method != "GET":
        if path in fx.get("write_fail", []): fail("HTTP 403")
        sys.exit(0)
fail("stub gh: unexpected call " + " ".join(args))
'''


def pr(n, updated, labels=()):
    return {"number": n, "updatedAt": updated, "labels": [{"name": x} for x in labels]}


def events_path(repo, n):
    return f"repos/{repo}/issues/{n}/events"


def labeled(ts, name="stale"):
    return [{"event": "labeled", "label": {"name": name}, "created_at": ts}]


def step_script():
    wf = yaml.safe_load(WORKFLOW.read_text())
    steps = wf["jobs"]["stale"]["steps"]
    assert len(steps) == 1 and "run" in steps[0], "expected one run step"
    return steps[0]["run"], wf["jobs"]["stale"].get("env", {}), wf


def structure_checks(wf, script):
    errs = []
    on = wf.get(True) or wf.get("on")
    if set(on) != {"schedule", "workflow_dispatch"}:
        errs.append(f"triggers are {sorted(on)}")
    cron = on["schedule"][0]["cron"].split()
    if cron[0] in ("0", "00") or len(on["schedule"]) != 1 or cron[2:] != ["*", "*", "*"]:
        errs.append(f"schedule must be one daily cron off the hour, got {on['schedule']}")
    if "dry_run" not in on["workflow_dispatch"].get("inputs", {}):
        errs.append("workflow_dispatch must offer dry_run")
    if wf.get("permissions") != {}:
        errs.append("top-level permissions must be {}")
    job = wf["jobs"]["stale"]
    if "Automation-Architecture/.github" not in job.get("if", "") or "refs/heads/main" not in job.get("if", ""):
        errs.append("job must be limited to main of this repository")
    if any("uses" in st for st in job["steps"]):
        errs.append("must not use actions/stale or any third-party action; target repos via gh --repo")
    blob = json.dumps(wf)
    if "GITHUB_REPOSITORY" in blob:
        errs.append("must not set GITHUB_REPOSITORY (default GITHUB_* vars are not overwritable)")
    if "actions/stale" in blob:
        errs.append("actions/stale@v9 cannot be retargeted; do not use it here")
    env = job.get("env", {})
    if str(env.get("STALE_DAYS")) != "7" or str(env.get("CLOSE_DAYS")) != "7":
        errs.append("STALE_DAYS/CLOSE_DAYS must be 7/7 (14d inactivity total)")
    if env.get("EXEMPT_LABELS") != "do-not-stale,security,client-bug":
        errs.append(f"EXEMPT_LABELS drifted: {env.get('EXEMPT_LABELS')}")
    if "AAA_ORG_TOKEN" not in str(env.get("GH_TOKEN", "")):
        errs.append("job must use AAA_ORG_TOKEN")
    if "--repo \"$repo\"" not in script and '--repo "$repo"' not in script:
        errs.append("PR writes must pass --repo \"$repo\"")
    if "repos/$repo/" not in script:
        errs.append("API writes must use repos/$repo/... paths")
    msg = str(env.get("STALE_MSG", "")) + str(env.get("CLOSE_MSG", ""))
    if "PM renewed WIP" not in msg:
        errs.append("comments must say reopen = PM renewed WIP")
    header = WORKFLOW.read_text().split("on:", 1)[0]
    if "client-bug is exempt" not in header and "exempt (not a longer window)" not in header:
        errs.append("header must document the client-bug exempt choice")
    if "no owner/repo inputs" not in header:
        errs.append("header must explain why actions/stale@v9 is not used")
    return errs


def run(fx, env_over=None):
    script, job_env, _ = step_script()
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        (tmp / "bin").mkdir()
        gh = tmp / "bin" / "gh"
        gh.write_text(STUB)
        gh.chmod(0o755)
        (tmp / "fx.json").write_text(json.dumps(fx))
        env = {
            "PATH": f"{tmp / 'bin'}:{os.environ['PATH']}",
            "HOME": os.environ.get("HOME", "/tmp"),
            "GH_TOKEN": "org-pat",
            "ORG": ORG,
            "DRY_RUN": "false",
            "STALE_DAYS": "7",
            "CLOSE_DAYS": "7",
            "STALE_LABEL": "stale",
            "EXEMPT_LABELS": "do-not-stale,security,client-bug",
            "STALE_MSG": job_env.get("STALE_MSG", "stale"),
            "CLOSE_MSG": job_env.get("CLOSE_MSG", "close"),
            "NOW": NOW,
            "RUNNER_TEMP": str(tmp / "work"),
            "FIXTURES": str(tmp / "fx.json"),
            "STATE_DIR": str(tmp),
        }
        (tmp / "work").mkdir()
        env.update(env_over or {})
        p = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)
        calls = [json.loads(l) for l in (tmp / "calls").read_text().splitlines()] if (tmp / "calls").exists() else []
        return p.returncode, p.stdout + p.stderr, calls


def writes(calls):
    """Mutating gh calls: pr close, or api -X not GET."""
    out = []
    for c in calls:
        if c[:2] == ["pr", "close"]:
            out.append(c)
        elif c[:1] == ["api"] and "-X" in c and c[c.index("-X") + 1] != "GET":
            out.append(c)
    return out


def write_targets(calls):
    """Owner/name each write aimed at."""
    found = []
    for c in writes(calls):
        if "--repo" in c:
            found.append(c[c.index("--repo") + 1])
        else:
            path = next((a for a in c if a.startswith("repos/")), "")
            parts = path.split("/")
            if len(parts) >= 3:
                found.append(f"{parts[1]}/{parts[2]}")
    return found


CASES = []


def case(name, fx, *, ok=True, says=(), not_says=(), env=None, stale=(), closed=(), unstale=(), writes_n=None):
    CASES.append(dict(name=name, fx=fx, ok=ok, says=says, not_says=not_says, env=env or {},
                      stale=list(stale), closed=list(closed), unstale=list(unstale), writes_n=writes_n))


R = f"{ORG}/aaa-runbooks"
S = f"{ORG}/.github"

case("fresh PR: no writes", {"repos": ["aaa-runbooks"], "prs": {R: [pr(1, FRESH)]}}, writes_n=0,
     says=("stale=0", "closed=0"))
case("idle PR: stales with explicit repos/owner/name",
     {"repos": ["aaa-runbooks"], "prs": {R: [pr(2, IDLE)]}}, stale=[f"{R}#2"],
     says=("stale=1",))
case("stale label old enough: closes with --repo",
     {"repos": ["aaa-runbooks"], "prs": {R: [pr(3, IDLE, ("stale",))]},
      "events": {events_path(R, 3): labeled(LABELED_OLD)}},
     closed=[f"{R}#3"], says=("closed=1",))
case("stale label recent: wait",
     {"repos": ["aaa-runbooks"], "prs": {R: [pr(4, IDLE, ("stale",))]},
      "events": {events_path(R, 4): labeled(LABELED_NEW)}},
     writes_n=0, says=("closed=0", "stale=0"))
case("activity after stale label: unstale via repos/owner/name",
     {"repos": ["aaa-runbooks"], "prs": {R: [pr(5, ACTIVITY_AFTER, ("stale",))]},
      "events": {events_path(R, 5): labeled(LABELED_OLD)}},
     unstale=[f"{R}#5"])
case("client-bug exempt: skip",
     {"repos": ["aaa-runbooks"], "prs": {R: [pr(6, IDLE, ("client-bug",))]}}, writes_n=0,
     says=("exempt label",))
case("do-not-stale and security exempt",
     {"repos": ["aaa-runbooks"], "prs": {R: [pr(7, IDLE, ("do-not-stale",)), pr(8, IDLE, ("security",))]}},
     writes_n=0)
case("dry run: lists, writes nothing",
     {"repos": ["aaa-runbooks"], "prs": {R: [pr(9, IDLE)]}}, env={"DRY_RUN": "true"},
     writes_n=0, says=("[dry-run] stale",))
case("targets each repo, never the workflow repo by default",
     {"repos": ["aaa-runbooks", ".github"],
      "prs": {R: [pr(10, IDLE)], S: [pr(11, IDLE)]}},
     stale=[f"{R}#10", f"{S}#11"])
case("gh repo list fails: red before any PR read",
     {"repos": "__error", "prs": {}}, ok=False, writes_n=0, says=("gh repo list failed",))
case("empty org list: red", {"repos": [], "prs": {}}, ok=False, says=("returned no repositories",))
case("pr list fails in one repo: other repo still staled, run red",
     {"repos": ["aaa-runbooks", ".github"], "prs": {R: [pr(12, IDLE)], S: "__error"}},
     ok=False, stale=[f"{R}#12"], says=("READ-FAILED", "unreadable="))
case("no token: red, no calls",
     {"repos": ["aaa-runbooks"], "prs": {R: [pr(1, IDLE)]}}, env={"GH_TOKEN": ""},
     ok=False, writes_n=0, says=("AAA_ORG_TOKEN is not available",))


def classify(calls):
    staled, closed, unstaled = [], [], []
    for c in writes(calls):
        if c[:2] == ["pr", "close"]:
            closed.append(f"{c[c.index('--repo') + 1]}#{c[2]}")
        elif c[:1] == ["api"] and "-X" in c:
            method = c[c.index("-X") + 1]
            path = next((a for a in c if a.startswith("repos/")), "")
            parts = path.split("/")
            repo = f"{parts[1]}/{parts[2]}" if len(parts) >= 3 else path
            num = parts[4] if len(parts) > 4 else "?"
            if method == "POST" and path.endswith("/labels"):
                staled.append(f"{repo}#{num}")
            elif method == "DELETE" and "/labels/" in path:
                unstaled.append(f"{repo}#{num}")
    return staled, closed, unstaled


def main():
    script, _, wf = step_script()
    failures = 0
    errs = structure_checks(wf, script)
    for e in errs:
        print(f"FAIL  structure: {e}")
    if not errs:
        print("PASS  structure: no actions/stale, no GITHUB_REPOSITORY, explicit --repo")
    failures += len(errs)

    for c in CASES:
        code, text, calls = run(c["fx"], c["env"])
        why = []
        if (code == 0) != c["ok"]:
            why.append(f"exit {code}")
        staled, closed, unstaled = classify(calls)
        if staled != c["stale"]:
            why.append(f"stale {staled}")
        if closed != c["closed"]:
            why.append(f"closed {closed}")
        if unstaled != c["unstale"]:
            why.append(f"unstale {unstaled}")
        if c["writes_n"] is not None and len(writes(calls)) != c["writes_n"]:
            why.append(f"writes {len(writes(calls))}")
        for s in c["says"]:
            if s not in text:
                why.append(f"missing {s!r}")
        for s in c["not_says"]:
            if s in text:
                why.append(f"unexpected {s!r}")
        if c["env"].get("GH_TOKEN") == "" and calls:
            why.append("called gh without a token")
        targets = write_targets(calls)
        if any(t and "/" not in t for t in targets):
            why.append(f"write without owner/name {targets}")
        # A write must never omit the intended repo (no implicit context.repo).
        if writes(calls) and not targets:
            why.append("writes did not name a repo")
        failures += bool(why)
        print(f"{'FAIL' if why else 'PASS'}  {c['name']}" + (f"  ({'; '.join(why)})" if why else ""))
        if why:
            print(text[-2500:])

    total = 1 + len(CASES)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
