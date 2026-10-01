#!/usr/bin/env python3
"""Offline tests for codex-auto-request.yml (the simplified nudge, Brad 2026-10-01).

Extracts the `decide` step's inline script, puts a stub `gh` first on PATH that
serves canned API responses, runs it with no wait, and checks the decision it
writes to GITHUB_OUTPUT. Also asserts the workflow's security shape (trigger,
permissions, where the org PAT may appear). Needs python3 + PyYAML, bash, jq.

    uv run --with pyyaml python tests/codex-auto-request/test_codex_auto_request.py
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
WORKFLOW = ROOT / ".github/workflows/codex-auto-request.yml"

REPO = "acme/widget"
PR = 7
HEAD = "034be02bb328773fbe762ec8c93c14a1bedde66b"
OLD = "9f1c2d3e4b5a69788796a5b4c3d2e1f00112233a"
NEW = "1" * 40
CODEX = "chatgpt-codex-connector[bot]"
COMMITTED = "2026-10-01T09:58:00Z"   # head commit's committer date
PUSHED = "2026-10-01T10:00:00Z"      # synchronize event time
RETARGET = "2026-10-01T10:30:00Z"    # edited (base changed) event time
SUMMARY = "<!-- codex-pull-request-review-summary -->"

# Stub gh: `gh api [-X M] [--paginate] [--jq Q] [-f k=v] <path>`. "__error" fails
# the call. --jq output matches gh: strings raw, other JSON compact. Any write
# fails the case: the decide step must never write.
STUB = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
fx = json.load(open(os.environ["FIXTURES"]))
state = os.environ["STATE_DIR"]
args = sys.argv[1:]
if args[:1] != ["api"]:
    sys.exit("stub gh: only `gh api` is supported")
args = args[1:]
jq = None; method = "GET"; path = None
i = 0
while i < len(args):
    a = args[i]
    if a == "--jq": jq = args[i + 1]; i += 2; continue
    if a == "-X": method = args[i + 1]; i += 2; continue
    if a in ("-f", "-F"): i += 2; continue
    if a == "--paginate": i += 1; continue
    path = a; i += 1
if method != "GET":
    open(os.path.join(state, "writes"), "a").write(method + " " + path + "\n")
    sys.exit("stub gh: the decide step must never write")
key = "graphql" if path == "graphql" else path.split("repos/" + os.environ["REPO"], 1)[-1].split("?", 1)[0]
if key not in fx:
    sys.exit(f"stub gh: no fixture for {key}")
val = fx[key]
if isinstance(val, dict) and "__seq" in val:
    cf = os.path.join(state, "count_" + key.replace("/", "_"))
    n = int(open(cf).read()) if os.path.exists(cf) else 0
    open(cf, "w").write(str(n + 1))
    val = val["__seq"][min(n, len(val["__seq"]) - 1)]
if val == "__error":
    sys.stderr.write("HTTP 500\n"); sys.exit(1)
doc = json.dumps(val)
if jq is None:
    sys.stdout.write(doc); sys.exit(0)
out = subprocess.run(["jq", "-r", "-c", jq], input=doc, capture_output=True, text=True)
sys.stdout.write(out.stdout); sys.stderr.write(out.stderr); sys.exit(out.returncode)
'''


def pr(state="open", head=HEAD):
    return {"number": PR, "state": state, "head": {"sha": head}}


def comment(user, body, at="2026-10-01T10:01:00Z"):
    return {"user": {"login": user}, "created_at": at, "body": body}


def row(status, sha, at="2026-10-01T10:03:00"):
    cells = {"Completed": f'✅ **Completed** <relative-time datetime="{at}.123Z">x</relative-time>',
             "Running": f'🔄 **Running** since <relative-time datetime="{at}.5Z">x</relative-time>',
             "Failed": f'⚠️ **Failed** <relative-time datetime="{at}Z">x</relative-time>',
             "Queued": "⏳ **Queued**"}
    return f"| 📝 **Code Review** | {cells[status]} | `{sha[:7]}` | PR opened |"


def summary(*rows):
    return comment(CODEX, "\n".join([SUMMARY, "", "## Codex Review Summary", "",
                                      "| Review | Status | Commit | Review trigger |", "| --- | --- | --- | --- |",
                                      *rows, "", "<details>Comment \"@codex review\".</details>"]),
                   at="2026-10-01T10:02:00Z")


def review(commit=HEAD, at="2026-10-01T10:04:00Z", user=CODEX, state="COMMENTED"):
    return {"user": {"login": user}, "commit_id": commit, "submitted_at": at, "state": state}


def base(comments=(), reviews=(), **prkw):
    return {f"/pulls/{PR}": pr(**prkw),
            f"/commits/{HEAD}": {"commit": {"committer": {"date": COMMITTED}}},
            f"/issues/{PR}/comments": list(comments),
            f"/pulls/{PR}/reviews": list(reviews)}


def with_(fx, **kw):
    fx = copy.deepcopy(fx)
    fx.update(kw)
    return fx


CASES = []


def case(name, expect, fx, env=None):
    """expect: 'nudge' (nudge=true), 'skip' (nudge=false) or 'error' (exit != 0)."""
    CASES.append((name, expect, fx, env or {}))


SYNC = {"EVENT_AT": PUSHED}
EDIT = {"EVENT_AT": RETARGET, "RETARGET": "true"}

# --- nudges -------------------------------------------------------------------
case("opened, Codex has not touched the head: nudges", "nudge", base())
case("synchronize, Codex has not touched the new head: nudges", "nudge", base(), env=SYNC)
case("Codex rows and reviews only for an OLDER head: nudges", "nudge",
     base(comments=[summary(row("Completed", OLD))], reviews=[review(commit=OLD)]), env=SYNC)
case("Codex's 'create an environment' reply is not a row: nudges", "nudge",
     base(comments=[comment(CODEX, "To use Codex here, [create an environment for this repo](https://x).")]))
case("an @codex review from BEFORE the push: nudges", "nudge",
     base(comments=[comment("web3sea", "@codex review", at="2026-10-01T09:59:59Z")]), env=SYNC)
case("a mention inside a word is not a request: nudges", "nudge",
     base(comments=[comment("web3sea", "mail@codex reviewers", at="2026-10-01T10:05:00Z")]))
case("Codex's own footer text is not a request: nudges", "nudge",
     base(comments=[comment(CODEX, "Try again later by commenting \u201c@codex review\u201d.", at="2026-10-01T10:05:00Z")]))
case("another account's review on the head: nudges", "nudge", base(reviews=[review(user="web3sea")]))

# --- (a) the head moved / closed ------------------------------------------------
case("head moved during the wait: skips", "skip", with_(base(), **{f"/pulls/{PR}": pr(head=NEW)}), env=SYNC)
case("closed during the wait: skips", "skip", base(state="closed"))

# --- (b) Codex already touched the head, ANY status -------------------------------
for st in ("Completed", "Running", "Queued", "Failed"):
    case(f"summary row {st} for the head: skips", "skip", base(comments=[summary(row(st, HEAD))]))
case("row for the head next to a row for an older head: skips", "skip",
     base(comments=[summary(row("Completed", OLD), row("Failed", HEAD))]))
case("a row in an OLDER summary comment still counts: skips", "skip",
     base(comments=[summary(row("Completed", HEAD)), summary(row("Running", OLD))]))
case("Codex review object on the head (any state): skips", "skip", base(reviews=[review(state="DISMISSED")]))

# --- (c) someone already asked -----------------------------------------------------
case("web3sea @codex review after the push: skips", "skip",
     base(comments=[comment("web3sea", "@codex review", at="2026-10-01T10:00:30Z")]), env=SYNC)
case("our own earlier nudge for this head: skips (once per head)", "skip",
     base(comments=[comment("web3sea", "@codex review", at="2026-10-01T10:03:00Z")]), env=SYNC)
case("a bot's @codex review after the push also counts (simple rule): skips", "skip",
     base(comments=[comment("claude[bot]", "@codex review please", at="2026-10-01T10:05:00Z")]), env=SYNC)
case("@codex security review after the push: skips", "skip",
     base(comments=[comment("web3sea", "@codex security review", at="2026-10-01T10:05:00Z")]))
case("request at exactly the push time counts: skips", "skip",
     base(comments=[comment("web3sea", "@CODEX REVIEW", at=PUSHED)]), env=SYNC)
case("opened: a request after the commit date counts: skips", "skip",
     base(comments=[comment("web3sea", "@codex review", at="2026-10-01T09:59:00Z")]))

# --- base retarget (edited with changes.base) --------------------------------------
case("retarget after Codex Completed the head: nudges again", "nudge",
     base(comments=[summary(row("Completed", HEAD))], reviews=[review()]), env=EDIT)
case("retarget after our earlier nudge for the head: nudges again", "nudge",
     base(comments=[comment("web3sea", "@codex review", at="2026-10-01T10:03:00Z")]), env=EDIT)
case("retarget, Codex row for the head AFTER the retarget: skips", "skip",
     base(comments=[summary(row("Running", HEAD, at="2026-10-01T10:31:00"))]), env=EDIT)
case("retarget, Codex review on the head AFTER the retarget: skips", "skip",
     base(reviews=[review(at="2026-10-01T10:32:00Z")]), env=EDIT)
case("retarget, @codex review AFTER the retarget: skips", "skip",
     base(comments=[comment("web3sea", "@codex review", at="2026-10-01T10:31:00Z")]), env=EDIT)
case("retarget, a head row with no time still counts: skips", "skip",
     base(comments=[summary(row("Queued", HEAD))]), env=EDIT)

# --- errors (red run, never a nudge) -------------------------------------------------
case("PR unreadable: red", "error", with_(base(), **{f"/pulls/{PR}": "__error"}))
case("comments unreadable: red", "error", with_(base(), **{f"/issues/{PR}/comments": "__error"}))
case("reviews unreadable: red", "error", with_(base(), **{f"/pulls/{PR}/reviews": "__error"}))
case("head commit date unreadable: red", "error", with_(base(), **{f"/commits/{HEAD}": "__error"}))
case("non-numeric PR number: red", "error", base(), env={"PR_NUMBER": "7; id"})
case("malformed head: red", "error", base(), env={"EVENT_HEAD": "$(id)"})
case("malformed event time: red", "error", base(), env={"EVENT_AT": "yesterday"})
case("retarget with no event time: red", "error", base(), env={"RETARGET": "true"})


def check_shape(wf):
    on = wf.get("on", wf.get(True))
    assert set(on) == {"pull_request_target"}, \
        "only pull_request_target: pull_request runs the PR's copy, workflow_dispatch --ref a branch's copy"
    assert set(on["pull_request_target"]["types"]) == {"opened", "synchronize", "reopened", "ready_for_review", "edited"}
    assert wf["permissions"] == {"contents": "read", "pull-requests": "read", "issues": "read"}, \
        "GITHUB_TOKEN stays read-only; the one write uses the PAT"
    job = wf["jobs"]["codex-auto-request"]
    assert "permissions" not in job, "no job-level permission widening"
    for needle in ("github.event_name == 'pull_request_target'", "github.event.pull_request.draft == false",
                   "github.event.pull_request.head.repo.full_name == github.repository",
                   "github.event.changes.base != null"):
        assert needle in job["if"], f"job condition must include {needle}"
    text = WORKFLOW.read_text()
    assert "actions/checkout" not in text and "uses:" not in text, "no checkout and no action"
    assert text.count("secrets.AAA_ORG_TOKEN") == 1, "the org PAT appears exactly once"
    steps = job["steps"]
    pat = [s for s in steps if "secrets.AAA_ORG_TOKEN" in json.dumps(s)]
    assert len(pat) == 1 and pat[0]["id"] == "nudge" and pat[0]["if"] == "steps.decide.outputs.nudge == 'true'"
    assert pat[0]["run"].count("gh api") == 1 and "-X POST" in pat[0]["run"] and "body='@codex review'" in pat[0]["run"], \
        "the PAT makes exactly one call: the fixed comment"
    for s in steps:
        assert "${{" not in s.get("run", ""), "no expression interpolated into a script; pass values via env"
        for bad in ("pull_request.title", "pull_request.body", "head.ref", "github.event.comment"):
            assert bad not in json.dumps(s), f"never pass {bad}"
    assert wf["concurrency"]["cancel-in-progress"] is False, "a cancelled check reads as red to doc-auto-merge"
    for needle in ("github.event.pull_request.number", "github.event.pull_request.head.sha"):
        assert needle in wf["concurrency"]["group"]
    assert 150 <= int(steps[0]["env"]["DELAY_SECONDS"]) <= 240, "about 3 minutes for Codex's own review"


def main():
    wf = yaml.safe_load(WORKFLOW.read_text())
    check_shape(wf)
    step = wf["jobs"]["codex-auto-request"]["steps"][0]
    static_env = {k: str(v) for k, v in step["env"].items() if "${{" not in str(v)}
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        (tmp / "script.sh").write_text(step["run"])
        (tmp / "bin").mkdir()
        (tmp / "bin/gh").write_text(STUB)
        (tmp / "bin/gh").chmod(0o755)
        for name, expect, fx, env_over in CASES:
            state = tmp / "state"
            subprocess.run(["rm", "-rf", str(state)], check=True)
            state.mkdir()
            (tmp / "fx.json").write_text(json.dumps(fx))
            outf = state / "github_output"
            outf.write_text("")
            env = {"PATH": f"{tmp / 'bin'}:{os.environ['PATH']}", "HOME": os.environ.get("HOME", "/tmp"),
                   **static_env, "REPO": REPO, "PR_NUMBER": str(PR), "EVENT_HEAD": HEAD, "EVENT_AT": "",
                   "RETARGET": "false", "GH_TOKEN": "read-token", "DELAY_SECONDS": "0",
                   "GITHUB_OUTPUT": str(outf), "FIXTURES": str(tmp / "fx.json"), "STATE_DIR": str(state),
                   **env_over}
            out = subprocess.run(["bash", str(tmp / "script.sh")], env=env, capture_output=True, text=True,
                                 timeout=60)
            kv = dict(l.split("=", 1) for l in outf.read_text().splitlines() if "=" in l)
            got = ("error" if out.returncode != 0 else
                   {"true": "nudge", "false": "skip"}.get(kv.get("nudge"), "no-decision"))
            ok = got == expect and not (state / "writes").exists() and not (got == "error" and "nudge" in kv)
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
