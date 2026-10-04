#!/usr/bin/env python3
"""Offline tests for actions-budget-alert.yml.

Extracts the three step scripts (fetch, compute, alert), puts a stub `gh`
first on PATH that serves canned API responses (fixture JSON for the billing
usage API, the public repo list, labels, issues and comments) and records
every write, then runs the steps in order exactly as the job does: a step
that exits non-zero ends the run (red). Needs python3 + PyYAML, bash 4+ and jq.

    uv run --with pyyaml python tests/actions-budget-alert/test_actions_budget_alert.py
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
WORKFLOW = ROOT / ".github/workflows/actions-budget-alert.yml"

ORG = "acme"
REPO = "acme/.github"
BOT = "github-actions[bot]"
TODAY = "2026-10-05"
DAY = "2026-10-04"
TITLE = "Actions budget alert 2026-10"
BILLING = f"organizations/{ORG}/settings/billing/usage"

# Stub gh. Keys are the API path without a leading "/" or "repos/" and
# without the query, e.g. "acme/.github/labels/actions-budget". A value
# "__error" fails the call (HTTP 502), "__404" fails it with HTTP 404;
# {"__pages": [...]} is served one JSON document per page with --paginate.
# Writes (POST/PATCH/...) are recorded in STATE_DIR/writes and answered from
# fx["<METHOD> <key>"] (default {"id": 1, "number": 99}). -F key=@file reads
# the file, as gh does. --jq output matches gh: strings raw, other JSON compact.
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
rec = {"method": method, "key": key, "query": full.partition("?")[2], "fields": fields,
       "token": os.environ.get("GH_TOKEN", "")}
with open(os.path.join(state_dir, "calls"), "a") as f:
    f.write(json.dumps(rec) + "\n")
if method != "GET":
    with open(os.path.join(state_dir, "writes"), "a") as f:
        f.write(json.dumps(rec) + "\n")
    v = fx.get(method + " " + key, {"id": 1, "number": 99})
    if v == "__error":
        sys.stderr.write("HTTP 502\n"); sys.exit(1)
    sys.stdout.write(json.dumps(v)); sys.exit(0)
if key not in fx:
    sys.stderr.write("gh: Not Found (HTTP 404)\n"); sys.exit(1)
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


def row(date=DAY, repo="widget", minutes=10.0, net=None, sku="Actions Linux", unit="Minutes", t="12:00:00Z",
        **over):
    """One billing usage row, priced like the real API (0.006/min)."""
    gross = round(minutes * 0.006, 6)
    net = gross if net is None else net
    r = {"date": f"{date}T{t}", "product": "actions", "sku": sku, "quantity": minutes, "unitType": unit,
         "pricePerUnit": 0.006, "grossAmount": gross, "discountAmount": round(gross - net, 6),
         "netAmount": net, "organizationName": ORG, "repositoryName": repo}
    r.update(over)
    return r


PUBLIC = ["pub-site", ".github"]


def world(rows=None, usage=None, public=None, label=True, issues=(), comments=None, **extra):
    """A complete set of fixtures. rows: the usage rows (default: one quiet
    day). issues: issue objects for the label listing. comments: {n: [...]}."""
    if usage is None:
        usage = {"usageItems": rows if rows is not None else [row()]}
    fx = {
        BILLING: usage,
        f"orgs/{ORG}/repos": {"__pages": [[{"name": n} for n in (PUBLIC if public is None else public)]]},
        f"{REPO}/labels/actions-budget": {"name": "actions-budget"} if label else "__404",
        f"{REPO}/issues": {"__pages": [list(issues)]},
    }
    for n, cs in (comments or {}).items():
        fx[f"{REPO}/issues/{n}/comments"] = {"__pages": [cs]}
    for i in issues:
        fx.setdefault(f"{REPO}/issues/{i['number']}/comments", {"__pages": [[]]})
    fx.update(extra)
    return copy.deepcopy(fx)


def issue(n, title=TITLE, state="open", user=BOT, body="", pr=False):
    i = {"number": n, "title": title, "state": state, "user": {"login": user}, "body": body}
    if pr:
        i["pull_request"] = {"url": "x"}
    return i


def comment(body, user=BOT):
    return {"id": 5, "user": {"login": user}, "body": body}


DAY_MARK = f"<!-- actions-budget-alert:day={DAY} -->"
INCLUDED_MARK = "<!-- actions-budget-alert:included=2026-10 -->"

CASES = []


def case(name, fx, *, ok=True, red_at=None, says=(), not_says=(), env=None, writes=None, check=None,
         alerts=None):
    """ok: the run is green. red_at: the step that must fail ("fetch",
    "compute", "alert"). says / not_says: stdout substrings. writes: the exact
    list of "METHOD key" writes, in order. alerts: the exact alert keys the
    compute step reports. check(writes, report): extra assertions."""
    CASES.append(dict(name=name, fx=fx, ok=ok, red_at=red_at, says=says, not_says=not_says, env=env or {},
                      writes=writes, check=check, alerts=alerts))


ISSUE_POST = f"POST {REPO}/issues"
COMMENT_POST = f"POST {REPO}/issues/7/comments"
LABEL_POST = f"POST {REPO}/labels"


def big_day(minutes=1600):
    # Spread over 4 repos so no single repo trips the per-repo limit.
    return [row(repo=f"r{i}", minutes=minutes / 4) for i in range(4)]


# ── Thresholds ─────────────────────────────────────────────────────────────
case("quiet day: no alert, summary only, no writes", world(), alerts=[], writes=[], says=("No alert for 2026-10-04",))
case("day of exactly 1,500 minutes: no daily alert", world(big_day(1500)), alerts=[], writes=[])
case("day of 1,501 minutes: daily alert", world(big_day(1504)), alerts=["daily"],
     writes=[ISSUE_POST])
case("one repo at exactly 800 minutes: no repo alert", world([row(minutes=800)]), alerts=[], writes=[])
case("one repo over 800 minutes: repo alert naming it", world([row(repo="hog", minutes=801)]), alerts=["repo"],
     writes=[ISSUE_POST], check=lambda w, r: "hog (801 min)" in w[0]["fields"]["body"])
case("billable month to date at 3,000: included alert",
     world([row(date="2026-10-01", minutes=700) for _ in range(4)] + [row(minutes=200)]),
     alerts=["included", "projected"])
case("billable month to date 2,999: no included alert",
     world([row(date="2026-10-01", repo=f"r{i}", minutes=700) for i in range(4)] + [row(minutes=199)]),
     check=lambda w, r: "included" not in [a["key"] for a in r["alerts"]])
case("public minutes never count toward the included 3,000",
     world([row(date="2026-10-01", repo="pub-site", minutes=750, net=0) for _ in range(4)] + [row(minutes=10)]),
     check=lambda w, r: r["mtd"]["billable_minutes"] == 10 and r["mtd"]["public_minutes"] == 3000
     and "included" not in [a["key"] for a in r["alerts"]])
case("a repo missing from the public list (deleted) counts as billable",
     world([row(repo="gone-repo", minutes=5)]),
     check=lambda w, r: r["mtd"]["billable_minutes"] == 5 and r["day_top"][0]["public"] is False)
case("projected net over $100: projected alert",
     # 4 days x 120 billable min/day -> 3,720 projected -> (3720-3000)*0.006 = $4.32: no alert ...
     world([row(date=f"2026-10-0{d}", minutes=120) for d in range(1, 5)]),
     check=lambda w, r: r["projection"]["billable_minutes"] == 3720 and r["projection"]["net"] == 4.32
     and r["alerts"] == [])
case("projected net over $100 (900 billable min/day): projected alert",
     # 4 x 900 = 3,600 MTD; 27,900 projected; (27900-3000)*0.006 = $149.40
     world([row(date=f"2026-10-0{d}", repo=f"r{d}", minutes=900, net=0) for d in range(1, 5)]),
     check=lambda w, r: r["projection"]["net"] == 149.4 and "projected" in [a["key"] for a in r["alerts"]])
case("projected net never below the net already billed",
     world([row(minutes=10, net=50.0)]),
     check=lambda w, r: r["projection"]["net"] == 50.0)
case("thresholds come from env: lower daily limit trips",
     world([row(minutes=100)]), env={"DAILY_MINUTES_MAX": "99"}, alerts=["daily"])
case("non-numeric threshold is red", world(), env={"DAILY_MINUTES_MAX": "lots"}, ok=False, red_at="compute")

# ── The numbers ────────────────────────────────────────────────────────────
case("rows after the report date are not month to date (today's partial data)",
     world([row(minutes=10), row(date=TODAY, minutes=5000)]),
     check=lambda w, r: r["mtd"]["minutes"] == 10 and r["alerts"] == [])
case("other SKUs (storage, macOS) are ignored",
     world([row(minutes=10), row(sku="Actions storage", unit="GigabyteHours", minutes=9999),
            row(sku="Actions macOS 3-core", minutes=9999)]),
     check=lambda w, r: r["mtd"]["minutes"] == 10)
case("top 5 repos by minutes, ties by name, both windows",
     world([row(repo=f"r{i}", minutes=m) for i, m in enumerate([5, 50, 20, 20, 1, 30, 2])]
           + [row(date="2026-10-01", repo="r4", minutes=100)]),
     check=lambda w, r: [x["repo"] for x in r["day_top"]] == ["r1", "r5", "r2", "r3", "r0"]
     and [x["repo"] for x in r["mtd_top"]] == ["r4", "r1", "r5", "r2", "r3"])
case("a row with no repositoryName is grouped, not dropped",
     world([row(repositoryName=None, minutes=3), row(minutes=4)]),
     check=lambda w, r: r["mtd"]["minutes"] == 7 and any(x["repo"] == "(no repository)" for x in r["day_top"]))
case("month-to-date money sums gross, discount, net",
     world([row(minutes=1000, net=1.0), row(date="2026-10-01", minutes=1000, net=0.5)]),
     check=lambda w, r: r["mtd"]["gross"] == 12.0 and r["mtd"]["net"] == 1.5 and r["mtd"]["discount"] == 10.5)
case("summary carries the numbers and the top repos",
     world([row(repo="hog", minutes=1234)]),
     says=("1234 min",), check=lambda w, r: "| hog | billable | 1,234 | $7.40 |" in r["markdown"]
     and "| 2026-10-04 | 1,234 | $7.40 |" in r["markdown"])


def days_in(today, day, dim, year_month):
    return world([row(date=day, minutes=1)]), dict(TODAY=today), (day, dim, year_month)


for today, day, dim, ym in [("2026-11-01", "2026-10-31", 31, "year=2026&month=10"),
                            ("2026-10-01", "2026-09-30", 30, "year=2026&month=9"),
                            ("2027-01-01", "2026-12-31", 31, "year=2026&month=12"),
                            ("2028-03-01", "2028-02-29", 29, "year=2028&month=2"),
                            ("2027-03-01", "2027-02-28", 28, "year=2027&month=2")]:
    case(f"today {today}: report date {day}, {dim}-day month, queries {ym}",
         world([row(date=day, minutes=1)]), env={"TODAY": today},
         check=(lambda d, n, q: lambda w, r, calls: r["date"] == d and r["days_in_month"] == n
                and any(c["key"] == BILLING and c["query"] == q for c in calls))(day, dim, ym))

# ── Fails closed on data errors ────────────────────────────────────────────
case("billing API error: red at fetch", world(**{BILLING: "__error"}), ok=False, red_at="fetch",
     says=("reading org billing usage",))
case("billing API 404 (token cannot read billing): red at fetch", world(**{BILLING: "__404"}), ok=False,
     red_at="fetch")
case("no token: red at fetch, no call", world(), env={"GH_TOKEN_FETCH": ""}, ok=False, red_at="fetch",
     check=lambda w, r, calls: calls == [])
case("payload is not JSON: red", world(**{BILLING: "__raw"}), ok=False, red_at="compute")
case("payload without usageItems: red", world(usage={"items": []}), ok=False, red_at="compute")
case("usageItems not an array: red", world(usage={"usageItems": {"a": 1}}), ok=False, red_at="compute")
case("a row that is not an object: red", world(usage={"usageItems": [row(), "x"]}), ok=False, red_at="compute")
case("quantity is a string: red", world([row(quantity="10")]), ok=False, red_at="compute")
case("quantity negative: red", world([row(quantity=-1)]), ok=False, red_at="compute")
case("netAmount missing: red", world([{k: v for k, v in row().items() if k != "netAmount"}]), ok=False,
     red_at="compute")
case("unitType not Minutes on Actions Linux: red", world([row(unit="Hours")]), ok=False, red_at="compute")
case("date malformed: red", world([row(), row(date="yesterday", t="")]), ok=False, red_at="compute")
case("a row from another month: red", world([row(), row(date="2026-09-30")]), ok=False, red_at="compute")
case("repositoryName not a string: red", world([row(repositoryName=7)]), ok=False, red_at="compute")
case("no rows at all on the report date (data missing or late): red",
     world([row(date="2026-10-01")]), ok=False, red_at="compute", says=("no Actions Linux rows dated",))
case("empty usageItems: red", world([]), ok=False, red_at="compute")
case("public repo list fails: red", world(**{f"orgs/{ORG}/repos": "__error"}), ok=False, red_at="compute")
case("public repo list without .github: red, not trusted", world(public=["pub-site"]), ok=False,
     red_at="compute")
case("malformed data never writes to an issue", world([row(quantity="x", minutes=9999)]), ok=False,
     red_at="compute", writes=[])

# ── Dispatch inputs ────────────────────────────────────────────────────────
case("dry run with alerts: summary only, no writes", world(big_day()), env={"DRY_RUN": "true"},
     alerts=["daily"], writes=[], says=("Dry run: would alert (daily)",))
case("report_date replay with dry run: that day is used",
     world([row(date="2026-10-02", minutes=1)]), env={"DRY_RUN": "true", "REPORT_DATE_INPUT": "2026-10-02"},
     check=lambda w, r: r["date"] == "2026-10-02")
case("report_date without dry run: red, nothing read", world(), env={"REPORT_DATE_INPUT": "2026-10-02"},
     ok=False, red_at="fetch", check=lambda w, r, calls: calls == [])
case("report_date of today: red", world(), env={"DRY_RUN": "true", "REPORT_DATE_INPUT": TODAY}, ok=False,
     red_at="fetch")
case("report_date not a date: red", world(), env={"DRY_RUN": "true", "REPORT_DATE_INPUT": "2026-02-30"},
     ok=False, red_at="fetch")
case("report_date with shell metacharacters: red", world(),
     env={"DRY_RUN": "true", "REPORT_DATE_INPUT": "2026-10-01; id"}, ok=False, red_at="fetch")

# ── Credentials: PAT for the billing GET only ──────────────────────────────
case("the org PAT is used only for the billing GET; everything else uses GITHUB_TOKEN",
     world(big_day(), label=False),
     check=lambda w, r, calls: [c["key"] for c in calls if c["token"] == "org-pat"] == [BILLING]
     and all(c["token"] == "gh-token" for c in calls if c["key"] != BILLING)
     and all(c["method"] == "GET" for c in calls if c["token"] == "org-pat"))

# ── The issue: one per month, de-duplicated ────────────────────────────────
case("first alert of the month, label missing: label created, issue opened with label and markers",
     world(big_day(), label=False), writes=[LABEL_POST, ISSUE_POST],
     check=lambda w, r: w[0]["fields"]["name"] == "actions-budget"
     and w[1]["fields"]["title"] == TITLE and w[1]["fields"]["labels"] == ["actions-budget"]
     and DAY_MARK in w[1]["fields"]["body"] and INCLUDED_MARK not in w[1]["fields"]["body"]
     and "1,600" in w[1]["fields"]["body"])
case("label exists: not re-created", world(big_day()), writes=[ISSUE_POST])
case("label read fails (not 404): red, no issue", world(big_day(), **{f"{REPO}/labels/actions-budget": "__error"}),
     ok=False, red_at="alert", writes=[])
case("open month issue: comment on it, no new issue",
     world(big_day(), issues=[issue(7)]), writes=[COMMENT_POST],
     check=lambda w, r: DAY_MARK in w[0]["fields"]["body"])
case("already posted today (bot comment carries the day marker): no second post",
     world(big_day(), issues=[issue(7)], comments={7: [comment("x " + DAY_MARK)]}), writes=[],
     says=("Already alerted for 2026-10-04 on #7",))
case("already posted today in the issue body itself: no second post",
     world(big_day(), issues=[issue(7, body=DAY_MARK)]), writes=[])
case("yesterday's post does not block today's",
     world(big_day(), issues=[issue(7, body="<!-- actions-budget-alert:day=2026-10-03 -->")]),
     writes=[COMMENT_POST])
case("day marker in a NON-bot comment (spoof) does not suppress the alert",
     world(big_day(), issues=[issue(7)], comments={7: [comment(DAY_MARK, user="mallory")]}),
     writes=[COMMENT_POST])
case("same title by a non-bot user is not our issue: a bot issue is opened",
     world(big_day(), issues=[issue(3, user="mallory")]), writes=[ISSUE_POST])
case("a pull request with the title is ignored", world(big_day(), issues=[issue(4, pr=True)]),
     writes=[ISSUE_POST])
case("last month's issue is not this month's", world(big_day(), issues=[issue(6, title="Actions budget alert 2026-09")]),
     writes=[ISSUE_POST])
case("closed month issue: reopened and commented, never duplicated",
     world(big_day(), issues=[issue(7, state="closed")]),
     writes=[f"PATCH {REPO}/issues/7", COMMENT_POST],
     check=lambda w, r: w[0]["fields"] == {"state": "open"})
case("open issue preferred over a closed one",
     world(big_day(), issues=[issue(7), issue(9, state="closed")]), writes=[COMMENT_POST])
case("issues listed across pages",
     world(big_day(), **{f"{REPO}/issues": {"__pages": [[issue(i, title="other") for i in range(100, 200)],
                                                         [issue(7)]]},
                         f"{REPO}/issues/7/comments": {"__pages": [[]]}}),
     writes=[COMMENT_POST])
case("comments listed across pages: a marker on page 2 still counts",
     world(big_day(), issues=[issue(7)],
           **{f"{REPO}/issues/7/comments": {"__pages": [[comment("x")] * 100, [comment(DAY_MARK)]]}}),
     writes=[])

# ── Included-minutes crossing: once per month ──────────────────────────────
OVER = [row(date="2026-10-01", repo=f"r{i}", minutes=750) for i in range(4)] + [row(minutes=10)]
LOW_PRICE = {"PROJECTED_NET_MAX": "100000"}  # isolate the included alert
case("crossing first seen: posted with the included marker",
     world(OVER), env=LOW_PRICE, alerts=["included"], writes=[ISSUE_POST],
     check=lambda w, r: INCLUDED_MARK in w[0]["fields"]["body"])
case("crossing already posted this month, no other alert: nothing posted",
     world(OVER, issues=[issue(7, body="<!-- actions-budget-alert:day=2026-10-02 -->\n" + INCLUDED_MARK)]),
     env=LOW_PRICE, writes=[], says=("already posted this month on #7",))
case("crossing already posted (in a bot comment), plus a daily alert: comment without the crossing",
     world(OVER + big_day(), issues=[issue(7)], comments={7: [comment(INCLUDED_MARK)]}), env=LOW_PRICE,
     writes=[COMMENT_POST],
     check=lambda w, r: INCLUDED_MARK not in w[0]["fields"]["body"] and "included are used up" not in w[0]["fields"]["body"]
     and "over the daily limit" in w[0]["fields"]["body"])
case("crossing marker from a non-bot comment does not count: posted",
     world(OVER, issues=[issue(7)], comments={7: [comment(INCLUDED_MARK, user="mallory")]}), env=LOW_PRICE,
     writes=[COMMENT_POST], check=lambda w, r: INCLUDED_MARK in w[0]["fields"]["body"])
case("last month's crossing marker does not suppress this month's",
     world(OVER, issues=[issue(7, body="<!-- actions-budget-alert:included=2026-09 -->")]), env=LOW_PRICE,
     writes=[COMMENT_POST])

# ── Write failures are red ─────────────────────────────────────────────────
case("opening the issue fails: red", world(big_day(), **{ISSUE_POST: "__error"}), ok=False, red_at="alert")
case("opening the issue returns no number: red", world(big_day(), **{ISSUE_POST: {"message": "?"}}), ok=False,
     red_at="alert")
case("commenting fails: red", world(big_day(), issues=[issue(7)], **{COMMENT_POST: "__error"}), ok=False,
     red_at="alert")
case("comment returns no id: red", world(big_day(), issues=[issue(7)], **{COMMENT_POST: {}}), ok=False,
     red_at="alert")
case("reopen fails: red, no comment", world(big_day(), issues=[issue(7, state="closed")],
                                           **{f"PATCH {REPO}/issues/7": "__error"}),
     ok=False, red_at="alert", writes=[f"PATCH {REPO}/issues/7"])
case("issue list unreadable: red", world(big_day(), **{f"{REPO}/issues": "__error"}), ok=False, red_at="alert")
case("comments unreadable: red", world(big_day(), issues=[issue(7)], **{f"{REPO}/issues/7/comments": "__error"}),
     ok=False, red_at="alert")


def structure_checks(wf):
    """Static checks on the workflow file itself."""
    errs = []
    on = wf.get(True) or wf.get("on")
    if set(on) != {"schedule", "workflow_dispatch"}:
        errs.append(f"triggers are {sorted(on)}")
    if wf.get("permissions") != {}:
        errs.append("top-level permissions must be {}")
    job = wf["jobs"]["check"]
    if job.get("permissions") != {"issues": "write"}:
        errs.append(f"job permissions are {job.get('permissions')}")
    if "refs/heads/main" not in job.get("if", ""):
        errs.append("job must be limited to main")
    for st in job["steps"]:
        if "uses" in st:
            errs.append(f"step uses an action ({st['uses']}): no checkout or third-party code")
    pat = [st.get("id") or st["name"] for st in job["steps"] if "AAA_ORG_TOKEN" in json.dumps(st.get("env", {}))]
    if pat != ["fetch"]:
        errs.append(f"AAA_ORG_TOKEN is exposed to steps {pat}; only the billing fetch may see it")
    for k in ("INCLUDED_MINUTES", "DAILY_MINUTES_MAX", "REPO_DAILY_MINUTES_MAX", "PROJECTED_NET_MAX"):
        if k not in wf.get("env", {}):
            errs.append(f"threshold {k} missing from the top-level env")
    want = {"INCLUDED_MINUTES": "3000", "DAILY_MINUTES_MAX": "1500", "REPO_DAILY_MINUTES_MAX": "800",
            "PROJECTED_NET_MAX": "100"}
    for k, v in want.items():
        if wf.get("env", {}).get(k) != v:
            errs.append(f"{k} is {wf.get('env', {}).get(k)!r}, approved value {v}")
    return errs


def main():
    wf = yaml.safe_load(WORKFLOW.read_text())
    errs = structure_checks(wf)
    for e in errs:
        print(f"FAIL  structure: {e}")
    if not errs:
        print("PASS  structure: triggers, permissions, PAT scope, thresholds")
    steps = {st.get("id", "alert"): st for st in wf["jobs"]["check"]["steps"]}
    assert list(steps) == ["fetch", "compute", "alert"], list(steps)
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
            fetch_token = env_over.pop("GH_TOKEN_FETCH", "org-pat")
            base = {"PATH": f"{tmp / 'bin'}:{os.environ['PATH']}", "HOME": os.environ.get("HOME", "/tmp"),
                    **{k: str(v) for k, v in wf["env"].items()},
                    "ORG": ORG, "REPO": REPO, "TODAY": TODAY, "RUNNER_TEMP": str(state),
                    "USAGE_FILE": str(state / "usage.json"), "REPORT_FILE": str(state / "report.json"),
                    "BODY_FILE": str(state / "body.md"), "BOT_LOGIN": BOT, "DRY_RUN": "false",
                    "REPORT_DATE_INPUT": "", "GITHUB_STEP_SUMMARY": str(state / "summary.md"),
                    "FIXTURES": str(tmp / "fx.json"), "STATE_DIR": str(state)}
            base.update(env_over)
            out_all, red_at, outputs = "", None, {}
            for sid in ("fetch", "compute", "alert"):
                env = dict(base)
                env["GH_TOKEN"] = fetch_token if sid == "fetch" else "gh-token"
                env["GITHUB_OUTPUT"] = str(state / f"out_{sid}")
                if sid == "compute":
                    env["REPORT_DATE"] = outputs.get("report_date", "")
                if sid == "alert":
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
            report = json.loads(rfile.read_text()) if rfile.exists() and red_at not in ("fetch", "compute") else None
            why = []
            if (red_at is None) != c["ok"]:
                why.append(f"red_at={red_at}")
            if c["red_at"] and red_at != c["red_at"]:
                why.append(f"expected red at {c['red_at']}, got {red_at}")
            if c["writes"] is not None and [f"{w['method']} {w['key']}" for w in writes] != c["writes"]:
                why.append(f"writes {[w['method'] + ' ' + w['key'] for w in writes]}")
            if c["alerts"] is not None and (report is None or [a["key"] for a in report["alerts"]] != c["alerts"]):
                why.append(f"alerts {None if report is None else [a['key'] for a in report['alerts']]}")
            for s in c["says"]:
                if s not in out_all:
                    why.append(f"missing {s!r}")
            for s in c["not_says"]:
                if s in out_all:
                    why.append(f"unexpected {s!r}")
            if c["check"] is not None:
                try:
                    argc = c["check"].__code__.co_argcount
                    res = c["check"](writes, report, calls) if argc == 3 else c["check"](writes, report)
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
