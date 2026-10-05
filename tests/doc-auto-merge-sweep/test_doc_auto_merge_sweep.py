#!/usr/bin/env python3
"""Offline tests for doc-auto-merge-sweep.yml.

Extracts the inline script, puts a stub `gh` first on PATH that serves canned
API responses for several repositories (and records merge calls), and runs the
script with no waits. Needs python3 + PyYAML, bash 4+ and jq.

    uv run --with pyyaml python tests/doc-auto-merge-sweep/test_doc_auto_merge_sweep.py
"""
import copy
import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/doc-auto-merge-sweep.yml"
CANONICAL = ROOT / ".github/workflows/doc-auto-merge.yml"

ORG = "acme"
REPO = "acme/widget"
REPO2 = "acme/gadget"
HEAD = "034be02bb328773fbe762ec8c93c14a1bedde66b"
HEAD2 = "2" * 40
NEW_HEAD = "1" * 40
TIP = "a" * 40               # the live base tip
BASE_SHAS = [TIP, "c" * 40, "d" * 40]
LAGGING = "b" * 40            # the pulls API `.base.sha`, lagging the tip
MOVED = "f" * 40
MERGE_SHA = "e" * 40
GATE = "agency-delivery/gate"
GATE_APP = 5021608
CODEX = "chatgpt-codex-connector[bot]"
OLD_HEAD = "9" * 40
VERDICT = ROOT / ".github/workflows/review-verdict.yml"


def ago(minutes, micro=False):
    """An ISO time `minutes` before the case runs (the script reads the real
    clock). A placeholder, resolved by resolve_times when the case starts, so
    a slow suite cannot age a fixture past the Codex wait."""
    return f"__AGO{'M' if micro else ''}_{minutes}__"


def resolve_times(text):
    now = datetime.datetime.now(datetime.timezone.utc)

    def sub(m):
        t = now - datetime.timedelta(minutes=int(m.group(2)))
        return t.strftime("%Y-%m-%dT%H:%M:%S.123456Z" if m.group(1) else "%Y-%m-%dT%H:%M:%SZ")
    return re.sub(r"__AGO(M?)_([0-9]+)__", sub, text)

# Stub gh. Keys are the API path without "repos/" and without the query, e.g.
# "acme/widget/pulls/7", "search/issues", "user". A value
# {"__seq": [a, b, ...]} is served one entry per call (the last repeats);
# "__error" fails the call; {"__pages": [...]} is concatenated like
# --paginate; {"__rate_limited": n, "value": v} fails the first n calls with a
# rate-limit error. After a PUT to <repo>/pulls/<n>/merge, "<key>@merged"
# wins over "<key>" for that PR. fx["__merge_fails"] lists merge keys GitHub
# refuses. --jq output matches gh: strings raw, other JSON compact.
STUB = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
fx = json.load(open(os.environ["FIXTURES"]))
state_dir = os.environ["STATE_DIR"]
args = sys.argv[1:]
if args[:1] != ["api"]:
    sys.exit("stub gh: only `gh api` is supported")
args = args[1:]
jq = None; method = "GET"; fields = {}; path = None; paginate = False
i = 0
while i < len(args):
    a = args[i]
    if a == "--jq": jq = args[i + 1]; i += 2; continue
    if a == "-X": method = args[i + 1]; i += 2; continue
    if a in ("-f", "-F"):
        k, _, v = args[i + 1].partition("="); fields[k] = v; i += 2; continue
    if a == "--paginate": paginate = True; i += 1; continue
    if a == "-i": i += 1; continue
    path = a; i += 1
key = path.lstrip("/").split("?", 1)[0]
if key.startswith("repos/"):
    key = key[len("repos/"):]
if key == "graphql":
    key = "graphql/" + fields.get("id", "") + "/" + fields.get("after", "null")
with open(os.path.join(state_dir, "calls"), "a") as f:
    f.write(json.dumps({"method": method, "key": key, "fields": fields}) + "\n")
parts = key.split("/")
prkey = "/".join(parts[:4]) if len(parts) >= 4 and parts[2] == "pulls" else ""
merged_flag = os.path.join(state_dir, "merged_" + prkey.replace("/", "_"))
if method == "PUT" and key.endswith("/merge"):
    with open(os.path.join(state_dir, "merge_calls"), "a") as f:
        f.write(json.dumps({"key": key, **fields, "token": os.environ.get("GH_TOKEN", "")}) + "\n")
    if key in fx.get("__merge_fails", []):
        sys.stderr.write("HTTP 405: merge refused\n"); sys.exit(1)
    open(merged_flag, "w").close()
    sys.stdout.write(json.dumps({"merged": True})); sys.exit(0)
if method != "GET" or "@codex" in json.dumps(fields):
    sys.exit("stub gh: unexpected %s %s" % (method, key))
if prkey and os.path.exists(merged_flag) and key + "@merged" in fx:
    key = key + "@merged"
if key not in fx:
    sys.stderr.write("stub gh: no fixture for %s\n" % key); sys.exit(1)
v = fx[key]
def bump(name):
    cfile = os.path.join(state_dir, "count_" + name.replace("/", "_"))
    n = int(open(cfile).read()) if os.path.exists(cfile) else 0
    open(cfile, "w").write(str(n + 1))
    return n
if isinstance(v, dict) and "__rate_limited" in v:
    if bump("rl_" + key) < v["__rate_limited"]:
        sys.stderr.write("gh: You have exceeded a secondary rate limit. (HTTP 403)\n"); sys.exit(1)
    v = v["value"]
if isinstance(v, dict) and "__seq" in v:
    v = v["__seq"][min(bump(key), len(v["__seq"]) - 1)]
if v == "__error":
    sys.stderr.write("HTTP 502\n"); sys.exit(1)
if "-i" in sys.argv:
    # Only `gh api -i user` (the quota read): headers, a blank line, the body.
    sys.stdout.write("HTTP/2.0 200 OK\r\nX-Ratelimit-Limit: 5000\r\nX-Ratelimit-Remaining: %s\r\n\r\n{}" % v["remaining"])
    sys.exit(0)
if isinstance(v, dict) and "__pages" in v:
    pages = v["__pages"] if paginate else v["__pages"][:1]
    if jq:
        out = "".join(subprocess.run(["jq", "-r", "-c", jq], input=json.dumps(pg), capture_output=True,
                                     text=True, check=True).stdout for pg in pages)
        sys.stdout.write(out); sys.exit(0)
    sys.stdout.write("".join(json.dumps(pg) for pg in pages)); sys.exit(0)
data = json.dumps(v)
if jq:
    data = subprocess.run(["jq", "-r", "-c", jq], input=data, capture_output=True, text=True, check=True).stdout
sys.stdout.write(data)
'''


def pr(n, repo=REPO, head=HEAD, files=1, draft=False, labels=(), head_repo=None, merged=False, state="open",
       mergeable=True, base_sha=LAGGING, updated=None):
    return {"number": n, "state": state, "merged": merged, "draft": draft, "changed_files": files,
            "mergeable": mergeable, "merge_commit_sha": MERGE_SHA if merged else None,
            "updated_at": updated or ago(120), "node_id": f"PR_{repo}_{n}",
            "labels": [{"name": l} for l in labels],
            "head": {"sha": head, "ref": "docs/x", "repo": {"full_name": head_repo or repo}},
            "base": {"ref": "main", "sha": base_sha}}


def run_(name, conclusion="success", status="completed", slug="github-actions", rid=1, app_id=15368, run=None,
         external_id=None):
    r = {"id": rid, "name": name, "status": status, "conclusion": conclusion if status == "completed" else None,
         "app": {"slug": slug, "id": app_id}}
    if run is not None:
        r["details_url"] = f"https://github.com/{REPO}/actions/runs/{run}/job/{rid + 1000}"
    if external_id is not None:
        r["external_id"] = external_id
    return r


def gate(conclusion, rid, n, base=TIP, head=HEAD, repo=REPO):
    """The delivery gate's check run, bound to PR #n (v2.10 key: live tip)."""
    return run_(GATE, conclusion, slug="agency-delivery-gate", rid=rid, app_id=GATE_APP,
                external_id=f"{repo}#{n}:{base}:{head}:v2.10")


def verdict(conclusion, rid, n, base=TIP, head=HEAD, repo=REPO):
    """review-verdict.yml's check, bound to PR #n (keys on the pulls `.base.sha`)."""
    return run_("review/verdict", conclusion, rid=rid, app_id=15368,
                external_id=f"{repo}#{n}:{base}:{head}:review-verdict")


def tip(sha):
    return {"ref": "refs/heads/main", "object": {"sha": sha, "type": "commit"}}


def repo_fx(repo=REPO, required=(), base_statuses=None, tip_value=None):
    """Per-repository fixtures: base branch tip, last 3 commits, rules."""
    base_statuses = base_statuses or [[], [], []]
    return {
        f"{repo}/git/ref/heads/main": tip_value if tip_value is not None else tip(TIP),
        f"{repo}/commits": [{"sha": s} for s in BASE_SHAS[:len(base_statuses)]],
        **{f"{repo}/commits/{s}/status": {"statuses": list(st)} for s, st in zip(BASE_SHAS, base_statuses)},
        f"{repo}/rules/branches/main": [{"type": "pull_request", "parameters": {}}] + (
            [{"type": "required_status_checks", "parameters": {"required_status_checks": [
                {"context": c, "integration_id": a} for c, a in required]}}] if required else []),
        f"{repo}/branches/main": {"name": "main", "protection": {"enabled": False}},
        f"{repo}/commits/{MERGE_SHA}": {"sha": MERGE_SHA, "parents": [{"sha": TIP}]},
    }


def summary(sha, status="completed", at=None):
    """Codex's live review-summary comment with one row for `sha`."""
    at = at or ago(100, micro=True)
    cell = (f'✅ **Completed** <relative-time datetime="{at}">{at}</relative-time>' if status == "completed"
            else f'🔄 **Running** since <relative-time datetime="{at}">{at}</relative-time>' if status == "running"
            else status)
    body = ("<!-- codex-pull-request-review-summary -->\n\n## Codex Review Summary\n\n"
            "| Review | Status | Commit | Review trigger |\n| --- | --- | --- | --- |\n"
            f"| 📝 **Code Review** | {cell} | `{sha[:7]}` | PR opened |\n\n\n<details>about</details>")
    return {"id": 900, "node_id": "IC_900", "user": {"login": CODEX, "type": "Bot"},
            "performed_via_github_app": {"slug": "chatgpt-codex-connector"}, "body": body,
            "created_at": ago(110), "updated_at": at}


def notice(text="You have reached your Codex usage limits for code reviews. See the dashboard.", at=None, cid=901):
    return {"id": cid, "user": {"login": CODEX, "type": "Bot"},
            "performed_via_github_app": {"slug": "chatgpt-codex-connector"}, "body": text,
            "created_at": at or ago(30), "updated_at": at or ago(30)}


def review(rid, commit=HEAD, body="### Codex Review\n\n**Reviewed commit:** `034be02bb3`", state="COMMENTED", at=None):
    return {"id": rid, "user": {"login": CODEX, "type": "Bot"}, "commit_id": commit, "state": state,
            "body": body, "submitted_at": at or ago(90)}


def finding(cid, rid, sev="P1", commit=HEAD, reply_to=None, body=None, at=None):
    badge = f"**<sub><sub>![{sev} Badge](https://img.shields.io/badge/{sev}-orange?style=flat)</sub></sub>  A finding**" \
        if sev else "**A finding with no badge**"
    return {"id": cid, "user": {"login": CODEX, "type": "Bot"}, "pull_request_review_id": rid,
            "commit_id": commit, "original_commit_id": commit, "in_reply_to_id": reply_to,
            "body": body if body is not None else badge + "\n\nDetails.", "created_at": at or ago(90)}


def thread(sev="P1", resolved=False, outdated=False, author=CODEX, typename="Bot"):
    badge = f"**<sub><sub>![{sev} Badge](x)</sub></sub>  Remove prohibited pricing figures**" if sev else "**No badge**"
    login = author[:-5] if author.endswith("[bot]") else author
    return {"isResolved": resolved, "isOutdated": outdated, "comments": {"nodes": [
        {"author": {"__typename": typename, "login": login}, "path": "docs/a.md", "body": badge}]}}


def threads_fx(repo, n, nodes=()):
    return {f"graphql/PR_{repo}_{n}/null": {"data": {"node": {"reviewThreads": {
        "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": list(nodes)}}}}}


def editors(last="chatgpt-codex-connector", newest="chatgpt-codex-connector", edited=True):
    """GraphQL answer for who last edited a comment (review-verdict's check)."""
    return {"data": {"node": {"lastEditedAt": ago(100) if edited else None,
                              "author": {"login": "chatgpt-codex-connector"}, "editor": {"login": last} if edited else None,
                              "userContentEdits": {"nodes": [{"editor": {"login": newest}}] if edited else []}}}}


def codex_fx(n, repo=REPO, head=HEAD, comments=None, reviews=(), inline=(), threads=(), head_date=None):
    """Codex activity on PR #n. Default: the summary shows `head` Completed, nothing open."""
    comments = list(comments) if comments is not None else [summary(head)]
    return {
        **{f"graphql/{c['node_id']}/null": editors() for c in comments if c.get("node_id")},
        f"{repo}/issues/{n}/comments": comments,
        f"{repo}/pulls/{n}/reviews": list(reviews),
        f"{repo}/pulls/{n}/comments": list(inline),
        f"{repo}/git/commits/{head}": {"sha": head, "committer": {"date": head_date or ago(120)}},
        **threads_fx(repo, n, threads),
    }


def pr_fx(n, repo=REPO, head=HEAD, files=("README.md",), runs=(), statuses=(), codex=None, **prkw):
    entries = [{"filename": f[0], "previous_filename": f[1]} if isinstance(f, tuple) else {"filename": f}
               for f in files]
    prkw.setdefault("files", len(entries))
    return {
        **codex_fx(n, repo=repo, head=head, **(codex or {})),
        f"{repo}/pulls/{n}": pr(n, repo=repo, head=head, **prkw),
        f"{repo}/pulls/{n}@merged": pr(n, repo=repo, head=head, merged=True, state="closed"),
        f"{repo}/pulls/{n}/files": entries,
        f"{repo}/commits/{head}/check-runs": {"check_runs": list(runs)},
        f"{repo}/commits/{head}/status": {"statuses": list(statuses)},
    }


def search(*items, incomplete=False, total=None):
    return {"search/issues": {"total_count": len(items) if total is None else total,
                              "incomplete_results": incomplete,
                              "items": [{"repository_url": f"https://api.github.com/repos/{r}", "number": n}
                                        for r, n in items]}}


RATE = {"user": {"remaining": 4900}}


def world(*parts):
    fx = {}
    for p in parts:
        fx.update(copy.deepcopy(p))
    fx.setdefault("user", RATE["user"])
    return fx


def one(runs=(), required=(), repo_kw=None, **kw):
    """The common case: one PR, acme/widget#7, in the search."""
    return world(search((REPO, 7)), repo_fx(required=required, **(repo_kw or {})), pr_fx(7, runs=runs, **kw))


CASES = []


def case(name, fx, merged=(), exit_ok=True, env=None, says=None, not_says=None, merge_calls=None):
    """merged: the exact set of "repo#n" that must have a merge call.
    exit_ok: the run must exit 0 (True) or non-zero (False).
    says / not_says: substrings of stdout.
    merge_calls: exact number of merge calls (defaults to len(merged))."""
    CASES.append((name, fx, set(merged), exit_ok, env or {}, says, not_says, merge_calls))


W7 = f"{REPO}#7"
GREEN = [run_("unittest", rid=1), run_("lint", "skipped", rid=2)]
REQ_GATE = [(GATE, GATE_APP)]
REQ_VERDICT = [("review/verdict", 15368)]

# ── Eligibility ────────────────────────────────────────────────────────────
case("doc-only, no CI at all: merges", one(), merged={W7})
case("doc-only, CI green: merges", one(runs=GREEN), merged={W7})
case("instruction files, .md and .markdown qualify",
     one(files=("CLAUDE.md", "sub/AGENTS.md", "b.markdown", ".github/pull_request_template.md")), merged={W7})
case("code PR is never merged", one(files=("README.md", "app.py")), says="not documentation-only")
case(".mdx is never merged", one(files=("docs/a.mdx",)))
case("rename .py -> .md is never merged", one(files=(("app.md", "app.py"),)))
case("rename .md -> .mdx is never merged", one(files=(("a.mdx", "a.md"),)))
case("rename between Markdown names qualifies", one(files=(("new.md", "old.md"),)), merged={W7})
case("truncated file list (1 of 2) is never merged", world(one(), {f"{REPO}/pulls/7": pr(7, files=2)}))
case("300 changed files (list complete): not merged", world(one(), {f"{REPO}/pulls/7": pr(7, files=300),
                                                      f"{REPO}/pulls/7/files": [{"filename": f"d/{i}.md"} for i in range(300)]}),
     says="not 1-299")
case("299 changed files: merges", world(one(), {f"{REPO}/pulls/7": pr(7, files=299),
                                                f"{REPO}/pulls/7/files": {"__pages": [
                                                    [{"filename": f"d/{i}.md"} for i in range(100)],
                                                    [{"filename": f"e/{i}.md"} for i in range(100)],
                                                    [{"filename": f"f/{i}.md"} for i in range(99)]]}}),
     merged={W7})
case("0 changed files: not merged", world(one(), {f"{REPO}/pulls/7": pr(7, files=0), f"{REPO}/pulls/7/files": []}))
case("draft never merges (search lagged)", one(draft=True), says="drafts never auto-merge")
case("hold label stops it (search lagged)", one(labels=("no-auto-merge",)), says="a person merges it")
case("labels unreadable: not merged", world(one(), {f"{REPO}/pulls/7": {**pr(7), "labels": None}}),
     says="cannot be ruled out")
case("fork PR never merges", one(head_repo="mallory/widget"), says="fork")
case("already merged: nothing to do", world(one(), {f"{REPO}/pulls/7": pr(7, merged=True, state="closed")}))
case("merge conflict: waits, no merge call", one(mergeable=False), says="merge conflict")
case("mergeability not computed yet (null): merges, GitHub decides", one(mergeable=None), merged={W7})

# ── CI ─────────────────────────────────────────────────────────────────────
case("failing check: not merged, run not red", one(runs=[run_("unittest", "failure")]), says="not green")
case("running check: waits", one(runs=[run_("unittest", status="in_progress")]), says="still running")
case("action_required check: waits", one(runs=[run_("review/verdict", "action_required")]))
case("failing commit status: not merged", one(statuses=[{"context": "ci/x", "state": "failure"}]))
case("pending commit status: waits", one(statuses=[{"context": "ci/x", "state": "pending"}]))
case("required check missing: waits", one(required=[("unittest", 15368)]), says="required check(s) not passed")
case("required check passed: merges", one(runs=GREEN, required=[("unittest", 15368)]), merged={W7})
case("required check from the wrong app: waits",
     one(runs=[run_("unittest", app_id=999)], required=[("unittest", 15368)]))
case("a cancelled CI job (not review-verdict) is red",
     world(one(runs=[run_("unittest", "cancelled", rid=3, run=50)]),
           {f"{REPO}/actions/runs/50": {"id": 50, "path": ".github/workflows/ci.yml"}}), says="cancelled")
case("review-verdict's superseded cancelled job is not a failure",
     world(one(runs=[run_("publish", "cancelled", rid=3, run=51), run_("review/verdict", rid=4)]),
           {f"{REPO}/actions/runs/51": {"id": 51, "path": ".github/workflows/review-verdict.yml"}}), merged={W7})
case("the per-repo doc-auto-merge job still running is not CI",
     one(runs=GREEN + [run_("doc-auto-merge", status="in_progress", rid=9)]), merged={W7})
case("Vercel on the base branch, absent on head: waits",
     one(repo_kw={"base_statuses": [[{"context": "Vercel", "state": "success"}], [], []]}), says="Vercel")
case("Vercel on the base branch, success on head: merges",
     one(repo_kw={"base_statuses": [[{"context": "Vercel", "state": "success"}], [], []]},
         statuses=[{"context": "Vercel", "state": "success"}]), merged={W7})
case("check-runs unreadable: waits", world(one(), {f"{REPO}/commits/{HEAD}/check-runs": "__error"}))
case("rules unreadable: waits", world(one(), {f"{REPO}/rules/branches/main": "__error"}))

# ── Freshness (gate v2.10 keys on the live tip) ────────────────────────────
case("gate bound to this PR, head, live tip: merges", one(runs=[gate("success", 5, 7)], required=REQ_GATE),
     merged={W7})
case("gate keyed on a base that is not the live tip (lagging .base.sha): pending",
     one(runs=[gate("success", 5, 7, base=LAGGING)], required=REQ_GATE), says="older candidate")
case("gate keyed on the live tip but an older head: pending",
     one(runs=[gate("success", 5, 7, head=NEW_HEAD)], required=REQ_GATE), says="older candidate")
case("gate bound to this PR, older candidate, not even required: still pending",
     one(runs=[gate("success", 5, 7, base=LAGGING)]), says="older candidate")
case("older-candidate run is the newest for its name (current one older): pending",
     one(runs=[gate("success", 4, 7), gate("success", 5, 7, base=LAGGING)], required=REQ_GATE),
     says="older candidate")
case("current run is the newest for its name (older-candidate one older): merges",
     one(runs=[gate("success", 4, 7, base=LAGGING), gate("success", 5, 7)], required=REQ_GATE), merged={W7})
case("older-candidate run that FAILED: pending, never red",
     one(runs=[gate("failure", 5, 7, base=LAGGING)], required=REQ_GATE), says="older candidate",
     not_says="CI not green")
case("bound check failed for the live candidate: not green",
     one(runs=[gate("failure", 5, 7)], required=REQ_GATE), says="not green")
case("review/verdict bound to this PR and head, keyed on a stale base: counts, merges",
     one(runs=[verdict("success", 6, 7, base=LAGGING)], required=REQ_VERDICT), merged={W7})
case("review/verdict bound to this PR, older head: pending",
     one(runs=[verdict("success", 6, 7, head=NEW_HEAD)], required=REQ_VERDICT), says="older candidate")
case("review/verdict stale base + gate on live tip, both required: merges",
     one(runs=[verdict("success", 6, 7, base=LAGGING), gate("success", 5, 7)], required=REQ_VERDICT + REQ_GATE),
     merged={W7})
case("review/verdict stale base + gate on a stale base: pending (gate is the base authority)",
     one(runs=[verdict("success", 6, 7, base=LAGGING), gate("success", 5, 7, base=LAGGING)],
         required=REQ_VERDICT + REQ_GATE), says="keyed on live tip")
case("a run from the gate App under another name is held to the live tip too",
     one(runs=[run_("gate-extra", slug="agency-delivery-gate", rid=5, app_id=GATE_APP,
                    external_id=f"{REPO}#7:{LAGGING}:{HEAD}:v2.10")]), says="older candidate")
case("a run named like the gate from another app is held to the live tip too",
     one(runs=[run_(GATE, rid=5, app_id=777, external_id=f"{REPO}#7:{LAGGING}:{HEAD}:v2.10")]),
     says="older candidate")
case("review/verdict bound to ANOTHER PR does not count: pending",
     one(runs=[verdict("success", 6, 8, base=LAGGING)], required=REQ_VERDICT), says="required check(s) not passed")
case("gate bound to ANOTHER PR at the same head does not count: pending",
     one(runs=[gate("success", 5, 8)], required=REQ_GATE), says="required check(s) not passed")
case("another PR's failing gate (older) is ignored for this PR: merges",
     one(runs=[gate("failure", 4, 8), gate("success", 5, 7)], required=REQ_GATE), merged={W7})
case("another PR's failing gate is the NEWEST run GitHub enforces: pending, not red",
     one(runs=[gate("success", 4, 7), gate("failure", 5, 8)], required=REQ_GATE), says="GitHub enforces")
case("unparseable key bound to this PR: pending",
     one(runs=[run_(GATE, slug="agency-delivery-gate", rid=5, app_id=GATE_APP, external_id=f"{REPO}#7:junk")],
         required=REQ_GATE), says="older candidate")
case("gate keyed on another repo's PR #7: never a pass, pending",
     one(runs=[gate("success", 5, 7, repo="acme/other")], required=REQ_GATE), says="older candidate")
case("gate run with no external_id: never a pass, pending",
     one(runs=[run_(GATE, slug="agency-delivery-gate", rid=5, app_id=GATE_APP)], required=REQ_GATE),
     says="older candidate")
case("gate App run with a free-form external_id: pending",
     one(runs=[run_("gate-extra", slug="agency-delivery-gate", rid=5, app_id=GATE_APP, external_id="whatever")]),
     says="older candidate")
case("non-gate check bound to another repo's PR counts as unbound: merges",
     one(runs=[verdict("success", 6, 7, repo="acme/other")], required=REQ_VERDICT), merged={W7})
case("tip unreadable in pass 1: pending, no merge",
     one(runs=[gate("success", 5, 7)], required=REQ_GATE, repo_kw={"tip_value": "__error"}),
     says="could not read the tip")

# ── Codex (Brad, 2026-10-05: doc merges wait briefly for Codex) ──────────────
def cx(**codex):
    return one(codex=codex)


P1_198 = dict(comments=[summary(HEAD)], reviews=[review(50)], inline=[finding(1, 50), finding(2, 50)],
              threads=[thread("P1"), thread("P1")])
case("Codex completed on the head, clean: merges", cx(), merged={W7}, says="summary row Completed")
case("Codex still running on the head: waits (even long past the timeout)",
     cx(comments=[summary(HEAD, "running")]), says="still running", merge_calls=0)
case("nothing from Codex, timeout reached: merges", cx(comments=[]), merged={W7}, says="Codex posted nothing")
case("nothing from Codex, timeout reached: logged as a merge without review", cx(comments=[]), merged={W7},
     says="::notice::")
case("nothing from Codex, timeout not reached: waits",
     world(cx(comments=[], head_date=ago(5)), {f"{REPO}/pulls/7": pr(7, updated=ago(5))}),
     says="5 of 15 min", merge_calls=0)
case("timeout uses the LATER of updated_at and the head commit date (updated_at recent): waits",
     world(cx(comments=[], head_date=ago(300)), {f"{REPO}/pulls/7": pr(7, updated=ago(3))}), says="of 15 min")
case("timeout uses the LATER of updated_at and the head commit date (commit date recent): waits",
     world(cx(comments=[], head_date=ago(3))), says="of 15 min")
case("head commit date unreadable and nothing from Codex: waits, never merges",
     world(cx(comments=[]), {f"{REPO}/git/commits/{HEAD}": "__error"}), says="cannot be estimated")
case("DOC_CODEX_WAIT_MINUTES=60 with 120 min elapsed: merges", cx(comments=[]), merged={W7},
     env={"DOC_CODEX_WAIT_MINUTES": "60"})
case("DOC_CODEX_WAIT_MINUTES=180 with 120 min elapsed: waits", cx(comments=[]), says="of 180 min",
     env={"DOC_CODEX_WAIT_MINUTES": "180"})
case("DOC_CODEX_WAIT_MINUTES=0: red, nothing merged", cx(comments=[]), env={"DOC_CODEX_WAIT_MINUTES": "0"},
     exit_ok=False)
case("DOC_CODEX_WAIT_MINUTES junk: red, nothing merged", cx(), env={"DOC_CODEX_WAIT_MINUTES": "15m"},
     exit_ok=False)
case("aaa-runbooks#198: Completed with two open P1 findings on the head: never merged, not red",
     cx(**P1_198), says="left for a person", not_says="would squash")
case("open P1 on the head is never merged, even long after the timeout",
     cx(comments=[], reviews=[review(50)], inline=[finding(1, 50)], threads=[thread("P1")]),
     says="open P0/P1 finding(s)", merge_calls=0)
case("open P0 thread blocks", cx(threads=[thread("P0")]), says="P0 on docs/a.md")
case("blocked PR is a warning annotation and counted", cx(**P1_198), says="codex-blocked 1")
case("unbadged Codex thread counts as P1: blocks", cx(threads=[thread(None)]), says="unbadged")
# P2/P3: never merged over; fixed (outdated) or resolved by a person (Codex P1s on #84).
P2_HEAD = dict(reviews=[review(50)], inline=[finding(1, 50, "P2"), finding(2, 50, "P3")],
               threads=[thread("P2"), thread("P3")])
case("P2/P3 findings open on the head: held, never merged, not red",
     cx(**P2_HEAD), says="held for the P2/P3 fix round", merge_calls=0)
case("P2/P3 held is a warning annotation and counted as codex-blocked", cx(**P2_HEAD), says="codex-blocked 1")
case("P2/P3 held even long after the timeout with nothing else from Codex",
     cx(comments=[], **P2_HEAD), says="open P2/P3 Codex finding(s)", merge_calls=0)
case("P2/P3 left on an earlier commit, still applying after a push: held",
     cx(reviews=[review(40, commit=OLD_HEAD)], inline=[finding(1, 40, "P2", commit=OLD_HEAD)], threads=[thread("P2")]),
     says="held for the P2/P3 fix round", merge_calls=0)
case("P2/P3 made outdated by the fix push: merges",
     cx(reviews=[review(40, commit=OLD_HEAD)], inline=[finding(1, 40, "P2", commit=OLD_HEAD)],
        threads=[thread("P2", outdated=True)]), merged={W7})
case("P2/P3 threads resolved by a person: merges", cx(reviews=[review(50)], inline=[finding(1, 50, "P2")],
                                                       threads=[thread("P2", resolved=True)]), merged={W7})
case("one P2 resolved, one P3 still open: held", cx(reviews=[review(50)], inline=[finding(1, 50, "P2"), finding(2, 50, "P3")],
                                                    threads=[thread("P2", resolved=True), thread("P3")]),
     says="P3 on docs/a.md", merge_calls=0)
case("P2 open with a P1 open: P0/P1-blocked", cx(threads=[thread("P2"), thread("P1")]),
     says="open P0/P1 finding(s)", merge_calls=0)
case("P2/P3 thread by someone else does not hold: merges", cx(threads=[thread("P2", author="mallory", typename="User")]),
     merged={W7})
case("dry run: P2/P3-held PR is not a would-merge", cx(**P2_HEAD), env={"ENABLED": ""}, not_says="would squash")
case("resolved P1 thread: merges", cx(threads=[thread("P1", resolved=True)]), merged={W7})
case("P1-looking thread by someone else: merges", cx(threads=[thread("P1", author="mallory", typename="User")]),
     merged={W7})
case("findings on an older head (outdated thread) do not block: merges",
     cx(reviews=[review(40, commit=OLD_HEAD)], inline=[finding(1, 40, commit=OLD_HEAD)],
        threads=[thread("P1", outdated=True)]), merged={W7})
case("findings on an older head, nothing from Codex for this head: timeout merges",
     cx(comments=[summary(OLD_HEAD)], reviews=[review(40, commit=OLD_HEAD)], inline=[finding(1, 40, commit=OLD_HEAD)],
        threads=[thread("P1", outdated=True)]), merged={W7}, says="Codex posted nothing")
case("a P1 from an older head that still applies (not outdated) blocks",
     cx(reviews=[review(40, commit=OLD_HEAD)], inline=[finding(1, 40, commit=OLD_HEAD)], threads=[thread("P1")]),
     says="left for a person")
case("older head Completed, this head nothing yet, timeout not reached: waits",
     world(cx(comments=[summary(OLD_HEAD)], head_date=ago(4)), {f"{REPO}/pulls/7": pr(7, updated=ago(4))}),
     says="of 15 min")
case("usage-limit notice + timeout reached: merges, logged as unavailable",
     world(cx(comments=[notice(at=ago(30))], head_date=ago(40)), {f"{REPO}/pulls/7": pr(7, updated=ago(30))}),
     merged={W7}, says="Codex unavailable")
case("usage-limit notice, timeout not reached: waits",
     world(cx(comments=[notice(at=ago(5))], head_date=ago(6)), {f"{REPO}/pulls/7": pr(7, updated=ago(5))}),
     says="Codex unavailable", merge_calls=0)
case("create-an-environment notice (issue comment) + timeout: merges",
     cx(comments=[notice("To use Codex here, [create an environment for this repo](https://x).")]),
     merged={W7}, says="Codex unavailable")
case("create-an-environment reply as an empty review on the head + timeout: merges",
     cx(comments=[], reviews=[review(60, body="")],
        inline=[finding(5, 60, sev=None, reply_to=4, body="To use Codex here, create an environment for this repo.")]),
     merged={W7}, says="Codex unavailable")
case("Running row, then a later usage-limit notice: unavailable, timeout merges",
     cx(comments=[summary(HEAD, "running", at=ago(60, micro=True)), notice(at=ago(50))]), merged={W7},
     says="Codex unavailable")
case("usage-limit notice, then a later Running row (retry): waits",
     cx(comments=[notice(at=ago(60)), summary(HEAD, "running", at=ago(50, micro=True))]), says="still running")
case("notice from before the head commit does not count as unavailable",
     cx(comments=[notice(at=ago(200))]), merged={W7}, says="Codex posted nothing")
case("pass 1 clean, P1 lands during the settle: not merged",
     world(cx(), {f"graphql/PR_{REPO}_7/null": {"__seq": [threads_fx(REPO, 7)[f"graphql/PR_{REPO}_7/null"],
                                                            threads_fx(REPO, 7, [thread("P1")])[f"graphql/PR_{REPO}_7/null"]]}}),
     says="left for a person", merge_calls=0)
case("Codex review on the head with its findings deleted: blocks",
     cx(reviews=[review(50)], inline=[]), says="deleted")
case("Codex review body flagging P1: blocks",
     cx(reviews=[review(50, body="Codex Review ![P1 Badge](x) bad")], inline=[finding(1, 50, "P2")]),
     says="flags P0/P1")
case("Codex review object on the head, no summary: clean, merges",
     cx(comments=[], reviews=[review(50)], inline=[finding(1, 50, "P3")], threads=[thread("P3", resolved=True)]),
     merged={W7}, says="review object on the head")
case("older clean comment naming the head is not evidence (editable): waits for the timeout",
     world(cx(comments=[{**notice(), "body": "Codex Review: Didn't find any major issues. Nice.\n\n**Reviewed commit:** `034be02bb3`"}]),
           {f"{REPO}/pulls/7": pr(7, updated=ago(2))}),
     says="Codex posted nothing", merge_calls=0)
case("unknown status in the head row: waits", cx(comments=[summary(HEAD, "❌ **Failed**")]), says="still running")
case("unreadable summary row: waits", cx(comments=[{**summary(HEAD), "body": summary(HEAD)["body"].replace("`034be02`", "034be02")}]),
     says="cannot be read")
case("summary comment not from the Codex App does not count: timeout path",
     world(cx(comments=[{**summary(HEAD), "performed_via_github_app": None}], head_date=ago(4)),
           {f"{REPO}/pulls/7": pr(7, updated=ago(4))}), says="Codex posted nothing", merge_calls=0)
case("summary edited by a writer (forged Completed row): ignored, waits for the timeout",
     world(cx(head_date=ago(4)), {f"{REPO}/pulls/7": pr(7, updated=ago(4)), "graphql/IC_900/null": editors(last="mallory")}),
     says="last edited by someone other than Codex", merge_calls=0)
case("summary whose newest edit is by a writer: ignored",
     world(cx(head_date=ago(4)), {f"{REPO}/pulls/7": pr(7, updated=ago(4)),
                                  "graphql/IC_900/null": editors(newest="mallory")}), says="of 15 min", merge_calls=0)
case("summary never edited (Codex wrote it Completed): counts", world(cx(), {"graphql/IC_900/null": editors(edited=False)}),
     merged={W7})
case("summary editor unreadable: waits", world(cx(), {"graphql/IC_900/null": "__error"}),
     says="could not read who last edited", merge_calls=0)
case("forged summary over an open P1: still blocked",
     world(cx(threads=[thread("P1")]), {"graphql/IC_900/null": editors(last="mallory")}), says="left for a person")
case("comments unreadable: waits", world(cx(), {f"{REPO}/issues/7/comments": "__error"}), says="could not read the PR comments")
case("reviews unreadable: waits", world(cx(), {f"{REPO}/pulls/7/reviews": "__error"}), says="could not read the PR reviews")
case("inline comments unreadable: waits", world(cx(), {f"{REPO}/pulls/7/comments": "__error"}),
     says="could not read the inline comments")
case("review threads unreadable: waits", world(cx(), {f"graphql/PR_{REPO}_7/null": "__error"}),
     says="could not read the review threads")
case("review threads malformed: waits", world(cx(), {f"graphql/PR_{REPO}_7/null": {"data": None}}),
     says="could not evaluate the review threads")
case("review threads paged: a P1 on page 2 blocks",
     world(cx(), {f"graphql/PR_{REPO}_7/null": {"data": {"node": {"reviewThreads": {
         "pageInfo": {"hasNextPage": True, "endCursor": "C1"}, "nodes": [thread("P2")]}}}},
                  f"graphql/PR_{REPO}_7/C1": threads_fx(REPO, 7, [thread("P1")])[f"graphql/PR_{REPO}_7/null"]}),
     says="left for a person")
case("Codex never gates a code PR (not evaluated)", one(files=("app.py",), codex={"threads": [thread("P1")]}),
     says="not documentation-only")
case("dry run: Codex-blocked PR is not a would-merge", cx(**P1_198), env={"ENABLED": ""}, not_says="would squash")
case("dry run: Codex clean is a would-merge", cx(), env={"ENABLED": ""}, says="would squash-merge")

# ── The merge ──────────────────────────────────────────────────────────────
case("merge call pins head, squash, org token, no title/message", one(), merged={W7})
case("base tip moved before the merge call: refused, no merge, run not red",
     world(one(), {f"{REPO}/git/ref/heads/main": {"__seq": [tip(TIP), tip(TIP), tip(MOVED)]}}), says="base moved")
case("base tip moved between passes: re-judged on the new tip (gate key now stale), no merge",
     world(one(runs=[gate("success", 5, 7)], required=REQ_GATE),
           {f"{REPO}/git/ref/heads/main": {"__seq": [tip(TIP), tip(MOVED)]},
            f"{REPO}/commits": {"__seq": [[{"sha": s} for s in BASE_SHAS], [{"sha": MOVED}]]},
            f"{REPO}/commits/{MOVED}/status": {"statuses": []}}), says="older candidate")
case("tip unreadable right before the merge: red, no merge",
     world(one(), {f"{REPO}/git/ref/heads/main": {"__seq": [tip(TIP), tip(TIP), "__error"]}}), exit_ok=False)
case("squash parent is not the pinned tip: red (merge stands)",
     world(one(), {f"{REPO}/commits/{MERGE_SHA}": {"sha": MERGE_SHA, "parents": [{"sha": MOVED}]}}),
     merged={W7}, exit_ok=False, says="not the evaluated base")
case("squash parent unreadable: red (merge stands)",
     world(one(), {f"{REPO}/commits/{MERGE_SHA}": "__error"}), merged={W7}, exit_ok=False,
     says="parent could not be read")
case("merge reported but PR reads not merged: red",
     world(one(), {f"{REPO}/pulls/7@merged": pr(7)}), merged={W7}, exit_ok=False, says="reads as not merged")
case("GitHub refuses the merge: red",
     world(one(), {"__merge_fails": [f"{REPO}/pulls/7/merge"]}), merged={W7}, exit_ok=False)
case("merge call fails but the PR reads as merged onto the pinned tip: verified, not red",
     world(one(), {"__merge_fails": [f"{REPO}/pulls/7/merge"],
                   f"{REPO}/pulls/7": {"__seq": [pr(7), pr(7), pr(7), pr(7, merged=True, state="closed")]}}),
     merged={W7}, says="although the merge call failed")
case("merge call fails, PR reads as merged onto another parent: red",
     world(one(), {"__merge_fails": [f"{REPO}/pulls/7/merge"],
                   f"{REPO}/pulls/7": {"__seq": [pr(7), pr(7), pr(7), pr(7, merged=True, state="closed")]},
                   f"{REPO}/commits/{MERGE_SHA}": {"sha": MERGE_SHA, "parents": [{"sha": MOVED}]}}),
     merged={W7}, exit_ok=False, says="not the evaluated base")
case("hold label added right before the merge call: refused, no merge",
     world(one(), {f"{REPO}/pulls/7": {"__seq": [pr(7), pr(7), pr(7, labels=("no-auto-merge",))]}}),
     says="now held by")
case("retargeted right before the merge call: refused, no merge",
     world(one(), {f"{REPO}/pulls/7": {"__seq": [pr(7), pr(7), {**pr(7), "base": {"ref": "release", "sha": LAGGING}}]}}),
     says="retargeted from main to release")
case("made a draft right before the merge call: refused, no merge",
     world(one(), {f"{REPO}/pulls/7": {"__seq": [pr(7), pr(7), pr(7, draft=True)]}}), says="became a draft")
case("head moved right before the merge call: refused, no merge",
     world(one(), {f"{REPO}/pulls/7": {"__seq": [pr(7), pr(7), pr(7, head=NEW_HEAD)]}}), says="head moved")
case("PR unreadable right before the merge call: red, no merge",
     world(one(), {f"{REPO}/pulls/7": {"__seq": [pr(7), pr(7), "__error"]}}), exit_ok=False)
case("CI turns red in the final snapshot: not merged",
     world(one(), {f"{REPO}/commits/{HEAD}/check-runs": {"__seq": [
         {"check_runs": [run_("unittest")]}, {"check_runs": [run_("unittest", "failure", rid=2)]}]}}),
     says="final snapshot")
case("hold label added before the final snapshot: not merged",
     world(one(), {f"{REPO}/pulls/7": {"__seq": [pr(7), pr(7, labels=("no-auto-merge",))]}}), says="held by")
case("head pushed between the passes: waits for the next sweep",
     world(one(), pr_fx(7, head=NEW_HEAD), {f"{REPO}/pulls/7": {"__seq": [pr(7), pr(7, head=NEW_HEAD)]}}),
     says="head moved")

# ── Many PRs ───────────────────────────────────────────────────────────────
case("two repos, both green: both merge",
     world(search((REPO, 7), (REPO2, 3)), repo_fx(), repo_fx(REPO2), pr_fx(7), pr_fx(3, repo=REPO2, head=HEAD2)),
     merged={W7, f"{REPO2}#3"})
case("cap: 3 green PRs, MAX_MERGES=2 merges the 2 oldest",
     world(search((REPO, 7), (REPO2, 3), (REPO, 9)), repo_fx(), repo_fx(REPO2),
           pr_fx(7), pr_fx(3, repo=REPO2, head=HEAD2), pr_fx(9, head=NEW_HEAD)),
     merged={W7, f"{REPO2}#3"}, env={"MAX_MERGES": "2"}, says="cap of 2")
case("partial failure: one unreadable PR does not stop the others; run red",
     world(search((REPO, 7), (REPO2, 3)), repo_fx(), repo_fx(REPO2), pr_fx(7),
           pr_fx(3, repo=REPO2, head=HEAD2), {f"{REPO}/pulls/7": "__error"}),
     merged={f"{REPO2}#3"}, exit_ok=False)
case("partial failure in pass 2: a refused merge does not stop the next; run red",
     world(search((REPO, 7), (REPO2, 3)), repo_fx(), repo_fx(REPO2), pr_fx(7),
           pr_fx(3, repo=REPO2, head=HEAD2), {"__merge_fails": [f"{REPO}/pulls/7/merge"]}),
     merged={W7, f"{REPO2}#3"}, exit_ok=False)
case("code PR next to a doc PR: only the doc PR merges",
     world(search((REPO, 7), (REPO2, 3)), repo_fx(), repo_fx(REPO2), pr_fx(7),
           pr_fx(3, repo=REPO2, head=HEAD2, files=("src/x.ts",))), merged={W7})
case("search item from another org or malformed is ignored",
     world(search((REPO, 7), ("evil/repo", 1), ("acme/bad name", 2)), repo_fx(), pr_fx(7)), merged={W7})

# ── Search ─────────────────────────────────────────────────────────────────
case("incomplete search: red, nothing evaluated", world(search((REPO, 7), incomplete=True), repo_fx(), pr_fx(7)),
     exit_ok=False, says=None, merge_calls=0)
case("search over 1000 matches: red", world(search((REPO, 7), total=1001), repo_fx(), pr_fx(7)), exit_ok=False)
case("search fails: red", world({"search/issues": "__error"}), exit_ok=False)
case("incomplete flag on page 2: red",
     world({"search/issues": {"__pages": [
         {"total_count": 2, "incomplete_results": False,
          "items": [{"repository_url": f"https://api.github.com/repos/{REPO}", "number": 7}]},
         {"total_count": 2, "incomplete_results": True, "items": []}]}}, repo_fx(), pr_fx(7)), exit_ok=False)
case("no open PRs: nothing to do", world(search()), says="scanned 0")

# ── Kill switch ────────────────────────────────────────────────────────────
case("switch unset: dry run, no merge call", one(), env={"ENABLED": ""}, says="would squash-merge")
case("switch 'TRUE' (not exactly true): dry run", one(), env={"ENABLED": "TRUE"}, says="would squash-merge")
case("switch 'yes': dry run", one(), env={"ENABLED": "yes"}, says="DRY RUN")
case("switch on, dispatch with dry_run=true: dry run", one(), env={"DRY_RUN_INPUT": "true"},
     says="would squash-merge")
case("switch on, dry_run input empty: dry run", one(), env={"DRY_RUN_INPUT": ""}, says="DRY RUN")
case("dry run still refuses what it would not merge", one(files=("app.py",)), env={"ENABLED": ""},
     not_says="would squash-merge")

# ── Token and rate limits ──────────────────────────────────────────────────
case("no token: red, nothing read", one(), env={"GH_TOKEN": ""}, exit_ok=False)
case("rate limited once on a PR read: backs off, then merges",
     world(one(), {f"{REPO}/pulls/7": {"__rate_limited": 1, "value": pr(7)}}), merged={W7},
     says="api requests")
case("rate limited past the retries: that PR errors, run red",
     world(one(), {f"{REPO}/pulls/7": {"__rate_limited": 5, "value": pr(7)}}), exit_ok=False)
case("core quota low at the start: sweep skipped, not red",
     world(one(), {"user": {"remaining": 10}}), says="rate limit low")
case("quota unreadable: sweep proceeds", world(one(), {"user": "__error"}), merged={W7})
case("quota used is reported from the headers",
     world(one(), {"user": {"__seq": [{"remaining": 4900}, {"remaining": 4870}]}}), merged={W7},
     says="core quota used 30 (left 4870)")
case("run budget spent: remaining PRs deferred, not red",
     world(search((REPO, 7), (REPO2, 3)), repo_fx(), repo_fx(REPO2), pr_fx(7), pr_fx(3, repo=REPO2, head=HEAD2)),
     env={"RUN_BUDGET_SECONDS": "0"}, says="deferred 2")

def static_checks():
    wf = yaml.safe_load(WORKFLOW.read_text())
    on = wf.get("on", wf.get(True))
    assert set(on) == {"schedule", "workflow_dispatch"}, f"triggers must be schedule + dispatch only, got {set(on)}"
    for bad in ("pull_request", "pull_request_target", "workflow_run", "check_run", "status", "push"):
        assert bad not in on, f"no {bad} trigger: this must never run PR code or fire per event"
    assert on["workflow_dispatch"]["inputs"]["dry_run"]["default"] is True, "a dispatch defaults to a dry run"
    assert wf["permissions"] == {}, "GITHUB_TOKEN needs no permissions"
    assert wf["concurrency"]["cancel-in-progress"] is False, "never cancel a sweep between its read and its merge"
    job = wf["jobs"]["sweep"]
    for needle in ("github.ref == 'refs/heads/main'", "github.repository == 'Automation-Architecture/.github'",
                   "vars.DOC_AUTO_MERGE_SWEEP_ENABLED == 'true'"):
        assert needle in job["if"], f"job condition must include {needle}"
    assert job["timeout-minutes"] <= 15, "run time must stay bounded"
    assert all("uses" not in s for s in job["steps"]), "no actions (and so no checkout) in the sweep job"
    assert "checkout" not in WORKFLOW.read_text().split("\non:", 1)[1].replace("No checkout", ""), "no checkout"
    step = job["steps"][0]
    env = step["env"]
    assert env["GH_TOKEN"] == "${{ secrets.AAA_ORG_TOKEN }}", "the org PAT, so merges start post-merge workflows"
    assert env["ENABLED"] == "${{ vars.DOC_AUTO_MERGE_SWEEP_ENABLED }}", "kill switch variable"
    canon = yaml.safe_load(CANONICAL.read_text())["jobs"]["doc-auto-merge"]["steps"][0]["env"]
    for k in ("DOC_ONLY_PATTERN", "HOLD_LABEL", "MAX_FILES", "VERCEL_CONTEXT_PATTERN", "IGNORED_CHECKS"):
        assert str(env[k]) == str(canon[k]), f"{k} must match doc-auto-merge.yml"
    assert str(env["MAX_MERGES"]) == "10"
    assert env["GATE_CHECK"] == GATE and str(env["GATE_APP_ID"]) == str(GATE_APP), "the gate is the base authority"
    venv = {}
    for j in yaml.safe_load(VERDICT.read_text())["jobs"].values():
        for st in j.get("steps", []):
            if "REVIEW_SKIP_PATTERN" in (st.get("env") or {}):
                venv = st["env"]
    for k in ("CODEX_BOT", "CODEX_APP", "REVIEW_SKIP_PATTERN"):
        assert str(env[k]) == str(venv[k]), f"{k} must match review-verdict.yml"
    assert env["DOC_CODEX_WAIT_MINUTES"] == "${{ vars.DOC_CODEX_WAIT_MINUTES || '15' }}", "default wait 15 min"
    assert "@codex" not in step["run"], "the sweep never asks Codex for a review (codex-review-sweep does)"
    return env


def main():
    env_static = {k: str(v) for k, v in static_checks().items() if "${{" not in str(v)}
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        step = yaml.safe_load(WORKFLOW.read_text())["jobs"]["sweep"]["steps"][0]
        (tmp / "script.sh").write_text(step["run"])
        (tmp / "bin").mkdir()
        (tmp / "bin/gh").write_text(STUB)
        (tmp / "bin/gh").chmod(0o755)
        for name, fx, want_merged, exit_ok, env_over, says, not_says, want_calls in CASES:
            state = tmp / "state"
            subprocess.run(["rm", "-rf", str(state)], check=True)
            state.mkdir()
            (tmp / "fx.json").write_text(resolve_times(json.dumps(fx)))
            env = {"PATH": f"{tmp / 'bin'}:{os.environ['PATH']}", "HOME": os.environ.get("HOME", "/tmp"),
                   **env_static, "ORG": ORG, "GH_TOKEN": "org-token", "ENABLED": "true", "DRY_RUN_INPUT": "false",
                   "SETTLE_SECONDS": "0", "BACKOFF_SECONDS": "0", "DOC_CODEX_WAIT_MINUTES": "15",
                   "FIXTURES": str(tmp / "fx.json"), "STATE_DIR": str(state), **env_over}
            out = subprocess.run(["bash", str(tmp / "script.sh")], env=env, capture_output=True, text=True,
                                 timeout=120)
            mc = state / "merge_calls"
            calls = [json.loads(c) for c in mc.read_text().splitlines()] if mc.exists() else []
            got = {"/".join(c["key"].split("/")[:2]) + "#" + c["key"].split("/")[3] for c in calls}
            ok = got == want_merged and (out.returncode == 0) == exit_ok
            ok = ok and len(calls) == (len(want_merged) if want_calls is None else want_calls)
            for c in calls:
                ok = ok and c.get("merge_method") == "squash" and c.get("token") == "org-token" \
                    and "commit_title" not in c and "commit_message" not in c \
                    and c.get("sha") in (HEAD, HEAD2, NEW_HEAD)
                # The head pinned is the PR's head.
                key = c["key"].rsplit("/", 1)[0]
                v = fx.get(key)
                if isinstance(v, dict) and "number" in v:
                    ok = ok and c.get("sha") == v["head"]["sha"]
            if says is not None:
                ok = ok and says in out.stdout + out.stderr
            if not_says is not None:
                ok = ok and not_says not in out.stdout + out.stderr
            failures += not ok
            print(f"{'PASS' if ok else 'FAIL'}  {name}  (merged {sorted(got)}, exit {out.returncode})")
            if os.environ.get("VERBOSE"):
                print("      " + (out.stdout.strip().splitlines() or [""])[-1])
            if not ok:
                print(out.stdout[-2500:], out.stderr[-2500:], sep="\n")
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
