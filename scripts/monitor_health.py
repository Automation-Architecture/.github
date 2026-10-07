#!/usr/bin/env python3
"""Monitor Health reconciler (GAAA-3937).

Watches the org's scheduled monitors and alerts when one is failed, missing or
under-running, when the delivery gate is unhealthy or serves a stale policy,
and when an open PR has been held by CODEX_* gate blockers for too long.

For each workflow in the watch list (monitor-health/watchlist.json) it asserts:
  1. workflow state is `active`
  2. the newest SCHEDULED run is younger than `window_hours`
  3. the newest completed scheduled run concluded `success`
  4. scheduled runs created in the last 24h >= `min_runs_24h` (when > 0)

Findings go to one rolling GitHub issue (label `monitor-health`) in ALERT_REPO
and, when SLACK_WEBHOOK_URL is set, to Slack (#dev-ops) whenever the set of
findings changes. A weekly heartbeat says the reconciler is alive.

Exit status: 0 when the reconciler did its job (whether or not it found
problems; those are reported through the alert channels), 1 when it could not
do its job: a read failed, or an alert could not be delivered. Read errors are
also reported as findings before exiting 1, never swallowed.

Docs: docs/monitor-health.md. Tests: tests/monitor-health/.
"""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlparse

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "monitor-health" / "watchlist.json"
LABEL = "monitor-health"
HEARTBEAT_LABEL = "monitor-health-heartbeat"
BOT_LOGIN = "github-actions[bot]"
STATE_RE = re.compile(r"<!-- monitor-health-state (\{.*?\}) -->", re.S)
BLOCKER_RE = re.compile(r"^- `([A-Z0-9_]+)`", re.M)
DEAD_SLACK_URL = "https://hooks.slack.com/services/T00000000/B00000000/monitor-health-dead-channel-test"
SLACK_TEXT_LIMIT = 2900


class ReadError(Exception):
    """A read the reconciler depends on failed. Never treated as 'healthy'."""


class NotFound(ReadError):
    pass


class AlertError(Exception):
    """An alert could not be delivered."""


# --------------------------------------------------------------------------
# I/O adapters (replaced by fakes in tests)
# --------------------------------------------------------------------------

def gh_reader(token, attempts=4, sleep=time.sleep):
    """GET-only GitHub API reader through the gh CLI. Transient failures
    (5xx, timeouts; one HTTP 500 on a check-runs read was seen live) are
    retried with backoff; a 404 is final; the last failure is raised."""
    def once(endpoint, paginate):
        args = ["gh", "api", "--method", "GET", endpoint]
        if paginate:
            args += ["--paginate", "--slurp"]
        env = dict(os.environ, GH_TOKEN=token) if token else None
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=90, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReadError(f"GET {endpoint}: {type(exc).__name__}") from exc
        if r.returncode:
            if "HTTP 404" in r.stderr:
                raise NotFound(f"GET {endpoint}: HTTP 404")
            status = re.search(r"HTTP \d{3}", r.stderr)
            raise ReadError(f"GET {endpoint}: {status.group(0) if status else 'gh exited ' + str(r.returncode)}")
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError as exc:
            raise ReadError(f"GET {endpoint}: malformed JSON") from exc

    def read(endpoint, paginate=False):
        for attempt in range(attempts):
            try:
                return once(endpoint, paginate)
            except NotFound:
                raise
            except ReadError:
                if attempt == attempts - 1:
                    raise
                sleep(2 ** (attempt + 1))
    return read


def gh_writer(token):
    """Write adapter for the alert repository (issues, labels, comments)."""
    def write(method, endpoint, payload=None):
        args = ["gh", "api", "--method", method, endpoint, "--input", "-"]
        env = dict(os.environ, GH_TOKEN=token)
        try:
            r = subprocess.run(args, input=json.dumps(payload or {}), capture_output=True,
                               text=True, timeout=90, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AlertError(f"{method} {endpoint}: {type(exc).__name__}") from exc
        if r.returncode:
            status = re.search(r"HTTP \d{3}", r.stderr)
            raise AlertError(f"{method} {endpoint}: {status.group(0) if status else 'gh exited ' + str(r.returncode)}")
        return json.loads(r.stdout) if r.stdout.strip() else {}
    return write


def http_get(url, timeout=20):
    """Returns (status, body). Network failures raise ReadError."""
    req = urllib.request.Request(url, headers={"User-Agent": "monitor-health"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        raise ReadError(f"GET {url}: {type(exc).__name__}") from exc


def slack_post(url, text, timeout=20):
    """Posts to an incoming webhook. Delivery = HTTP 200 with body `ok`."""
    data = json.dumps({"text": text}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace").strip()
            status = r.status
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read().decode("utf-8", "replace").strip()
    except (urllib.error.URLError, OSError) as exc:
        raise AlertError(f"Slack post failed: {type(exc).__name__}") from exc
    if status != 200 or body != "ok":
        # Body only: the webhook URL itself is a secret and is never printed.
        raise AlertError(f"Slack answered HTTP {status} {body[:60]!r}, not 'ok'")


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------

def parse_ts(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hours(delta):
    return delta.total_seconds() / 3600


def finding(kind, subject, message, owner="Engineer", url=None, key=None):
    return {"key": key or f"{kind}:{subject}", "kind": kind, "subject": subject,
            "message": message, "owner": owner, "url": url}


# --------------------------------------------------------------------------
# Workflows
# --------------------------------------------------------------------------

def observe_workflow(read, org, entry, now):
    base = f"repos/{org}/{entry['repo']}/actions/workflows/{entry['workflow']}"
    try:
        meta = read(base)
    except NotFound:
        return {"missing": True}
    page = read(f"{base}/runs?event=schedule&per_page=20")
    runs = page.get("workflow_runs") if isinstance(page, dict) else None
    if not isinstance(meta, dict) or "state" not in meta or not isinstance(runs, list):
        raise ReadError(f"GET {base}: malformed workflow or runs response")
    stamps = [parse_ts(meta[k]) for k in ("created_at", "updated_at") if meta.get(k)]
    obs = {"missing": False, "state": meta["state"], "since": iso(max(stamps)) if stamps else None,
           "runs": [{k: r.get(k) for k in ("id", "created_at", "status", "conclusion", "html_url")} for r in runs],
           "count_24h": None}
    if entry.get("min_runs_24h", 0) > 0:
        since = quote(">=" + iso(now - dt.timedelta(hours=24)), safe="")
        counted = read(f"{base}/runs?event=schedule&created={since}&per_page=1")
        if not isinstance(counted, dict) or type(counted.get("total_count")) is not int:
            raise ReadError(f"GET {base}/runs: malformed count response")
        obs["count_24h"] = counted["total_count"]
    return obs


def merge_observations(a, b):
    """The runs API is served from replicas that can lag by days (on
    2026-10-07 about 1 read in 25-60 came back ~3 days stale). A lagging read
    can only hide runs, never invent them, so keep the union of runs and the
    highest count; take the state from the newest read."""
    if a.get("missing") or b.get("missing"):
        return b
    runs = {r["id"]: r for r in a["runs"]}
    runs.update({r["id"]: r for r in b["runs"]})
    counts = [c for c in (a["count_24h"], b["count_24h"]) if c is not None]
    return {"missing": False, "state": b["state"], "since": b.get("since"),
            "runs": sorted(runs.values(), key=lambda r: r["created_at"], reverse=True),
            "count_24h": max(counts) if counts else None}


def evaluate_workflow(entry, obs, now):
    subject = f"{entry['repo']}/{entry['workflow']}"
    owner = entry.get("owner", "Engineer")
    if obs.get("missing"):
        return [finding("workflow_missing", subject, "workflow file not found (renamed or deleted?)", owner)]
    if obs["state"] != "active":
        return [finding("workflow_not_active", subject,
                        f"workflow state is `{obs['state']}`, expected `active`: its schedule is not running", owner)]
    # Bootstrap: the workflow's created_at/updated_at moves when it is added,
    # enabled/disabled or its file changes. Inside one window of that, a
    # missing or old run says nothing yet; inside 24h the count is partial.
    fresh = hours(now - parse_ts(obs["since"])) if obs.get("since") else None
    in_window = fresh is not None and fresh < entry["window_hours"]
    runs = sorted(obs["runs"], key=lambda r: r["created_at"], reverse=True)
    if not runs:
        return [] if in_window else [finding("no_scheduled_run", subject, "no scheduled run exists", owner)]
    out = []
    newest = runs[0]
    age = hours(now - parse_ts(newest["created_at"]))
    if age > entry["window_hours"] and not in_window:
        out.append(finding("scheduled_run_late", subject,
                           f"newest scheduled run is {age:.1f}h old (window {entry['window_hours']}h): runs are missing",
                           owner, newest.get("html_url")))
    completed = [r for r in runs if r.get("status") == "completed"]
    if completed and completed[0].get("conclusion") != "success":
        last = completed[0]
        out.append(finding("last_run_failed", subject,
                           f"newest completed scheduled run ({hours(now - parse_ts(last['created_at'])):.1f}h ago) "
                           f"concluded `{last.get('conclusion')}`", owner, last.get("html_url")))
    floor = entry.get("min_runs_24h", 0)
    if floor > 0 and obs["count_24h"] is not None and obs["count_24h"] < floor and not (fresh is not None and fresh < 24):
        out.append(finding("under_running", subject,
                           f"{obs['count_24h']} scheduled runs in the last 24h (floor {floor})", owner))
    return out


def check_workflow(read, org, entry, now, attempts=3, delay=5.0, sleep=time.sleep):
    obs = observe_workflow(read, org, entry, now)
    found = evaluate_workflow(entry, obs, now)
    tries = 1
    while found and tries < attempts and not obs.get("missing"):
        sleep(delay)
        obs = merge_observations(obs, observe_workflow(read, org, entry, now))
        found = evaluate_workflow(entry, obs, now)
        tries += 1
    return found


# --------------------------------------------------------------------------
# Gate /health
# --------------------------------------------------------------------------

def observe_gate(read, get, org, cfg):
    obs = {}
    try:
        obs["status"], obs["body"] = get(cfg["url"])
    except ReadError as exc:
        obs["fetch_error"] = str(exc)
    content = read(f"repos/{org}/{cfg['policy_repo']}/contents/{cfg['policy_path']}?ref={cfg['policy_ref']}")
    try:
        policy = json.loads(base64.b64decode(content["content"]))
        obs["expected_revision"] = policy["revision"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ReadError("gate policy file: malformed") from exc
    commits = read(f"repos/{org}/{cfg['policy_repo']}/commits?path={quote(cfg['policy_path'], safe='')}"
                   f"&sha={cfg['policy_ref']}&per_page=1")
    try:
        obs["policy_changed_at"] = commits[0]["commit"]["committer"]["date"]
    except (KeyError, TypeError, IndexError) as exc:
        raise ReadError("gate policy history: malformed or empty") from exc
    return obs


def evaluate_gate(cfg, obs, now):
    subject = cfg["name"]
    owner = cfg.get("owner", "Engineer")
    if "fetch_error" in obs:
        return [finding("gate_unhealthy", subject, f"/health unreachable ({obs['fetch_error']})", owner)]
    if obs["status"] != 200:
        return [finding("gate_unhealthy", subject, f"/health answered HTTP {obs['status']}", owner)]
    try:
        health = json.loads(obs["body"])
    except ValueError:
        return [finding("gate_unhealthy", subject, "/health body is not JSON", owner)]
    if not isinstance(health, dict) or health.get("status") != cfg["expect_status"]:
        status = health.get("status") if isinstance(health, dict) else None
        return [finding("gate_unhealthy", subject, f"/health status is {status!r}, expected {cfg['expect_status']!r}", owner)]
    served, expected = health.get("policyRevision"), obs["expected_revision"]
    if served != expected:
        since = hours(now - parse_ts(obs["policy_changed_at"]))
        if since >= cfg["deploy_grace_hours"]:
            return [finding("gate_policy_drift", subject,
                            f"gate serves policy {served!r} but {cfg['policy_repo']}@{cfg['policy_ref']} has {expected!r} "
                            f"(changed {since:.1f}h ago, grace {cfg['deploy_grace_hours']}h): deploy or roll back", owner)]
    return []


def check_gate(read, get, org, cfg, now, attempts=2, delay=10.0, sleep=time.sleep):
    found = evaluate_gate(cfg, observe_gate(read, get, org, cfg), now)
    tries = 1
    while found and tries < attempts:
        sleep(delay)
        found = evaluate_gate(cfg, observe_gate(read, get, org, cfg), now)
        tries += 1
    return found


# --------------------------------------------------------------------------
# Aged CODEX_* gate blockers on open PRs
# --------------------------------------------------------------------------

def list_open_prs(read, org):
    """All open, non-archived PRs in the org via the search API, with the
    same completeness checks as rollout_monitor.py."""
    q = quote(f"org:{org} is:pr is:open archived:false", safe="")
    pages = read(f"search/issues?q={q}&per_page=100", paginate=True)
    if not isinstance(pages, list) or not pages:
        raise ReadError("PR search returned no page set")
    items, totals = [], set()
    for page in pages:
        if (not isinstance(page, dict) or page.get("incomplete_results") is not False
                or type(page.get("total_count")) is not int or not isinstance(page.get("items"), list)):
            raise ReadError("PR search is malformed or incomplete")
        totals.add(page["total_count"])
        items.extend(page["items"])
    if len(totals) != 1 or len(items) != totals.pop() or len(items) > 1000:
        raise ReadError("PR search coverage changed between pages, was truncated, or exceeds 1000")
    prs = []
    for item in items:
        parts = urlparse(item.get("repository_url", "")).path.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "repos" or parts[1] != org:
            raise ReadError("PR search item has an unexpected repository_url")
        prs.append({"repo": parts[2], "number": item["number"], "draft": bool(item.get("draft")),
                    "labels": [l.get("name", "") for l in item.get("labels", [])]})
    return prs


def observe_pr(read, org, pr):
    pull = read(f"repos/{org}/{pr['repo']}/pulls/{pr['number']}")
    sha = pull["head"]["sha"]
    if pull.get("state") != "open":
        return {"sha": sha, "state": pull.get("state"), "created_at": pull["created_at"],
                "draft": bool(pull.get("draft")), "check_runs": []}
    pages = read(f"repos/{org}/{pr['repo']}/commits/{sha}/check-runs?per_page=100", paginate=True)
    runs = [r for p in pages for r in p.get("check_runs", [])]
    return {"sha": sha, "state": pull.get("state"), "created_at": pull["created_at"],
            "draft": bool(pull.get("draft")), "check_runs": runs}


def evaluate_pr(cfg, pr, obs, now):
    """Age anchor: the earliest check run started on the current head (CI
    starts within a minute of a push; GitHub exposes no push time), else the
    PR's creation. The gate PATCHes its check run in place, so the gate run's
    own timestamps say when it last evaluated, not since when it blocks."""
    subject = f"{pr['repo']}#{pr['number']}"
    owner = cfg.get("owner", "Engineer")
    starts = [parse_ts(r["started_at"]) for r in obs["check_runs"] if r.get("started_at")]
    anchor = min(starts) if starts else parse_ts(obs["created_at"])
    age = hours(now - anchor)
    if age < cfg["max_age_hours"]:
        return []
    gate = [r for r in obs["check_runs"]
            if r.get("name") == cfg["check_name"] and (r.get("app") or {}).get("slug") == cfg["app_slug"]]
    if not gate:
        if cfg.get("alert_missing_gate_check", True):
            return [finding("gate_check_missing", subject,
                            f"no `{cfg['check_name']}` check on head {obs['sha'][:7]} after {age:.1f}h: the gate did not evaluate",
                            owner)]
        return []
    newest = max(gate, key=lambda r: r["id"])
    if newest.get("status") == "completed" and newest.get("conclusion") == "success":
        return []
    codes = BLOCKER_RE.findall((newest.get("output") or {}).get("summary") or "")
    codex = sorted({c for c in codes if c.startswith(cfg["code_prefix"])})
    if not codex:
        return []
    return [finding("codex_blocker_aged", subject,
                    f"{', '.join(codex)} on head {obs['sha'][:7]} for {age:.1f}h (limit {cfg['max_age_hours']}h)",
                    owner, newest.get("html_url"), key=f"codex_blocker_aged:{subject}:{obs['sha'][:12]}")]


def check_codex_blockers(read, org, cfg, now):
    """Returns (findings, errors). One unreadable PR is recorded as an error
    and does not stop the scan of the others."""
    found, errors = [], []
    ignore = {l.lower() for l in cfg.get("ignore_labels", [])}
    for pr in list_open_prs(read, org):
        if pr["draft"] or pr["repo"] in cfg.get("exclude_repos", []):
            continue
        if ignore & {l.lower() for l in pr["labels"]}:
            continue
        try:
            obs = observe_pr(read, org, pr)
        except ReadError as exc:
            errors.append(f"{pr['repo']}#{pr['number']}: {exc}")
            continue
        # The search index lags: a PR closed minutes ago can still be listed.
        if obs["draft"] or obs["state"] != "open":
            continue
        found += evaluate_pr(cfg, pr, obs, now)
    return found, errors


# --------------------------------------------------------------------------
# Reconcile
# --------------------------------------------------------------------------

def reconcile(config, read, get, now, sleep=time.sleep, codex_max_age=None):
    """Returns (findings, read_errors)."""
    org = config["org"]
    findings, errors = [], []
    for entry in config["workflows"]:
        try:
            findings += check_workflow(read, org, entry, now, sleep=sleep)
        except ReadError as exc:
            errors.append(f"{entry['repo']}/{entry['workflow']}: {exc}")
    if config.get("gate_health"):
        try:
            findings += check_gate(read, get, org, config["gate_health"], now, sleep=sleep)
        except ReadError as exc:
            errors.append(f"{config['gate_health']['name']}: {exc}")
    if config.get("codex_blockers"):
        cfg = dict(config["codex_blockers"])
        if codex_max_age is not None:
            cfg["max_age_hours"] = codex_max_age
        try:
            pr_found, pr_errors = check_codex_blockers(read, org, cfg, now)
            findings += pr_found
            errors += pr_errors
        except ReadError as exc:
            errors.append(f"codex blockers: {exc}")
    if errors:
        # One finding for all read errors, so a burst of GitHub 5xx changes
        # the alert once instead of churning one key per unreadable object.
        shown = "; ".join(errors[:5]) + (f"; and {len(errors) - 5} more (see the run log)" if len(errors) > 5 else "")
        findings.append(finding("read_error", "reconciler",
                                f"{len(errors)} read(s) failed, so this report is incomplete: {shown}"))
    return findings, errors


def fingerprint(keys):
    return hashlib.sha256("\n".join(sorted(keys)).encode()).hexdigest()[:12]


def render_findings(findings):
    lines = []
    for f in sorted(findings, key=lambda f: (f["kind"], f["subject"])):
        link = f" ([run]({f['url']}))" if f.get("url") else ""
        lines.append(f"- **{f['subject']}**: {f['message']}{link} (owner: {f['owner']})")
    return "\n".join(lines)


def render_issue(findings, now, run_url, slack_configured, slack_pending=False):
    keys = sorted(f["key"] for f in findings)
    kept = [{k: f.get(k) for k in ("key", "kind", "subject", "message", "owner", "url")} for f in findings]
    state = json.dumps({"keys": keys, "fingerprint": fingerprint(keys), "findings": kept,
                        "slack_pending": slack_pending}, separators=(",", ":"))
    # Keep the marker a valid HTML comment whatever a message contains.
    state = state.replace(">", "\\u003e").replace("<", "\\u003c")
    note = "" if slack_configured else (
        "\n> Slack is not configured for this reconciler (`SLACK_DEVOPS_WEBHOOK_URL` repo secret in "
        "Automation-Architecture/.github is unset), so this issue is the only alert channel.\n")
    body = (f"Monitor Health found {len(findings)} problem(s) at {iso(now)}.\n{note}\n"
            f"{render_findings(findings)}\n\n"
            f"Reconciler run: {run_url}\n"
            "What each check means and how to act: docs/monitor-health.md. "
            "This issue updates when the set of problems changes and closes itself when all clear.\n\n"
            f"<!-- monitor-health-state {state} -->\n")
    return f"Monitor Health: {len(findings)} problem(s)", body


def issue_state(issue):
    m = STATE_RE.search(issue.get("body") or "") if issue else None
    if not m:
        return None
    try:
        state = json.loads(m.group(1))
        state["keys"] = set(state["keys"])
        return state
    except (ValueError, KeyError, TypeError):
        return None


def previous_keys(issue):
    state = issue_state(issue)
    return state["keys"] if state else None


def carry_forward(findings, issue):
    """While the scan is incomplete (read errors), a problem that was open
    before and was not seen this run is unknown, not resolved: keep it, so
    nothing announces a recovery that was never observed."""
    state = issue_state(issue) or {}
    seen = {f["key"] for f in findings}
    carried = []
    for f in state.get("findings", []):
        if f.get("key") and f["key"] not in seen:
            carried.append(dict(f, message=f"{f.get('message', '')} (not re-checked this run: reads failed)"))
    return findings + carried


def plan(findings, open_issue):
    """Decides the alert action from the current findings and the open issue."""
    keys = {f["key"] for f in findings}
    if not findings:
        return {"action": "close" if open_issue else "none", "new": [], "resolved": sorted(previous_keys(open_issue) or [])}
    if not open_issue:
        return {"action": "create", "new": sorted(keys), "resolved": []}
    prev = previous_keys(open_issue)
    if prev == keys:
        # Same problems: refresh the body (ages, run links) without a
        # comment or a Slack post, so nobody is paged hourly.
        return {"action": "refresh", "new": [], "resolved": []}
    prev = prev or set()
    return {"action": "update", "new": sorted(keys - prev), "resolved": sorted(prev - keys)}


def truncate(text, limit=SLACK_TEXT_LIMIT):
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit("\n", 1)[0]
    return cut + "\n...truncated, see the issue"


def slack_text(p, findings, issue_url, run_url):
    by_key = {f["key"]: f for f in findings}
    if p["action"] == "close":
        return f"Monitor Health: all clear, {len(p['resolved'])} problem(s) resolved. {issue_url} closed."
    head = {"create": "Monitor Health: new problems", "update": "Monitor Health: problems changed"}.get(
        p["action"], "Monitor Health: open problems (re-sent; an earlier post failed)")
    lines = [f"{head} ({len(findings)} open). Issue: {issue_url} Run: {run_url}"]
    if p["new"]:
        lines.append("New:")
        lines += [f"- {by_key[k]['subject']}: {by_key[k]['message']}" for k in p["new"]]
    elif p["action"] == "refresh":
        lines += [f"- {f['subject']}: {f['message']}" for f in findings]
    if p["resolved"]:
        lines.append("Resolved: " + ", ".join(k.split(":", 1)[1] if ":" in k else k for k in p["resolved"]))
    return truncate("\n".join(lines))


def find_bot_issue(read, repo, label):
    issues = read(f"repos/{repo}/issues?labels={label}&state=open&per_page=100", paginate=True)
    found = [i for page in issues for i in page
             if not i.get("pull_request") and (i.get("user") or {}).get("login") == BOT_LOGIN]
    # .github is public: anyone can open an issue with this label, so only
    # bot-authored issues carry state.
    return min(found, key=lambda i: i["number"]) if found else None


def ensure_label(read, write, repo, label, color, description):
    try:
        read(f"repos/{repo}/labels/{quote(label, safe='')}")
    except NotFound:
        write("POST", f"repos/{repo}/labels", {"name": label, "color": color, "description": description})


def post_with_retry(post, url, text, attempts=2, sleep=time.sleep):
    for attempt in range(attempts):
        try:
            return post(url, text)
        except AlertError:
            if attempt == attempts - 1:
                raise
            sleep(5)


def deliver(findings, *, read_alert, write, post, alert_repo, slack_url, run_url, now, dry_run, log,
            incomplete=False, sleep=time.sleep):
    """Applies the plan to the issue and Slack, each channel independently.
    Raises AlertError when either channel failed, after trying both.

    Slack state is persisted in the issue: a state change is first written
    with slack_pending=true and cleared only after Slack answered `ok`, so a
    failed post is re-sent by the next run instead of being lost."""
    def patch(number, payload):
        write("PATCH", f"repos/{alert_repo}/issues/{number}", payload)

    try:
        issue = find_bot_issue(read_alert, alert_repo, LABEL)
    except (ReadError, AlertError) as exc:
        # The issue channel is down. Slack does not depend on it: send what
        # this run found so the outage is not silent, then fail the run.
        log(f"alert plan: issue lookup failed ({exc}); Slack only")
        if slack_url and not dry_run:
            text = (f"Monitor Health: cannot read its alert issue in {alert_repo} ({exc}). "
                    f"{len(findings)} problem(s) this run. Run: {run_url}\n"
                    + "\n".join(f"- {f['subject']}: {f['message']}" for f in findings))
            post_with_retry(post, slack_url, truncate(text), sleep=sleep)
        raise AlertError(f"alert issue lookup failed: {exc}") from exc

    if incomplete:
        findings = carry_forward(findings, issue)
    p = plan(findings, issue)
    prior = issue_state(issue) or {}
    resend = bool(slack_url) and p["action"] == "refresh" and prior.get("slack_pending")
    needs_post = bool(slack_url) and (p["action"] in ("create", "update", "close") or resend)
    log(f"alert plan: {p['action']} (new {len(p['new'])}, resolved {len(p['resolved'])})"
        + (" + re-send pending Slack" if resend else ""))
    if p["action"] == "none" or dry_run:
        return p

    title, body = render_issue(findings, now, run_url, bool(slack_url), slack_pending=needs_post)
    if p["action"] == "close":
        # Post first: if Slack fails the issue stays open (marked pending),
        # so the next all-clear run tries the close and the post again.
        try:
            if needs_post:
                post_with_retry(post, slack_url, slack_text(p, findings, issue.get("html_url", ""), run_url), sleep=sleep)
        except AlertError:
            _, pending_body = render_issue(carry_forward([], issue), now, run_url, True, slack_pending=True)
            patch(issue["number"], {"body": pending_body})
            raise
        write("POST", f"repos/{alert_repo}/issues/{issue['number']}/comments",
              {"body": f"All clear at {iso(now)}: every watched monitor is healthy. Run: {run_url}"})
        patch(issue["number"], {"state": "closed", "state_reason": "completed"})
        return p

    issue_error = None
    try:
        if p["action"] == "create":
            ensure_label(read_alert, write, alert_repo, LABEL, "b60205", "Monitor Health reconciler findings (GAAA-3937)")
            issue = write("POST", f"repos/{alert_repo}/issues", {"title": title, "body": body, "labels": [LABEL]})
        else:
            patch(issue["number"], {"title": title, "body": body})
            if p["action"] == "update":
                write("POST", f"repos/{alert_repo}/issues/{issue['number']}/comments",
                      {"body": f"Problems changed at {iso(now)}.\n\nNew: {', '.join(p['new']) or 'none'}\n\n"
                               f"Resolved: {', '.join(p['resolved']) or 'none'}\n\nRun: {run_url}"})
    except (AlertError, ReadError) as exc:
        issue_error = exc
    if issue_error is not None:
        # The issue write failed: Slack does not depend on it, so send the
        # full findings there (no pending state can be recorded), then fail.
        log(f"issue write failed ({issue_error}); sending findings to Slack")
        if slack_url:
            text = (f"Monitor Health: could not write its alert issue in {alert_repo} ({issue_error}). "
                    f"{len(findings)} problem(s) this run. Run: {run_url}\n"
                    + "\n".join(f"- {f['subject']}: {f['message']}" for f in findings))
            post_with_retry(post, slack_url, truncate(text), sleep=sleep)
        raise AlertError(f"alert issue write failed: {issue_error}") from issue_error
    if needs_post:
        post_with_retry(post, slack_url, slack_text(p, findings, issue.get("html_url", ""), run_url), sleep=sleep)
        _, body = render_issue(findings, now, run_url, True, slack_pending=False)
        patch(issue["number"], {"body": body})
    return p


def heartbeat(config, findings, *, read_alert, write, post, alert_repo, slack_url, run_url, now, dry_run, log):
    watched = len(config["workflows"])
    try:
        issue = find_bot_issue(read_alert, alert_repo, LABEL)
    except ReadError:
        issue = None  # the link is optional; never let it block the heartbeat
    status = (f"{len(findings)} open problem(s): {issue['html_url']}" if findings and issue
              else f"{len(findings)} open problem(s)")
    text = (f"Monitor Health weekly heartbeat: alive, watching {watched} scheduled workflows, the gate's /health "
            f"and aged CODEX_* PR blockers. {status}. If this stops arriving on Mondays, the reconciler is down. "
            f"Run: {run_url}")
    log("heartbeat: " + text)
    if dry_run:
        return
    if slack_url:
        post(slack_url, text)
        return
    hb = find_bot_issue(read_alert, alert_repo, HEARTBEAT_LABEL)
    if not hb:
        ensure_label(read_alert, write, alert_repo, HEARTBEAT_LABEL, "0e8a16", "Monitor Health weekly heartbeat (GAAA-3937)")
        hb = write("POST", f"repos/{alert_repo}/issues",
                   {"title": "Monitor Health heartbeat", "labels": [HEARTBEAT_LABEL],
                    "body": "Weekly heartbeat comments from the Monitor Health reconciler while Slack is not configured. "
                            "Subscribe to this issue: a Monday without a comment means the reconciler is down."})
    write("POST", f"repos/{alert_repo}/issues/{hb['number']}/comments", {"body": text})


def main(argv=None, *, read=None, read_alert=None, write=None, get=http_get, post=slack_post,
         now=None, sleep=time.sleep, env=None, out=print):
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = ap.parse_args(argv)
    config = json.loads(Path(args.config).read_text())
    now = now or dt.datetime.now(dt.timezone.utc)
    flag = lambda name: env.get(name, "").lower() == "true"
    dry_run, want_heartbeat = flag("DRY_RUN"), flag("HEARTBEAT")
    alert_repo = env.get("ALERT_REPO") or f"{config['org']}/.github"
    run_url = env.get("RUN_URL", "(local run)")
    slack_url = DEAD_SLACK_URL if flag("TEST_DEAD_SLACK") else env.get("SLACK_WEBHOOK_URL", "")
    codex_age = env.get("CODEX_MAX_AGE_HOURS", "").strip()
    if read is None:
        if not env.get("GH_TOKEN"):
            out("::error::GH_TOKEN (AAA_ORG_TOKEN) is not set: the reconciler cannot read the org")
            return 1
        read = gh_reader(env["GH_TOKEN"])
    if read_alert is None:
        read_alert = gh_reader(env.get("ALERT_TOKEN") or env["GH_TOKEN"])
    if write is None:
        write = gh_writer(env.get("ALERT_TOKEN") or env["GH_TOKEN"])

    findings, errors = reconcile(config, read, get, now, sleep=sleep,
                                 codex_max_age=float(codex_age) if codex_age else None)
    out(f"Monitor Health at {iso(now)}: {len(findings)} finding(s), {len(errors)} read error(s)")
    out(render_findings(findings) or "all watched monitors healthy")
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(f"## Monitor Health\n\n{len(findings)} finding(s), {len(errors)} read error(s)\n\n"
                     f"{render_findings(findings) or 'All watched monitors healthy.'}\n")
    if not slack_url:
        out("::warning::SLACK_DEVOPS_WEBHOOK_URL is not set in this repository: alerts go to the issue only")

    failed = False
    kw = dict(read_alert=read_alert, write=write, post=post, alert_repo=alert_repo, slack_url=slack_url,
              run_url=run_url, now=now, dry_run=dry_run, log=out)
    try:
        deliver(findings, incomplete=bool(errors), sleep=sleep, **kw)
    except (AlertError, ReadError) as exc:
        out(f"::error::alert delivery failed: {exc}")
        failed = True
    if flag("TEST_DEAD_SLACK") and not dry_run:
        # Forced-failure test: always attempt one post to the dead webhook,
        # even when the findings are unchanged and deliver() posted nothing.
        try:
            post(slack_url, f"Monitor Health forced-failure test (dead channel). Run: {run_url}")
        except AlertError as exc:
            out(f"::error::alert delivery failed: {exc}")
            failed = True
    if want_heartbeat:
        try:
            heartbeat(config, findings, **kw)
        except (AlertError, ReadError) as exc:
            out(f"::error::heartbeat delivery failed: {exc}")
            failed = True
    if errors:
        for err in errors:
            out(f"::error::read failed: {err}")
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
