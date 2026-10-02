#!/usr/bin/env python3
"""Offline tests for codex-review-sweep.yml (the scheduled org-wide Codex nudge).

Extracts the `find` and `nudge` steps' inline scripts, puts a stub `gh` first on
PATH that serves canned API responses, and checks which PR heads the sweep
targets and what it posts. Also asserts the workflow's security shape and that
its nudge rules stay in step with codex-auto-request.yml and doc-auto-merge.yml.
Needs python3 + PyYAML, bash, jq.

    uv run --with pyyaml python tests/codex-auto-request/test_codex_review_sweep.py
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
WORKFLOW = ROOT / ".github/workflows/codex-review-sweep.yml"
EVENT_WORKFLOW = ROOT / ".github/workflows/codex-auto-request.yml"
DOC_AUTO_MERGE = ROOT / ".github/workflows/doc-auto-merge.yml"
REVIEW_VERDICT = ROOT / ".github/workflows/review-verdict.yml"

ORG = "acme"
CODEX = "chatgpt-codex-connector[bot]"
SUMMARY = "<!-- codex-pull-request-review-summary -->"
NOW = 1759320000                     # 2025-10-01T12:00:00Z, fixed "now" for the age check
OLD_ENOUGH = "2025-10-01T11:00:00Z"  # an hour before NOW
TOO_NEW = "2025-10-01T11:55:00Z"     # five minutes before NOW
GATE = "agency-delivery/gate"

# Stub gh: `gh api [-X M] [--paginate] [--jq Q] [-f k=v] <path>`. Fixtures are
# keyed by the path without its query string. "__error" fails the call. Writes
# are recorded; the find step must never write.
STUB = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
fx = json.load(open(os.environ["FIXTURES"]))
state = os.environ["STATE_DIR"]
args = sys.argv[1:]
if args[:1] != ["api"]:
    sys.exit("stub gh: only `gh api` is supported")
args = args[1:]
jq = None; method = "GET"; path = None; fields = {}
i = 0
while i < len(args):
    a = args[i]
    if a == "--jq": jq = args[i + 1]; i += 2; continue
    if a == "-X": method = args[i + 1]; i += 2; continue
    if a in ("-f", "-F"):
        k, _, v = args[i + 1].partition("="); fields[k] = v; i += 2; continue
    if a == "--paginate": i += 1; continue
    path = a; i += 1
key = path.split("?", 1)[0]
if key == "graphql":
    key = "graphql " + fields.get("owner", "") + "/" + fields.get("name", "") + "#" + fields.get("number", "")
open(os.path.join(state, "calls"), "a").write(json.dumps({"method": method, "path": path, "fields": fields,
                                                          "token": os.environ.get("GH_TOKEN", "")}) + "\n")
if method != "GET":
    val = fx.get("POST " + key, {"html_url": "https://github.com/" + key})
else:
    if key not in fx:
        sys.exit(f"stub gh: no fixture for {key}")
    val = fx[key]
if val == "__error":
    sys.stderr.write("HTTP 500\n"); sys.exit(1)
doc = json.dumps(val)
if jq is None:
    sys.stdout.write(doc); sys.exit(0)
out = subprocess.run(["jq", "-r", "-c", jq], input=doc, capture_output=True, text=True)
sys.stdout.write(out.stdout); sys.stderr.write(out.stderr); sys.exit(out.returncode)
'''


def sha(c):
    return (c * 40)[:40]


def comment(user, body, at="2025-10-01T11:10:00Z", type_=None):
    return {"user": {"login": user, "type": type_ or ("Bot" if user.endswith("[bot]") else "User")},
            "created_at": at, "body": body}


def summary(status, head, at="2025-10-01T11:05:00Z"):
    return comment(CODEX, "\n".join([SUMMARY, "", "| Review | Status | Commit | Trigger |", "| --- | --- | --- | --- |",
                                      f"| 📝 **Code Review** | {status} | `{head[:7]}` | PR opened |"]), at=at)


def pr_fx(repo, n, head, *, state="open", draft=False, fork=False, committed=OLD_ENOUGH, gate=1,
          files=("app.py",), renamed=None, changed=None, labels=(), comments=(), reviews=(), retarget=None,
          gate_app=5021608):
    """Fixtures for one PR in `repo`."""
    listed = [{"filename": f, "status": "modified"} for f in files]
    for new, old in (renamed or {}).items():
        listed.append({"filename": new, "status": "renamed", "previous_filename": old})
    full = f"{ORG}/{repo}"
    r = f"repos/{full}"
    return {
        f"{r}/pulls/{n}": {"number": n, "state": state, "draft": draft,
                           "head": {"sha": head, "repo": {"full_name": "someone/fork" if fork else full}},
                           "changed_files": len(listed) if changed is None else changed,
                           "labels": [{"name": l} for l in labels], "user": {"login": "dependabot[bot]"}},
        f"{r}/commits/{head}": {"commit": {"committer": {"date": committed}}},
        f"{r}/commits/{head}/check-runs": gate if isinstance(gate, str) else
            {"total_count": gate, "check_runs": [{"name": GATE, "app": {"id": gate_app}} for _ in range(gate)]},
        f"graphql {full}#{n}": retarget if retarget == "__error" else
            {"data": {"repository": {"pullRequest": {"timelineItems": {"nodes": [{"createdAt": retarget}] if retarget else []}}}}},
        f"{r}/pulls/{n}/files": listed,
        f"{r}/issues/{n}/comments": list(comments),
        f"{r}/pulls/{n}/reviews": list(reviews),
    }


def world(*prs, search=None):
    """prs: (repo, number, fixtures) tuples, listed by the search in this order."""
    fx = {"search/issues": search if search is not None else
          {"total_count": len(prs), "incomplete_results": False,
           "items": [{"repository_url": f"https://api.github.com/repos/{ORG}/{repo}", "number": n}
                     for repo, n, _ in prs]}}
    for _, _, f in prs:
        fx.update(f)
    return fx


def one(**kw):
    head = kw.pop("head", sha("a"))
    return world(("widget", 7, pr_fx("widget", 7, head, **kw)))


CASES = []


def case(name, fx, expect_targets, env=None, expect_rc=0, expect_unreadable="0"):
    """expect_targets: list of 'repo#n' the sweep should nudge, in order."""
    CASES.append((name, fx, expect_targets, env or {}, expect_rc, expect_unreadable))


A = sha("a")

# --- nudges -------------------------------------------------------------------------
case("Dependabot PR Codex has not touched: nudged", one(), ["widget#7"])
case("Codex rows and reviews for an OLDER head only: nudged",
     one(comments=[summary("✅ **Completed**", sha("b"))], reviews=[{"user": {"login": CODEX}, "commit_id": sha("b")}]),
     ["widget#7"])
case("a request BEFORE the head was committed does not count: nudged",
     one(comments=[comment("web3sea", "@codex review", at="2025-10-01T10:59:59Z")]), ["widget#7"])
case("a bot's request never counts (Codex refuses bots): nudged",
     one(comments=[comment("claude[bot]", "@codex review"), comment("renovate", "@codex review", type_="Bot")]),
     ["widget#7"])
case("Codex's 'create an environment' reply is not a row or a quota notice: nudged",
     one(comments=[comment(CODEX, "To use Codex here, [create an environment for this repo](https://x).")]),
     ["widget#7"])
case("a usage-limit notice followed by a newer, different Codex reply: nudged",
     one(comments=[comment(CODEX, "You have reached your Codex usage limits for code reviews.", at="2025-10-01T11:05:00Z"),
                   comment(CODEX, "To use Codex here, create an environment.", at="2025-10-01T11:20:00Z")]),
     ["widget#7"])
case("a usage-limit notice from BEFORE the head: nudged",
     one(comments=[comment(CODEX, "You have reached your Codex usage limits for code reviews.", at="2025-10-01T10:00:00Z")]),
     ["widget#7"])
case(".mdx is code: nudged", one(files=("README.md", "page.mdx")), ["widget#7"])
case("Markdown-only but held by no-auto-merge: nudged", one(files=("README.md",), labels=("no-auto-merge",)),
     ["widget#7"])
case("Markdown-only with unreadable labels: nudged",
     (lambda fx: (fx[f"repos/{ORG}/widget/pulls/7"].update(labels=None), fx)[1])(one(files=("README.md",))),
     ["widget#7"])
case("300+ changed files is beyond the doc bound: nudged", one(files=("README.md",), changed=300), ["widget#7"])

# --- skips ---------------------------------------------------------------------------
for st in ("✅ **Completed**", "🔄 **Running**", "⏳ **Queued**", "⚠️ **Failed**"):
    case(f"summary row {st} for the head: skipped", one(comments=[summary(st, A)]), [])
case("Codex review on the head (any state): skipped",
     one(reviews=[{"user": {"login": CODEX}, "commit_id": A, "state": "DISMISSED"}]), [])
case("a person's @codex review since the head: skipped",
     one(comments=[comment("web3sea", "@codex review")]), [])
case("our own earlier nudge (web3sea) for the head: skipped (once per head)",
     one(comments=[comment("web3sea", "@codex review", at=OLD_ENOUGH)]), [])
case("@codex security review counts: skipped", one(comments=[comment("bradh", "@CODEX security review")]), [])
case("Codex's latest reply is a usage-limit notice: skipped",
     one(comments=[comment(CODEX, "You have reached your Codex usage limits for code reviews. ...")]), [])
case("head younger than MIN_AGE_SECONDS: skipped", one(committed=TOO_NEW), [])
case("no agency-delivery/gate check on the head: skipped", one(gate=0), [])
case("draft (search index lag): skipped", one(draft=True), [])
case("closed (search index lag): skipped", one(state="closed"), [])
case("fork PR: skipped", one(fork=True), [])
case("Markdown-only: skipped", one(files=("README.md", "docs/a.markdown")), [])
case("Markdown rename from Markdown: skipped", one(files=("a.md",), renamed={"b.md": "old/b.md"}), [])
case("Markdown with an unrelated label: skipped", one(files=("README.md",), labels=("documentation",)), [])

case("gate-named check from another producer (not the delivery App): skipped", one(gate_app=15368), [])

# --- base retarget: evidence before the latest BaseRefChangedEvent is stale (Codex P1, #78) ---
RT = "2025-10-01T11:30:00Z"
case("retarget after Codex Completed the head: nudged again",
     one(retarget=RT, comments=[summary(f'✅ **Completed** <relative-time datetime="2025-10-01T11:05:00.1Z">x</relative-time>', A)],
         reviews=[{"user": {"login": CODEX}, "commit_id": A, "submitted_at": "2025-10-01T11:06:00Z"}]),
     ["widget#7"])
case("retarget after a person's earlier request: nudged again",
     one(retarget=RT, comments=[comment("web3sea", "@codex review", at="2025-10-01T11:10:00Z")]), ["widget#7"])
case("retarget, Codex row for the head AFTER it: skipped",
     one(retarget=RT, comments=[summary(f'🔄 **Running** since <relative-time datetime="2025-10-01T11:31:00Z">x</relative-time>', A)]),
     [])
case("retarget, Codex review on the head AFTER it: skipped",
     one(retarget=RT, reviews=[{"user": {"login": CODEX}, "commit_id": A, "submitted_at": "2025-10-01T11:32:00Z"}]), [])
case("retarget, a person's request AFTER it: skipped",
     one(retarget=RT, comments=[comment("web3sea", "@codex review", at="2025-10-01T11:31:00Z")]), [])
case("retarget, an untimed head row still counts: skipped", one(retarget=RT, comments=[summary("⏳ **Queued**", A)]), [])
case("retarget BEFORE the head commit: requests count from the commit, rows after the retarget",
     one(retarget="2025-10-01T10:00:00Z", comments=[comment("web3sea", "@codex review", at="2025-10-01T10:30:00Z")]),
     ["widget#7"])
case("retarget lookup fails: unreadable, not nudged", one(retarget="__error"), [], expect_unreadable="1")

# --- many PRs, ordering, cap, isolation of failures ------------------------------------
many = world(*[("widget", n, pr_fx("widget", n, sha("0123456789abcdef"[n % 16]) if n < 16 else sha("f")))
               for n in range(1, 15)])
case("at most MAX_POSTS nudges per run, oldest first", many, [f"widget#{n}" for n in range(1, 11)])
case("MAX_POSTS=0 nudges nothing", many, [], env={"MAX_POSTS": "0"})
mixed = world(("widget", 1, pr_fx("widget", 1, sha("1"), comments=[summary("✅ **Completed**", sha("1"))])),
              ("gadget", 2, pr_fx("gadget", 2, sha("2"))),
              ("gizmo", 3, (lambda f: (f.update({f"repos/{ORG}/gizmo/issues/3/comments": "__error"}), f)[1])(
                  pr_fx("gizmo", 3, sha("3")))),
              ("doohickey", 4, pr_fx("doohickey", 4, sha("4"))))
case("one unreadable PR does not stop the sweep, but is reported", mixed, ["gadget#2", "doohickey#4"],
     expect_unreadable="1")
case("gate check unreadable: unreadable, not nudged", one(gate="__error"), [], expect_unreadable="1")
case("commit date unreadable: unreadable, not nudged",
     (lambda fx: (fx.update({f"repos/{ORG}/widget/commits/{A}": "__error"}), fx)[1])(one()), [], expect_unreadable="1")
case("search item from another org is ignored",
     world(search={"total_count": 1, "incomplete_results": False,
                   "items": [{"repository_url": "https://api.github.com/repos/evil/widget", "number": 7}]}), [])
case("search item with a hostile number is ignored",
     world(search={"total_count": 1, "incomplete_results": False,
                   "items": [{"repository_url": f"https://api.github.com/repos/{ORG}/widget", "number": "7;id"}]}), [])
case("no open PRs: nothing to do", world(), [])

# --- errors (red, no targets) -----------------------------------------------------------
case("search fails: red", {"search/issues": "__error"}, [], expect_rc=1, expect_unreadable=None)
case("search page flagged incomplete_results: red, nothing swept",
     (lambda fx: (fx["search/issues"].update(incomplete_results=True), fx)[1])(one()), [], expect_rc=1,
     expect_unreadable=None)
case("search page without incomplete_results: red", (lambda fx: (fx["search/issues"].pop("incomplete_results"), fx)[1])(one()),
     [], expect_rc=1, expect_unreadable=None)
case("more than 1000 matches (beyond what search returns): red",
     (lambda fx: (fx["search/issues"].update(total_count=1001), fx)[1])(one()), [], expect_rc=1, expect_unreadable=None)
case("token missing: red", one(), [], env={"GH_TOKEN": ""}, expect_rc=1, expect_unreadable=None)


def check_shape(wf):
    on = wf.get("on", wf.get(True))
    assert set(on) == {"schedule", "workflow_dispatch"}, "schedule (+ dispatch from main) only; never a PR trigger"
    assert on["workflow_dispatch"] in (None, {}), "no dispatch inputs"
    crons = [c["cron"] for c in on["schedule"]]
    assert len(crons) == 1 and crons[0].split()[0].count(",") == 1, "about every 30 minutes"
    assert wf["permissions"] == {}, "GITHUB_TOKEN gets no permissions"
    assert wf["concurrency"]["cancel-in-progress"] is False
    job = wf["jobs"]["sweep"]
    assert "permissions" not in job
    assert "github.repository == 'Automation-Architecture/.github'" in job["if"]
    assert "github.ref == 'refs/heads/main'" in job["if"], "dispatch --ref <branch> must not reach the PAT"
    text = WORKFLOW.read_text()
    assert "actions/checkout" not in text and "uses:" not in text, "no checkout and no action"
    steps = job["steps"]
    find, nudge = steps[0], steps[1]
    assert find["id"] == "find" and nudge["id"] == "nudge"
    assert nudge["if"] == "steps.find.outputs.targets != '0'"
    assert nudge["run"].count("gh api") == 1 and "-X POST" in nudge["run"] and "body='@codex review'" in nudge["run"], \
        "the nudge step makes exactly one kind of call: the fixed comment"
    assert "-X POST" not in find["run"] and "PATCH" not in find["run"] and "DELETE" not in find["run"]
    for s in steps:
        assert "${{" not in s.get("run", ""), "no expression interpolated into a script; pass values via env"
        assert "secrets." not in json.dumps(s) or s.get("id") in ("find", "nudge"), "the PAT only in find and nudge"
    assert text.count("secrets.AAA_ORG_TOKEN") == 2
    env = find["env"]
    assert int(env["MAX_POSTS"]) <= 20 and int(env["MIN_AGE_SECONDS"]) >= 300
    # Same rules as the event path and the gate's neighbours (keep them in step).
    ev = yaml.safe_load(EVENT_WORKFLOW.read_text())["jobs"]["codex-auto-request"]["steps"][0]["env"]
    dam = yaml.safe_load(DOC_AUTO_MERGE.read_text())["jobs"]["doc-auto-merge"]["steps"][0]["env"]
    rv = yaml.safe_load(REVIEW_VERDICT.read_text())["jobs"]["publish"]["steps"][0]["env"]
    assert env["DOC_ONLY_PATTERN"] == ev["DOC_ONLY_PATTERN"] == dam["DOC_ONLY_PATTERN"]
    assert env["MAX_DOC_FILES"] == ev["MAX_DOC_FILES"] == dam["MAX_FILES"]
    assert env["HOLD_LABEL"] == ev["HOLD_LABEL"] == rv["HOLD_LABEL"]
    assert env["CODEX_BOT"] == ev["CODEX_BOT"]
    ev_run = yaml.safe_load(EVENT_WORKFLOW.read_text())["jobs"]["codex-auto-request"]["steps"][0]["run"]
    import re
    ev_rows = re.search(r"def epoch:.*?\] \| length'", ev_run, re.S).group(0)
    sw_rows = re.search(r"def epoch:.*?\] \| length'", find["run"], re.S).group(0)
    assert " ".join(ev_rows.split()).replace("$head", "H") == " ".join(sw_rows.split()).replace("$head", "H"), \
        "summary-row jq drifted from codex-auto-request.yml"
    assert env["GATE_APP_ID"] == "5021608", "the gate check is pinned to the delivery App"
    assert "BASE_REF_CHANGED_EVENT" in find["run"], "retarget cutoff as in review-verdict"
    for shared in ('@codex (security )?review\\\\b', 'startswith("<!-- codex-pull-request-review-summary -->")',
                   '(?<sha>[0-9a-f]{7,40})', '.type != "Bot" and (.user | endswith("[bot]") | not)'):
        assert shared in find["run"] and shared in ev_run, f"rule drifted from codex-auto-request.yml: {shared}"


def read_calls(state):
    f = state / "calls"
    return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []


def run_case(tmp, find, nudge, name, fx, expect, env_over, expect_rc, expect_unreadable):
    state = tmp / "state"
    subprocess.run(["rm", "-rf", str(state)], check=True)
    state.mkdir()
    (tmp / "fx.json").write_text(json.dumps(fx))
    out_file = state / "github_output"
    out_file.write_text("")
    targets = state / "targets"
    static = {k: str(v) for k, v in find["env"].items() if "${{" not in str(v)}
    env = {"PATH": f"{tmp / 'bin'}:{os.environ['PATH']}", "HOME": os.environ.get("HOME", "/tmp"), **static,
           "GH_TOKEN": "org-pat", "ORG": ORG, "TARGETS_FILE": str(targets), "NOW_EPOCH": str(NOW),
           "GITHUB_OUTPUT": str(out_file), "FIXTURES": str(tmp / "fx.json"), "STATE_DIR": str(state), **env_over}
    r = subprocess.run(["bash", str(tmp / "find.sh")], env=env, capture_output=True, text=True, timeout=60)
    calls = read_calls(state)
    problems = []
    if r.returncode != expect_rc:
        problems.append(f"find rc {r.returncode} != {expect_rc}")
    if any(c["method"] != "GET" for c in calls):
        problems.append("find step wrote")
    kv = dict(l.split("=", 1) for l in out_file.read_text().splitlines() if "=" in l)
    got = [f"{l.split()[0].split('/')[1]}#{l.split()[1]}" for l in targets.read_text().splitlines()] \
        if targets.exists() else []
    if got != expect:
        problems.append(f"targets {got} != {expect}")
    if expect_rc == 0:
        if kv.get("targets") != str(len(expect)):
            problems.append(f"targets output {kv.get('targets')!r}")
        if kv.get("unreadable") != expect_unreadable:
            problems.append(f"unreadable output {kv.get('unreadable')!r} != {expect_unreadable!r}")
        if expect:
            # The nudge step posts exactly one fixed comment per target, with the PAT.
            (state / "calls").unlink(missing_ok=True)
            n_env = {"PATH": env["PATH"], "HOME": env["HOME"], "GH_TOKEN": "org-pat", "ORG": ORG,
                     "TARGETS_FILE": str(targets), "FIXTURES": env["FIXTURES"], "STATE_DIR": str(state)}
            r2 = subprocess.run(["bash", str(tmp / "nudge.sh")], env=n_env, capture_output=True, text=True, timeout=60)
            posts = read_calls(state)
            if r2.returncode != 0:
                problems.append(f"nudge rc {r2.returncode}")
            want = [f"repos/{ORG}/{t.split('#')[0]}/issues/{t.split('#')[1]}/comments" for t in expect]
            if [p["path"] for p in posts] != want or any(
                    p["method"] != "POST" or p["fields"] != {"body": "@codex review"} for p in posts):
                problems.append(f"posts {posts}")
    return problems, r


def nudge_cases(tmp, nudge):
    """The nudge step rejects a malformed target line and reports a failed post."""
    problems = []
    state = tmp / "state"
    subprocess.run(["rm", "-rf", str(state)], check=True)
    state.mkdir()
    (tmp / "fx.json").write_text(json.dumps({f"POST repos/{ORG}/widget/issues/8/comments": "__error"}))
    targets = state / "targets"
    targets.write_text(f"evil/widget 7 {sha('a')}\n{ORG}/widget 7; {sha('a')}\n{ORG}/widget 8 {sha('b')}\n"
                       f"{ORG}/widget 9 {sha('c')}\n")
    env = {"PATH": f"{tmp / 'bin'}:{os.environ['PATH']}", "HOME": os.environ.get("HOME", "/tmp"), "GH_TOKEN": "pat",
           "ORG": ORG, "TARGETS_FILE": str(targets), "FIXTURES": str(tmp / "fx.json"), "STATE_DIR": str(state)}
    r = subprocess.run(["bash", str(tmp / "nudge.sh")], env=env, capture_output=True, text=True, timeout=60)
    posts = [c["path"] for c in read_calls(state)]
    if r.returncode == 0:
        problems.append("nudge step must be red on a bad line or a failed post")
    if posts != [f"repos/{ORG}/widget/issues/8/comments", f"repos/{ORG}/widget/issues/9/comments"]:
        problems.append(f"posts {posts}")
    return problems


def main():
    wf = yaml.safe_load(WORKFLOW.read_text())
    check_shape(wf)
    steps = wf["jobs"]["sweep"]["steps"]
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        (tmp / "find.sh").write_text(steps[0]["run"])
        (tmp / "nudge.sh").write_text(steps[1]["run"])
        (tmp / "bin").mkdir()
        (tmp / "bin/gh").write_text(STUB)
        (tmp / "bin/gh").chmod(0o755)
        for name, fx, expect, env_over, rc, unreadable in CASES:
            problems, r = run_case(tmp, steps[0], steps[1], name, copy.deepcopy(fx), expect, env_over, rc, unreadable)
            failures += bool(problems)
            print(f"{'PASS' if not problems else 'FAIL'}  {name}")
            if problems:
                print("      " + "; ".join(problems))
                print(r.stdout[-1500:], r.stderr[-1500:], sep="\n")
        problems = nudge_cases(tmp, steps[1])
        failures += bool(problems)
        print(f"{'PASS' if not problems else 'FAIL'}  nudge step: bad target lines and failed posts are red")
        if problems:
            print("      " + "; ".join(problems))
    total = len(CASES) + 1
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
