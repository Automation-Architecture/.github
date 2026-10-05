#!/usr/bin/env python3
"""Offline tests for auto-merge-setting-guard.yml.

Extracts the two step scripts (list, report), puts a stub `gh` first on PATH
that serves canned API responses (GraphQL repository pages keyed by cursor,
the org's REST counts, labels, issues) and records every call, then runs the
steps in order exactly as the job does: a step that exits non-zero ends the
run (red). Needs python3 + PyYAML, bash 4+ and jq.

    uv run --with pyyaml python tests/auto-merge-setting-guard/test_auto_merge_setting_guard.py
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
WORKFLOW = ROOT / ".github/workflows/auto-merge-setting-guard.yml"

ORG = "acme"
REPO = "acme/.github"
BOT = "github-actions[bot]"
TITLE = "Native auto-merge is enabled on org repositories"
LABEL = "auto-merge-setting"

# Stub gh. Keys are the API path without a leading "/" or "repos/" and
# without the query. "graphql" is served from fx["graphql"]: {cursor: page},
# the first page under "" and later pages under the `after` field sent. A
# value "__error" fails the call (HTTP 502), "__404" with HTTP 404, "__raw"
# prints non-JSON. {"__pages": [...]} is served one JSON document per page
# with --paginate. Writes (POST/PATCH) are recorded in STATE_DIR/writes and
# answered from fx["<METHOD> <key>"] (default {"id": 1, "number": 99}). -F
# key=@file reads the file, as gh does. --jq output matches gh: strings raw,
# other JSON compact.
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
    if a == "-H": i += 2; continue
    if a in ("-f", "-F"):
        k, _, v = args[i + 1].partition("=")
        if a == "-F" and v.startswith("@"):
            v = open(v[1:]).read()
        if k.endswith("[]"):
            fields.setdefault(k[:-2], []).append(v)
        else:
            fields[k] = v
        i += 2; continue
    if a == "--paginate": paginate = True; i += 1; continue
    path = a; i += 1
full = path.lstrip("/")
key = full.split("?", 1)[0]
if key.startswith("repos/"):
    key = key[len("repos/"):]
rec = {"method": method, "key": key, "query": full.partition("?")[2], "token": os.environ.get("GH_TOKEN", ""),
       "fields": {k: v for k, v in fields.items() if k != "query"}}
with open(os.path.join(state_dir, "calls"), "a") as f:
    f.write(json.dumps(rec) + "\n")
if key == "graphql":
    if "mutation" in fields.get("query", ""):
        sys.exit("stub gh: a GraphQL mutation")
    v = fx["graphql"].get(fields.get("after", ""), "__404")
elif method != "GET":
    with open(os.path.join(state_dir, "writes"), "a") as f:
        f.write(json.dumps(rec) + "\n")
    v = fx.get(method + " " + key, {"id": 1, "number": 99})
    if v == "__error":
        sys.stderr.write("HTTP 502\n"); sys.exit(1)
    sys.stdout.write(json.dumps(v)); sys.exit(0)
elif key not in fx:
    sys.stderr.write("gh: Not Found (HTTP 404)\n"); sys.exit(1)
else:
    v = fx[key]
if v == "__error":
    sys.stderr.write("gh: Server Error (HTTP 502)\n"); sys.exit(1)
if v == "__404":
    sys.stderr.write("gh: Not Found (HTTP 404)\n"); sys.exit(1)
if v == "__raw":
    sys.stdout.write("<html>not json</html>"); sys.exit(0)
pages = v["__pages"] if isinstance(v, dict) and "__pages" in v else [v]
if not paginate:
    pages = pages[:1]
out = ""
for pg in pages:
    data = json.dumps(pg)
    if jq:
        data = subprocess.run(["jq", "-r", "-c", jq], input=data, capture_output=True, text=True, check=True).stdout
    out += data
sys.stdout.write(out)
'''


def repo(name, on=False, archived=False):
    return {"nameWithOwner": f"{ORG}/{name}", "isArchived": archived, "autoMergeAllowed": on}


def quiet_repos(n=3):
    return [repo(".github")] + [repo(f"r{i}") for i in range(n - 1)]


def pages(repos, size=2, total=None):
    """GraphQL responses for `repos`, `size` per page, keyed by cursor."""
    total = len(repos) if total is None else total
    out = {}
    chunks = [repos[i:i + size] for i in range(0, len(repos), size)] or [[]]
    for i, chunk in enumerate(chunks):
        last = i == len(chunks) - 1
        out["" if i == 0 else f"c{i}"] = {"data": {"organization": {"repositories": {
            "totalCount": total, "pageInfo": {"hasNextPage": not last, "endCursor": None if last else f"c{i + 1}"},
            "nodes": chunk}}}}
    return out


def world(repos=None, gql=None, counts=None, label=True, issues=(), **extra):
    """A complete set of fixtures. repos: every org repo (default: 3, all
    off). counts: the REST org counts (default: matching the repos)."""
    repos = quiet_repos() if repos is None else repos
    fx = {
        "graphql": gql if gql is not None else pages(repos),
        f"orgs/{ORG}": counts if counts is not None else {"public_repos": 1, "total_private_repos": len(repos) - 1},
        f"{REPO}/labels/{LABEL}": {"name": LABEL} if label else "__404",
        f"{REPO}/issues": {"__pages": [list(issues)]},
    }
    fx.update(extra)
    return copy.deepcopy(fx)


def issue(n, state="open", user=BOT, body="", title=TITLE, pr=False):
    i = {"number": n, "title": title, "state": state, "user": {"login": user}, "body": body}
    if pr:
        i["pull_request"] = {"url": "x"}
    return i


def mark(*names):
    return "<!-- auto-merge-setting-guard:repos=" + ",".join(f"{ORG}/{n}" for n in names) + " -->"


CASES = []


def case(name, fx, *, ok=True, red_at=None, says=(), not_says=(), env=None, writes=None, flagged=None, check=None):
    """ok: the run is green. red_at: the step that must fail ("list",
    "report"). says / not_says: stdout substrings. writes: the exact list of
    "METHOD key" writes, in order. flagged: the exact flagged repos (short
    names). check(writes, report, calls): extra assertions."""
    CASES.append(dict(name=name, fx=fx, ok=ok, red_at=red_at, says=says, not_says=not_says, env=env or {},
                      writes=writes, flagged=flagged, check=check))


ISSUE_POST = f"POST {REPO}/issues"
LABEL_POST = f"POST {REPO}/labels"
PATCH7 = f"PATCH {REPO}/issues/7"
COMMENT7 = f"POST {REPO}/issues/7/comments"

ONE_ON = quiet_repos() + [repo("hot", on=True)]
TWO_ON = quiet_repos() + [repo("hot", on=True), repo("warm", on=True)]

# ── Detection ──────────────────────────────────────────────────────────────
case("all off, no issue: no writes", world(), flagged=[], writes=[], says=("checked 3 non-archived repos (0 archived); flagged 0",
                                                                          "no open guard issue"))
case("one repo on: opens the issue with label, list and why", world(ONE_ON), flagged=["hot"],
     writes=[ISSUE_POST], says=("opened #99",),
     check=lambda w, r, c: w[0]["fields"]["title"] == TITLE and w[0]["fields"]["labels"] == [LABEL]
     and mark("hot") in w[0]["fields"]["body"] and "Why it must stay off" in w[0]["fields"]["body"]
     and "acme/hot" in w[0]["fields"]["body"] and "never changes settings" in w[0]["fields"]["body"])
case("archived repo with it on: ignored, named in the log", world(quiet_repos() + [repo("old", on=True, archived=True)]),
     flagged=[], writes=[], says=("(1 archived)", "archived with it on (ignored): acme/old"))
case("flagged list is sorted", world(quiet_repos() + [repo("zeta", on=True), repo("alpha", on=True)]),
     flagged=["alpha", "zeta"], check=lambda w, r, c: mark("alpha", "zeta") in w[0]["fields"]["body"])
case("repos on a later page are found (pagination follows cursors)",
     world(quiet_repos(5) + [repo("late", on=True)], gql=pages(quiet_repos(5) + [repo("late", on=True)], size=2)),
     flagged=["late"], check=lambda w, r, c: [x["fields"].get("after", "") for x in c if x["key"] == "graphql"]
     == ["", "c1", "c2"])

# ── Issue lifecycle ────────────────────────────────────────────────────────
case("label missing: created, then the issue", world(ONE_ON, label=False), writes=[LABEL_POST, ISSUE_POST])
case("open issue with the same list: no writes", world(ONE_ON, issues=[issue(7, body=mark("hot") + "\nx")]),
     writes=[], says=("#7 is open and up to date",))
case("open issue, list changed: body updated and a comment",
     world(TWO_ON, issues=[issue(7, body=mark("hot"))]), writes=[PATCH7, COMMENT7], says=("updated #7",),
     check=lambda w, r, c: mark("hot", "warm") in w[0]["fields"]["body"] and "state" not in w[0]["fields"])
case("closed issue: reopened, body updated, comment (no duplicate)",
     world(ONE_ON, issues=[issue(7, state="closed", body=mark("hot"))]),
     writes=[PATCH7, PATCH7, COMMENT7], says=("reopened #7",),
     check=lambda w, r, c: w[0]["fields"] == {"state": "open"})
case("open issue preferred over a newer closed one", world(ONE_ON, issues=[issue(9, state="closed"), issue(7)]),
     writes=[PATCH7, COMMENT7])
case("newest closed issue reused", world(ONE_ON, issues=[issue(3, state="closed"), issue(7, state="closed")]),
     writes=[PATCH7, PATCH7, COMMENT7])
case("all off, open issue: closing comment then closed",
     world(issues=[issue(7, body=mark("hot"))]), writes=[COMMENT7, PATCH7], says=("closed #7",),
     check=lambda w, r, c: w[1]["fields"] == {"state": "closed", "state_reason": "completed"})
case("all off, closed issue: nothing to do", world(issues=[issue(7, state="closed")]), writes=[])
case("an issue by someone else is ignored (public repo)", world(ONE_ON, issues=[issue(7, user="mallory")]),
     writes=[ISSUE_POST])
case("someone else's open issue is not closed", world(issues=[issue(7, user="mallory")]), writes=[])
case("an issue with another title or a PR is ignored",
     world(ONE_ON, issues=[issue(7, title="Other"), issue(8, pr=True)]), writes=[ISSUE_POST])

# ── Dry run ────────────────────────────────────────────────────────────────
case("dry run, repo on: no writes", world(ONE_ON, label=False), env={"DRY_RUN": "true"}, writes=[],
     says=("Dry run: would report 1 repo(s) (acme/hot)",))
case("dry run, all off with open issue: no writes", world(issues=[issue(7)]), env={"DRY_RUN": "true"},
     writes=[], says=("Dry run: would close #7",))
case("bad dry_run value: red", world(), env={"DRY_RUN": "maybe"}, ok=False, red_at="list")

# ── Fails closed on listing errors ─────────────────────────────────────────
case("no token: red, no call", world(), env={"GH_TOKEN_LIST": ""}, ok=False, red_at="list",
     check=lambda w, r, c: c == [])
case("GraphQL call fails: red", world(gql={"": "__error"}), ok=False, red_at="list")
case("GraphQL page 2 fails: red", world(gql={**pages(quiet_repos(4)), "c1": "__error"}), ok=False, red_at="list")
case("GraphQL returns non-JSON: red", world(gql={"": "__raw"}), ok=False, red_at="list")
case("GraphQL errors field: red",
     world(gql={"": {**pages(quiet_repos())[""], "errors": [{"message": "x"}]}}), ok=False, red_at="list",
     says=("GraphQL errors",))
case("no organization: red", world(gql={"": {"data": {"organization": None}}}), ok=False, red_at="list")
case("a null node: red", world(gql=pages(quiet_repos() + [None], size=10)), ok=False, red_at="list")
case("autoMergeAllowed null: red",
     world(gql=pages(quiet_repos() + [{"nameWithOwner": "acme/x", "isArchived": False, "autoMergeAllowed": None}])),
     ok=False, red_at="list", says=("autoMergeAllowed on acme/x",))
case("isArchived missing: red",
     world(gql=pages(quiet_repos() + [{"nameWithOwner": "acme/x", "autoMergeAllowed": False}])),
     ok=False, red_at="list")
case("fewer repos than totalCount: red", world(gql=pages(quiet_repos(), total=4),
                                              counts={"public_repos": 1, "total_private_repos": 3}),
     ok=False, red_at="list", says=("listing is incomplete",))
case("duplicate repo across pages: red",
     world(gql=pages(quiet_repos() + [repo("r0")], size=2, total=4),
           counts={"public_repos": 1, "total_private_repos": 3}), ok=False, red_at="list",
     says=("repeats a repository",))
case("totalCount changes between pages: red",
     world(gql={**pages(quiet_repos(4)), "c1": pages(quiet_repos(4), total=5)["c1"]}), ok=False, red_at="list",
     says=("totalCount changed during the listing",))
case("hasNextPage without endCursor: red",
     world(gql={"": {"data": {"organization": {"repositories": {"totalCount": 9, "pageInfo": {
         "hasNextPage": True, "endCursor": None}, "nodes": quiet_repos()}}}}}), ok=False, red_at="list")
case("a page loop that never ends: red at MAX_PAGES",
     world(gql={"": pages(quiet_repos(4))[""], "c1": {"data": {"organization": {"repositories": {
         "totalCount": 4, "pageInfo": {"hasNextPage": True, "endCursor": "c1"}, "nodes": []}}}}}),
     env={"MAX_PAGES": "5"}, ok=False, red_at="list", says=("did not end within 5 pages",))
case("REST org counts disagree with totalCount: red",
     world(counts={"public_repos": 1, "total_private_repos": 5}), ok=False, red_at="list",
     says=("REST counts 6",))
case("REST org counts missing (token cannot see private counts): red",
     world(counts={"public_repos": 1}), ok=False, red_at="list")
case("REST org read fails: red", world(**{f"orgs/{ORG}": "__error"}), ok=False, red_at="list")
case("listing without this repository: red",
     world([repo("a"), repo("b")], counts={"public_repos": 0, "total_private_repos": 2}), ok=False, red_at="list",
     says=("does not include acme/.github",))

# ── Fails closed on issue errors ───────────────────────────────────────────
case("issue list unreadable: red", world(ONE_ON, **{f"{REPO}/issues": "__error"}), ok=False, red_at="report")
case("label read fails (not 404): red", world(ONE_ON, **{f"{REPO}/labels/{LABEL}": "__error"}), ok=False,
     red_at="report", writes=[])
case("label create fails: red", world(ONE_ON, label=False, **{LABEL_POST: "__error"}), ok=False, red_at="report")
case("issue create fails: red", world(ONE_ON, **{ISSUE_POST: "__error"}), ok=False, red_at="report")
case("issue create returns no number: red", world(ONE_ON, **{ISSUE_POST: {}}), ok=False, red_at="report")
case("body update fails: red, no comment", world(TWO_ON, issues=[issue(7)], **{PATCH7: "__error"}), ok=False,
     red_at="report", writes=[PATCH7])
case("close fails: red", world(issues=[issue(7)], **{PATCH7: "__error"}), ok=False, red_at="report")

# ── Tokens ─────────────────────────────────────────────────────────────────
case("the org PAT only reads; every write uses GITHUB_TOKEN", world(ONE_ON, label=False),
     check=lambda w, r, c: all(x["method"] == "GET" for x in c if x["token"] == "org-pat")
     and all(x["token"] == "gh-token" for x in w)
     and all(x["token"] == "org-pat" for x in c if x["key"] in ("graphql", f"orgs/{ORG}"))
     and len(w) == 2)


def structure_checks(wf):
    """Static checks on the workflow file itself."""
    errs = []
    on = wf.get(True) or wf.get("on")
    if set(on) != {"schedule", "workflow_dispatch"}:
        errs.append(f"triggers are {sorted(on)}")
    cron = on["schedule"][0]["cron"].split()
    if cron[0] in ("0", "00") or len(on["schedule"]) != 1 or cron[2:] != ["*", "*", "*"]:
        errs.append(f"schedule must be one daily cron off the hour, got {on['schedule']}")
    if wf.get("permissions") != {}:
        errs.append("top-level permissions must be {}")
    job = wf["jobs"]["check"]
    if job.get("permissions") != {"issues": "write"}:
        errs.append(f"job permissions are {job.get('permissions')}")
    if "refs/heads/main" not in job.get("if", "") or "Automation-Architecture/.github" not in job.get("if", ""):
        errs.append("job must be limited to main of this repository")
    for st in job["steps"]:
        if "uses" in st:
            errs.append(f"step uses an action ({st['uses']}): no checkout or third-party code")
    pat = [st.get("id") or st["name"] for st in job["steps"] if "AAA_ORG_TOKEN" in json.dumps(st.get("env", {}))]
    if pat != ["list"]:
        errs.append(f"AAA_ORG_TOKEN is exposed to steps {pat}; only the listing may see it")
    lst = job["steps"][0]["run"]
    if "isArchived:" in lst.replace(" ", "") or "/repos" in lst:
        errs.append("the listing must be GraphQL organization.repositories over all repos (no REST list, no filter)")
    if "-X" in lst or "mutation" in lst:
        errs.append("the PAT step must not write")
    if wf["env"].get("ALERT_LABEL") != LABEL or wf["env"].get("ISSUE_TITLE") != TITLE:
        errs.append("ALERT_LABEL / ISSUE_TITLE changed; update the tests deliberately")
    return errs


def main():
    wf = yaml.safe_load(WORKFLOW.read_text())
    errs = structure_checks(wf)
    for e in errs:
        print(f"FAIL  structure: {e}")
    if not errs:
        print("PASS  structure: triggers, permissions, PAT scope, read-only listing")
    steps = {st.get("id", "report"): st for st in wf["jobs"]["check"]["steps"]}
    assert list(steps) == ["list", "report"], list(steps)
    failures = len(errs)
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        (tmp / "bin").mkdir()
        stub = tmp / "bin" / "gh"
        stub.write_text(STUB)
        stub.chmod(0o755)
        for sid, st in steps.items():
            (tmp / f"{sid}.sh").write_text(st["run"])
        for c in CASES:
            state = tmp / "state"
            subprocess.run(["rm", "-rf", str(state)], check=True)
            state.mkdir()
            (tmp / "fx.json").write_text(json.dumps(c["fx"]))
            env_over = dict(c["env"])
            list_token = env_over.pop("GH_TOKEN_LIST", "org-pat")
            base = {"PATH": f"{tmp / 'bin'}:{os.environ['PATH']}", "HOME": os.environ.get("HOME", "/tmp"),
                    **{k: str(v) for k, v in wf["env"].items()},
                    "ORG": ORG, "REPO": REPO, "SELF_REPO": REPO, "RUNNER_TEMP": str(state),
                    "REPORT_FILE": str(state / "report.json"), "BODY_FILE": str(state / "body.md"),
                    "BOT_LOGIN": BOT, "DRY_RUN": "false", "GITHUB_STEP_SUMMARY": str(state / "summary.md"),
                    "FIXTURES": str(tmp / "fx.json"), "STATE_DIR": str(state)}
            base.update(env_over)
            out_all, red_at, outputs = "", None, {}
            for sid in ("list", "report"):
                env = dict(base)
                env["GH_TOKEN"] = list_token if sid == "list" else "gh-token"
                env["GITHUB_OUTPUT"] = str(state / f"out_{sid}")
                if sid == "report":
                    env["DRY_RUN"] = outputs.get("dry_run", "")
                p = subprocess.run(["bash", str(tmp / f"{sid}.sh")], env=env, capture_output=True, text=True,
                                   timeout=60)
                out_all += p.stdout + p.stderr
                of = state / f"out_{sid}"
                if of.exists():
                    for line in of.read_text().splitlines():
                        k, _, v = line.partition("=")
                        outputs[k] = v
                if p.returncode != 0:
                    red_at = sid
                    break
            wfile = state / "writes"
            writes = [json.loads(x) for x in wfile.read_text().splitlines()] if wfile.exists() else []
            cfile = state / "calls"
            calls = [json.loads(x) for x in cfile.read_text().splitlines()] if cfile.exists() else []
            rfile = state / "report.json"
            report = json.loads(rfile.read_text()) if rfile.exists() and red_at != "list" else None
            why = []
            if (red_at is None) != c["ok"]:
                why.append(f"red_at={red_at}")
            if c["red_at"] and red_at != c["red_at"]:
                why.append(f"expected red at {c['red_at']}, got {red_at}")
            if c["writes"] is not None and [f"{w['method']} {w['key']}" for w in writes] != c["writes"]:
                why.append(f"writes {[w['method'] + ' ' + w['key'] for w in writes]}")
            if c["flagged"] is not None and (report is None or report["flagged"] != [f"{ORG}/{n}" for n in c["flagged"]]):
                why.append(f"flagged {None if report is None else report['flagged']}")
            for s in c["says"]:
                if s not in out_all:
                    why.append(f"missing {s!r}")
            for s in c["not_says"]:
                if s in out_all:
                    why.append(f"unexpected {s!r}")
            if c["check"] is not None:
                try:
                    res = c["check"](writes, report, calls)
                except Exception as e:  # noqa: BLE001 - a failed assertion is a test failure
                    res = False
                    why.append(f"check raised {e!r}")
                if not res:
                    why.append("check failed")
            failures += bool(why)
            print(f"{'FAIL' if why else 'PASS'}  {c['name']}" + (f"  ({'; '.join(why)})" if why else ""))
            if why:
                print(out_all[-3000:])
    total = len(CASES) + 1
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
