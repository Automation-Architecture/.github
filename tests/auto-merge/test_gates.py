#!/usr/bin/env python3
"""Offline tests for auto-merge.yml's Gates 1-3 (Codex-only policy, 2026-09-29).

Extracts the inline script from .github/workflows/auto-merge.yml, puts a stub
`gh` first on PATH that serves canned REST and GraphQL answers, and runs the
script for one PR. A case "merges" if the script called
`gh pr merge --squash --match-head-commit <head>`. Needs python3 + PyYAML,
bash and jq.

    python3 tests/auto-merge/test_gates.py
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
WORKFLOW = ROOT / ".github/workflows/auto-merge.yml"

REPO = "acme/widget"
PR = 7
HEAD = "034be02bb328773fbe762ec8c93c14a1bedde66b"
OLD = "609ac50ac6ab8188c4b5ad5238e149b871e45a1d"
BASE = "b" * 40
MERGE = "c" * 40
CODEX = "chatgpt-codex-connector[bot]"
GREPTILE = "greptile-apps[bot]"

CLEAN = ("Codex Review: Didn't find any major issues. Swish!\n\n"
         "**Reviewed commit:** `{sha}`\n\n<details> <summary>About Codex in GitHub</summary></details>")


def badge(p):
    return "**<sub><sub>![P%d Badge](https://img.shields.io/badge/P%d-orange?style=flat)</sub></sub>  Finding" % (p, p)


STUB = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
fx = json.load(open(os.environ["FIXTURES"]))
marker = os.environ["MERGE_MARKER"]
args = sys.argv[1:]
def out(v, jq=None):
    data = json.dumps(v)
    if jq:
        r = subprocess.run(["jq", "-r", "-c", jq], input=data, capture_output=True, text=True)
        if r.returncode: sys.stderr.write(r.stderr); sys.exit(r.returncode)
        data = r.stdout
    sys.stdout.write(data); sys.exit(0)
def opt(name):
    return args[args.index(name) + 1] if name in args else None
jq = opt("--jq")
if args[:2] == ["pr", "view"]:
    fields = opt("--json")
    out({"autoMergeRequest": None, "mergeStateStatus": "CLEAN"}, jq)
if args[:2] == ["pr", "list"]:
    out([{"number": fx["pr"]["number"]}], jq)
if args[:2] == ["pr", "merge"]:
    if "--squash" in args:
        open(marker, "w").write(opt("--match-head-commit") or "")
    sys.exit(0)
if args[:1] != ["api"]:
    sys.exit("stub gh: unsupported " + " ".join(args))
fields = {}; path = None; i = 1
while i < len(args):
    a = args[i]
    if a in ("--jq", "-X", "--input"): i += 2; continue
    if a in ("-f", "-F"):
        k, _, v = args[i + 1].partition("="); fields[k] = v; i += 2; continue
    if a == "--paginate": i += 1; continue
    path = a; i += 1
if path == "graphql":
    q = fields.get("query", "")
    g = fx["graphql"]
    if "dequeuePullRequest" in q: out({"data": {}}, jq)
    if "isInMergeQueue" in q: out({"data": {"repository": {"pullRequest": {"isInMergeQueue": False}}}}, jq)
    if "mergeQueue(" in q: out({"data": {"repository": {"mergeQueue": None}}}, jq)
    for key in ("headRefOid", "reviewThreads(first:100, after", "comments(last:100)"):
        if key in q:
            if g.get(key) is None: sys.exit("stub gh: graphql unavailable: " + key)
            out(g[key], jq)
    sys.exit("stub gh: unknown graphql query")
key = path.split("/repos/" + os.environ["REPO"], 1)[-1].split("?", 1)[0]
rest = fx["rest"]
if os.path.exists(marker):
    if key == "/pulls/%d" % fx["pr"]["number"]:
        out(dict(fx["pr"], merged=True, merge_commit_sha="c" * 40), jq)
    if key == "/commits/" + "c" * 40:
        out({"parents": [{"sha": fx["pr"]["base"]["sha"]}]}, jq)
if key == "/pulls/%d" % fx["pr"]["number"]:
    out(fx["pr"], jq)
if key not in rest or rest[key] is None:
    sys.stderr.write("stub gh: no fixture for %s\n" % key); sys.exit(1)
out(rest[key], jq)
'''


def codex_review(commit=HEAD, state="COMMENTED", body="### Codex Review\n\nHere are some automated review suggestions."):
    return {"user": {"login": CODEX, "type": "Bot"}, "commit_id": commit, "state": state,
            "body": body, "submitted_at": "2026-09-29T01:00:00Z"}


def review(login, state, commit=HEAD, utype="User"):
    return {"user": {"login": login, "type": utype}, "commit_id": commit, "state": state,
            "body": "", "submitted_at": "2026-09-29T01:00:00Z"}


def comment(body, login="chatgpt-codex-connector", typename="Bot", edited=False):
    return {"body": body, "lastEditedAt": "2026-09-29T02:00:00Z" if edited else None,
            "author": {"__typename": typename, "login": login}}


def thread(body, login="chatgpt-codex-connector", typename="Bot", resolved=False, outdated=False, commit=HEAD):
    return {"isResolved": resolved, "isOutdated": outdated, "comments": {"nodes": [{
        "author": {"__typename": typename, "login": login}, "path": "src/a.py", "body": body,
        "originalCommit": {"oid": commit}}]}}


def check(name, conclusion="success", status="completed", app="github-actions"):
    return {"name": name, "status": status, "conclusion": conclusion if status == "completed" else None,
            "app": {"slug": app}}


def build(reviews=(), comments=(), threads=(), checks=(check("CI"),), labels=(), draft=False,
          commits=(OLD, HEAD), files=("src/a.py",), snap_threads=None, snap_reviews=None,
          snap_comments=None, requested=(), drop=()):
    pr = {"number": PR, "state": "open", "draft": draft, "head": {"sha": HEAD},
          "base": {"sha": BASE, "ref": "main"}, "changed_files": len(files), "commits": len(commits),
          "labels": [{"name": l} for l in labels], "node_id": "PR_1",
          "requested_reviewers": [{"login": r} for r in requested], "requested_teams": []}

    def gql_reviews(rs):
        return [{"state": r["state"], "submittedAt": r["submitted_at"], "body": r["body"],
                 "commit": {"oid": r["commit_id"]},
                 "author": {"__typename": r["user"]["type"], "login": r["user"]["login"].replace("[bot]", "")}}
                for r in rs]
    sr = list(reviews if snap_reviews is None else snap_reviews)
    st = list(threads if snap_threads is None else snap_threads)
    sc = list(comments if snap_comments is None else snap_comments)
    fx = {
        "pr": pr,
        "rest": {
            f"/pulls/{PR}/files": [{"filename": f} for f in files],
            f"/commits/{HEAD}/check-runs": {"check_runs": list(checks)},
            f"/pulls/{PR}/reviews": list(reviews),
            f"/pulls/{PR}/commits": [{"sha": c} for c in commits],
        },
        "graphql": {
            "comments(last:100)": {"data": {"repository": {"pullRequest": {"comments": {"nodes": list(comments)}}}}},
            "reviewThreads(first:100, after": {"data": {"repository": {"pullRequest": {"reviewThreads": {
                "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": list(threads)}}}}},
            "headRefOid": {"data": {"repository": {"pullRequest": {
                "headRefOid": HEAD,
                "labels": {"totalCount": len(labels), "nodes": [{"name": l} for l in labels]},
                "reviewRequests": {"totalCount": len(requested)},
                "reviews": {"totalCount": len(sr), "nodes": gql_reviews(sr)},
                "comments": {"nodes": sc},
                "reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": st}}}}},
        },
    }
    for d in drop:
        where, key = d
        fx[where][key] = None
    return fx


CLEAN_HEAD = comment(CLEAN.format(sha=HEAD[:10]))
CASES = []


def case(name, expect, fx):
    CASES.append((name, expect, fx))


# --- Gate 2: Codex on the CURRENT head only -----------------------------------
case("clean Codex comment on head, CI green", "merge", build(comments=[CLEAN_HEAD]))
case("Codex review object on head, no findings", "merge", build(reviews=[codex_review()]))
case("no Codex review at all", "hold", build())
case("Codex review on an earlier commit only (aaa-runbooks#175)", "hold", build(reviews=[codex_review(OLD)]))
case("clean comment names an earlier commit", "hold", build(comments=[comment(CLEAN.format(sha=OLD[:10]))]))
case("clean comment names a commit the PR never carried", "hold", build(comments=[comment(CLEAN.format(sha="abcdef0123"))]))
case("clean comment edited", "hold", build(comments=[comment(CLEAN.format(sha=HEAD[:10]), edited=True)]))
case("clean comment by a User account named like Codex", "hold",
     build(comments=[comment(CLEAN.format(sha=HEAD[:10]), typename="User")]))
case("clean comment not at the start", "hold", build(comments=[comment("Quote:\n" + CLEAN.format(sha=HEAD[:10]))]))
COLLIDE = HEAD[:10] + "f" * 30
case("clean prefix also matches another PR commit", "hold", build(comments=[CLEAN_HEAD], commits=(COLLIDE, HEAD)))
case("12-hex prefix tells the colliding commits apart", "merge",
     build(comments=[comment(CLEAN.format(sha=HEAD[:12]))], commits=(COLLIDE, HEAD)))
case("PR commits unreadable: no comment evidence", "hold",
     build(comments=[CLEAN_HEAD], drop=[("rest", f"/pulls/{PR}/commits")]))
case("PR commits unreadable, Codex review on head still counts", "merge",
     build(reviews=[codex_review()], drop=[("rest", f"/pulls/{PR}/commits")]))
case("comments unreadable", "hold",
     build(reviews=[codex_review()], drop=[("graphql", "comments(last:100)")]))
case("Codex review on head dismissed", "hold", build(reviews=[codex_review(state="DISMISSED")]))
case("Codex review on head is a skip notice", "hold", build(reviews=[codex_review(body="Codex usage limit: rate limit reached")]))
case("Greptile success check + Greptile approval, no Codex", "hold",
     build(reviews=[review(GREPTILE, "APPROVED", utype="Bot")], checks=[check("CI"), check("Greptile Review", app="greptile-apps")]))
case("human approval, no Codex", "hold", build(reviews=[review("some-human", "APPROVED")]))

# --- Gate 1: Greptile's check is ignored --------------------------------------
case("Greptile Review pending, Codex clean", "merge",
     build(comments=[CLEAN_HEAD], checks=[check("CI"), check("Greptile Review", status="in_progress", app="greptile-apps")]))
case("Greptile Review failed, Codex clean", "merge",
     build(comments=[CLEAN_HEAD], checks=[check("CI"), check("Greptile Review", "failure", app="greptile-apps")]))
case("only Greptile's check on head (no CI)", "hold",
     build(comments=[CLEAN_HEAD], checks=[check("Greptile Review", app="greptile-apps")]))
case("CI failing, Codex clean", "hold", build(comments=[CLEAN_HEAD], checks=[check("CI", "failure")]))
case("CI pending, Codex clean", "hold", build(comments=[CLEAN_HEAD], checks=[check("CI", status="queued")]))
case("a non-Greptile check named 'Greptile Review' still counts", "hold",
     build(comments=[CLEAN_HEAD], checks=[check("CI"), check("Greptile Review", "failure", app="mallory")]))

# --- Gate 3: open P0/P1 Codex findings block; nothing else does ---------------
case("open P1 Codex finding on head", "hold", build(reviews=[codex_review()], threads=[thread(badge(1))]))
case("open P0 Codex finding on head", "hold", build(reviews=[codex_review()], threads=[thread(badge(0))]))
case("open P2 Codex finding on head", "merge", build(reviews=[codex_review()], threads=[thread(badge(2))]))
case("open P3 Codex finding on head", "merge", build(reviews=[codex_review()], threads=[thread(badge(3))]))
case("P1 finding resolved", "merge", build(reviews=[codex_review()], threads=[thread(badge(1), resolved=True)]))
case("Codex thread with no severity badge fails closed", "hold",
     build(reviews=[codex_review()], threads=[thread("Something looks off here")]))
case("P1 on an earlier commit, outdated, Codex clean on head", "merge",
     build(comments=[CLEAN_HEAD], threads=[thread(badge(1), outdated=True, commit=OLD)]))
case("P1 on an earlier commit, still applies, Codex clean on head", "hold",
     build(comments=[CLEAN_HEAD], threads=[thread(badge(1), commit=OLD)]))
case("human thread unresolved", "merge",
     build(comments=[CLEAN_HEAD], threads=[thread("please rename", login="some-human", typename="User")]))
case("human thread with a P1 badge", "merge",
     build(comments=[CLEAN_HEAD], threads=[thread(badge(1), login="some-human", typename="User")]))
case("Greptile P1 thread", "merge",
     build(comments=[CLEAN_HEAD], threads=[thread(badge(1), login="greptile-apps")]))
case("threads unreadable", "hold",
     build(comments=[CLEAN_HEAD], drop=[("graphql", "reviewThreads(first:100, after")]))

# --- No human approval; holds kept -------------------------------------------
case("requested human reviewer pending, Codex clean", "merge", build(comments=[CLEAN_HEAD], requested=["some-human"]))
case("Greptile CHANGES_REQUESTED, Codex clean", "merge",
     build(comments=[CLEAN_HEAD], reviews=[review(GREPTILE, "CHANGES_REQUESTED", utype="Bot")]))
case("human CHANGES_REQUESTED, Codex clean (explicit stop kept)", "hold",
     build(comments=[CLEAN_HEAD], reviews=[review("some-human", "CHANGES_REQUESTED")]))
case("draft", "hold", build(comments=[CLEAN_HEAD], draft=True))
case("no-auto-merge label", "hold", build(comments=[CLEAN_HEAD], labels=["no-auto-merge"]))
case("Gate 0: PR edits AGENTS.md", "hold", build(comments=[CLEAN_HEAD], files=("AGENTS.md",)))

# --- Final snapshot re-runs Gates 2 and 3 -------------------------------------
case("P1 filed between evaluation and merge", "hold",
     build(reviews=[codex_review()], snap_threads=[thread(badge(1))]))
case("Codex review dismissed between evaluation and merge", "hold",
     build(reviews=[codex_review()], snap_reviews=[codex_review(state="DISMISSED")]))
case("clean comment edited between evaluation and merge", "hold",
     build(comments=[CLEAN_HEAD], snap_comments=[comment(CLEAN.format(sha=HEAD[:10]), edited=True)]))
def label_added_late():
    fx = build(comments=[CLEAN_HEAD])
    fx["graphql"]["headRefOid"]["data"]["repository"]["pullRequest"]["labels"] = {
        "totalCount": 1, "nodes": [{"name": "no-auto-merge"}]}
    return fx


case("label added between evaluation and merge", "hold", label_added_late())

# --- Triggers: Codex's clean pass is an issue comment -------------------------
case("issue_comment event evaluates the commented PR", "merge",
     dict(build(comments=[CLEAN_HEAD]), __env={"EVENT": "issue_comment", "PRR_PR": "", "IC_PR": str(PR)}))

def main():
    wf = yaml.safe_load(WORKFLOW.read_text())
    step = wf["jobs"]["auto-merge"]["steps"][0]
    static_env = {k: str(v) for k, v in step["env"].items() if "${{" not in str(v)}
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        (tmp / "script.sh").write_text(step["run"])
        (tmp / "bin").mkdir()
        (tmp / "bin/gh").write_text(STUB)
        (tmp / "bin/gh").chmod(0o755)
        for name, expect, fx in CASES:
            marker = tmp / "merged"
            if marker.exists():
                marker.unlink()
            fx = dict(fx)
            env_over = fx.pop("__env", {})
            (tmp / "fx.json").write_text(json.dumps(fx))
            env = {"PATH": f"{tmp / 'bin'}:{os.environ['PATH']}", "HOME": os.environ.get("HOME", "/tmp"),
                   **static_env, "REPO": REPO, "EVENT": "pull_request_review", "PRR_PR": str(PR), "WR_PR": "",
                   "GH_TOKEN": "x", "FIXTURES": str(tmp / "fx.json"), "MERGE_MARKER": str(marker), **env_over}
            out = subprocess.run(["bash", str(tmp / "script.sh")], env=env, capture_output=True, text=True)
            got = "merge" if marker.exists() and marker.read_text() == HEAD else "hold"
            ok = got == expect and out.returncode == 0
            failures += not ok
            print(f"{'PASS' if ok else 'FAIL'}  {name}  (expected {expect}, got {got}, rc {out.returncode})")
            if not ok:
                print(out.stdout[-2000:], out.stderr[-1500:], sep="\n")
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
