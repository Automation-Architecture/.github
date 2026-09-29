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
    key = ("graphql:threads:" if "reviewThreads" in fields.get("query", "") else "graphql:") + fields["id"]
else:
    key = path.split("/repos/" + os.environ["REPO"], 1)[-1].split("?", 1)[0]
if key not in fx:
    sys.stderr.write("stub gh: no fixture for %s\n" % key); sys.exit(1)
v = fx[key]
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


def codex_review(commit, rid=11, state="COMMENTED", at="2026-09-27T23:12:30Z", body=None):
    return {"id": rid, "user": {"login": CODEX, "type": "Bot"}, "state": state, "commit_id": commit,
            "submitted_at": at,
            "body": body if body is not None else "### Codex Review\n\n**Reviewed commit:** `%s`" % commit[:10]}


def badge(p):
    return "**<sub><sub>![P%d Badge](https://img.shields.io/badge/P%d-orange?style=flat)</sub></sub>  Finding" % (p, p)


def finding(original, rid=11, login=CODEX, body="Something is wrong here", resolved=False, outdated=None):
    """An inline review comment that starts a thread. By default a finding on an
    earlier commit is outdated (a later push rewrote its lines). The harness
    turns these into the GraphQL review threads and each review's comments."""
    return {"user": {"login": login}, "pull_request_review_id": rid, "body": body,
            "original_commit_id": original, "commit_id": HEAD, "resolved": resolved,
            "outdated": (original != HEAD) if outdated is None else outdated}


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
        "path": "a.py", "body": f["body"]}]}} for f in found]
    fx["graphql:threads:PR_1"] = {"data": {"node": {"reviewThreads": {
        "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}}}
    for r in fx.get(f"/pulls/{PR}/reviews") or []:
        if "id" in r:
            fx[f"/pulls/{PR}/reviews/{r['id']}/comments"] = [
                {"id": i} for i, f in enumerate(found) if f["pull_request_review_id"] == r["id"]]
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
            if fx.pop("__drop_threads", False):
                fx.pop("graphql:threads:PR_1", None)
            env_over = fx.pop("__env", {})
            (tmp / "fx.json").write_text(json.dumps(fx))
            env = {"PATH": f"{tmp / 'bin'}:{os.environ['PATH']}", "HOME": os.environ.get("HOME", "/tmp"),
                   **static_env, "REPO": REPO, "EVENT": "workflow_dispatch", "PR_NUMBER": str(PR),
                   "HEAD_SHA": HEAD, "PUBLISH": "0", "WAIT_SECONDS": "0", "DETAILS_URL": "x",
                   "FIXTURES": str(tmp / "fx.json"), **env_over}
            out = subprocess.run(["bash", str(tmp / "script.sh")], env=env, capture_output=True, text=True)
            verdict = next((l for l in out.stdout.splitlines() if l.startswith("Verdict:")), "")
            got = "success" if verdict.startswith("Verdict: success") else "fail"
            ok = got == expect and out.returncode == 0 and (contains is None or contains in verdict)
            failures += not ok
            print(f"{'PASS' if ok else 'FAIL'}  {name}  (expected {expect}, got {got})")
            if not ok:
                print(out.stdout[-1500:], out.stderr[-1500:], sep="\n")
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
