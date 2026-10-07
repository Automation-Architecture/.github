"""Offline tests for scripts/monitor_health.py (GAAA-3937).

Fake GitHub reads keyed by endpoint, a fixed clock, fake Slack and issue
writes. No token, no network.

    python3 -m unittest discover -s tests/monitor-health -v
"""
import base64
import datetime as dt
import importlib.util
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("monitor_health", ROOT / "scripts" / "monitor_health.py")
mh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mh)

ORG = "acme"
NOW = dt.datetime(2026, 10, 7, 16, 0, tzinfo=dt.timezone.utc)


def ts(hours_ago):
    return mh.iso(NOW - dt.timedelta(hours=hours_ago))


def run(id_, hours_ago, conclusion="success", status="completed"):
    return {"id": id_, "created_at": ts(hours_ago), "status": status, "conclusion": conclusion,
            "html_url": f"https://github.com/{ORG}/r/actions/runs/{id_}"}


class FakeRead:
    """Endpoint -> response. A list value is served one element per call (last
    one repeats); "__404" raises NotFound, "__error" raises ReadError. Prefix
    match on the path before '?' when no exact key exists."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.served = {}

    def __call__(self, endpoint, paginate=False):
        self.calls.append(endpoint)
        key = endpoint if endpoint in self.routes else None
        if key is None:
            path = endpoint.split("?", 1)[0]
            matches = [k for k in self.routes if k.split("?", 1)[0] == path and "?" not in k]
            key = matches[0] if matches else None
        if key is None:
            raise AssertionError(f"unexpected read {endpoint}")
        value = self.routes[key]
        if isinstance(value, list) and value and isinstance(value[0], dict) and value[0].get("__seq"):
            i = self.served.get(key, 0)
            self.served[key] = i + 1
            value = value[min(i, len(value) - 1)]["value"]
        if value == "__404":
            raise mh.NotFound(f"GET {endpoint}: HTTP 404")
        if value == "__error":
            raise mh.ReadError(f"GET {endpoint}: HTTP 502")
        return json.loads(json.dumps(value))


def seq(*values):
    return [{"__seq": True, "value": v} for v in values]


def wf_routes(repo, wf, state="active", runs=(), count=None, created_hours_ago=24 * 30):
    base = f"repos/{ORG}/{repo}/actions/workflows/{wf}"
    routes = {
        base: {"state": state, "created_at": ts(created_hours_ago), "updated_at": ts(created_hours_ago)},
        f"{base}/runs?event=schedule&per_page=20": {"total_count": len(runs), "workflow_runs": list(runs)},
    }
    if count is not None:
        since = mh.quote(">=" + mh.iso(NOW - dt.timedelta(hours=24)), safe="")
        routes[f"{base}/runs?event=schedule&created={since}&per_page=1"] = {"total_count": count, "workflow_runs": []}
    return routes


def entry(repo="r", wf="w.yml", window=27, floor=0):
    return {"repo": repo, "workflow": wf, "window_hours": window, "min_runs_24h": floor, "owner": "Engineer"}


def no_sleep(_):
    pass


class WorkflowChecks(unittest.TestCase):
    def check(self, routes, e=None):
        return mh.check_workflow(FakeRead(routes), ORG, e or entry(), NOW, sleep=no_sleep)

    def kinds(self, found):
        return sorted(f["kind"] for f in found)

    def test_healthy_daily(self):
        self.assertEqual(self.check(wf_routes("r", "w.yml", runs=[run(1, 15)])), [])

    def test_disabled_reports_state_only(self):
        found = self.check(wf_routes("r", "w.yml", state="disabled_manually", runs=[run(1, 90, "failure")]))
        self.assertEqual(self.kinds(found), ["workflow_not_active"])
        self.assertIn("disabled_manually", found[0]["message"])

    def test_disabled_inactivity(self):
        found = self.check(wf_routes("r", "w.yml", state="disabled_inactivity", runs=[run(1, 1)]))
        self.assertEqual(self.kinds(found), ["workflow_not_active"])

    def test_missing_workflow(self):
        found = self.check({f"repos/{ORG}/r/actions/workflows/w.yml": "__404"})
        self.assertEqual(self.kinds(found), ["workflow_missing"])

    def test_late_run(self):
        self.assertEqual(self.kinds(self.check(wf_routes("r", "w.yml", runs=[run(1, 30)]))), ["scheduled_run_late"])

    def test_no_run_at_all(self):
        self.assertEqual(self.kinds(self.check(wf_routes("r", "w.yml", runs=[]))), ["no_scheduled_run"])

    def test_failed_run(self):
        found = self.check(wf_routes("r", "w.yml", runs=[run(2, 3, "failure"), run(1, 27, "success")]))
        self.assertEqual(self.kinds(found), ["last_run_failed"])
        self.assertIn("`failure`", found[0]["message"])
        self.assertTrue(found[0]["url"].endswith("/2"))

    def test_late_and_failed_both_reported(self):
        found = self.check(wf_routes("r", "w.yml", runs=[run(1, 40, "cancelled")]))
        self.assertEqual(self.kinds(found), ["last_run_failed", "scheduled_run_late"])

    def test_in_progress_newest_uses_newest_completed(self):
        runs = [run(3, 0.1, None, "in_progress"), run(2, 24, "success")]
        self.assertEqual(self.check(wf_routes("r", "w.yml", runs=runs)), [])

    def test_in_progress_newest_with_failed_completed(self):
        runs = [run(3, 0.1, None, "in_progress"), run(2, 24, "failure")]
        self.assertEqual(self.kinds(self.check(wf_routes("r", "w.yml", runs=runs))), ["last_run_failed"])

    def test_under_floor(self):
        routes = wf_routes("r", "w.yml", runs=[run(1, 0.2)], count=31)
        found = self.check(routes, entry(window=2, floor=40))
        self.assertEqual(self.kinds(found), ["under_running"])
        self.assertIn("31 scheduled runs", found[0]["message"])

    def test_at_floor_ok(self):
        routes = wf_routes("r", "w.yml", runs=[run(1, 0.2)], count=40)
        self.assertEqual(self.check(routes, entry(window=2, floor=40)), [])

    def test_floor_zero_never_reads_count(self):
        read = FakeRead(wf_routes("r", "w.yml", runs=[run(1, 1)]))
        mh.check_workflow(read, ORG, entry(), NOW, sleep=no_sleep)
        self.assertFalse(any("created=" in c for c in read.calls))

    def test_stale_replica_read_is_confirmed_away(self):
        # First read is ~3 days stale (seen live 2026-10-07); the re-read is current.
        routes = wf_routes("r", "w.yml")
        key = f"repos/{ORG}/r/actions/workflows/w.yml/runs?event=schedule&per_page=20"
        routes[key] = seq({"workflow_runs": [run(1, 87)]}, {"workflow_runs": [run(2, 15), run(1, 87)]})
        slept = []
        found = mh.check_workflow(FakeRead(routes), ORG, entry(), NOW, sleep=slept.append)
        self.assertEqual(found, [])
        self.assertEqual(len(slept), 1)

    def test_stale_count_read_is_confirmed_away(self):
        routes = wf_routes("r", "w.yml", runs=[run(1, 0.2)], count=10)
        ck = next(k for k in routes if "created=" in k)
        routes[ck] = seq({"total_count": 12}, {"total_count": 70})
        self.assertEqual(self.check(routes, entry(window=2, floor=40)), [])

    def test_stale_re_read_cannot_hide_runs_seen_earlier(self):
        # Read 1: runs current, count stale. Read 2: count current, runs stale.
        # Keeping the union of runs and the max count, both checks pass.
        routes = wf_routes("r", "w.yml", count=0)
        rk = f"repos/{ORG}/r/actions/workflows/w.yml/runs?event=schedule&per_page=20"
        ck = next(k for k in routes if "created=" in k)
        routes[rk] = seq({"workflow_runs": [run(2, 0.5)]}, {"workflow_runs": [run(1, 87)]})
        routes[ck] = seq({"total_count": 10}, {"total_count": 70})
        self.assertEqual(self.check(routes, entry(window=2, floor=40)), [])

    def test_real_problem_survives_three_reads(self):
        read = FakeRead(wf_routes("r", "w.yml", runs=[run(1, 50)]))
        found = mh.check_workflow(read, ORG, entry(), NOW, sleep=no_sleep)
        self.assertEqual(self.kinds(found), ["scheduled_run_late"])
        self.assertEqual(sum(1 for c in read.calls if "/runs?" in c), 3)

    def test_newly_added_workflow_bootstrap(self):
        routes = wf_routes("r", "w.yml", runs=[], count=0, created_hours_ago=1)
        self.assertEqual(self.check(routes, entry(window=4, floor=16)), [])

    def test_bootstrap_ends_after_window(self):
        routes = wf_routes("r", "w.yml", runs=[], created_hours_ago=5)
        self.assertEqual(self.kinds(self.check(routes, entry(window=4))), ["no_scheduled_run"])

    def test_floor_suppressed_inside_first_24h_only(self):
        routes = wf_routes("r", "w.yml", runs=[run(1, 0.5)], count=5, created_hours_ago=10)
        self.assertEqual(self.check(routes, entry(window=4, floor=16)), [])
        routes = wf_routes("r", "w.yml", runs=[run(1, 0.5)], count=5, created_hours_ago=30)
        self.assertEqual(self.kinds(self.check(routes, entry(window=4, floor=16))), ["under_running"])

    def test_reenabled_workflow_gets_one_window(self):
        routes = wf_routes("r", "w.yml", runs=[run(1, 860, "failure")])
        base = f"repos/{ORG}/r/actions/workflows/w.yml"
        routes[base]["updated_at"] = ts(2)
        found = self.check(routes)
        # Old failed run still reported; lateness is not, inside the window.
        self.assertEqual(self.kinds(found), ["last_run_failed"])

    def test_read_error_propagates(self):
        routes = wf_routes("r", "w.yml")
        routes[f"repos/{ORG}/r/actions/workflows/w.yml/runs?event=schedule&per_page=20"] = "__error"
        with self.assertRaises(mh.ReadError):
            self.check(routes)

    def test_malformed_runs_response_is_read_error(self):
        routes = wf_routes("r", "w.yml")
        routes[f"repos/{ORG}/r/actions/workflows/w.yml/runs?event=schedule&per_page=20"] = {"message": "x"}
        with self.assertRaises(mh.ReadError):
            self.check(routes)


def policy_routes(revision="v2.12", changed_hours_ago=48):
    content = base64.b64encode(json.dumps({"revision": revision}).encode()).decode()
    return {
        f"repos/{ORG}/pr-bot/contents/gate/policy.json?ref=main": {"content": content},
        f"repos/{ORG}/pr-bot/commits?path=gate%2Fpolicy.json&sha=main&per_page=1":
            [{"commit": {"committer": {"date": ts(changed_hours_ago)}}}],
    }


GATE = {"name": "gate", "url": "https://gate.example/health", "expect_status": "ok", "policy_repo": "pr-bot",
        "policy_path": "gate/policy.json", "policy_ref": "main", "deploy_grace_hours": 6, "owner": "Engineer"}


class GateChecks(unittest.TestCase):
    def check(self, get, routes=None):
        return mh.check_gate(FakeRead(routes or policy_routes()), get, ORG, GATE, NOW, sleep=no_sleep)

    def test_healthy(self):
        self.assertEqual(self.check(lambda u: (200, '{"status":"ok","policyRevision":"v2.12"}')), [])

    def test_http_error(self):
        self.assertEqual([f["kind"] for f in self.check(lambda u: (503, "down"))], ["gate_unhealthy"])

    def test_unreachable(self):
        def get(u):
            raise mh.ReadError("GET x: URLError")
        found = self.check(get)
        self.assertEqual(found[0]["kind"], "gate_unhealthy")
        self.assertIn("unreachable", found[0]["message"])

    def test_bad_status(self):
        self.assertEqual(self.check(lambda u: (200, '{"status":"degraded"}'))[0]["kind"], "gate_unhealthy")

    def test_non_json(self):
        self.assertEqual(self.check(lambda u: (200, "<html>"))[0]["kind"], "gate_unhealthy")

    def test_policy_drift_after_grace(self):
        found = self.check(lambda u: (200, '{"status":"ok","policyRevision":"v2.11"}'))
        self.assertEqual(found[0]["kind"], "gate_policy_drift")
        self.assertIn("'v2.11'", found[0]["message"])

    def test_policy_drift_inside_grace_is_quiet(self):
        found = self.check(lambda u: (200, '{"status":"ok","policyRevision":"v2.11"}'), policy_routes(changed_hours_ago=2))
        self.assertEqual(found, [])

    def test_transient_unhealthy_recovers_on_retry(self):
        answers = iter([(502, "x"), (200, '{"status":"ok","policyRevision":"v2.12"}')])
        self.assertEqual(self.check(lambda u: next(answers)), [])

    def test_policy_read_error_raises(self):
        routes = policy_routes()
        routes[f"repos/{ORG}/pr-bot/contents/gate/policy.json?ref=main"] = "__error"
        with self.assertRaises(mh.ReadError):
            self.check(lambda u: (200, '{"status":"ok","policyRevision":"v2.12"}'), routes)


CODEX = {"check_name": "agency-delivery/gate", "app_slug": "agency-delivery-gate", "code_prefix": "CODEX_",
         "max_age_hours": 6, "alert_missing_gate_check": True, "exclude_repos": ["rmbc"],
         "ignore_labels": ["hold"], "owner": "Engineer"}


def gate_run(id_, conclusion, summary, started_hours_ago=0.1):
    return {"id": id_, "name": "agency-delivery/gate", "app": {"slug": "agency-delivery-gate"},
            "status": "completed", "conclusion": conclusion, "started_at": ts(started_hours_ago),
            "html_url": f"https://github.com/x/runs/{id_}", "output": {"summary": summary}}


def ci_run(started_hours_ago):
    return {"id": 1, "name": "test", "app": {"slug": "github-actions"}, "status": "completed",
            "conclusion": "success", "started_at": ts(started_hours_ago)}


def pr_routes(items, prs, total=None):
    q = mh.quote(f"org:{ORG} is:pr is:open archived:false", safe="")
    routes = {f"search/issues?q={q}&per_page=100": [{"total_count": len(items) if total is None else total,
                                                    "incomplete_results": False, "items": items}]}
    for (repo, n), (sha, runs, draft) in prs.items():
        routes[f"repos/{ORG}/{repo}/pulls/{n}"] = {"head": {"sha": sha}, "created_at": ts(100), "draft": draft}
        routes[f"repos/{ORG}/{repo}/commits/{sha}/check-runs?per_page=100"] = [{"check_runs": runs}]
    return routes


def item(repo, n, draft=False, labels=()):
    return {"repository_url": f"https://api.github.com/repos/{ORG}/{repo}", "number": n, "draft": draft,
            "labels": [{"name": l} for l in labels]}


MISSING = "- `CI_MISSING`: x\n- `CODEX_REVIEW_MISSING`: No completed Codex review exists"


class CodexBlockers(unittest.TestCase):
    def check(self, routes, cfg=CODEX):
        found, errors = mh.check_codex_blockers(FakeRead(routes), ORG, cfg, NOW)
        self.assertEqual(errors, [])
        return found

    def test_one_unreadable_pr_does_not_stop_the_scan(self):
        routes = pr_routes([item("app", 4), item("app", 5)],
                           {("app", 5): ("a" * 40, [ci_run(8), gate_run(9, "failure", MISSING)], False)})
        routes[f"repos/{ORG}/app/pulls/4"] = "__error"
        found, errors = mh.check_codex_blockers(FakeRead(routes), ORG, CODEX, NOW)
        self.assertEqual([f["subject"] for f in found], ["app#5"])
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("app#4: "))

    def test_aged_codex_blocker(self):
        routes = pr_routes([item("app", 5)], {("app", 5): ("a" * 40, [ci_run(8), gate_run(9, "failure", MISSING)], False)})
        found = self.check(routes)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["kind"], "codex_blocker_aged")
        self.assertIn("CODEX_REVIEW_MISSING", found[0]["message"])
        self.assertNotIn("CI_MISSING", found[0]["message"])
        self.assertEqual(found[0]["subject"], "app#5")

    def test_young_blocker_is_quiet(self):
        routes = pr_routes([item("app", 5)], {("app", 5): ("a" * 40, [ci_run(2), gate_run(9, "failure", MISSING)], False)})
        self.assertEqual(self.check(routes), [])

    def test_non_codex_blocker_is_quiet(self):
        runs = [ci_run(10), gate_run(9, "failure", "- `CI_NOT_SUCCESSFUL`: x")]
        self.assertEqual(self.check(pr_routes([item("app", 5)], {("app", 5): ("a" * 40, runs, False)})), [])

    def test_passing_gate_is_quiet(self):
        runs = [ci_run(10), gate_run(9, "success", "No policy blockers for this candidate.")]
        self.assertEqual(self.check(pr_routes([item("app", 5)], {("app", 5): ("a" * 40, runs, False)})), [])

    def test_newest_gate_run_wins(self):
        runs = [ci_run(10), gate_run(9, "failure", MISSING), gate_run(12, "success", "No policy blockers")]
        self.assertEqual(self.check(pr_routes([item("app", 5)], {("app", 5): ("a" * 40, runs, False)})), [])

    def test_other_apps_gate_named_check_ignored(self):
        fake = gate_run(20, "success", "ok")
        fake["app"] = {"slug": "someone-else"}
        runs = [ci_run(10), gate_run(9, "failure", MISSING), fake]
        self.assertEqual(len(self.check(pr_routes([item("app", 5)], {("app", 5): ("a" * 40, runs, False)}))), 1)

    def test_missing_gate_check_aged(self):
        found = self.check(pr_routes([item("app", 5)], {("app", 5): ("a" * 40, [ci_run(10)], False)}))
        self.assertEqual(found[0]["kind"], "gate_check_missing")

    def test_missing_gate_check_can_be_disabled(self):
        cfg = dict(CODEX, alert_missing_gate_check=False)
        self.assertEqual(self.check(pr_routes([item("app", 5)], {("app", 5): ("a" * 40, [ci_run(10)], False)}), cfg), [])

    def test_drafts_excluded_repos_and_hold_labels_skipped(self):
        items = [item("app", 1, draft=True), item("rmbc", 2), item("app", 3, labels=["Hold"])]
        self.assertEqual(self.check(pr_routes(items, {})), [])

    def test_pull_reported_draft_skipped(self):
        routes = pr_routes([item("app", 5)], {("app", 5): ("a" * 40, [ci_run(10), gate_run(9, "failure", MISSING)], True)})
        self.assertEqual(self.check(routes), [])

    def test_key_changes_with_head(self):
        r1 = self.check(pr_routes([item("app", 5)], {("app", 5): ("a" * 40, [ci_run(8), gate_run(9, "failure", MISSING)], False)}))
        r2 = self.check(pr_routes([item("app", 5)], {("app", 5): ("b" * 40, [ci_run(8), gate_run(9, "failure", MISSING)], False)}))
        self.assertNotEqual(r1[0]["key"], r2[0]["key"])

    def test_incomplete_search_is_read_error(self):
        routes = pr_routes([item("app", 5)], {})
        key = next(k for k in routes if k.startswith("search/"))
        routes[key][0]["incomplete_results"] = True
        with self.assertRaises(mh.ReadError):
            self.check(routes)

    def test_truncated_search_is_read_error(self):
        with self.assertRaises(mh.ReadError):
            self.check(pr_routes([item("app", 5)], {}, total=2))

    def test_foreign_repository_url_is_read_error(self):
        bad = item("app", 5)
        bad["repository_url"] = "https://api.github.com/repos/other/app"
        with self.assertRaises(mh.ReadError):
            self.check(pr_routes([bad], {}))


class Planning(unittest.TestCase):
    F1 = mh.finding("last_run_failed", "r/a.yml", "failed")
    F2 = mh.finding("workflow_not_active", "r/b.yml", "disabled")

    def issue(self, findings):
        _, body = mh.render_issue(findings, NOW, "run", True)
        return {"number": 7, "body": body, "html_url": "https://github.com/acme/.github/issues/7",
                "user": {"login": mh.BOT_LOGIN}}

    def test_create_when_problems_and_no_issue(self):
        p = mh.plan([self.F1], None)
        self.assertEqual((p["action"], p["new"]), ("create", [self.F1["key"]]))

    def test_none_when_clean_and_no_issue(self):
        self.assertEqual(mh.plan([], None)["action"], "none")

    def test_refresh_when_same_problems(self):
        self.assertEqual(mh.plan([self.F1], self.issue([self.F1]))["action"], "refresh")

    def test_update_with_diff(self):
        p = mh.plan([self.F2], self.issue([self.F1]))
        self.assertEqual((p["action"], p["new"], p["resolved"]), ("update", [self.F2["key"]], [self.F1["key"]]))

    def test_close_when_all_clear(self):
        p = mh.plan([], self.issue([self.F1]))
        self.assertEqual((p["action"], p["resolved"]), ("close", [self.F1["key"]]))

    def test_issue_without_state_marker_is_updated(self):
        p = mh.plan([self.F1], {"number": 1, "body": "edited by someone"})
        self.assertEqual(p["action"], "update")

    def test_issue_body_mentions_missing_slack(self):
        _, body = mh.render_issue([self.F1], NOW, "run", False)
        self.assertIn("SLACK_DEVOPS_WEBHOOK_URL", body)
        _, body = mh.render_issue([self.F1], NOW, "run", True)
        self.assertNotIn("SLACK_DEVOPS_WEBHOOK_URL", body)

    def test_slack_text_truncates(self):
        many = [mh.finding("last_run_failed", f"r/{i}.yml", "x" * 200) for i in range(40)]
        text = mh.slack_text(mh.plan(many, None), many, "issue", "run")
        self.assertLessEqual(len(text), mh.SLACK_TEXT_LIMIT + 40)
        self.assertIn("truncated", text)


class FakeAlerts:
    def __init__(self, issues=(), labels=("monitor-health",)):
        self.issues = list(issues)
        self.labels = set(labels)
        self.writes = []
        self.posts = []

    def read(self, endpoint, paginate=False):
        if "/labels/" in endpoint:
            name = mh.urllib.parse.unquote(endpoint.rsplit("/", 1)[1])
            if name not in self.labels:
                raise mh.NotFound("404")
            return {"name": name}
        if "/issues?" in endpoint:
            label = endpoint.split("labels=", 1)[1].split("&", 1)[0]
            return [[i for i in self.issues if label in i.get("labels", []) and i.get("state") == "open"]]
        raise AssertionError(endpoint)

    def write(self, method, endpoint, payload=None):
        self.writes.append((method, endpoint, payload))
        if method == "POST" and endpoint.endswith("/issues"):
            issue = {"number": 100 + len(self.issues), "html_url": "https://github.com/acme/.github/issues/new",
                     "user": {"login": mh.BOT_LOGIN}, "body": payload["body"], "labels": payload["labels"],
                     "state": "open"}
            self.issues.append(issue)
            return issue
        return {}

    def post(self, url, text):
        self.posts.append((url, text))


CONFIG = {"org": ORG, "workflows": [entry("r", "a.yml")]}


class Main(unittest.TestCase):
    def run_main(self, routes, alerts, env=None, get=None):
        out = []
        env = dict({"ALERT_REPO": "acme/.github", "RUN_URL": "https://run"}, **(env or {}))
        cfg = pathlib.Path(self.tmp) / "w.json"
        cfg.write_text(json.dumps(CONFIG))
        rc = mh.main(["--config", str(cfg)], read=FakeRead(routes), read_alert=alerts.read, write=alerts.write,
                     post=alerts.post, get=get or (lambda u: (200, "{}")), now=NOW, sleep=no_sleep, env=env,
                     out=out.append)
        return rc, out

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def test_problem_creates_issue_and_posts_slack(self):
        alerts = FakeAlerts()
        rc, out = self.run_main(wf_routes("r", "a.yml", runs=[run(1, 2, "failure")]), alerts,
                                {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/x"})
        self.assertEqual(rc, 0)
        self.assertEqual([w[0] for w in alerts.writes], ["POST"])
        self.assertEqual(len(alerts.posts), 1)
        self.assertIn("new problems", alerts.posts[0][1])

    def test_creates_label_when_missing(self):
        alerts = FakeAlerts(labels=())
        self.run_main(wf_routes("r", "a.yml", runs=[run(1, 2, "failure")]), alerts)
        self.assertTrue(alerts.writes[0][1].endswith("/labels"))

    def test_no_slack_warns_but_succeeds(self):
        alerts = FakeAlerts()
        rc, out = self.run_main(wf_routes("r", "a.yml", runs=[run(1, 2, "failure")]), alerts)
        self.assertEqual(rc, 0)
        self.assertEqual(alerts.posts, [])
        self.assertTrue(any("SLACK_DEVOPS_WEBHOOK_URL is not set" in o for o in out))

    def test_same_problems_next_hour_is_quiet(self):
        alerts = FakeAlerts()
        routes = wf_routes("r", "a.yml", runs=[run(1, 2, "failure")])
        self.run_main(routes, alerts, {"SLACK_WEBHOOK_URL": "u"})
        alerts.writes.clear(); alerts.posts.clear()
        rc, _ = self.run_main(routes, alerts, {"SLACK_WEBHOOK_URL": "u"})
        self.assertEqual(rc, 0)
        self.assertEqual(alerts.posts, [])
        self.assertEqual([w[0] for w in alerts.writes], ["PATCH"])

    def test_recovery_closes_issue(self):
        alerts = FakeAlerts()
        self.run_main(wf_routes("r", "a.yml", runs=[run(1, 2, "failure")]), alerts)
        alerts.writes.clear()
        rc, _ = self.run_main(wf_routes("r", "a.yml", runs=[run(2, 1), run(1, 2, "failure")]), alerts,
                              {"SLACK_WEBHOOK_URL": "u"})
        self.assertEqual(rc, 0)
        self.assertEqual(alerts.writes[-1][2], {"state": "closed", "state_reason": "completed"})
        self.assertIn("all clear", alerts.posts[-1][1])

    def test_issue_by_non_bot_is_ignored(self):
        _, body = mh.render_issue([mh.finding("last_run_failed", "r/a.yml", "x")], NOW, "run", True)
        spoof = {"number": 1, "body": body, "labels": ["monitor-health"], "state": "open", "user": {"login": "mallory"}}
        alerts = FakeAlerts(issues=[spoof])
        self.run_main(wf_routes("r", "a.yml", runs=[run(1, 2, "failure")]), alerts)
        self.assertTrue(alerts.writes[0][1].endswith("/issues"))  # a new bot issue, not the spoof

    def test_dry_run_writes_nothing(self):
        alerts = FakeAlerts()
        rc, _ = self.run_main(wf_routes("r", "a.yml", runs=[run(1, 2, "failure")]), alerts,
                              {"DRY_RUN": "true", "HEARTBEAT": "true", "SLACK_WEBHOOK_URL": "u"})
        self.assertEqual((rc, alerts.writes, alerts.posts), (0, [], []))

    def test_read_error_is_reported_and_fails_run(self):
        alerts = FakeAlerts()
        routes = wf_routes("r", "a.yml")
        routes[f"repos/{ORG}/r/actions/workflows/a.yml/runs?event=schedule&per_page=20"] = "__error"
        rc, out = self.run_main(routes, alerts, {"SLACK_WEBHOOK_URL": "u"})
        self.assertEqual(rc, 1)
        self.assertIn("read_error", alerts.writes[0][2]["body"])
        self.assertEqual(len(alerts.posts), 1)
        self.assertTrue(any(o.startswith("::error::read failed") for o in out))

    def test_dead_slack_fails_run_after_issue(self):
        alerts = FakeAlerts()

        def post(url, text):
            raise mh.AlertError("Slack answered HTTP 404 'no_service', not 'ok'")
        alerts.post = post
        rc, out = self.run_main(wf_routes("r", "a.yml", runs=[run(1, 2, "failure")]), alerts, {"SLACK_WEBHOOK_URL": "u"})
        self.assertEqual(rc, 1)
        self.assertEqual(len(alerts.issues), 1)
        self.assertTrue(any("alert delivery failed" in o for o in out))

    def test_test_dead_slack_uses_dead_url(self):
        alerts = FakeAlerts()
        self.run_main(wf_routes("r", "a.yml", runs=[run(1, 2, "failure")]), alerts,
                      {"TEST_DEAD_SLACK": "true", "SLACK_WEBHOOK_URL": "real"})
        self.assertEqual(alerts.posts[0][0], mh.DEAD_SLACK_URL)

    def test_test_dead_slack_fails_even_with_unchanged_findings(self):
        alerts = FakeAlerts()
        routes = wf_routes("r", "a.yml", runs=[run(1, 2, "failure")])
        self.run_main(routes, alerts)

        def post(url, text):
            raise mh.AlertError("Slack answered HTTP 404 'no_service', not 'ok'")
        alerts.post = post
        rc, out = self.run_main(routes, alerts, {"TEST_DEAD_SLACK": "true"})
        self.assertEqual(rc, 1)
        self.assertEqual([w[0] for w in alerts.writes][-1], "PATCH")  # issue still refreshed

    def test_heartbeat_to_slack(self):
        alerts = FakeAlerts()
        rc, _ = self.run_main(wf_routes("r", "a.yml", runs=[run(1, 2)]), alerts,
                              {"HEARTBEAT": "true", "SLACK_WEBHOOK_URL": "u"})
        self.assertEqual(rc, 0)
        self.assertEqual(len(alerts.posts), 1)
        self.assertIn("weekly heartbeat", alerts.posts[0][1])

    def test_heartbeat_falls_back_to_issue_comment(self):
        alerts = FakeAlerts(labels=())
        rc, _ = self.run_main(wf_routes("r", "a.yml", runs=[run(1, 2)]), alerts, {"HEARTBEAT": "true"})
        self.assertEqual(rc, 0)
        self.assertEqual(alerts.writes[-1][0], "POST")
        self.assertTrue(alerts.writes[-1][1].endswith("/comments"))
        self.assertIn("weekly heartbeat", alerts.writes[-1][2]["body"])

    def test_missing_token_fails(self):
        rc = mh.main(["--config", str(ROOT / "monitor-health" / "watchlist.json")], env={}, out=lambda s: None)
        self.assertEqual(rc, 1)


class SlackPost(unittest.TestCase):
    def fake(self, status, body):
        class Resp:
            def __init__(s):
                s.status = status
            def read(s):
                return body.encode()
            def __enter__(s):
                return s
            def __exit__(s, *a):
                return False
        return lambda req, timeout: Resp()

    def test_ok(self):
        orig = mh.urllib.request.urlopen
        mh.urllib.request.urlopen = self.fake(200, "ok")
        try:
            mh.slack_post("https://hooks.slack.com/x", "hi")
        finally:
            mh.urllib.request.urlopen = orig

    def test_not_ok_raises_without_url(self):
        orig = mh.urllib.request.urlopen
        mh.urllib.request.urlopen = self.fake(200, "invalid_payload")
        try:
            with self.assertRaises(mh.AlertError) as cm:
                mh.slack_post("https://hooks.slack.com/services/SECRET", "hi")
            self.assertNotIn("SECRET", str(cm.exception))
        finally:
            mh.urllib.request.urlopen = orig


class GhReader(unittest.TestCase):
    def fake_run(self, results):
        calls = []

        def run(args, **kw):
            calls.append(args)
            rc, out, err = results[min(len(calls) - 1, len(results) - 1)]
            return mh.subprocess.CompletedProcess(args, rc, out, err)
        return run, calls

    def with_run(self, results, fn):
        orig = mh.subprocess.run
        run, calls = self.fake_run(results)
        mh.subprocess.run = run
        try:
            return fn(), calls
        finally:
            mh.subprocess.run = orig

    def test_retries_transient_then_succeeds(self):
        read = mh.gh_reader("t", sleep=lambda s: None)
        value, calls = self.with_run([(1, "", "gh: HTTP 500"), (0, '{"ok": 1}', "")], lambda: read("x"))
        self.assertEqual((value, len(calls)), ({"ok": 1}, 2))

    def test_gives_up_after_attempts(self):
        read = mh.gh_reader("t", sleep=lambda s: None)
        with self.assertRaises(mh.ReadError):
            self.with_run([(1, "", "gh: HTTP 502")], lambda: read("x"))

    def test_404_is_final(self):
        read = mh.gh_reader("t", sleep=lambda s: None)
        orig = mh.subprocess.run
        run, calls = self.fake_run([(1, "", "gh: Not Found (HTTP 404)")])
        mh.subprocess.run = run
        try:
            with self.assertRaises(mh.NotFound):
                read("x")
        finally:
            mh.subprocess.run = orig
        self.assertEqual(len(calls), 1)

    def test_get_only(self):
        read = mh.gh_reader("t", sleep=lambda s: None)
        _, calls = self.with_run([(0, "{}", "")], lambda: read("x", paginate=True))
        self.assertEqual(calls[0][:4], ["gh", "api", "--method", "GET"])
        self.assertIn("--slurp", calls[0])


class WatchList(unittest.TestCase):
    def test_watchlist_shape(self):
        cfg = json.loads((ROOT / "monitor-health" / "watchlist.json").read_text())
        seen = set()
        for e in cfg["workflows"]:
            self.assertTrue({"repo", "workflow", "window_hours", "min_runs_24h", "owner", "why"} <= set(e), e)
            self.assertTrue(e["workflow"].endswith(".yml"))
            self.assertGreater(e["window_hours"], 0)
            self.assertGreaterEqual(e["min_runs_24h"], 0)
            key = (e["repo"], e["workflow"])
            self.assertNotIn(key, seen)
            seen.add(key)
        self.assertIn((".github", "doc-auto-merge-sweep.yml"), seen)
        self.assertIn(("aaa-client-dashboard", "sync-linear-data.yml"), seen)
        self.assertNotIn((".github", "monitor-health.yml"), seen)  # cross-watched by the dashboard watchdog

    def test_local_workflows_in_watchlist_exist(self):
        cfg = json.loads((ROOT / "monitor-health" / "watchlist.json").read_text())
        for e in cfg["workflows"]:
            if e["repo"] == ".github":
                self.assertTrue((ROOT / ".github" / "workflows" / e["workflow"]).exists(), e["workflow"])


if __name__ == "__main__":
    unittest.main()
