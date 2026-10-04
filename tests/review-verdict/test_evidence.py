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
jq = None; fields = {}; path = None; method = "GET"
i = 0
while i < len(args):
    a = args[i]
    if a == "--jq": jq = args[i + 1]; i += 2; continue
    if a in ("-f", "-F"):
        k, _, v = args[i + 1].partition("="); fields[k] = v; i += 2; continue
    if a in ("--paginate",): i += 1; continue
    if a == "-X": method = args[i + 1]; i += 2; continue
    if a == "--input": i += 2; continue
    path = a; i += 1
if method == "POST" and path.endswith("/check-runs"):
    # PUBLISH=1 cases: record the check-run payload (read from stdin).
    with open(os.environ["FIXTURES"] + ".posted", "a") as f:
        f.write(sys.stdin.read().replace("\n", " ") + "\n")
    sys.stdout.write("{}"); sys.exit(0)
if path == "graphql":
    key = ("graphql:threads:" if "reviewThreads" in fields.get("query", "") else "graphql:") + fields["id"]
else:
    key = path.split("/repos/" + os.environ["REPO"], 1)[-1].split("?", 1)[0]
if key not in fx:
    sys.stderr.write("stub gh: no fixture for %s\n" % key); sys.exit(1)
v = fx[key]
if isinstance(v, dict) and "__seq" in v:
    # Successive calls get successive values; the last one repeats.
    ctr = os.environ["FIXTURES"] + ".calls"
    calls = json.load(open(ctr)) if os.path.exists(ctr) else {}
    i = calls.get(key, 0); calls[key] = i + 1
    json.dump(calls, open(ctr, "w"))
    v = v["__seq"][min(i, len(v["__seq"]) - 1)]
if isinstance(v, dict) and "__pages" in v:
    sys.stdout.write("".join(json.dumps(pg) for pg in v["__pages"])); sys.exit(0)
data = json.dumps(v)
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


def timeline(*events, more=False):
    """Retarget / head force-push history, oldest first: (typename, createdAt) or,
    for a force-push, (typename, createdAt, replaced_oid)."""
    nodes = []
    for t, at, *before in events:
        n = {"__typename": t, "createdAt": at}
        if t == "HeadRefForcePushedEvent":
            n["beforeCommit"] = {"oid": before[0]} if before and before[0] else (None if before else {"oid": OLD})
        nodes.append(n)
    return {"graphql:PR_1": {"data": {"node": {"timelineItems": {
        "pageInfo": {"hasPreviousPage": more}, "nodes": nodes}}}}}


def compare(oid, history, total=None, pages=1):
    """History of a replaced head past the base (the compare endpoint). With
    pages > 1 the stub serves a list of per-page objects, as `gh --paginate`
    concatenates them."""
    total = len(history) if total is None else total
    chunks = [history[i::pages] for i in range(pages)]
    objs = [{"total_commits": total, "commits": [{"sha": s} for s in c]} for c in chunks]
    return {f"/compare/{'b' * 40}...{oid}": objs[0] if pages == 1 else {"__pages": objs}}


def base():
    return {
        f"/pulls/{PR}": {"head": {"sha": HEAD}, "base": {"ref": "main", "sha": "b" * 40},
                         "created_at": "2026-09-27T23:06:05Z", "state": "open",
                         "node_id": "PR_1", "commits": 2},
        f"/pulls/{PR}/commits": [{"sha": OLD}, {"sha": HEAD}],
        **timeline(),
        **compare(OLD, [OLD]),
        f"/commits/{HEAD}": {"author": {"login": "dev"}, "committer": {"login": "web-flow"}},
        f"/pulls/{PR}/reviews": [],
        f"/pulls/{PR}/comments": [],
        f"/issues/{PR}/comments": [],
        f"/commits/{HEAD}/check-runs": {"check_runs": []},
        f"/commits/{HEAD}/pulls": [{"number": PR, "state": "open", "head": {"sha": HEAD}}],
        # The live tip of the base branch. Deliberately not `.base.sha` ('b' * 40):
        # the pulls API value lags the tip (aaa-pr-bot#51).
        "/git/ref/heads/main": base_ref_tip(),
    }


TIP = "c" * 40


def base_ref_tip(sha=TIP, ref="refs/heads/main", type_="commit"):
    return {"ref": ref, "object": {"type": type_, "sha": sha}}


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


def codex_review(commit, rid=11, state="COMMENTED", at="2026-09-27T23:12:30Z", body=None):
    return {"id": rid, "user": {"login": CODEX, "type": "Bot"}, "state": state, "commit_id": commit,
            "submitted_at": at,
            "body": body if body is not None else "### Codex Review\n\n**Reviewed commit:** `%s`" % commit[:10]}


def badge(p):
    return "**<sub><sub>![P%d Badge](https://img.shields.io/badge/P%d-orange?style=flat)</sub></sub>  Finding" % (p, p)


def finding(original, rid=11, login=CODEX, body="Something is wrong here", resolved=False, outdated=None,
            reply_to=None):
    """An inline review comment that starts a thread (or, with reply_to, a reply
    in an existing thread). By default a finding on an earlier commit is
    outdated (a later push rewrote its lines). The harness turns these into the
    GraphQL review threads and each review's comments."""
    return {"user": {"login": login}, "pull_request_review_id": rid, "body": body,
            "original_commit_id": original, "commit_id": HEAD, "resolved": resolved,
            "outdated": (original != HEAD) if outdated is None else outdated, "in_reply_to_id": reply_to}


def derive_findings(fx):
    """/pulls/{PR}/comments (the findings) -> review threads + per-review comments."""
    key = f"/pulls/{PR}/comments"
    if key not in fx:
        return fx
    fx = dict(fx)
    found = fx[key]
    nodes = [{"isResolved": f["resolved"], "isOutdated": f["outdated"], "comments": {"nodes": [{
        "author": {"__typename": "Bot" if f["user"]["login"].endswith("[bot]") else "User",
                   "login": f["user"]["login"].replace("[bot]", "")},
        "path": "a.py", "body": f["body"]}]}} for f in found if not f.get("in_reply_to_id")]
    fx["graphql:threads:PR_1"] = {"data": {"node": {"reviewThreads": {
        "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}}}
    for r in fx.get(f"/pulls/{PR}/reviews") or []:
        if "id" in r:
            fx.setdefault(f"/pulls/{PR}/reviews/{r['id']}/comments", [
                {"id": i, "body": f["body"], "in_reply_to_id": f.get("in_reply_to_id")}
                for i, f in enumerate(found) if f["pull_request_review_id"] == r["id"]])
    return fx


CASES = []


def case(name, expect, fx, contains=None):
    CASES.append((name, expect, fx, contains))


DOC_COMPARE = f"/compare/{'b' * 40}...{HEAD}"


def doc(files, changed=None, draft=False, labels=(), head_repo=REPO):
    """A PR whose changed files are `files` (names, or (name, previous_name)
    for a rename). The immutable compare is keyed to the captured SHAs."""
    b = base()
    entries = [{"filename": f[0], "previous_filename": f[1], "status": "renamed"} if isinstance(f, tuple)
               else {"filename": f, "status": "modified"} for f in files]
    b[f"/pulls/{PR}"] = {**b[f"/pulls/{PR}"], "draft": draft,
                         "changed_files": len(entries) if changed is None else changed,
                         "labels": [{"name": l} for l in labels],
                         "head": {"sha": HEAD, "repo": {"full_name": head_repo} if head_repo else None}}
    b[DOC_COMPARE] = {"base_commit": {"sha": "b" * 40},
                      "merge_base_commit": {"sha": "b" * 40},
                      "commits": [{"sha": HEAD}], "files": entries}
    return b


DOC_EVIDENCE = "documentation-only PR: no review required by policy (Brad, 2026-09-28)"


# --- Evidence 3: Codex clean issue comment ------------------------------------
case("clean Codex comment on head", "success", clean())
case("clean comment on head, stale finding on an earlier commit", "success",
     with_(clean(), **{f"/pulls/{PR}/comments": [finding(OLD)], f"/pulls/{PR}/reviews": [codex_review(OLD)]}))
case("clean comment names an earlier commit", "fail", clean(sha=OLD[:10]))
# Not caught by the prefix-uniqueness rule, so this pins the "names this head" test itself.
case("clean comment names a commit this PR never carried", "fail", clean(sha="abcdef0123"))
case("human account quoting the clean comment", "fail",
     clean(login="some-human", utype="User", app=None))
case("human comment with Codex-like login but not via the App", "fail", clean(app=None))
case("clean comment edited after posting", "fail",
     with_(clean(), extra=gql("IC_1", edited="2026-09-27T23:20:00Z")))
# Minimising is not a revocation (it fires no event, so it could not be enforced
# after a pass was published); edits and deletions are.
case("clean comment minimised still counts", "success", with_(clean(), extra=gql("IC_1", minimized=True)))
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
# A Codex review object on the head is a durable finding marker (Codex P1 on
# aios-coffee#122): deleting its inline findings or dismissing it does not
# revive a clean comment on the same head.
case("clean comment, then a Codex review on the same head whose findings were deleted", "fail",
     with_(clean(), **{f"/pulls/{PR}/reviews": [codex_review(HEAD)]}))
case("clean comment, Codex review on the same head dismissed", "fail",
     with_(clean(), **{f"/pulls/{PR}/reviews": [dict(codex_review(HEAD), state="DISMISSED")]}))
case("reviews unreadable -> Codex evidence refused", "fail",
     {k: v for k, v in clean().items() if k != f"/pulls/{PR}/reviews"})
case("inline comments unreadable -> Codex evidence refused", "fail",
     {k: v for k, v in clean().items() if k != f"/pulls/{PR}/comments"})
case("Codex authored the head commit", "fail",
     with_(clean(), **{f"/commits/{HEAD}": {"author": {"login": CODEX}, "committer": {"login": "web-flow"}}}))
case("CODEX_APP configured empty fails closed", "fail", clean() | {"__env": {"CODEX_APP": " "}})

# Bound to this base and this line of heads (Codex P1 / Greptile P1 on aios-coffee#122).
# The clean comment is posted at 2026-09-27T23:11:57Z.
case("PR retargeted after the clean comment", "fail",
     with_(clean(), extra=timeline(("BaseRefChangedEvent", "2026-09-27T23:30:00Z"))))
case("PR retargeted before the clean comment", "success",
     with_(clean(), extra=timeline(("BaseRefChangedEvent", "2026-09-27T23:08:00Z"))))
# A base force-push fires no event here, so it is not queried (Codex P1 on .github#48);
# the stub serves the timeline as given, so this pins that the query does not ask for it.
case("base force-push is not part of the cutoff query", "success",
     with_(clean(), extra=timeline()))
case("head force-pushed after the clean comment (back to the same SHA)", "fail",
     with_(clean(), extra=timeline(("HeadRefForcePushedEvent", "2026-09-27T23:30:00Z"))))
case("latest of several events wins", "fail",
     with_(clean(), extra=timeline(("BaseRefChangedEvent", "2026-09-27T23:08:00Z"),
                                   ("HeadRefForcePushedEvent", "2026-09-27T23:30:00Z"))))
case("event in the same second as the clean comment", "fail",
     with_(clean(), extra=timeline(("BaseRefChangedEvent", "2026-09-27T23:11:57Z"))))
case("newer clean comment after a retarget counts", "success",
     with_(base(), extra={f"/issues/{PR}/comments": [
         issue_comment(CLEAN_BODY.format(sha=HEAD[:10])),
         issue_comment(CLEAN_BODY.format(sha=HEAD[:10]), node="IC_2", at="2026-09-27T23:40:00Z")],
         **gql("IC_1"), **gql("IC_2"), **timeline(("BaseRefChangedEvent", "2026-09-27T23:30:00Z"))}))
case("timeline unreadable", "fail",
     {k: v for k, v in clean().items() if k != "graphql:PR_1"})
case("timeline node missing", "fail",
     with_(clean(), extra={"graphql:PR_1": {"data": {"node": None}}}))
COLLIDE = HEAD[:10] + "f" * 30
case("another PR commit shares the 10-hex reviewed prefix", "fail",
     with_(clean(), **{f"/pulls/{PR}/commits": [{"sha": COLLIDE}, {"sha": HEAD}]}))
case("12-hex reviewed prefix tells the colliding commits apart", "success",
     with_(clean(sha=HEAD[:12]), **{f"/pulls/{PR}/commits": [{"sha": COLLIDE}, {"sha": HEAD}]}))
case("PR commit list unreadable", "fail",
     {k: v for k, v in clean().items() if k != f"/pulls/{PR}/commits"})
case("PR commit list shorter than the PR's commit count", "fail",
     with_(clean(), **{f"/pulls/{PR}/commits": [{"sha": HEAD}]}))
FILLER = ["%040x" % (i + 1) for i in range(249)]
case("PR with exactly 250 commits (the endpoint's full page) counts", "success",
     with_(clean(), **{f"/pulls/{PR}": {**base()[f"/pulls/{PR}"], "commits": 250},
                       f"/pulls/{PR}/commits": [{"sha": s} for s in FILLER + [HEAD]]}))
case("PR with more than 250 commits refuses", "fail",
     with_(clean(), **{f"/pulls/{PR}": {**base()[f"/pulls/{PR}"], "commits": 251},
                       f"/pulls/{PR}/commits": [{"sha": s} for s in FILLER + [HEAD]]}))

# Heads a force-push replaced also count as "carried" (Codex P1 on
# opportunity-builder#178): a review of A still running when a colliding B is
# force-pushed posts A's comment after the push, and A is gone from the list.
FP_EARLY = "2026-09-27T23:08:00Z"  # before the clean comment (23:11:57)
case("force-push replaced a colliding head", "fail",
     with_(clean(), extra={**timeline(("HeadRefForcePushedEvent", FP_EARLY, COLLIDE)), **compare(COLLIDE, [COLLIDE])}))
case("colliding commit in the history of a replaced head", "fail",
     with_(clean(), extra={**timeline(("HeadRefForcePushedEvent", FP_EARLY, OLD)), **compare(OLD, [COLLIDE, OLD])}))
case("force-push replaced a non-colliding head", "success",
     with_(clean(), extra=timeline(("HeadRefForcePushedEvent", FP_EARLY, OLD))))
case("replaced head's history unreadable", "fail",
     {k: v for k, v in with_(clean(), extra=timeline(("HeadRefForcePushedEvent", FP_EARLY, OLD))).items()
      if not k.startswith("/compare/")})
case("replaced head's history truncated", "fail",
     with_(clean(), extra={**timeline(("HeadRefForcePushedEvent", FP_EARLY, OLD)), **compare(OLD, [OLD], total=300)}))
LONG = ["%040x" % (0xabc000 + i) for i in range(300)]
case("replaced head with a 300-commit history, served in pages", "success",
     with_(clean(), extra={**timeline(("HeadRefForcePushedEvent", FP_EARLY, OLD)), **compare(OLD, LONG, pages=3)}))
case("paged history of a replaced head still catches a collision", "fail",
     with_(clean(), extra={**timeline(("HeadRefForcePushedEvent", FP_EARLY, OLD)),
                           **compare(OLD, LONG + [COLLIDE], pages=3)}))
case("paged history short of total_commits", "fail",
     with_(clean(), extra={**timeline(("HeadRefForcePushedEvent", FP_EARLY, OLD)), **compare(OLD, LONG, total=400, pages=3)}))
case("force-push without a recorded before commit", "fail",
     with_(clean(), extra=timeline(("HeadRefForcePushedEvent", FP_EARLY, None))))
case("more than 100 retarget / force-push events", "fail", with_(clean(), extra=timeline(more=True)))

# --- Evidence 1: review objects ------------------------------------------------
# Codex review objects never count (Codex P1 on aios-coffee#122): deleting the
# findings would otherwise leave a bare review object that passes.
case("Codex review on head whose findings were deleted", "fail",
     with_(base(), **{f"/pulls/{PR}/reviews": [codex_review(HEAD)]}))
case("Codex review on head with inline findings", "fail",
     with_(base(), **{f"/pulls/{PR}/reviews": [codex_review(HEAD)], f"/pulls/{PR}/comments": [finding(HEAD)]}))
case("Codex review on an earlier commit only", "fail",
     with_(base(), **{f"/pulls/{PR}/reviews": [codex_review(OLD)]}))
case("no evidence at all", "fail", base())

# --- Codex is the only reviewer (Brad, 2026-09-29; Codex P1 on .github#53) ----
# Greptile is outside the merge policy: its check and review objects are never
# read, so they can neither pass nor block the verdict. No human approval counts.
def greptile_check(conclusion="success"):
    return {f"/commits/{HEAD}/check-runs": {"check_runs": [{
        "name": "Greptile Review", "app": {"slug": "greptile-apps"}, "status": "completed",
        "conclusion": conclusion, "started_at": "2026-09-27T23:07:00Z", "completed_at": "2026-09-27T23:09:00Z",
        "output": {"title": "ok", "summary": "", "text": ""}}]}}


GREPTILE_REVIEW = {"id": 12, "user": {"login": GREPTILE, "type": "Bot"}, "state": "APPROVED", "commit_id": HEAD, "body": ""}
HUMAN_APPROVAL = {"id": 13, "user": {"login": "some-human", "type": "User"}, "state": "APPROVED", "commit_id": HEAD, "body": "LGTM"}

case("Greptile success with no Codex review", "fail", with_(base(), **greptile_check()))
case("Greptile success with an open Codex finding", "fail",
     with_(base(), **greptile_check(), **{f"/pulls/{PR}/reviews": [codex_review(HEAD)],
                                           f"/pulls/{PR}/comments": [finding(HEAD)]}))
case("Greptile review on head with an open Codex finding", "fail",
     with_(base(), **{f"/pulls/{PR}/reviews": [codex_review(HEAD), GREPTILE_REVIEW],
                      f"/pulls/{PR}/comments": [finding(HEAD)]}))
case("Greptile review on head, no Codex review", "fail",
     with_(base(), **{f"/pulls/{PR}/reviews": [GREPTILE_REVIEW]}))
case("Greptile check, Greptile review and clean Codex, but a Codex finding on head", "fail",
     with_(clean(), **greptile_check(), **{f"/pulls/{PR}/reviews": [codex_review(HEAD), GREPTILE_REVIEW],
                                            f"/pulls/{PR}/comments": [finding(HEAD)]}))
case("human approval on head, no Codex review", "fail",
     with_(base(), **{f"/pulls/{PR}/reviews": [HUMAN_APPROVAL]}))
case("human approval on head with an open Codex finding", "fail",
     with_(clean(), **{f"/pulls/{PR}/reviews": [codex_review(HEAD), HUMAN_APPROVAL],
                       f"/pulls/{PR}/comments": [finding(HEAD)]}))
case("clean Codex on head passes whatever Greptile did (failed check)", "success",
     with_(clean(), **greptile_check("failure")))
case("clean Codex on head passes with no Greptile and no human", "success", clean())
case("Codex review on an older head only, Greptile success on head", "fail",
     with_(base(), **greptile_check(), **{f"/pulls/{PR}/reviews": [codex_review(OLD)]}))
case("clean Codex comment for an older head only, Greptile success on head", "fail",
     with_(clean(sha=OLD[:10]), **greptile_check()))


# --- Evidence 0: documentation-only exemption (Brad, 2026-09-28) --------------
case("doc-only PR, no review at all", "success", doc(["README.md"]), DOC_EVIDENCE)
case("doc-only: .md and .markdown at any depth, instruction files included", "success",
     doc(["docs/guide.md", "notes/a.markdown", "CLAUDE.md", "pkg/AGENTS.md", ".github/pull_request_template.md",
          ".claude/agents/x.md"]),
     DOC_EVIDENCE)
case(".mdx is not documentation-only (reviewed like code)", "fail", doc(["docs/guide.mdx"]))
case("rename from .md to .mdx refuses", "fail", doc([("docs/guide.mdx", "docs/guide.md")]))
case("rename from .mdx to .md refuses", "fail", doc([("docs/guide.md", "docs/guide.mdx")]))
case("mixed .md + .mdx refuses", "fail", doc(["README.md", "docs/guide.mdx"]))
case("doc-only: rename between two Markdown names", "success", doc([("docs/new.md", "docs/old.md")]), DOC_EVIDENCE)
case("doc-only with Codex findings on the head still needs no review", "success",
     with_(doc(["README.md"]), **{f"/pulls/{PR}/comments": [finding(HEAD)], f"/pulls/{PR}/reviews": [codex_review(HEAD)]}),
     DOC_EVIDENCE)
case("mixed diff: Markdown plus code refuses the exemption", "fail", doc(["README.md", "src/app.py"]))
case("mixed diff: Markdown plus a workflow refuses", "fail", doc(["README.md", ".github/workflows/ci.yml"]))
case("mixed diff: Markdown plus a lockfile refuses", "fail", doc(["README.md", "package-lock.json"]))
case("rename from .py to .md refuses (previous name is code)", "fail", doc([("app.md", "app.py")]))
case("truncated list: fewer files than changed_files refuses", "fail", doc(["README.md", "b.md"], changed=3))
case("file list unreadable refuses", "fail",
     {k: v for k, v in doc(["README.md"]).items() if k != DOC_COMPARE})
case("changed_files at the 300 cap refuses", "fail", doc(["README.md"], changed=300))
case("empty diff refuses", "fail", doc([], changed=0))
case("changed_files missing refuses", "fail",
     with_(doc(["README.md"]), **{f"/pulls/{PR}": {k: v for k, v in doc(["README.md"])[f"/pulls/{PR}"].items()
                                                   if k != "changed_files"}}))
MANY = ["docs/p%03d.md" % i for i in range(250)]
case("250 Markdown files in the pinned compare", "success", doc(MANY), DOC_EVIDENCE)
case("pinned compare with one non-Markdown file refuses", "fail", doc(MANY + ["docs/p.json"]))
case("pinned compare short of changed_files refuses", "fail", doc(MANY, changed=251))
case("299 Markdown files below the compare cap", "success",
     doc(["docs/p%03d.md" % i for i in range(299)]), DOC_EVIDENCE)
case("live PR file list cannot replace pinned code diff", "fail",
     with_(doc(["README.md"]), extra={DOC_COMPARE: {
         "base_commit": {"sha": "b" * 40}, "merge_base_commit": {"sha": "b" * 40},
         "commits": [{"sha": HEAD}], "files": [{"filename": "src/app.py"}]},
         f"/pulls/{PR}/files": [{"filename": "README.md"}]}))
case("compare with a different base refuses", "fail",
     with_(doc(["README.md"]), extra={DOC_COMPARE: {
         "base_commit": {"sha": OLD}, "merge_base_commit": {"sha": OLD},
         "commits": [{"sha": HEAD}], "files": [{"filename": "README.md"}]}}))
case("branch behind base still uses its pinned three-dot diff", "success",
     with_(doc(["README.md"]), extra={DOC_COMPARE: {
         "base_commit": {"sha": "b" * 40}, "merge_base_commit": {"sha": OLD},
         "commits": [{"sha": HEAD}], "files": [{"filename": "README.md"}]}}), DOC_EVIDENCE)
case("compare commit page can end before the pinned head", "success",
     with_(doc(["README.md"]), extra={DOC_COMPARE: {
         "base_commit": {"sha": "b" * 40}, "merge_base_commit": {"sha": "b" * 40},
         "total_commits": 251, "commits": [{"sha": OLD}],
         "files": [{"filename": "README.md"}]}}), DOC_EVIDENCE)
case("extension must be at the end: notes.md.py refuses", "fail", doc(["notes.md.py"]))
case("extension is case-sensitive: README.MD refuses", "fail", doc(["README.MD"]))
case("newline in a filename cannot smuggle code past the pattern", "fail", doc(["x.md\ny.py"]))
case("a trailing newline after .md is not Markdown (pins \\z over $)", "fail", doc(["x.md\n"]))
case("directory named like Markdown: docs.md/run.sh refuses", "fail", doc(["docs.md/run.sh"]))
case("draft doc-only PR does not take the exemption", "fail", doc(["README.md"], draft=True))
case("draft doc-only PR still publishes normally from review evidence", "success",
     with_(doc(["README.md"], draft=True), **{f"/issues/{PR}/comments": [issue_comment(CLEAN_BODY.format(sha=HEAD[:10]))]},
           extra=gql("IC_1")), "clean Codex result")
case("hold label switches the exemption off", "fail", doc(["README.md"], labels=["no-auto-merge"]))
case("held doc-only PR can still pass on review evidence", "success",
     with_(doc(["README.md"], labels=["docs", "no-auto-merge"]),
           **{f"/issues/{PR}/comments": [issue_comment(CLEAN_BODY.format(sha=HEAD[:10]))]}, extra=gql("IC_1")),
     "clean Codex result")
case("unrelated label keeps the exemption", "success", doc(["README.md"], labels=["documentation"]), DOC_EVIDENCE)
case("labels unreadable refuses", "fail",
     with_(doc(["README.md"]), **{f"/pulls/{PR}": {**doc(["README.md"])[f"/pulls/{PR}"], "labels": None}}))
case("doc-only PR from a fork refuses", "fail", doc(["README.md"], head_repo="mallory/widget"))
case("doc-only PR whose head repository is gone refuses", "fail", doc(["README.md"], head_repo=None))



def trigger_types():
    wf = yaml.safe_load(WORKFLOW.read_text())
    on = wf.get("on", wf.get(True))
    return on["pull_request_target"]["types"]

# --- P0/P1 threshold (Brad, 2026-09-29): only open P0/P1 Codex findings fail --
R = f"/pulls/{PR}/reviews"
C = f"/pulls/{PR}/comments"
case("Codex review on head, P2 finding only", "success",
     with_(base(), **{R: [codex_review(HEAD)], C: [finding(HEAD, body=badge(2))]}))
case("Codex review on head, P3 finding only", "success",
     with_(base(), **{R: [codex_review(HEAD)], C: [finding(HEAD, body=badge(3))]}))
case("Codex review on head, P1 finding", "fail",
     with_(base(), **{R: [codex_review(HEAD)], C: [finding(HEAD, body=badge(1))]}))
case("Codex review on head, P0 finding", "fail",
     with_(base(), **{R: [codex_review(HEAD)], C: [finding(HEAD, body=badge(0))]}))
case("Codex review on head, unbadged finding (fail closed)", "fail",
     with_(base(), **{R: [codex_review(HEAD)], C: [finding(HEAD)]}))
case("Codex review on head, P1 finding resolved", "success",
     with_(base(), **{R: [codex_review(HEAD)], C: [finding(HEAD, body=badge(1), resolved=True)]}))
case("Codex review on head, P2 plus a P1", "fail",
     with_(base(), **{R: [codex_review(HEAD)], C: [finding(HEAD, body=badge(2)), finding(HEAD, body=badge(1))]}))
case("clean comment on head, P2 finding on head", "success",
     with_(clean(), **{R: [codex_review(HEAD)], C: [finding(HEAD, body=badge(2))]}))
case("P1 raised on an earlier commit that still applies, clean on head", "fail",
     with_(clean(), **{R: [codex_review(OLD)], C: [finding(OLD, body=badge(1), outdated=False)]}))
case("P1 raised on an earlier commit, now outdated, clean on head", "success",
     with_(clean(), **{R: [codex_review(OLD)], C: [finding(OLD, body=badge(1))]}))
case("human thread with a P1 badge never blocks", "success",
     with_(clean(), **{C: [finding(HEAD, login="some-human", body=badge(1))]}))
case("Greptile thread with a P1 badge never blocks", "success",
     with_(clean(), **{C: [finding(HEAD, login=GREPTILE, body=badge(1))]}))
case("dismissed Codex review on head with a P2 is not evidence", "fail",
     with_(base(), **{R: [codex_review(HEAD, state="DISMISSED")], C: [finding(HEAD, body=badge(2))]}))
case("Codex review on head is a skip notice", "fail",
     with_(base(), **{R: [codex_review(HEAD, body="Codex hit a usage limit: rate limit")], C: [finding(HEAD, body=badge(2))]}))
case("Codex review on head predates a retarget", "fail",
     with_(base(), **{R: [codex_review(HEAD)], C: [finding(HEAD, body=badge(2))]},
           extra=timeline(("BaseRefChangedEvent", "2026-09-27T23:30:00Z"))))
case("Codex review on head after a retarget", "success",
     with_(base(), **{R: [codex_review(HEAD, at="2026-09-27T23:40:00Z")], C: [finding(HEAD, body=badge(2))]},
           extra=timeline(("BaseRefChangedEvent", "2026-09-27T23:30:00Z"))))
case("Codex review on head, Codex authored the head", "fail",
     with_(base(), **{R: [codex_review(HEAD)], C: [finding(HEAD, body=badge(2))],
                      f"/commits/{HEAD}": {"author": {"login": CODEX}, "committer": {"login": "web-flow"}}}))
case("Codex review on head by a User account named like Codex", "fail",
     with_(base(), **{R: [dict(codex_review(HEAD), user={"login": CODEX, "type": "User"})], C: []}))
case("Codex skip-notice review on head does not block a later clean comment", "success",
     with_(clean(), **{R: [codex_review(HEAD, body="Codex hit a usage limit: rate limit")]}))
case("review threads unreadable", "fail",
     {k: v for k, v in with_(clean(), **{R: [codex_review(HEAD)], C: [finding(HEAD, body=badge(2))]}).items()} | {"__drop_threads": True})


# --- Evidence 1b: Codex review summary Completed on the head + Codex's 👍 -----
# Codex's current clean signal (opportunity-builder#193, aaa-client-dashboard#263):
# no "Didn't find any major issues" comment; the live summary table turns to
# Completed on the head and Codex reacts 👍 on the PR.
SUMMARY_EVIDENCE = "the Codex review summary"
DONE_AT = "2026-09-27T23:11:50.934144Z"   # row completion; the 👍 lands seconds later


def summary_row(sha=HEAD[:7], status=None, kind="📝 **Code Review**", trigger="PR opened", at=DONE_AT):
    status = status if status is not None else (
        '✅ **Completed** <relative-time datetime="%s">%s</relative-time>' % (at, at))
    return "| %s | %s | `%s` | %s |" % (kind, status, sha, trigger)


def summary_body(*rows):
    rows = rows or (summary_row(),)
    return ("<!-- codex-pull-request-review-summary -->\n\n## Codex Review Summary\n\n"
            "This comment shows the latest Codex review activity on this pull request.\n\n"
            "| Review | Status | Commit | Review trigger |\n| --- | --- | --- | --- |\n"
            + "\n".join(rows) +
            "\n\n\n\n<details> <summary>ℹ️ About Codex in GitHub</summary>\n<br/>\n\n"
            "Codex reacts with 👀 while any review is running, comments if it has suggestions, and reacts "
            "with 👍 once all reviews finish with no findings.\n\n</details>")


def reaction(content="+1", login=CODEX, utype="User", at="2026-09-27T23:11:54Z"):
    return {"user": {"login": login, "type": utype}, "content": content, "created_at": at}


def gql_summary(node="IC_S", login=CODEX_APP, edited="2026-09-27T23:11:51Z", editor=CODEX_APP, last_edit=CODEX_APP):
    return {"graphql:" + node: {"data": {"node": {
        "lastEditedAt": edited, "author": {"login": login},
        "editor": {"login": editor} if editor else None,
        "userContentEdits": {"nodes": [{"editor": {"login": last_edit}}] if last_edit else []}}}}}


def summary(*rows, body=None, reacts=None, login=CODEX, utype="Bot", app=CODEX_APP, gq=None, at="2026-09-27T23:08:10Z"):
    b = base()
    b[f"/issues/{PR}/comments"] = [issue_comment(body if body is not None else summary_body(*rows),
                                                 login=login, utype=utype, app=app, node="IC_S", at=at)]
    b[f"/issues/{PR}/reactions"] = [reaction()] if reacts is None else reacts
    b.update(gq if gq is not None else gql_summary())
    return b


I = f"/issues/{PR}/reactions"
case("summary Completed on head + Codex 👍", "success", summary(), SUMMARY_EVIDENCE)
case("summary row naming the full 40-hex head", "success", summary(summary_row(sha=HEAD)), SUMMARY_EVIDENCE)
case("summary never edited (created Completed) + 👍", "success",
     summary(gq=gql_summary(edited=None, editor=None, last_edit=None)), SUMMARY_EVIDENCE)
case("summary: two review rows, both Completed on head", "success",
     summary(summary_row(), summary_row(kind="🔒 **Security Review**", trigger="Manual request")), SUMMARY_EVIDENCE)
# The 👍 is required: it is Codex's only "no findings" signal in this form, and the
# one part a writer cannot forge (Completed also follows a findings review).
case("summary Completed on head, no 👍", "fail", summary(reacts=[]), "has not reacted")
case("summary Completed on head, 👍 by a human", "fail", summary(reacts=[reaction(login="dev", utype="User")]))
# The reactions API reports the Codex bot as type "User" (opportunity-builder#193,
# 2026-10-01); the "[bot]" login is what no person can hold.
case("summary Completed on head, Codex 👍 typed User as the reactions API serves it", "success",
     summary(reacts=[reaction(utype="User")]), SUMMARY_EVIDENCE)
case("summary Completed on head, 👍 by a login without the [bot] suffix", "fail",
     summary(reacts=[reaction(login=CODEX_APP, utype="User")]))
case("summary Completed on head, Codex reacted 👀 only", "fail", summary(reacts=[reaction(content="eyes")]))
case("summary Completed on head, Codex reacted 👎", "fail", summary(reacts=[reaction(content="-1")]))
case("summary Completed on head, Codex 👍 predates the completion", "fail",
     summary(reacts=[reaction(at="2026-09-27T23:05:00Z")]))
case("summary Completed on head, 👍 in the completion second counts", "success",
     summary(reacts=[reaction(at="2026-09-27T23:11:50Z")]), SUMMARY_EVIDENCE)
case("reactions unreadable", "fail", {k: v for k, v in summary().items() if k != I})
case("summary row names an earlier commit (👍 after the push)", "fail", summary(summary_row(sha=OLD[:7])))
case("summary row names a commit this PR never carried", "fail", summary(summary_row(sha="abcdef0")))
case("summary row with a 6-hex commit", "fail", summary(summary_row(sha=HEAD[:6])))
case("summary row still Running on head", "fail",
     summary(summary_row(status='🔄 **Running** since <relative-time datetime="%s">x</relative-time>' % DONE_AT)))
case("summary row Failed on head", "fail", summary(summary_row(status="❌ **Failed**")))
case("summary: one row Completed, one Running", "fail",
     summary(summary_row(), summary_row(kind="🔒 **Security Review**",
                                        status='🔄 **Running** since <relative-time datetime="%s">x</relative-time>' % DONE_AT)))
case("summary: one row on head, one on an earlier commit", "fail",
     summary(summary_row(), summary_row(sha=OLD[:7], kind="🔒 **Security Review**")))
case("summary table with no review rows", "fail",
     summary(body=summary_body("").replace("| --- | --- | --- | --- |\n\n", "| --- | --- | --- | --- |\n")))
case("summary row with two commits in it", "fail",
     summary(summary_row(sha=HEAD[:7] + "` `" + OLD[:7])))
case("summary marker not at the start", "fail", summary(body="Quoting Codex:\n" + summary_body()))
case("human comment quoting the summary", "fail", summary(login="dev", utype="User", app=None))
case("Codex-like login not posted via the App", "fail", summary(app=None))
case("GraphQL author is not the Codex App", "fail", summary(gq=gql_summary(login="mallory")))
case("summary last edited by a writer", "fail", summary(gq=gql_summary(editor="mallory", last_edit="mallory")))
case("summary editor is Codex but the newest edit is a writer's", "fail", summary(gq=gql_summary(last_edit="mallory")))
case("summary edited but its edit history is empty", "fail", summary(gq=gql_summary(last_edit=None)))
case("summary GraphQL lookup fails", "fail", {k: v for k, v in summary().items() if not k.startswith("graphql:IC_S")})
case("summary + 👍, but Codex authored the head commit", "fail",
     with_(summary(), **{f"/commits/{HEAD}": {"author": {"login": CODEX}, "committer": {"login": "web-flow"}}}))
case("summary completed before a retarget", "fail",
     with_(summary(), extra=timeline(("BaseRefChangedEvent", "2026-09-27T23:20:00Z"))))
case("summary completed after a head force-push", "success",
     with_(summary(), extra=timeline(("HeadRefForcePushedEvent", FP_EARLY, OLD))), SUMMARY_EVIDENCE)
case("summary completed after a retarget but the 👍 predates it", "fail",
     with_(summary(reacts=[reaction(at="2026-09-27T23:11:52Z")]),
           extra=timeline(("BaseRefChangedEvent", "2026-09-27T23:11:51Z"))))
COLLIDE7 = HEAD[:7] + "f" * 33
case("another PR commit shares the 7-hex summary prefix", "fail",
     with_(summary(), **{f"/pulls/{PR}/commits": [{"sha": COLLIDE7}, {"sha": HEAD}]}))
case("force-push replaced a head sharing the 7-hex summary prefix", "fail",
     with_(summary(), extra={**timeline(("HeadRefForcePushedEvent", FP_EARLY, COLLIDE7)), **compare(COLLIDE7, [COLLIDE7])}))
case("PR commit list unreadable (summary path)", "fail",
     {k: v for k, v in summary().items() if k != f"/pulls/{PR}/commits"})
# Findings on the head are never judged by the summary row.
case("summary + 👍, open P1 Codex finding on head", "fail",
     with_(summary(), **{R: [codex_review(HEAD)], C: [finding(HEAD, body=badge(1))]}))
case("summary + 👍, P1 raised earlier that still applies", "fail",
     with_(summary(), **{R: [codex_review(OLD)], C: [finding(OLD, body=badge(1), outdated=False)]}))
case("summary + 👍, P1 raised earlier, now outdated", "success",
     with_(summary(), **{R: [codex_review(OLD)], C: [finding(OLD, body=badge(1))]}), SUMMARY_EVIDENCE)
case("summary + 👍, Codex review on head with P2 only: judged as a review, not the row", "success",
     with_(summary(), **{R: [codex_review(HEAD)], C: [finding(HEAD, body=badge(2))]}), "a Codex review of this head")
case("summary + 👍, Codex review on head whose findings were deleted", "fail",
     with_(summary(), **{R: [codex_review(HEAD)]}))
# A Codex review object that carries no findings (pending, or a skip notice
# matching REVIEW_SKIP_PATTERN) is the same thing to every check: ignored by the
# findings block, never evidence 2, and no veto on the summary row (Codex P1 on
# aios-coffee#137: an earlier attempt that produced nothing rejected the clean retry).
SKIP_NOTICE = "Codex review skipped: rate limit reached for this repository"
case("summary + 👍, Codex skip-notice review on head", "success",
     with_(summary(), **{R: [codex_review(HEAD, body="Codex hit a usage limit: rate limit")]}), SUMMARY_EVIDENCE)
case("earlier pending Codex review on head, then a clean Completed retry + 👍", "success",
     with_(summary(), **{R: [codex_review(HEAD, state="PENDING", at=None, body="")]}), SUMMARY_EVIDENCE)
case("earlier skip-notice Codex review on head, then a clean retry + 👍", "success",
     with_(summary(summary_row(trigger="Manual request")),
           **{R: [codex_review(HEAD, body=SKIP_NOTICE, at="2026-09-27T23:09:00Z")]}), SUMMARY_EVIDENCE)
case("pending and skip-notice reviews on head, then a clean retry + 👍", "success",
     with_(summary(), **{R: [codex_review(HEAD, rid=11, state="PENDING", at=None, body=""),
                             codex_review(HEAD, rid=12, body=SKIP_NOTICE)]}), SUMMARY_EVIDENCE)
case("earlier Codex findings review on head with P1 open, then Completed + 👍", "fail",
     with_(summary(), **{R: [codex_review(HEAD, rid=11, at="2026-09-27T23:09:00Z")],
                         C: [finding(HEAD, rid=11, body=badge(1))]}), "unresolved P0/P1")
case("skip notice ignored, but a findings review on head with P1 open still blocks", "fail",
     with_(summary(), **{R: [codex_review(HEAD, rid=11, body=SKIP_NOTICE),
                             codex_review(HEAD, rid=12, at="2026-09-27T23:10:00Z")],
                         C: [finding(HEAD, rid=12, body=badge(1))]}))
case("skip notice ignored, a findings review on head with P2 only is judged as a review", "success",
     with_(summary(), **{R: [codex_review(HEAD, rid=11, body=SKIP_NOTICE),
                             codex_review(HEAD, rid=12, at="2026-09-27T23:10:00Z")],
                         C: [finding(HEAD, rid=12, body=badge(2))]}), "a Codex review of this head")
# Dismissal revokes a review as evidence (evidence 2); it does not turn a review
# with findings into a clean summary pass.
case("summary + 👍, DISMISSED Codex findings review on head (P2 only)", "fail",
     with_(summary(), **{R: [codex_review(HEAD, state="DISMISSED")], C: [finding(HEAD, body=badge(2))]}))
case("summary + 👍, DISMISSED Codex skip-notice review on head", "success",
     with_(summary(), **{R: [codex_review(HEAD, state="DISMISSED", body=SKIP_NOTICE)]}), SUMMARY_EVIDENCE)
# The review-object check is not redundant with the inline-comment check: a
# Codex review whose comments sit on an earlier commit's thread (as a Codex
# reply does: review commit_id = head, comment original_commit_id = old) has no
# inline comment "on this head", so only the review check keeps it off the row.
case("summary + 👍, Codex review on head whose comment is on an earlier commit", "success",
     with_(summary(), **{R: [codex_review(HEAD, rid=12, body="")], C: [finding(OLD, rid=12, body=badge(3))]}),
     "a Codex review of this head")
case("summary + 👍, DISMISSED Codex review on head whose comment is on an earlier commit", "fail",
     with_(summary(), **{R: [codex_review(HEAD, rid=12, state="DISMISSED", body="")],
                         C: [finding(OLD, rid=12, body=badge(3))]}))
case("summary + 👍, a skip notice on an earlier commit only", "success",
     with_(summary(), **{R: [codex_review(OLD, body=SKIP_NOTICE)]}), SUMMARY_EVIDENCE)
case("summary + 👍, Codex reviews unreadable", "fail",
     {k: v for k, v in summary().items() if k != R})
case("summary + 👍, stray Codex inline comment on head without a review", "fail",
     with_(summary(), **{C: [finding(HEAD, body=badge(3), rid=None)]}))
case("summary + 👍, inline comments unreadable", "fail",
     {k: v for k, v in summary().items() if k != C})
case("real summary from opportunity-builder#193 at its head", "success",
     summary(body=(
         "<!-- codex-pull-request-review-summary -->\n\n## Codex Review Summary\n\nThis comment shows the latest "
         "Codex review activity on this pull request.\n\n| Review | Status | Commit | Review trigger |\n"
         "| --- | --- | --- | --- |\n| 📝 **Code Review** | ✅ **Completed** <relative-time datetime=\""
         "2026-09-27T23:11:50.934144Z\">2026-09-27T23:11:50.934144Z</relative-time> | `%s` | PR opened |\n\n\n\n"
         "<details> <summary>ℹ️ About Codex in GitHub</summary>\n<br/>\n\n[Your team has set up Codex to review "
         "pull requests in this repo](https://chatgpt.com/codex/cloud/settings/general). Reviews are triggered "
         "when you\n- Open a pull request for review\n- Mark a draft as ready\n- Comment \"@codex review\" or "
         "\"@codex security review\".\n\nCodex reacts with 👀 while any review is running, comments if it has "
         "suggestions, and reacts with 👍 once all reviews finish with no findings.\n\n</details>") % HEAD[:7]),
     SUMMARY_EVIDENCE)
# The 👍 trails the Completed edit by seconds and fires no event: a run that sees
# the row first polls for it within REACTION_GRACE_SECONDS.
case("👍 arrives during the grace window", "success",
     summary(reacts={"__seq": [[], [reaction()]]}) | {"__env": {"REACTION_GRACE_SECONDS": "15"}}, SUMMARY_EVIDENCE)
# The grace window opens when the reviewer wait ends, not when the run starts
# (opportunity-builder#195): a pull_request_target run that waits past the
# grace length still polls for the 👍. The loop polls every 20 s, then every
# 10 s in the grace window: reads at ~0, ~20, ~40 s miss it, the read at ~50 s
# (inside 21 + 25) finds it; a window counted from the start closed at 25 s.
case("👍 after the reviewer wait, inside a grace window counted from its end", "success",
     summary(reacts={"__seq": [[], [], [], [reaction()]]})
     | {"__env": {"EVENT": "pull_request_target", "PR_ACTION": "synchronize",
                  "WAIT_SECONDS": "21", "REACTION_GRACE_SECONDS": "25"}}, SUMMARY_EVIDENCE)
case("no grace wait when the row is not Completed on head", "fail",
     summary(summary_row(sha=OLD[:7]), reacts={"__seq": [[], [reaction()]]}) | {"__env": {"REACTION_GRACE_SECONDS": "15"}})


# --- Codex's real notice texts, and notice-only review objects (.github#73) ----
# The texts Codex really posts (fas-portal#519, .github#71, aaa-runbooks#165).
USAGE_NOTICE = ("You have reached your Codex usage limits for code reviews. You can see your limits in the "
                "[Codex usage dashboard](https://chatgpt.com/codex/cloud/settings/usage).")
ENV_NOTICE = ("To use Codex here, [create an environment for this repo]"
              "(https://chatgpt.com/codex/cloud/settings/environments).")
ACCOUNT_NOTICE = "To use Codex here, create a Codex account and connect to github."
for label, text in (("usage-limit", USAGE_NOTICE), ("environment", ENV_NOTICE), ("account", ACCOUNT_NOTICE)):
    case(f"Codex {label} notice as the review body on head is not evidence 2", "fail",
         with_(base(), **{R: [codex_review(HEAD, body=text)]}))
    case(f"summary + 👍, Codex {label} notice review on head does not veto the row", "success",
         with_(summary(), **{R: [codex_review(HEAD, body=text)]}), SUMMARY_EVIDENCE)
    case(f"clean comment carrying the Codex {label} notice is not a clean pass", "fail",
         with_(base(), extra={f"/issues/{PR}/comments": [issue_comment(
             CLEAN_BODY.format(sha=HEAD[:10]) + "\n" + text)], **gql("IC_1")}))
# .github#71 at 17ae535: replying to a Codex thread makes Codex post its
# environment notice as a review object on the CURRENT head: empty body, one
# inline comment that is a reply under the old thread.
ORIG_THREAD = finding(OLD, rid=10, body=badge(2))
NOTICE_REPLY = finding(OLD, rid=12, body=ENV_NOTICE, reply_to=900)
case("notice-only review on head (empty body, env-notice reply) is not evidence 2", "fail",
     with_(base(), **{R: [codex_review(OLD, rid=10), codex_review(HEAD, rid=12, body="")],
                      C: [ORIG_THREAD, NOTICE_REPLY]}), "no Codex review object on this head")
case("ten notice-only reviews on head (as on .github#71) are still not evidence 2", "fail",
     with_(base(), **{R: [codex_review(OLD, rid=10)] + [codex_review(HEAD, rid=20 + i, body="") for i in range(10)],
                      C: [ORIG_THREAD] + [finding(OLD, rid=20 + i, body=ENV_NOTICE, reply_to=900) for i in range(10)]}),
     "no Codex review object on this head")
case("notice-only review whose reply is not a notice is still not a review of the head", "fail",
     with_(base(), **{R: [codex_review(OLD, rid=10), codex_review(HEAD, rid=12, body="")],
                      C: [ORIG_THREAD, finding(OLD, rid=12, body="Thanks, noted.", reply_to=900)]}))
case("empty-body review on head whose only comment is a top-level env notice is not evidence 2", "fail",
     with_(base(), **{R: [codex_review(HEAD, rid=12, body="")], C: [finding(HEAD, rid=12, body=ENV_NOTICE)]}))
case("summary + 👍, notice-only review on head does not veto the row", "success",
     with_(summary(), **{R: [codex_review(OLD, rid=10), codex_review(HEAD, rid=12, body="")],
                         C: [ORIG_THREAD, NOTICE_REPLY]}), SUMMARY_EVIDENCE)
case("notice-only reviews plus a real Codex review of the head (P2 only): evidence 2", "success",
     with_(base(), **{R: [codex_review(HEAD, rid=12, body=""), codex_review(HEAD, rid=13)],
                      C: [NOTICE_REPLY, finding(HEAD, rid=13, body=badge(2))]}), "a Codex review of this head")
case("empty-body review with a reply AND a top-level finding is a real review (P2): evidence 2", "success",
     with_(base(), **{R: [codex_review(HEAD, rid=12, body="")],
                      C: [NOTICE_REPLY, finding(HEAD, rid=12, body=badge(2))]}), "a Codex review of this head")
# Only an EMPTY body makes a reply-only review notice-only: a review whose body is
# Codex's own review header naming this head is a review of the head.
case("Codex review with a real body whose only comment is a reply is a review of the head", "success",
     with_(base(), **{R: [codex_review(OLD, rid=10), codex_review(HEAD, rid=12)], C: [ORIG_THREAD, NOTICE_REPLY]}),
     "a Codex review of this head")
case("empty-body review on head with no comments left still blocks (deleted findings)", "fail",
     with_(summary(), **{R: [codex_review(HEAD, rid=12, body="")]}), "no inline findings left")
case("comments of an empty-body Codex review unreadable: fails closed", "fail",
     with_(summary(), **{R: [codex_review(HEAD, rid=12, body="")], C: [NOTICE_REPLY],
                         "__drop": [f"/pulls/{PR}/reviews/12/comments"]}), "could not read the findings")
case("comments of a real Codex review unreadable: fails closed", "fail",
     with_(base(), **{R: [codex_review(HEAD, rid=13)], C: [finding(HEAD, rid=13, body=badge(2))],
                      "__drop": [f"/pulls/{PR}/reviews/13/comments"]}), "could not read the findings")

# --- review/verdict is bound to its PR by external_id (aios-coffee#135) --------
# (name, fixtures, env, expected external_id or None for none)
# The key's base is the live tip of the base branch (gate v2.10 form), never `.base.sha`.
SLASH_BRANCH = "release/2026-10#1"
PUBLISH_CASES = [
    ("published verdict names its PR, the live base tip and head in external_id", summary(), {},
     f"{REPO}#{PR}:{TIP}:{HEAD}:review-verdict"),
    ("a failing verdict is bound to its PR and the live tip too", base(), {},
     f"{REPO}#{PR}:{TIP}:{HEAD}:review-verdict"),
    ("base tip unreadable: keyed `unresolved`, never `.base.sha`",
     with_(summary(), **{"/git/ref/heads/main": "__missing"}), {},
     f"{REPO}#{PR}:unresolved:{HEAD}:review-verdict"),
    ("base ref resolves to a tag object: `unresolved`",
     with_(summary(), **{"/git/ref/heads/main": base_ref_tip(type_="tag")}), {},
     f"{REPO}#{PR}:unresolved:{HEAD}:review-verdict"),
    ("base ref answers for another ref: `unresolved`",
     with_(summary(), **{"/git/ref/heads/main": base_ref_tip(ref="refs/heads/main-old")}), {},
     f"{REPO}#{PR}:unresolved:{HEAD}:review-verdict"),
    ("base tip not a full SHA: `unresolved`",
     with_(summary(), **{"/git/ref/heads/main": base_ref_tip(sha=TIP[:7])}), {},
     f"{REPO}#{PR}:unresolved:{HEAD}:review-verdict"),
    ("a branch name with a slash and a reserved character is read by its encoded ref",
     with_(summary(), **{f"/pulls/{PR}": {**summary()[f"/pulls/{PR}"], "base": {"ref": SLASH_BRANCH, "sha": "b" * 40}},
                         "/git/ref/heads/release/2026-10%231": base_ref_tip(ref=f"refs/heads/{SLASH_BRANCH}")}), {},
     f"{REPO}#{PR}:{TIP}:{HEAD}:review-verdict"),
    ("no PR number and an ambiguous head: published unbound, never a success",
     with_(base(), **{f"/commits/{HEAD}/pulls": [{"number": PR, "state": "open", "head": {"sha": HEAD}},
                                                   {"number": 8, "state": "open", "head": {"sha": HEAD}}]}),
     {"PR_NUMBER": ""}, None),
]


def main():
    assert {"labeled", "unlabeled"} <= set(trigger_types()), "the hold label must re-evaluate the verdict"
    wf = yaml.safe_load(WORKFLOW.read_text())
    # GitHub rejects the WHOLE file if it lists itself under workflow_run
    # ("cannot listen to itself"), so no event runs and no verdict is ever
    # published. Shipped once, 2026-09-29 (.github#55); actionlint misses it.
    on = wf.get("on", wf.get(True, {})) or {}
    listened = (on.get("workflow_run") or {}).get("workflows", []) if isinstance(on, dict) else []
    if wf.get("name") in listened:
        print(f"FAIL  workflow listens to itself via workflow_run ({wf.get('name')!r})")
        return 1
    # One publisher group per PR AND head: verdicts are bound to one PR, so two
    # PRs at one commit must never cancel each other's publisher (Codex P1 on .github#76).
    group = wf["concurrency"]["group"]
    for needle in ("github.event.pull_request.number || github.event.inputs.pr_number",
                   "github.event.pull_request.head.sha || github.event.inputs.head_sha", "github.run_id"):
        if needle not in group:
            print(f"FAIL  concurrency group must key on {needle}")
            return 1
    print("PASS  concurrency group keys on the PR number and the head SHA")
    step = wf["jobs"]["publish"]["steps"][0]
    static_env = {k: str(v) for k, v in step["env"].items() if "${{" not in str(v)}
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        (tmp / "script.sh").write_text(step["run"])
        (tmp / "bin").mkdir()
        (tmp / "bin/gh").write_text(STUB)
        (tmp / "bin/gh").chmod(0o755)
        for name, expect, fx, contains in CASES:
            fx = derive_findings(dict(fx))
            for k in fx.pop("__drop", []):
                fx.pop(k, None)
            if fx.pop("__drop_threads", False):
                fx.pop("graphql:threads:PR_1", None)
            env_over = fx.pop("__env", {})
            (tmp / "fx.json").write_text(json.dumps(fx))
            (tmp / "fx.json.calls").unlink(missing_ok=True)
            env = {"PATH": f"{tmp / 'bin'}:{os.environ['PATH']}", "HOME": os.environ.get("HOME", "/tmp"),
                   **static_env, "REPO": REPO, "EVENT": "workflow_dispatch", "PR_NUMBER": str(PR),
                   "HEAD_SHA": HEAD, "PUBLISH": "0", "WAIT_SECONDS": "0", "DETAILS_URL": "x",
                   "REACTION_GRACE_SECONDS": "0",
                   "FIXTURES": str(tmp / "fx.json"), **env_over}
            out = subprocess.run(["bash", str(tmp / "script.sh")], env=env, capture_output=True, text=True)
            verdict = next((l for l in out.stdout.splitlines() if l.startswith("Verdict:")), "")
            got = "success" if verdict.startswith("Verdict: success") else "fail"
            ok = got == expect and out.returncode == 0 and (contains is None or contains in verdict)
            failures += not ok
            print(f"{'PASS' if ok else 'FAIL'}  {name}  (expected {expect}, got {got})")
            if not ok:
                print(out.stdout[-1500:], out.stderr[-1500:], sep="\n")
        for name, fx, env_over, want in PUBLISH_CASES:
            fx = derive_findings(dict(fx))
            fx = {k: v for k, v in fx.items() if v != "__missing"}
            (tmp / "fx.json").write_text(json.dumps(fx))
            for suffix in (".calls", ".posted"):
                (tmp / ("fx.json" + suffix)).unlink(missing_ok=True)
            env = {"PATH": f"{tmp / 'bin'}:{os.environ['PATH']}", "HOME": os.environ.get("HOME", "/tmp"),
                   **static_env, "REPO": REPO, "EVENT": "workflow_dispatch", "PR_NUMBER": str(PR),
                   "HEAD_SHA": HEAD, "PUBLISH": "1", "WAIT_SECONDS": "0", "DETAILS_URL": "x",
                   "REACTION_GRACE_SECONDS": "0", "FIXTURES": str(tmp / "fx.json"), **env_over}
            out = subprocess.run(["bash", str(tmp / "script.sh")], env=env, capture_output=True, text=True)
            posted_file = tmp / "fx.json.posted"
            posted = [json.loads(l) for l in posted_file.read_text().splitlines()] if posted_file.exists() else []
            ok = out.returncode == 0 and len(posted) == 1 and posted[0].get("head_sha") == HEAD \
                and posted[0].get("external_id") == want and ("external_id" in posted[0]) == (want is not None)
            if want is None:
                ok = ok and posted[0].get("conclusion") != "success"
            failures += not ok
            print(f"{'PASS' if ok else 'FAIL'}  {name}  (external_id {posted[0].get('external_id') if posted else '-'})")
            if not ok:
                print(out.stdout[-1500:], out.stderr[-1500:], sep="\n")
    # The step env and the shell default of REVIEW_SKIP_PATTERN must be one value.
    run_text = step["run"]
    default = run_text.split('REVIEW_SKIP_PATTERN="${REVIEW_SKIP_PATTERN:-', 1)[1].split('}"', 1)[0]
    if default != step["env"]["REVIEW_SKIP_PATTERN"]:
        failures += 1
        print("FAIL  REVIEW_SKIP_PATTERN env and shell default differ")
    else:
        print("PASS  REVIEW_SKIP_PATTERN env and shell default are equal")
    total = len(CASES) + len(PUBLISH_CASES) + 1
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
