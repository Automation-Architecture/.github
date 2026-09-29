#!/usr/bin/env python3
"""Offline tests for doc-auto-merge.yml.

Extracts the inline script, puts a stub `gh` first on PATH that serves canned
API responses (and records a merge call), and runs the script with no waits.
Needs python3 + PyYAML, bash and jq.

    uv run --with pyyaml python tests/doc-auto-merge/test_doc_auto_merge.py
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
WORKFLOW = ROOT / ".github/workflows/doc-auto-merge.yml"

REPO = "acme/widget"
PR = 7
HEAD = "034be02bb328773fbe762ec8c93c14a1bedde66b"
NEW_HEAD = "1" * 40

# Stub gh. A fixture value {"__seq": [a, b, ...]} is served one entry per call
# (the last repeats); with no wait the script reads the PR once to qualify
# and once more right before merging. After a PUT to /pulls/<n>/merge, a key
# "<path>@merged" wins over "<path>". A key missing from the fixtures fails
# the call. --jq output matches gh: strings raw, other JSON compact.
STUB = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
fx = json.load(open(os.environ["FIXTURES"]))
state_dir = os.environ["STATE_DIR"]
args = sys.argv[1:]
if args[:1] != ["api"]:
    sys.exit("stub gh: only `gh api` is supported")
args = args[1:]
jq = None; method = "GET"; fields = {}; path = None
i = 0
while i < len(args):
    a = args[i]
    if a == "--jq": jq = args[i + 1]; i += 2; continue
    if a == "-X": method = args[i + 1]; i += 2; continue
    if a in ("-f", "-F"):
        k, _, v = args[i + 1].partition("="); fields[k] = v; i += 2; continue
    if a == "--paginate": i += 1; continue
    path = a; i += 1
if path == "graphql":
    with open(os.path.join(state_dir, "graphql_calls"), "a") as f:
        f.write(json.dumps(fields) + "\n")
    # node ids in the fixtures are PR_<number>
    open(os.path.join(state_dir, "disarmed_" + fields.get("id", "").replace("PR_", "")), "w").close()
    sys.stdout.write("{}"); sys.exit(0)
key = path.split("/repos/" + os.environ["REPO"], 1)[-1].split("?", 1)[0]
# merge / disarm state is per PR number
prnum = key.split("/")[2] if key.startswith("/pulls/") else ""
merged_flag = os.path.join(state_dir, "merged_" + prnum)
if method == "PUT" and key.endswith("/merge"):
    with open(os.path.join(state_dir, "merge_calls"), "a") as f:
        f.write(json.dumps({"path": key, **fields, "token": os.environ.get("GH_TOKEN", "")}) + "\n")
    if fx.get("__merge_fails"):
        sys.stderr.write("HTTP 405: merge refused\n"); sys.exit(1)
    open(merged_flag, "w").close()
    sys.stdout.write(json.dumps({"merged": True})); sys.exit(0)
if method != "GET":
    sys.exit("stub gh: unexpected %s %s" % (method, key))
if os.path.exists(merged_flag) and key + "@merged" in fx:
    key = key + "@merged"
elif os.path.exists(os.path.join(state_dir, "disarmed_" + prnum)) and key + "@disarmed" in fx:
    key = key + "@disarmed"
if key not in fx:
    sys.stderr.write("stub gh: no fixture for %s\n" % key); sys.exit(1)
v = fx[key]
if isinstance(v, dict) and "__seq" in v:
    cfile = os.path.join(state_dir, "count_" + key.replace("/", "_"))
    n = int(open(cfile).read()) if os.path.exists(cfile) else 0
    open(cfile, "w").write(str(n + 1))
    v = v["__seq"][min(n, len(v["__seq"]) - 1)]
if v == "__error":
    sys.stderr.write("HTTP 502\n"); sys.exit(1)
data = json.dumps(v)
if jq:
    data = subprocess.run(["jq", "-r", "-c", jq], input=data, capture_output=True, text=True, check=True).stdout
sys.stdout.write(data)
'''


def pr(files=1, draft=False, labels=(), head_repo=REPO, head=HEAD, base="main", merged=False, state="open",
       number=PR, armed=False):
    return {"number": number, "node_id": f"PR_{number}", "auto_merge": {"merge_method": "squash"} if armed else None,
            "state": state, "merged": merged, "draft": draft, "changed_files": files,
            "labels": [{"name": l} for l in labels],
            "head": {"sha": head, "ref": "docs/x", "repo": {"full_name": head_repo} if head_repo else None},
            "base": {"ref": base, "sha": "b" * 40}}


def run_(name, conclusion="success", status="completed", slug="github-actions", rid=1, app_id=15368, run=None):
    r = {"id": rid, "name": name, "status": status, "conclusion": conclusion if status == "completed" else None,
         "app": {"slug": slug, "id": app_id}}
    if run is not None:
        r["details_url"] = f"https://github.com/{REPO}/actions/runs/{run}/job/{rid + 1000}"
    return r


def actions_run(run, path):
    return {f"/actions/runs/{run}": {"id": run, "path": path}}


def base(files=("README.md",), runs=(), statuses=(), required=(), classic=None, **prkw):
    entries = [{"filename": f[0], "previous_filename": f[1]} if isinstance(f, tuple) else {"filename": f}
               for f in files]
    prkw.setdefault("files", len(entries))
    return {
        f"/pulls/{PR}": pr(**prkw),
        f"/pulls/{PR}@merged": pr(merged=True, state="closed"),
        f"/pulls/{PR}/files": entries,
        f"/commits/{HEAD}/check-runs": {"check_runs": list(runs)},
        f"/commits/{HEAD}/status": {"statuses": list(statuses)},
        "/rules/branches/main": [{"type": "pull_request", "parameters": {}}] + (
            [{"type": "required_status_checks", "parameters": {"required_status_checks": [
                {"context": c, "integration_id": a} for c, a in required]}}] if required else []),
        "/branches/main": {"name": "main", "protection": classic or {"enabled": False}},
    }


def with_(fx, **kw):
    fx = copy.deepcopy(fx)
    fx.update(kw)
    return fx


CASES = []


def case(name, expect, fx, env=None, merged=None, disarmed=None):
    """expect: 'merged' or 'held' (no merge call, exit 0) or 'error' (exit != 0, no merge).
    merged: the exact set of PR numbers that must be merged (multi-PR cases).
    disarmed: the exact set of PR numbers whose native auto-merge must be disabled."""
    CASES.append((name, expect, fx, env or {}, merged, disarmed))


OTHER = 8


def pr_entry(number, head=HEAD, repo=REPO):
    """One item of workflow_run.pull_requests, as GitHub sends it."""
    return {"number": number, "head": {"sha": head}, "base": {"repo": {"url": f"https://api.github.com/repos/{repo}"}}}


def second(files=("README.md",), **prkw):
    """Fixtures for PR #8 at the same head commit as #7."""
    entries = [{"filename": f[0], "previous_filename": f[1]} if isinstance(f, tuple) else {"filename": f}
               for f in files]
    prkw.setdefault("files", len(entries))
    return {f"/pulls/{OTHER}": pr(number=OTHER, **prkw),
            f"/pulls/{OTHER}@merged": pr(number=OTHER, merged=True, state="closed"),
            f"/pulls/{OTHER}/files": entries}


def multi(*numbers, **kw):
    return {"PRS_JSON": json.dumps([pr_entry(n, **kw) for n in numbers]), "PR_NUMBER": "", "HEAD_HINT": HEAD}


GREEN_CI = [run_("unittest", rid=1), run_("lint", "skipped", rid=2)]

case("doc-only, no CI at all: merges", "merged", base())
case("doc-only, CI green: merges", "merged", base(runs=GREEN_CI))
case("instruction files, .md and .markdown qualify", "merged",
     base(files=("CLAUDE.md", "sub/AGENTS.md", "docs/a.md", "b.markdown", ".github/pull_request_template.md",
                 ".claude/skills/x/SKILL.md")))
case(".mdx is never auto-merged", "held", base(files=("docs/a.mdx",)))
case("rename .md -> .mdx is never auto-merged", "held", base(files=(("docs/a.mdx", "docs/a.md"),)))
case("rename .mdx -> .md is never auto-merged", "held", base(files=(("docs/a.md", "docs/a.mdx"),)))
case("mixed .md + .mdx is never auto-merged", "held", base(files=("README.md", "docs/a.mdx")))
case("rename between Markdown names qualifies", "merged", base(files=(("new.md", "old.md"),)))
case("mixed diff is never merged", "held", base(files=("README.md", "app.py")))
case("rename from .py to .md is never merged", "held", base(files=(("app.md", "app.py"),)))
case("truncated file list (1 of 2 files) is never merged", "held", with_(base(), **{f"/pulls/{PR}": pr(files=2)}))
case("file list unreadable: not merged", "held", with_(base(), **{f"/pulls/{PR}/files": "__error"}))
case("3000 changed files: not merged", "held", with_(base(), **{f"/pulls/{PR}": pr(files=3000)}))
case("draft never merges", "held", base(draft=True))
case("hold label stops it", "held", base(labels=("no-auto-merge",)))
case("hold label with native auto-merge armed: disarms it, does not merge", "held",
     with_(base(), **{f"/pulls/{PR}": pr(labels=("no-auto-merge",), armed=True),
                      f"/pulls/{PR}@disarmed": pr(labels=("no-auto-merge",))}))
case("hold label, auto-merge cannot be disarmed: red run", "error",
     with_(base(), **{f"/pulls/{PR}": pr(labels=("no-auto-merge",), armed=True)}))
case("CI turns red in the final snapshot: not merged", "held",
     with_(base(), **{f"/commits/{HEAD}/check-runs": {"__seq": [
         {"check_runs": [run_("unittest")]}, {"check_runs": [run_("unittest", "failure", rid=2)]}]}}))
case("labels unreadable: not merged", "held", with_(base(), **{f"/pulls/{PR}": {**pr(), "labels": None}}))
case("fork PR never merges", "held", base(head_repo="mallory/widget"))
case("already merged: nothing to do", "held", with_(base(), **{f"/pulls/{PR}": pr(merged=True, state="closed")}))
case("failing CI check: not merged", "held", base(runs=[run_("unittest", "failure")]))
case("action_required check (e.g. review/verdict held): waited on, not merged", "held",
     base(runs=[run_("review/verdict", "action_required")]))
case("stale review/verdict action_required, then its re-run passes: merges", "merged",
     with_(base(), **{f"/commits/{HEAD}/check-runs": {"__seq": [
         {"check_runs": [run_("review/verdict", "action_required", rid=1)]},
         {"check_runs": [run_("review/verdict", "action_required", rid=1), run_("review/verdict", rid=2)]}]}}),
     env={"WAIT_SECONDS": "5", "POLL_SECONDS": "0"})
case("failing commit status: not merged", "held", base(statuses=[{"context": "ci/legacy", "state": "failure"}]))
case("status retried: older failure, newer success merges", "merged",
     base(statuses=[{"id": 1, "context": "ci/legacy", "state": "failure", "updated_at": "2026-09-29T10:00:00Z"},
                    {"id": 2, "context": "ci/legacy", "state": "success", "updated_at": "2026-09-29T10:05:00Z"}]))
case("status retried: older success, newer failure holds", "held",
     base(statuses=[{"id": 1, "context": "ci/legacy", "state": "success", "updated_at": "2026-09-29T10:00:00Z"},
                    {"id": 2, "context": "ci/legacy", "state": "failure", "updated_at": "2026-09-29T10:05:00Z"}]))
case("pending status then never settles: not merged", "held",
     base(statuses=[{"context": "vercel", "state": "pending"}]))
case("CI still running at the deadline: not merged", "held", base(runs=[run_("unittest", status="in_progress")]))
case("CI running, then green: merges after waiting", "merged",
     with_(base(), **{f"/commits/{HEAD}/check-runs": {"__seq": [
         {"check_runs": [run_("unittest", status="in_progress")]},
         {"check_runs": [run_("unittest")]}]}}),
     env={"WAIT_SECONDS": "5", "POLL_SECONDS": "0"})
case("newest run of a check wins: failed then re-run green merges", "merged",
     base(runs=[run_("unittest", "failure", rid=1), run_("unittest", rid=2)]))
case("newest run of a check wins: green then re-run failed holds", "held",
     base(runs=[run_("unittest", rid=1), run_("unittest", "failure", rid=2)]))
case("review-verdict's superseded (cancelled) publish job is not a failure", "merged",
     with_(base(runs=[run_("publish", "cancelled", run=900), run_("review/verdict", rid=2), run_("unittest", rid=3)]),
           **actions_run(900, ".github/workflows/review-verdict.yml")))
case("a cancelled newest CI run is a failure", "held",
     with_(base(runs=[run_("unittest", "cancelled", run=901)]), **actions_run(901, ".github/workflows/ci.yml")))
case("a CI job named like review-verdict's still counts when cancelled", "held",
     with_(base(runs=[run_("publish", "cancelled", run=902)]), **actions_run(902, ".github/workflows/release.yml")))
case("cancelled run whose workflow cannot be read: not merged", "held",
     base(runs=[run_("unittest", "cancelled", run=903)]))
case("cancelled then re-run green (newest wins) merges", "merged",
     base(runs=[run_("unittest", "cancelled", rid=1, run=904), run_("unittest", rid=2)]))
case("cancelled non-Actions check is a failure", "held",
     base(runs=[run_("vercel-build", "cancelled", slug="vercel", app_id=8)]))
case("this job's own check is not waited on", "merged",
     base(runs=[run_("doc-auto-merge", status="in_progress"), run_("doc-auto-merge", "cancelled", rid=2)]))
case("Greptile is a reviewer, not CI: its pending check is not waited on", "merged",
     base(runs=[run_("Greptile Review", status="in_progress", slug="greptile-apps", app_id=867647)]))
case("a same-named check from another app is still waited on", "held",
     base(runs=[run_("Greptile Review", status="in_progress", slug="mallory-app", app_id=1)]))
case("required check (ruleset) passed: merges", "merged",
     base(runs=[run_("review/verdict")], required=[("review/verdict", 15368)]))
case("required check absent: not merged", "held", base(required=[("review/verdict", 15368)]))
case("required check from the wrong app does not count", "held",
     base(runs=[run_("review/verdict", slug="other", app_id=99)], required=[("review/verdict", 15368)]))
case("required check cancelled does not pass", "held",
     base(runs=[run_("review/verdict", "cancelled")], required=[("review/verdict", 15368)]))
case("required check listed only in classic protection is enforced", "held",
     base(classic={"enabled": True, "required_status_checks": {"contexts": ["backend"], "checks": [
         {"context": "backend", "app_id": 15368}]}}))
case("classic required check passed: merges", "merged",
     base(runs=[run_("backend")], classic={"enabled": True, "required_status_checks": {
         "contexts": ["backend"], "checks": [{"context": "backend", "app_id": 15368}]}}))
case("base branch with a slash is URL-encoded for the rules and branch lookups", "merged",
     with_({k.replace("/branches/main", "/branches/release%2F1.x"): v for k, v in base(base="release/1.x").items()}))
case("check-runs unreadable: never merges", "held", with_(base(), **{f"/commits/{HEAD}/check-runs": "__error"}))
case("rules unreadable: never merges", "held", with_(base(), **{"/rules/branches/main": "__error"}))
case("hold label added in the final snapshot stops it", "held",
     with_(base(), **{f"/pulls/{PR}": {"__seq": [pr(), pr(labels=("no-auto-merge",))]}}))
case("head moved in the final snapshot: not merged", "held",
     with_(base(), **{f"/pulls/{PR}": {"__seq": [pr(), pr(head=NEW_HEAD)]},
                      f"/commits/{NEW_HEAD}/check-runs": {"check_runs": []}}))
case("stale CI completion for an older commit: refuses, never evaluates the new head", "held",
     base(), env={"HEAD_HINT": NEW_HEAD})
case("CI completion for the current head: merges", "merged", base(), env={"HEAD_HINT": HEAD})
case("head moves while waiting: this run stops", "held",
     with_(base(), **{f"/pulls/{PR}": {"__seq": [pr(), pr(head=NEW_HEAD)]},
                      f"/commits/{HEAD}/check-runs": {"check_runs": [run_("unittest", status="in_progress")]}}),
     env={"WAIT_SECONDS": "5", "POLL_SECONDS": "0"})
case("GitHub refuses the merge: red run", "error", with_(base(), __merge_fails=True))
case("merge refused because another run already merged it: not an error", "merged",
     with_(base(), __merge_fails=True, **{f"/pulls/{PR}": {"__seq": [pr(), pr(), pr(merged=True, state="closed")]}}))

# --- CI completion naming several PRs (Brad, 2026-09-29) ---------------------
case("CI run names a doc PR and a code PR: only the doc PR merges", "merged",
     with_(base(), **second(files=("README.md", "app.py"))), env=multi(PR, OTHER), merged={PR})
case("CI run names a held PR first: the second still merges", "merged",
     with_(base(labels=("no-auto-merge",)), **second()), env=multi(PR, OTHER), merged={OTHER})
case("CI run names a held, armed PR first: it is disarmed and the second still merges", "merged",
     with_(base(), **{f"/pulls/{PR}": pr(labels=("no-auto-merge",), armed=True),
                      f"/pulls/{PR}@disarmed": pr(labels=("no-auto-merge",))}, **second()),
     env=multi(PR, OTHER), merged={OTHER}, disarmed={PR})
case("CI run names two doc PRs: both merge", "merged",
     with_(base(), **second()), env=multi(PR, OTHER), merged={PR, OTHER})
case("CI run: one PR cannot be read, the other still merges (red run)", "error",
     with_(base(), **{f"/pulls/{PR}": "__error"}, **second()), env=multi(PR, OTHER), merged={OTHER})
case("CI run: a PR whose head is not the run's head is not evaluated", "merged",
     with_(base(), **second()), env={**multi(PR), "PRS_JSON": json.dumps([pr_entry(PR), pr_entry(OTHER, head=NEW_HEAD)])},
     merged={PR})
case("CI run: a PR based in another repository is not evaluated", "held",
     with_(base(), **second()), env={**multi(PR), "PRS_JSON": json.dumps([pr_entry(OTHER, repo="mallory/widget")])},
     merged=set())
case("CI run naming no PR at its head: nothing to do", "held", base(), env={**multi(), "PRS_JSON": "[]"})

# --- auto_merge_enabled: a refused PR is disarmed at once (Brad, 2026-09-29) --
case("auto-merge armed on a held doc PR: disarmed, not merged", "held",
     with_(base(), **{f"/pulls/{PR}": pr(labels=("no-auto-merge",), armed=True),
                      f"/pulls/{PR}@disarmed": pr(labels=("no-auto-merge",))}), disarmed={PR})
case("auto-merge armed on a code PR: disarmed, not merged", "held",
     with_(base(files=("app.py",)), **{f"/pulls/{PR}": pr(armed=True), f"/pulls/{PR}@disarmed": pr()}), disarmed={PR})
case("auto-merge armed on a mixed Markdown + code PR: disarmed", "held",
     with_(base(files=("README.md", "src/x.ts")), **{f"/pulls/{PR}": pr(files=2, armed=True), f"/pulls/{PR}@disarmed": pr(files=2)}), disarmed={PR})
case("auto-merge armed on a fork PR: disarmed", "held",
     with_(base(), **{f"/pulls/{PR}": pr(head_repo="mallory/widget", armed=True),
                      f"/pulls/{PR}@disarmed": pr(head_repo="mallory/widget")}), disarmed={PR})
case("auto-merge armed on a PR whose files cannot be read: disarmed", "held",
     with_(base(), **{f"/pulls/{PR}": pr(armed=True), f"/pulls/{PR}@disarmed": pr(), f"/pulls/{PR}/files": "__error"}),
     disarmed={PR})
case("auto-merge armed on a qualifying doc PR is left alone (it merges)", "merged",
     with_(base(), **{f"/pulls/{PR}": pr(armed=True)}), disarmed=set())
case("auto-merge armed on a doc PR whose optional CI failed: disarmed", "held",
     with_(base(runs=[run_("unittest", "failure")]), **{f"/pulls/{PR}": pr(armed=True), f"/pulls/{PR}@disarmed": pr()}),
     disarmed={PR})
case("auto-merge armed on a doc PR whose CI never settled: disarmed", "held",
     with_(base(runs=[run_("unittest", status="in_progress")]),
           **{f"/pulls/{PR}": pr(armed=True), f"/pulls/{PR}@disarmed": pr()}), disarmed={PR})
case("auto-merge armed, CI turns red in the final snapshot: disarmed", "held",
     with_(base(), **{f"/pulls/{PR}": pr(armed=True), f"/pulls/{PR}@disarmed": pr(),
                      f"/commits/{HEAD}/check-runs": {"__seq": [
                          {"check_runs": [run_("unittest")]}, {"check_runs": [run_("unittest", "failure", rid=2)]}]}}),
     disarmed={PR})
case("code PR with auto-merge that will not disarm: red run", "error",
     {k: v for k, v in with_(base(files=("app.py",)), **{f"/pulls/{PR}": pr(armed=True)}).items()}, disarmed={PR})
case("merge call succeeds but PR not merged: red run", "error",
     {k: v for k, v in base().items() if not k.endswith("@merged")} | {f"/pulls/{PR}": {"__seq": [pr(), pr(), pr(), pr()]}})


def main():
    wf = yaml.safe_load(WORKFLOW.read_text())
    group = wf["concurrency"]["group"]
    for needle in ("github.event.pull_request.number", "github.event.pull_request.head.sha",
                   "github.event.workflow_run.head_sha", "github.run_id"):
        assert needle in group, f"concurrency group must key on {needle}"
    assert "pull_requests[0]" not in group, "a CI completion must not be keyed on its first PR only"
    on = wf.get("on", wf.get(True))
    assert "auto_merge_enabled" in on["pull_request_target"]["types"], "auto_merge_enabled must trigger"
    assert "paths" not in on["pull_request_target"], "code PRs (no Markdown) must be seen when auto-merge is armed"
    assert "pull_request" not in on, "never run the PR's own copy of this workflow (org PAT in reach; Codex on #60)"
    step = wf["jobs"]["doc-auto-merge"]["steps"][0]
    static_env = {k: str(v) for k, v in step["env"].items() if "${{" not in str(v)}
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        (tmp / "script.sh").write_text(step["run"])
        (tmp / "bin").mkdir()
        (tmp / "bin/gh").write_text(STUB)
        (tmp / "bin/gh").chmod(0o755)
        for name, expect, fx, env_over, want_merged, want_disarmed in CASES:
            state = tmp / "state"
            subprocess.run(["rm", "-rf", str(state)], check=True)
            state.mkdir()
            (tmp / "fx.json").write_text(json.dumps(fx))
            env = {"PATH": f"{tmp / 'bin'}:{os.environ['PATH']}", "HOME": os.environ.get("HOME", "/tmp"),
                   **static_env, "REPO": REPO, "PR_NUMBER": str(PR), "GH_TOKEN": "read-token",
                   "MERGE_TOKEN": "merge-token", "SETTLE_SECONDS": "0", "WAIT_SECONDS": "0",
                   "POLL_SECONDS": "0", "FIXTURES": str(tmp / "fx.json"), "STATE_DIR": str(state), **env_over}
            out = subprocess.run(["bash", str(tmp / "script.sh")], env=env, capture_output=True, text=True)
            calls = (state / "merge_calls").read_text().splitlines() if (state / "merge_calls").exists() else []
            calls = [json.loads(c) for c in calls]
            gq = (state / "graphql_calls").read_text().splitlines() if (state / "graphql_calls").exists() else []
            disarmed_prs = {int(json.loads(c)["id"].replace("PR_", "")) for c in gq}
            merged_prs = {int(c["path"].split("/")[2]) for c in calls}
            if out.returncode != 0:
                got = "error"
            elif calls:
                got = "merged"
            else:
                got = "held"
            if want_merged is not None and got != "error":
                got = "merged" if merged_prs else "held"
            ok = got == expect
            if want_merged is not None:
                ok = ok and merged_prs == set(want_merged) and len(calls) == len(merged_prs)
            if want_disarmed is not None:
                ok = ok and disarmed_prs == set(want_disarmed)
            if got == "merged" and want_merged is None:
                c = calls[0]
                ok = ok and len(calls) == 1 and c.get("sha") == HEAD and c.get("merge_method") == "squash" \
                    and c.get("token") == "merge-token"
            if expect == "error":
                ok = ok and all(c.get("sha") == HEAD for c in calls)
            failures += not ok
            print(f"{'PASS' if ok else 'FAIL'}  {name}  (expected {expect}, got {got})")
            if os.environ.get("VERBOSE"):
                print("      " + (out.stdout.strip().splitlines() or [""])[-1])
            if not ok:
                print(out.stdout[-1500:], out.stderr[-1500:], sep="\n")
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
