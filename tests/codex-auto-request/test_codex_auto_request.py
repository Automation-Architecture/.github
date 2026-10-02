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
DOC_AUTO_MERGE = ROOT / ".github/workflows/doc-auto-merge.yml"

REPO = "acme/widget"
PR = 7
HEAD = "034be02bb328773fbe762ec8c93c14a1bedde66b"
OLD = "9f1c2d3e4b5a69788796a5b4c3d2e1f00112233a"
NEW = "1" * 40
CODEX = "chatgpt-codex-connector[bot]"
COMMITTED = "2026-10-01T09:58:00Z"   # head commit's committer date
PUSHED = "2026-10-01T10:00:00Z"      # synchronize event time
RETARGET = "2026-10-01T10:30:00Z"    # edited (base changed) event time
READY = "2026-10-01T10:30:00Z"       # ready_for_review event time
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


def pr(state="open", head=HEAD, draft=False, changed=None, files=("app.py",), labels=()):
    return {"number": PR, "state": state, "head": {"sha": head}, "draft": draft,
            "changed_files": len(files) if changed is None else changed,
            "labels": [{"name": l} for l in labels]}


def files_(*names, renamed=None):
    out = [{"filename": n, "status": "modified"} for n in names]
    for new, old in (renamed or {}).items():
        out.append({"filename": new, "status": "renamed", "previous_filename": old})
    return out


def seq(*vals):
    """Successive calls to one endpoint return successive values (the last repeats)."""
    return {"__seq": list(vals)}


def comment(user, body, at="2026-10-01T10:01:00Z", type_=None):
    login_type = type_ or ("Bot" if user.endswith("[bot]") else "User")
    return {"user": {"login": user, "type": login_type}, "created_at": at, "body": body}


def row(status, sha, at="2026-10-01T10:03:00", dt=None):
    """dt overrides the whole datetime attribute value (None: the status's usual form)."""
    stamp = {"Completed": f"{at}.123Z", "Running": f"{at}.5Z", "Failed": f"{at}Z"}.get(status)
    if dt is not None:
        stamp = dt
    cells = {"Completed": f'✅ **Completed** <relative-time datetime="{stamp}">x</relative-time>',
             "Running": f'🔄 **Running** since <relative-time datetime="{stamp}">x</relative-time>',
             "Failed": f'⚠️ **Failed** <relative-time datetime="{stamp}">x</relative-time>',
             "Queued": "⏳ **Queued**"}
    return f"| 📝 **Code Review** | {cells[status]} | `{sha[:7]}` | PR opened |"


def summary(*rows):
    return comment(CODEX, "\n".join([SUMMARY, "", "## Codex Review Summary", "",
                                      "| Review | Status | Commit | Review trigger |", "| --- | --- | --- | --- |",
                                      *rows, "", "<details>Comment \"@codex review\".</details>"]),
                   at="2026-10-01T10:02:00Z")


def review(commit=HEAD, at="2026-10-01T10:04:00Z", user=CODEX, state="COMMENTED"):
    return {"user": {"login": user}, "commit_id": commit, "submitted_at": at, "state": state}


def base(comments=(), reviews=(), files=("app.py",), renamed=None, **prkw):
    listed = files_(*files, renamed=renamed)
    prkw.setdefault("changed", len(listed))
    return {f"/pulls/{PR}": pr(files=files, **prkw),
            f"/pulls/{PR}/files": listed,
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
EDIT = {"EVENT_AT": RETARGET, "AFTER_EVENT": "true"}
READY_ENV = {"EVENT_AT": READY, "AFTER_EVENT": "true"}

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

# --- (a) the head moved / closed / back to draft ----------------------------------
case("head already moved before the wait: skips", "skip", with_(base(), **{f"/pulls/{PR}": pr(head=NEW)}), env=SYNC)
case("head moved during the wait: skips", "skip",
     with_(base(), **{f"/pulls/{PR}": seq(pr(), pr(head=NEW))}), env=SYNC)
case("closed during the wait: skips", "skip", with_(base(), **{f"/pulls/{PR}": seq(pr(), pr(state="closed"))}))
case("converted to draft during the wait: skips (wave-2 P2)", "skip",
     with_(base(), **{f"/pulls/{PR}": seq(pr(), pr(draft=True))}), env=SYNC)
case("already a draft again when the run starts: skips", "skip", base(draft=True))
case("still open, same head, not a draft after the wait: nudges", "nudge",
     with_(base(), **{f"/pulls/{PR}": seq(pr(), pr())}), env=SYNC)

# --- Markdown-only PRs are skipped before the wait (doc-auto-merge's definition) ------
case("Markdown-only PR: skips (the gate needs no Codex review)", "skip",
     base(files=("README.md", "docs/guide.markdown", "AGENTS.md")))
case("Markdown-only PR, even with nothing from Codex and no request: skips", "skip",
     with_(base(files=("CLAUDE.md",)), **{f"/issues/{PR}/comments": "__error", f"/pulls/{PR}/reviews": "__error"}))
case("Markdown rename from Markdown: skips", "skip", base(files=("a.md",), renamed={"b.md": "old/b.md"}))
case("Markdown-only and also a draft-free retarget: skips", "skip", base(files=("README.md",)), env=EDIT)
case(".mdx is code, not Markdown: nudges", "nudge", base(files=("README.md", "docs/page.mdx")))
case("Markdown plus one code file: nudges", "nudge", base(files=("README.md", "app.py")))
case("rename from a code file to .md: nudges", "nudge", base(files=("README.md",), renamed={"notes.md": "notes.py"}))
case("upper-case .MD does not match (case-sensitive, like doc-auto-merge): nudges", "nudge",
     base(files=("README.MD",)))
case("300 or more changed files is beyond the doc bound: nudges", "nudge", base(files=("README.md",), changed=300))
case("file list shorter than changed_files (truncated): nudges", "nudge", base(files=("README.md",), changed=2))
case("changed_files missing: treated as code, nudges", "nudge",
     with_(base(files=("README.md",)), **{f"/pulls/{PR}": {"number": PR, "state": "open", "head": {"sha": HEAD}, "draft": False}}))
case("file list unreadable: red", "error", with_(base(files=("README.md",)), **{f"/pulls/{PR}/files": "__error"}))
# A held Markdown-only PR needs Codex: review-verdict switches the documentation
# exemption off for the no-auto-merge label (Codex on .github#72).
case("Markdown-only PR held by no-auto-merge: nudged like code", "nudge",
     base(files=("README.md",), labels=("no-auto-merge",)))
case("Markdown-only PR held, Codex already has a row for the head: skips", "skip",
     base(files=("README.md",), labels=("no-auto-merge",), comments=[summary(row("Completed", HEAD))]))
case("Markdown-only PR held, someone already asked: skips", "skip",
     base(files=("README.md",), labels=("no-auto-merge",), comments=[comment("web3sea", "@codex review")]))
case("Markdown-only PR whose labels are unreadable: nudged (the exemption cannot be shown to apply)", "nudge",
     with_(base(files=("README.md",)), **{f"/pulls/{PR}": {**pr(files=("README.md",)), "labels": None}}))
case("Markdown-only PR with an unrelated label: skips", "skip", base(files=("README.md",), labels=("documentation",)))
case("code PR held by no-auto-merge: nudges as before", "nudge", base(labels=("no-auto-merge",)))

# --- (b) Codex already touched the head, ANY status -------------------------------
for st in ("Completed", "Running", "Queued", "Failed"):
    case(f"summary row {st} for the head: skips", "skip", base(comments=[summary(row(st, HEAD))]))
case("row for the head next to a row for an older head: skips", "skip",
     base(comments=[summary(row("Completed", OLD), row("Failed", HEAD))]))
case("a row in an OLDER summary comment still counts: skips", "skip",
     base(comments=[summary(row("Completed", HEAD)), summary(row("Running", OLD))]))
case("Codex review object on the head (any state): skips", "skip", base(reviews=[review(state="DISMISSED")]))
case("a head row whose status has NO datetime counts (no retarget): skips", "skip",
     base(comments=[summary(row("Queued", HEAD))]))
case("a head row with an unparseable datetime still counts: skips", "skip",
     base(comments=[summary(row("Completed", HEAD, dt="soon"))]), env=EDIT)
case("pending Codex review on the head with no submitted_at: skips", "skip",
     base(reviews=[{"user": {"login": CODEX}, "commit_id": HEAD, "submitted_at": None, "state": "PENDING"}]))
case("retarget, pending undated Codex review on the head: skips", "skip",
     base(reviews=[{"user": {"login": CODEX}, "commit_id": HEAD, "submitted_at": None, "state": "PENDING"}]), env=EDIT)

# --- (c) someone already asked -----------------------------------------------------
case("web3sea @codex review after the push: skips", "skip",
     base(comments=[comment("web3sea", "@codex review", at="2026-10-01T10:00:30Z")]), env=SYNC)
case("our own earlier nudge for this head: skips (once per head)", "skip",
     base(comments=[comment("web3sea", "@codex review", at="2026-10-01T10:03:00Z")]), env=SYNC)

# --- (c) bots' requests never count: Codex refuses them (P1, aaa-profit-run-rate#15) ---
for bot in ("claude[bot]", "github-actions[bot]", "aaa-dashboard-bot[bot]"):
    case(f"{bot}'s @codex review after the push does NOT count: nudges", "nudge",
         base(comments=[comment(bot, "@codex review please", at="2026-10-01T10:05:00Z")]), env=SYNC)
case("a type-Bot account without a [bot] suffix does not count: nudges", "nudge",
     base(comments=[comment("renovate", "@codex review", at="2026-10-01T10:05:00Z", type_="Bot")]), env=SYNC)
case("a [bot] login typed User by the API still does not count: nudges", "nudge",
     base(comments=[comment("claude[bot]", "@codex review", at="2026-10-01T10:05:00Z", type_="User")]), env=SYNC)
case("a comment and a review by a deleted user (user: null) do not break the run (P2 #72): nudges", "nudge",
     base(comments=[{"user": None, "created_at": "2026-10-01T10:05:00Z", "body": "LGTM"}],
          reviews=[{"user": None, "commit_id": HEAD, "submitted_at": "2026-10-01T10:04:00Z", "state": "COMMENTED"}]),
     env=SYNC)
case("a bot's request AND a person's request after the push: skips", "skip",
     base(comments=[comment("claude[bot]", "@codex review", at="2026-10-01T10:04:00Z"),
                    comment("web3sea", "@codex review", at="2026-10-01T10:05:00Z")]), env=SYNC)
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

# --- summary-row times: Z, fractions, offsets, compared as instants (wave-2 P2s) -----
for dt, when in (("2026-10-01T10:29:59Z", "before"), ("2026-10-01T10:29:59.999Z", "before"),
                 ("2026-10-01T10:29:59", "before"), ("2026-10-01T12:29:00+02:00", "before"),
                 ("2026-10-01T05:29:00-0500", "before"),
                 ("2026-10-01T10:30:01Z", "after"), ("2026-10-01T10:30:00.5Z", "after"),
                 ("2026-10-01T12:31:00+02:00", "after"), ("2026-10-01T06:31:00-04:00", "after")):
    case(f"retarget, head row at {dt} ({when} the retarget): {'nudges' if when == 'before' else 'skips'}",
         "nudge" if when == "before" else "skip",
         base(comments=[summary(row("Completed", HEAD, dt=dt))]), env=EDIT)

# --- draft -> ready: count only what came after the ready event (wave-2 P2s) --------
case("ready, Codex reviewed the head before (while draft / before a draft retarget): nudges", "nudge",
     base(comments=[summary(row("Completed", HEAD))], reviews=[review()]), env=READY_ENV)
case("ready, a person's request made while draft (before ready): nudges", "nudge",
     base(comments=[comment("web3sea", "@codex review", at="2026-10-01T10:20:00Z")]), env=READY_ENV)
case("ready, Codex row after the ready event (Draft marked ready): skips", "skip",
     base(comments=[summary(row("Running", HEAD, at="2026-10-01T10:30:40"))]), env=READY_ENV)
case("ready, Codex review after the ready event: skips", "skip",
     base(reviews=[review(at="2026-10-01T10:33:00Z")]), env=READY_ENV)
case("ready, a person's request after the ready event: skips", "skip",
     base(comments=[comment("web3sea", "@codex review", at="2026-10-01T10:31:00Z")]), env=READY_ENV)
case("ready, nothing at all: nudges", "nudge", base(), env=READY_ENV)

# --- Dependabot-triggered runs get no secrets: stop at once, no API call ---------------
case("Dependabot-triggered run: skips before any read or wait", "skip",
     {k: "__error" for k in base()}, env={"ACTOR": "dependabot[bot]", "DELAY_SECONDS": "600"})
case("a person's event on a Dependabot PR runs normally: nudges", "nudge", base(), env={"ACTOR": "web3sea"})

# --- errors (red run, never a nudge) -------------------------------------------------
case("PR unreadable: red", "error", with_(base(), **{f"/pulls/{PR}": "__error"}))
case("comments unreadable: red", "error", with_(base(), **{f"/issues/{PR}/comments": "__error"}))
case("reviews unreadable: red", "error", with_(base(), **{f"/pulls/{PR}/reviews": "__error"}))
case("head commit date unreadable: red", "error", with_(base(), **{f"/commits/{HEAD}": "__error"}))
case("non-numeric PR number: red", "error", base(), env={"PR_NUMBER": "7; id"})
case("malformed head: red", "error", base(), env={"EVENT_HEAD": "$(id)"})
case("malformed event time: red", "error", base(), env={"EVENT_AT": "yesterday"})
case("retarget or ready with no event time: red", "error", base(), env={"AFTER_EVENT": "true"})


def check_shape(wf):
    on = wf.get("on", wf.get(True))
    assert set(on) == {"pull_request_target"}, \
        "only pull_request_target: pull_request runs the PR's copy, workflow_dispatch --ref a branch's copy"
    assert set(on["pull_request_target"]["types"]) == {"opened", "synchronize", "reopened", "ready_for_review", "edited",
                                                       "labeled"}
    assert wf["permissions"] == {"contents": "read", "pull-requests": "read", "issues": "read"}, \
        "GITHUB_TOKEN stays read-only; the one write uses the PAT"
    job = wf["jobs"]["codex-auto-request"]
    assert "permissions" not in job, "no job-level permission widening"
    for needle in ("github.event_name == 'pull_request_target'", "github.event.pull_request.draft == false",
                   "github.event.pull_request.head.repo.full_name == github.repository",
                   "github.event.changes.base != null",
                   "github.event.action != 'labeled' || github.event.label.name == 'no-auto-merge'"):
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
    group = wf["concurrency"]["group"]
    for needle in ("github.event.pull_request.number", "github.event.pull_request.head.sha", "github.run_id",
                   "'shared'", '["opened","synchronize","reopened"]', "github.event.pull_request.draft == false",
                   "github.event.pull_request.head.repo.full_name == github.repository"):
        assert needle in group, f"concurrency group must include {needle}"
    assert "edited" not in group and "ready_for_review" not in group, \
        "skipped edits, retargets and ready events get their own group (GitHub replaces a pending run)"
    assert steps[0]["env"]["ACTOR"] == "${{ github.actor }}", "the Dependabot early exit keys on the run's actor"
    assert 150 <= int(steps[0]["env"]["DELAY_SECONDS"]) <= 240, "about 3 minutes for Codex's own review"
    env = steps[0]["env"]
    for action in ("synchronize", "edited", "ready_for_review"):
        assert f"github.event.action == '{action}'" in env["EVENT_AT"], f"EVENT_AT must be set for {action}"
    for action in ("edited", "ready_for_review"):
        assert f"github.event.action == '{action}'" in env["AFTER_EVENT"], f"AFTER_EVENT must be set for {action}"
    for action in ("opened", "reopened", "synchronize"):
        assert action not in env["AFTER_EVENT"], f"{action} must count evidence for the head whenever made"
    # Same Markdown-only definition as doc-auto-merge.yml (keep the two in step).
    dam_env = yaml.safe_load(DOC_AUTO_MERGE.read_text())["jobs"]["doc-auto-merge"]["steps"][0]["env"]
    assert env["DOC_ONLY_PATTERN"] == dam_env["DOC_ONLY_PATTERN"], "doc-only pattern drifted from doc-auto-merge.yml"
    assert env["MAX_DOC_FILES"] == dam_env["MAX_FILES"], "doc-only file bound drifted from doc-auto-merge.yml"
    # The hold label is review-verdict.yml's, and the job condition names the same one.
    rv_env = yaml.safe_load((ROOT / ".github/workflows/review-verdict.yml").read_text())["jobs"]["publish"]["steps"][0]["env"]
    assert env["HOLD_LABEL"] == rv_env["HOLD_LABEL"] == dam_env["HOLD_LABEL"], "hold label drifted from review-verdict.yml"
    assert f"github.event.label.name == '{env['HOLD_LABEL']}'" in job["if"], "job condition must name the hold label"
    assert "labeled" not in group.replace("github.event.action", ""), "a labeled run gets its own group"


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
                   "AFTER_EVENT": "false", "ACTOR": "web3sea", "GH_TOKEN": "read-token", "DELAY_SECONDS": "0",
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
