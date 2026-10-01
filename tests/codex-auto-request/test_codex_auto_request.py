#!/usr/bin/env python3
"""Offline tests for codex-auto-request.yml.

Extracts the `decide` step's inline script, puts a stub `gh` first on PATH that
serves canned API responses, runs it with no waits, and checks the decision it
writes to GITHUB_OUTPUT. Also asserts the workflow's security shape (trigger,
permissions, where the org PAT may appear). Needs python3 + PyYAML, bash, jq.

    uv run --with pyyaml python tests/codex-auto-request/test_codex_auto_request.py
"""
import calendar
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
PUSHED = "2026-10-01T10:00:00Z"
SUMMARY = "<!-- codex-pull-request-review-summary -->"

# Stub gh: `gh api [-X M] [--paginate] [--jq Q] [-f k=v] <path>`. A fixture value
# {"__seq": [a, b]} is served one entry per call (the last repeats); "__error"
# fails the call. --jq output matches gh: strings raw, other JSON compact.
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


def pr(state="open", draft=False, head=HEAD, head_repo=REPO, user="mac-tuongnh"):
    return {"number": PR, "node_id": f"PR_{PR}", "state": state, "draft": draft, "user": {"login": user},
            "head": {"sha": head, "repo": {"full_name": head_repo} if head_repo else None}}


def comment(user, body, at="2026-10-01T10:01:00Z", typ=None):
    return {"user": {"login": user, "type": typ or ("Bot" if user.endswith("[bot]") else "User")},
            "created_at": at, "body": body}


def summary(*rows):
    lines = [SUMMARY, "", "## Codex Review Summary", "", "| Review | Status | Commit | Review trigger |",
             "| --- | --- | --- | --- |"]
    for status, sha in rows:
        if status == "Completed":
            st = '✅ **Completed** <relative-time datetime="2026-10-01T10:03:00Z">x</relative-time>'
        elif status == "Running":
            st = '🔄 **Running** since <relative-time datetime="2026-10-01T10:02:00Z">x</relative-time>'
        elif status == "Failed":
            st = '⚠️ **Failed** <relative-time datetime="2026-10-01T10:02:00Z">x</relative-time>'
        else:
            st = status
        lines.append(f"| 📝 **Code Review** | {st} | `{sha[:7]}` | PR opened |")
    lines += ["", "<details> <summary>About</summary>Comment \"@codex review\".</details>"]
    return comment(CODEX, "\n".join(lines), at="2026-10-01T10:02:00Z")


def review(commit=HEAD, state="COMMENTED", at="2026-10-01T10:04:00Z", body="", user=CODEX):
    return {"user": {"login": user}, "commit_id": commit, "state": state, "submitted_at": at, "body": body}


def timeline(*times, more=False):
    return {"data": {"node": {"timelineItems": {"pageInfo": {"hasPreviousPage": more},
                                                "nodes": [{"createdAt": t} for t in times]}}}}


RETARGET = "2026-10-01T10:30:00Z"


def thumbs(at="2026-10-01T10:03:03Z", user=CODEX):
    return {"user": {"login": user}, "content": "+1", "created_at": at}


THUMBS = thumbs()


def epoch(ts):
    return str(calendar.timegm(__import__("time").strptime(ts, "%Y-%m-%dT%H:%M:%SZ")))


REFUSAL = "To use Codex here, [create a Codex account and connect to github](https://x)."


def base(comments=(), reviews=(), reactions=(), cutoff=(), **prkw):
    return {
        "graphql": timeline(*cutoff),
        f"/pulls/{PR}": pr(**prkw),
        f"/commits/{HEAD}": {"commit": {"committer": {"date": PUSHED}}},
        f"/issues/{PR}/comments": list(comments),
        f"/pulls/{PR}/reviews": list(reviews),
        f"/issues/{PR}/reactions": list(reactions),
    }


def with_(fx, **kw):
    fx = copy.deepcopy(fx)
    fx.update(kw)
    return fx


CASES = []


def case(name, expect, fx, env=None):
    """expect: 'request' (request=true, head=HEAD), 'skip' (request=false) or 'error' (exit != 0)."""
    CASES.append((name, expect, fx, env or {}))


SYNC = {"EVENT_HEAD": HEAD, "PUSHED_AT": PUSHED}

# --- asks ---------------------------------------------------------------------
case("contractor PR with no Codex activity: requests", "request", base())
case("bot PR (aaa-dashboard-bot) with no Codex activity: requests", "request", base(user="aaa-dashboard-bot[bot]"))
case("synchronize event, nothing on the new head: requests", "request", base(), env=SYNC)
case("Codex Completed only an OLDER head: requests", "request", base(comments=[summary(("Completed", OLD))]))
case("Codex review object only on an older head: requests", "request",
     base(reviews=[review(commit=OLD)]))
case("Codex review Failed on this head: requests (once)", "request", base(comments=[summary(("Failed", HEAD))]))
case("'create an environment' reply (aaa-runbooks): requests", "request",
     base(comments=[comment(CODEX, "To use Codex here, [create an environment for this repo](https://x).")]))
case("a bot's @codex review after the push does not count (Codex refuses bots): requests", "request",
     base(comments=[comment("claude[bot]", "@codex review please", at="2026-10-01T10:05:00Z")]))
case("github-actions[bot] @codex review does not count: requests", "request",
     base(comments=[comment("github-actions[bot]", "@codex review", at="2026-10-01T10:05:00Z")]))
case("a human @codex review BEFORE the push does not count: requests", "request",
     base(comments=[comment("web3sea", "@codex review", at="2026-10-01T09:59:59Z")]))
case("a human @codex review that Codex refused (no linked account): requests", "request",
     base(comments=[comment("mac-tuongnh", "@codex review", at="2026-10-01T10:05:00Z"),
                    comment(CODEX, "To use Codex here, [create a Codex account and connect to github](https://x).",
                            at="2026-10-01T10:05:04Z")]))
case("a quoted mention mid-word does not count: requests", "request",
     base(comments=[comment("web3sea", "see the note about email@codex reviewers", at="2026-10-01T10:05:00Z")]))
case("our marker for an OLDER head does not block this head: requests", "request",
     base(comments=[comment("web3sea", f"@codex review\n\n<!-- codex-auto-request head={OLD} -->",
                            at="2026-10-01T09:00:00Z")]))
case("usage-limit reply on an older head: requests once for the new head", "request",
     base(comments=[comment("web3sea", f"@codex review\n\n<!-- codex-auto-request head={OLD} -->", at="2026-10-01T09:00:00Z"),
                    comment(CODEX, "You have reached your Codex usage limits for code reviews.", at="2026-10-01T09:00:10Z")]))
case("Codex 👀 on the PR but nothing appears before the busy wait ends: requests", "request",
     base(reactions=[{"user": {"login": CODEX}, "content": "eyes"}]), env={"BUSY_WAIT_SECONDS": "0"})
case("Codex 👀 clears on the re-check, still no row: requests", "request",
     with_(base(), **{f"/issues/{PR}/reactions": {"__seq": [[{"user": {"login": CODEX}, "content": "eyes"}], []]}}),
     env={"BUSY_WAIT_SECONDS": "5"})
case("another account's thumbs-up does not matter: requests", "request",
     base(comments=[summary(("Completed", HEAD))], reactions=[thumbs(user="web3sea")]))

# --- skips --------------------------------------------------------------------
case("Codex Completed on this head + its 👍: skips", "skip",
     base(comments=[summary(("Completed", HEAD))], reactions=[THUMBS]))
case("Codex Completed on this head, no 👍 and no review (gate cannot accept): requests once", "request",
     base(comments=[summary(("Completed", HEAD))]))
case("Codex Completed, 👍 predates the completion (an older review's): requests", "request",
     base(comments=[summary(("Completed", HEAD))], reactions=[thumbs("2026-10-01T09:00:00Z")]))
case("Codex Completed, 👍 arrives on the re-check: skips", "skip",
     with_(base(comments=[summary(("Completed", HEAD))]),
           **{f"/issues/{PR}/reactions": {"__seq": [[], [THUMBS]]}}),
     env={"REACTION_GRACE_SECONDS": "5"})
case("Codex Completed, no 👍, already requested once for this head: skips", "skip",
     base(comments=[summary(("Completed", HEAD)),
                    comment("web3sea", f"@codex review\n\n<!-- codex-auto-request head={HEAD} -->", at="2026-10-01T10:04:00Z")]))
case("Codex Running on this head: skips", "skip", base(comments=[summary(("Running", HEAD))]))
case("Codex row in an unknown (queued) status on this head: skips", "skip",
     base(comments=[summary(("⏳ **Queued**", HEAD))]))
case("Codex Completed + a Failed row on this head: requests (the gate needs every row Completed)", "request",
     base(comments=[summary(("Completed", HEAD), ("Failed", HEAD))]))
case("Codex Completed + a Running row on this head: skips (still in progress)", "skip",
     base(comments=[summary(("Completed", HEAD), ("Running", HEAD))]))
case("Codex Completed + a Failed row, already requested once: skips", "skip",
     base(comments=[summary(("Completed", HEAD), ("Failed", HEAD)),
                    comment("web3sea", f"@codex review\n\n<!-- codex-auto-request head={HEAD} -->", at="2026-10-01T10:03:00Z")]))
case("only the LATEST summary comment is read (older one Completed on head is stale)", "request",
     base(comments=[summary(("Completed", HEAD)), summary(("Failed", HEAD))]))
case("Codex review object on this head: skips", "skip",
     base(reviews=[review()]))
case("already requested once for this head (marker): skips", "skip",
     base(comments=[comment("web3sea", f"@codex review\n\n<!-- codex-auto-request head={HEAD} -->",
                            at="2026-10-01T10:03:00Z")]))
case("marker for this head + Codex usage-limit reply: no second request", "skip",
     base(comments=[comment("web3sea", f"@codex review\n\n<!-- codex-auto-request head={HEAD} -->", at="2026-10-01T10:03:00Z"),
                    comment(CODEX, "You have reached your Codex usage limits for code reviews.", at="2026-10-01T10:03:05Z")]))
case("marker for this head, Codex refused it: still no second request (no loop)", "skip",
     base(comments=[comment("web3sea", f"@codex review\n\n<!-- codex-auto-request head={HEAD} -->", at="2026-10-01T10:03:00Z"),
                    comment(CODEX, "To use Codex here, [create a Codex account and connect to github](https://x).",
                            at="2026-10-01T10:03:04Z")]))
case("marker for this head posted before a later re-push of the same head: skips", "skip",
     base(comments=[comment("web3sea", f"@codex review\n\n<!-- codex-auto-request head={HEAD} -->",
                            at="2026-10-01T09:30:00Z")]), env=SYNC)
case("web3sea commented @codex review after the push: skips", "skip",
     base(comments=[comment("web3sea", "@codex review", at="2026-10-01T10:00:30Z")]))
case("@codex security review after the push: skips", "skip",
     base(comments=[comment("web3sea", "@codex security review", at="2026-10-01T10:00:30Z")]))
case("human request exactly at the push time counts: skips", "skip",
     base(comments=[comment("web3sea", "@CODEX REVIEW", at=PUSHED)]))
case("human request, refusal arrives outside the window (unrelated): skips", "skip",
     base(comments=[comment("web3sea", "@codex review", at="2026-10-01T10:05:00Z"),
                    comment(CODEX, "To use Codex here, [create a Codex account and connect to github](https://x).",
                            at="2026-10-01T10:30:00Z")]))
case("Codex 👀 then a Running row appears on the re-check: skips", "skip",
     with_(base(reactions=[{"user": {"login": CODEX}, "content": "eyes"}]),
           **{f"/issues/{PR}/comments": {"__seq": [[], [summary(("Running", HEAD))]]}}),
     env={"BUSY_WAIT_SECONDS": "5"})
case("draft: skips", "skip", base(draft=True))
case("closed: skips", "skip", base(state="closed"))
case("fork PR (dispatch): skips", "skip", base(head_repo="mallory/widget"))
case("deleted head repository: skips", "skip", base(head_repo=None))
case("head moved during the wait: skips (the newer run decides)", "skip",
     with_(base(), **{f"/pulls/{PR}": pr(head=NEW)}), env=SYNC)

# --- only evidence the gate can accept counts (Codex P1s on .github#71) ---------
case("dismissed Codex review on this head: requests", "request", base(reviews=[review(state="DISMISSED")]))
case("pending Codex review on this head: requests", "request", base(reviews=[review(state="PENDING")]))
case("Codex review on this head that is a usage/rate-limit skip notice: requests", "request",
     base(reviews=[review(body="Codex hit a rate limit; review skipped.")]))
case("another account's review on this head: requests", "request", base(reviews=[review(user="web3sea")]))
case("retarget AFTER Codex reviewed the head: requests", "request",
     base(reviews=[review()], cutoff=[RETARGET]))
case("retarget AFTER Codex's Completed row: requests", "request",
     base(comments=[summary(("Completed", HEAD))], reactions=[THUMBS], cutoff=[RETARGET]))
case("retarget AFTER our marker for this head: requests again", "request",
     base(comments=[comment("web3sea", f"@codex review\n\n<!-- codex-auto-request head={HEAD} -->",
                            at="2026-10-01T10:03:00Z")], cutoff=[RETARGET]))
case("retarget AFTER a human @codex review: requests", "request",
     base(comments=[comment("web3sea", "@codex review", at="2026-10-01T10:05:00Z")], cutoff=[RETARGET]))
case("Codex review AFTER the retarget: skips", "skip",
     base(reviews=[review(at="2026-10-01T10:35:00Z")], cutoff=["2026-09-30T00:00:00Z", RETARGET]))
case("our marker AFTER the retarget: skips", "skip",
     base(comments=[comment("web3sea", f"@codex review\n\n<!-- codex-auto-request head={HEAD} -->",
                            at="2026-10-01T10:31:00Z")], cutoff=[RETARGET]))
case("Codex row with no readable time once there is a cutoff: requests", "request",
     base(comments=[summary(("⏳ **Queued**", HEAD))], cutoff=[RETARGET]))

# --- the whole table must be on the head; a fresh request may yet be refused (round 4) ---
case("Completed + 👍 on this head, but the table also has a row for an older head: requests", "request",
     base(comments=[summary(("Completed", HEAD), ("Completed", OLD))], reactions=[THUMBS]))
case("Running on this head + a row for an older head: skips (in progress)", "skip",
     base(comments=[summary(("Running", HEAD), ("Completed", OLD))]))
case("young human request, Codex refuses it during the wait: requests", "request",
     with_(base(), **{f"/issues/{PR}/comments": {"__seq": [
         [comment("mac-tuongnh", "@codex review", at="2026-10-01T10:05:00Z")],
         [comment("mac-tuongnh", "@codex review", at="2026-10-01T10:05:00Z"),
          comment(CODEX, REFUSAL, at="2026-10-01T10:05:04Z")]]}}),
     env={"NOW_EPOCH": epoch("2026-10-01T10:05:01Z"), "REFUSAL_WINDOW_SECONDS": "6", "POLL_SECONDS": "1"})
case("young human request, no refusal within the window: skips", "skip",
     base(comments=[comment("web3sea", "@codex review", at="2026-10-01T10:05:00Z")]),
     env={"NOW_EPOCH": epoch("2026-10-01T10:05:01Z"), "REFUSAL_WINDOW_SECONDS": "2", "POLL_SECONDS": "1"})
case("human request older than the window, not refused: skips at once", "skip",
     base(comments=[comment("web3sea", "@codex review", at="2026-10-01T10:05:00Z")]),
     env={"NOW_EPOCH": epoch("2026-10-01T10:08:00Z")})

# --- errors (red run, never a request) ------------------------------------------
case("retarget history unreadable: red", "error", with_(base(), graphql="__error"))
case("retarget history over 100 events: red", "error", with_(base(), graphql=timeline(RETARGET, more=True)))
case("PR unreadable: red", "error", with_(base(), **{f"/pulls/{PR}": "__error"}))
case("comments unreadable: red", "error", with_(base(), **{f"/issues/{PR}/comments": "__error"}))
case("reviews unreadable: red", "error", with_(base(), **{f"/pulls/{PR}/reviews": "__error"}))
case("reactions unreadable: red", "error", with_(base(), **{f"/issues/{PR}/reactions": "__error"}))
case("head commit date unreadable (non-synchronize): red", "error", with_(base(), **{f"/commits/{HEAD}": "__error"}))
case("non-numeric PR number: red", "error", base(), env={"PR_NUMBER": "7; rm -rf /"})
case("malformed event head: red", "error", base(), env={"EVENT_HEAD": "$(id)"})
case("malformed push time: red", "error", base(), env={"PUSHED_AT": "yesterday"})


def check_shape(wf):
    on = wf.get("on", wf.get(True))
    assert set(on) == {"pull_request_target", "workflow_dispatch"}, \
        "only default-branch-copy triggers: never pull_request (it runs the PR's copy with the PAT in reach)"
    assert set(on["pull_request_target"]["types"]) == {"opened", "synchronize", "reopened", "ready_for_review", "edited"}, \
        "a base retarget (edited) must re-request: review-verdict ignores older evidence"
    assert "github.event.changes.base != null" in wf["jobs"]["codex-auto-request"]["if"], \
        "edited runs only for a retarget, not a title/body edit"
    verdict = (ROOT / ".github/workflows/review-verdict.yml").read_text()
    pat = wf["jobs"]["codex-auto-request"]["steps"][0]["env"]["REVIEW_SKIP_PATTERN"]
    assert f'REVIEW_SKIP_PATTERN: "{pat}"' in verdict, "REVIEW_SKIP_PATTERN must equal review-verdict.yml's"
    assert wf["permissions"] == {"contents": "read", "pull-requests": "read", "issues": "read"}, \
        "GITHUB_TOKEN must stay read-only; the one write uses the PAT"
    job = wf["jobs"]["codex-auto-request"]
    assert "permissions" not in job, "no job-level permission widening"
    assert "github.event.pull_request.draft == false" in job["if"]
    assert "github.event.pull_request.head.repo.full_name == github.repository" in job["if"]
    text = WORKFLOW.read_text()
    assert "actions/checkout" not in text and "uses:" not in text, "no checkout and no third-party action"
    assert text.count("secrets.AAA_ORG_TOKEN") == 1, "the org PAT appears exactly once"
    steps = job["steps"]
    pat_steps = [s for s in steps if "secrets.AAA_ORG_TOKEN" in json.dumps(s)]
    assert len(pat_steps) == 1 and pat_steps[0]["id"] == "request", "only the request step sees the PAT"
    req = pat_steps[0]
    assert req["if"] == "steps.decide.outputs.request == 'true'"
    assert req["run"].count("gh api") == 1 and "-X POST" in req["run"] and "/comments" in req["run"], \
        "the PAT makes exactly one API call: the comment"
    for s in steps:
        run = s.get("run", "")
        for bad in ("github.event.pull_request.title", "github.event.pull_request.body",
                    "github.event.pull_request.head.ref", "github.event.comment"):
            assert bad not in run and bad not in json.dumps(s.get("env", {})), f"never interpolate {bad}"
        assert "${{" not in run, "no expression is interpolated into a script; pass values via env"
    assert wf["concurrency"]["cancel-in-progress"] is False, \
        "never cancel: a cancelled check on the head reads as red to doc-auto-merge"
    for needle in ("github.event.pull_request.number", "github.event.inputs.pr_number",
                   "github.event.pull_request.head.sha"):
        assert needle in wf["concurrency"]["group"], f"concurrency group must key on {needle}"
    decide = steps[0]
    assert decide["id"] == "decide" and decide["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert int(decide["env"]["DELAY_SECONDS"]) >= 120, "give Codex's own auto-review 2-3 minutes"


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
                   **static_env, "REPO": REPO, "PR_NUMBER": str(PR), "EVENT_HEAD": "", "PUSHED_AT": "",
                   "GH_TOKEN": "read-token", "DELAY_SECONDS": "0", "POLL_SECONDS": "0",
                   "REACTION_GRACE_SECONDS": "0", "GITHUB_OUTPUT": str(outf), "FIXTURES": str(tmp / "fx.json"), "STATE_DIR": str(state),
                   **env_over}
            out = subprocess.run(["bash", str(tmp / "script.sh")], env=env, capture_output=True, text=True,
                                 timeout=60)
            kv = dict(l.split("=", 1) for l in outf.read_text().splitlines() if "=" in l)
            if out.returncode != 0:
                got = "error"
            elif kv.get("request") == "true":
                got = "request"
            elif kv.get("request") == "false":
                got = "skip"
            else:
                got = "no-decision"
            ok = got == expect and not (state / "writes").exists()
            if got == "request":
                ok = ok and kv.get("head") == HEAD
            if got == "error":
                ok = ok and "request" not in kv
            failures += not ok
            last = (out.stdout.strip().splitlines() or [""])[-1]
            print(f"{'PASS' if ok else 'FAIL'}  {name}  (expected {expect}, got {got})")
            if os.environ.get("VERBOSE"):
                print("      " + last)
            if not ok:
                print(out.stdout[-1500:], out.stderr[-1500:], sep="\n")
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
