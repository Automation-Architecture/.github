#!/usr/bin/env python3
"""Offline tests for the review/verdict adapter's evidence rules.

Extracts the inline script from .github/workflows/review-verdict.yml, puts a
stub `gh` first on PATH that serves canned API responses, and runs the script
with PUBLISH=0 so nothing is written. Needs python3 + PyYAML, bash and jq.

    python3 tests/review-verdict/test_evidence.py
"""
import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/review-verdict.yml"

REPO = "acme/widget"
PR = 7
HEAD = "034be02bb328773fbe762ec8c93c14a1bedde66b"
OLD = "609ac50ac6ab8188c4b5ad5238e149b871e45a1d"
CODEX = "chatgpt-codex-connector[bot]"
CODEX_APP = "chatgpt-codex-connector"
GREPTILE = "greptile-apps[bot]"

CLEAN_BODY = (
    "Codex Review: Didn't find any major issues. Already looking forward to the next diff.\n\n"
    "**Reviewed commit:** `{sha}`\n\n<details> <summary>About Codex in GitHub</summary>\n"
    "Comment \"@codex review\".\n</details>"
)

# The stub gh: resolves an API path to a fixture key, applies --jq if given.
STUB = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
fx = json.load(open(os.environ["FIXTURES"]))
args = sys.argv[1:]
if args[:1] != ["api"]:
    sys.exit("stub gh: only `gh api` is supported")
args = args[1:]
jq = None; fields = {}; path = None
i = 0
while i < len(args):
    a = args[i]
    if a == "--jq": jq = args[i + 1]; i += 2; continue
    if a in ("-f", "-F"):
        k, _, v = args[i + 1].partition("="); fields[k] = v; i += 2; continue
    if a in ("--paginate",): i += 1; continue
    if a in ("-X", "--input"): i += 2; continue
    path = a; i += 1
if path == "graphql":
    key = "graphql:" + fields["id"]
else:
    key = path.split("/repos/" + os.environ["REPO"], 1)[-1]
if key not in fx:
    sys.stderr.write("stub gh: no fixture for %s\n" % key); sys.exit(1)
data = json.dumps(fx[key])
if jq:
    data = subprocess.run(["jq", "-c", jq], input=data, capture_output=True, text=True, check=True).stdout
sys.stdout.write(data)
'''


def issue_comment(body, login=CODEX, utype="Bot", app=CODEX_APP, node="IC_1", at="2026-09-27T23:11:57Z"):
    return {"user": {"login": login, "type": utype},
            "performed_via_github_app": {"slug": app} if app else None,
            "body": body, "node_id": node, "created_at": at}


def gql(node, login=CODEX_APP, edited=None, minimized=False):
    return {"graphql:" + node: {"data": {"node": {
        "lastEditedAt": edited, "isMinimized": minimized, "author": {"login": login}}}}}


def base():
    return {
        f"/pulls/{PR}": {"head": {"sha": HEAD}, "base": {"ref": "main", "sha": "b" * 40},
                         "created_at": "2026-09-27T23:06:05Z", "state": "open"},
        f"/commits/{HEAD}": {"author": {"login": "dev"}, "committer": {"login": "web-flow"}},
        f"/pulls/{PR}/reviews": [],
        f"/pulls/{PR}/comments": [],
        f"/issues/{PR}/comments": [],
        f"/commits/{HEAD}/check-runs": {"check_runs": []},
        f"/commits/{HEAD}/pulls": [{"number": PR, "state": "open", "head": {"sha": HEAD}}],
    }


def with_(fx, **kw):
    fx = copy.deepcopy(fx)
    for k, v in kw.items():
        fx.update(v) if k == "extra" else fx.__setitem__(k, v)
    return fx


def clean(sha=HEAD[:10], **kw):
    b = base()
    b[f"/issues/{PR}/comments"] = [issue_comment(CLEAN_BODY.format(sha=sha), **kw)]
    b.update(gql("IC_1"))
    return b


def codex_review(commit, rid=11):
    return {"id": rid, "user": {"login": CODEX}, "state": "COMMENTED", "commit_id": commit,
            "body": "### Codex Review\n\n**Reviewed commit:** `%s`" % commit[:10]}


def finding(original, rid=11, login=CODEX):
    return {"user": {"login": login}, "pull_request_review_id": rid,
            "original_commit_id": original, "commit_id": HEAD}


CASES = []


def case(name, expect, fx):
    CASES.append((name, expect, fx))


# --- Evidence 3: Codex clean issue comment ------------------------------------
case("clean Codex comment on head", "success", clean())
case("clean comment on head, stale finding on an earlier commit", "success",
     with_(clean(), **{f"/pulls/{PR}/comments": [finding(OLD)], f"/pulls/{PR}/reviews": [codex_review(OLD)]}))
case("clean comment names an earlier commit", "fail", clean(sha=OLD[:10]))
case("human account quoting the clean comment", "fail",
     clean(login="some-human", utype="User", app=None))
case("human comment with Codex-like login but not via the App", "fail", clean(app=None))
case("clean comment edited after posting", "fail",
     with_(clean(), extra=gql("IC_1", edited="2026-09-27T23:20:00Z")))
case("clean comment minimised", "fail", with_(clean(), extra=gql("IC_1", minimized=True)))
case("GraphQL author is not the Codex App", "fail", with_(clean(), extra=gql("IC_1", login="mallory")))
case("GraphQL lookup fails", "fail",
     {k: v for k, v in clean().items() if not k.startswith("graphql:")})
case("7-hex reviewed commit is too short", "fail", clean(sha=HEAD[:7]))
case("two Reviewed commit lines", "fail",
     with_(base(), extra={f"/issues/{PR}/comments": [issue_comment(
         CLEAN_BODY.format(sha=HEAD[:10]) + "\n**Reviewed commit:** `%s`" % OLD[:10])], **gql("IC_1")}))
case("clean sentence not at the start of the comment", "fail",
     with_(base(), extra={f"/issues/{PR}/comments": [issue_comment(
         "Earlier result:\n" + CLEAN_BODY.format(sha=HEAD[:10]))], **gql("IC_1")}))
case("skip notice wording in the comment", "fail",
     with_(base(), extra={f"/issues/{PR}/comments": [issue_comment(
         CLEAN_BODY.format(sha=HEAD[:10]) + "\nReview skipped: rate limit")], **gql("IC_1")}))
case("Codex findings on this head veto the clean comment", "fail",
     with_(clean(), **{f"/pulls/{PR}/comments": [finding(HEAD)], f"/pulls/{PR}/reviews": [codex_review(HEAD)]}))
case("inline comments unreadable -> Codex evidence refused", "fail",
     {k: v for k, v in clean().items() if k != f"/pulls/{PR}/comments"})
case("Codex authored the head commit", "fail",
     with_(clean(), **{f"/commits/{HEAD}": {"author": {"login": CODEX}, "committer": {"login": "web-flow"}}}))
case("Codex removed from TRUSTED_REVIEWERS", "fail", clean() | {"__env": {"TRUSTED_REVIEWERS": GREPTILE}})

# --- Evidence 1: review objects ------------------------------------------------
case("Codex review on head with no inline findings", "success",
     with_(base(), **{f"/pulls/{PR}/reviews": [codex_review(HEAD)]}))
case("Codex review on head with inline findings", "fail",
     with_(base(), **{f"/pulls/{PR}/reviews": [codex_review(HEAD)], f"/pulls/{PR}/comments": [finding(HEAD)]}))
case("Codex review on an earlier commit only", "fail",
     with_(base(), **{f"/pulls/{PR}/reviews": [codex_review(OLD)]}))
case("Greptile review on head still counts (Codex findings do not veto Greptile)", "success",
     with_(base(), **{f"/pulls/{PR}/reviews": [codex_review(HEAD), {
         "id": 12, "user": {"login": GREPTILE}, "state": "COMMENTED", "commit_id": HEAD, "body": ""}],
         f"/pulls/{PR}/comments": [finding(HEAD)]}))
case("no evidence at all", "fail", base())

# --- Evidence 2: Greptile check (unchanged) ------------------------------------
case("successful Greptile Review check on head", "success",
     with_(base(), **{f"/commits/{HEAD}/check-runs": {"check_runs": [{
         "name": "Greptile Review", "app": {"slug": "greptile-apps"}, "status": "completed",
         "conclusion": "success", "started_at": "2026-09-27T23:07:00Z", "completed_at": "2026-09-27T23:09:00Z",
         "output": {"title": "ok", "summary": "", "text": ""}}]}}))


def main():
    wf = yaml.safe_load(WORKFLOW.read_text())
    step = wf["jobs"]["publish"]["steps"][0]
    static_env = {k: str(v) for k, v in step["env"].items() if "${{" not in str(v)}
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        (tmp / "script.sh").write_text(step["run"])
        (tmp / "bin").mkdir()
        (tmp / "bin/gh").write_text(STUB)
        (tmp / "bin/gh").chmod(0o755)
        for name, expect, fx in CASES:
            fx = dict(fx)
            env_over = fx.pop("__env", {})
            (tmp / "fx.json").write_text(json.dumps(fx))
            env = {"PATH": f"{tmp / 'bin'}:{os.environ['PATH']}", "HOME": os.environ.get("HOME", "/tmp"),
                   **static_env, "REPO": REPO, "EVENT": "workflow_dispatch", "PR_NUMBER": str(PR),
                   "HEAD_SHA": HEAD, "PUBLISH": "0", "WAIT_SECONDS": "0", "DETAILS_URL": "x",
                   "FIXTURES": str(tmp / "fx.json"), **env_over}
            out = subprocess.run(["bash", str(tmp / "script.sh")], env=env, capture_output=True, text=True)
            verdict = next((l for l in out.stdout.splitlines() if l.startswith("Verdict:")), "")
            got = "success" if verdict.startswith("Verdict: success") else "fail"
            ok = got == expect and out.returncode == 0
            failures += not ok
            print(f"{'PASS' if ok else 'FAIL'}  {name}  (expected {expect}, got {got})")
            if not ok:
                print(out.stdout[-1500:], out.stderr[-1500:], sep="\n")
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
