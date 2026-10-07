#!/usr/bin/env python3
"""Offline tests for pr-stale.yml.

Checks the workflow structure (GAAA-3961 windows, exempt labels, actions/stale@v9,
main-only of this repo) and runs the list-job script against a stub `gh`.
No token, no network. Needs python3 + PyYAML, bash and jq.

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

STUB = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
fx = json.load(open(os.environ["FIXTURES"]))
args = sys.argv[1:]
with open(os.path.join(os.environ["STATE_DIR"], "calls"), "a") as f:
    f.write(json.dumps(args) + "\n")
def opt(name):
    return args[args.index(name) + 1] if name in args else None
if args[:2] == ["repo", "list"]:
    v = fx["repos"]
    if v == "__error":
        sys.stderr.write("gh: Server Error (HTTP 502)\n"); sys.exit(1)
    if v == "__raw":
        sys.stdout.write("<html>not json</html>"); sys.exit(0)
    data = json.dumps([{"nameWithOwner": f"{args[2]}/{r}"} for r in v])
    jq = opt("--jq")
    if jq:
        data = subprocess.run(["jq", "-r", jq], input=data, capture_output=True, text=True, check=True).stdout
    sys.stdout.write(data); sys.exit(0)
sys.stderr.write("stub gh: unexpected call " + " ".join(args) + "\n"); sys.exit(1)
'''


def structure_checks(wf):
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
    for name in ("list", "stale"):
        job = wf["jobs"][name]
        if "Automation-Architecture/.github" not in job.get("if", "") or "refs/heads/main" not in job.get("if", ""):
            errs.append(f"{name} job must be limited to main of this repository")
    lst = wf["jobs"]["list"]["steps"][0]
    if "AAA_ORG_TOKEN" not in json.dumps(lst.get("env", {})):
        errs.append("list step must use AAA_ORG_TOKEN")
    stale = wf["jobs"]["stale"]["steps"][0]
    if not str(stale.get("uses", "")).startswith("actions/stale@v9"):
        errs.append(f"must use actions/stale@v9, got {stale.get('uses')}")
    if "AAA_ORG_TOKEN" not in str(stale.get("with", {}).get("repo-token", "")):
        errs.append("stale action must use AAA_ORG_TOKEN as repo-token")
    if stale.get("env", {}).get("GITHUB_REPOSITORY") != "${{ matrix.repo }}":
        errs.append("stale action must target matrix.repo via GITHUB_REPOSITORY")
    w = stale.get("with", {})
    if str(w.get("days-before-pr-stale")) != "7":
        errs.append(f"days-before-pr-stale must be 7, got {w.get('days-before-pr-stale')}")
    if str(w.get("days-before-pr-close")) != "7":
        errs.append("days-before-pr-close must be 7 (close 7d after stale = 14d inactivity)")
    if str(w.get("days-before-issue-stale")) != "-1" or str(w.get("days-before-stale")) != "-1":
        errs.append("issues must be disabled (days-before-issue-stale and days-before-stale = -1)")
    exempt = {x.strip() for x in str(w.get("exempt-pr-labels", "")).split(",") if x.strip()}
    if exempt != {"do-not-stale", "security", "client-bug"}:
        errs.append(f"exempt-pr-labels must be do-not-stale,security,client-bug; got {exempt}")
    msg = str(w.get("stale-pr-message", "")) + str(w.get("close-pr-message", ""))
    if "PM renewed WIP" not in msg:
        errs.append("comments must say reopen = PM renewed WIP")
    if "debug-only" not in w:
        errs.append("must wire debug-only for dry_run")
    header = WORKFLOW.read_text().split("on:", 1)[0]
    if "client-bug is exempt" not in header and "exempt (not a longer window)" not in header:
        errs.append("header must document the client-bug exempt choice")
    return errs


def run_list(fx, env_over=None):
    wf = yaml.safe_load(WORKFLOW.read_text())
    script = wf["jobs"]["list"]["steps"][0]["run"]
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
            "GITHUB_OUTPUT": str(tmp / "out"),
            "FIXTURES": str(tmp / "fx.json"),
            "STATE_DIR": str(tmp),
        }
        env.update(env_over or {})
        p = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30)
        out = (tmp / "out").read_text() if (tmp / "out").exists() else ""
        return p.returncode, p.stdout + p.stderr, out


def main():
    wf = yaml.safe_load(WORKFLOW.read_text())
    failures = 0
    errs = structure_checks(wf)
    for e in errs:
        print(f"FAIL  structure: {e}")
    if not errs:
        print("PASS  structure: triggers, stale@v9, 7/7 windows, exempt labels, main-only")
    failures += len(errs)

    code, text, gho = run_list({"repos": [".github", "aaa-runbooks"]})
    why = []
    if code != 0:
        why.append(f"exit {code}")
    if "listed 2 non-archived repos" not in text:
        why.append("missing listed count")
    if 'repos=["acme/.github","acme/aaa-runbooks"]' not in gho.replace(" ", ""):
        why.append(f"bad output {gho!r}")
    print(f"{'FAIL' if why else 'PASS'}  list two repos" + (f"  ({'; '.join(why)})" if why else ""))
    failures += bool(why)
    if why:
        print(text[-2000:])

    code, text, gho = run_list({"repos": []})
    why = []
    if code == 0:
        why.append("empty list was green")
    if "returned no repositories" not in text:
        why.append("missing empty-list error")
    if gho:
        why.append("wrote output on failure")
    print(f"{'FAIL' if why else 'PASS'}  empty list is red" + (f"  ({'; '.join(why)})" if why else ""))
    failures += bool(why)

    code, text, _ = run_list({"repos": "__error"})
    why = []
    if code == 0:
        why.append("failed list was green")
    if "gh repo list failed" not in text:
        why.append("missing list-failed error")
    print(f"{'FAIL' if why else 'PASS'}  list error is red" + (f"  ({'; '.join(why)})" if why else ""))
    failures += bool(why)

    code, text, _ = run_list({"repos": [".github"]}, env_over={"GH_TOKEN": ""})
    why = []
    if code == 0:
        why.append("missing token was green")
    if "AAA_ORG_TOKEN is not available" not in text:
        why.append("missing token error")
    print(f"{'FAIL' if why else 'PASS'}  no token is red" + (f"  ({'; '.join(why)})" if why else ""))
    failures += bool(why)

    total = 1 + 4
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
