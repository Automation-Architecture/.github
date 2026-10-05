#!/usr/bin/env python3
"""Offline tests for actions/change-scope.

Unit-tests the classifier, then runs the script end to end with a stub `gh`
first on PATH, the way the composite step runs it. Needs python3 only.

    python3 tests/change-scope/test_change_scope.py
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "actions/change-scope/change_scope.py"
sys.path.insert(0, str(SCRIPT.parent))
import change_scope as cs  # noqa: E402

OWN = ".github/workflows/ci.yml"
failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}: got {got!r}, want {want!r}")


def files(*paths):
    out = []
    for p in paths:
        if isinstance(p, tuple):
            out.append({"filename": p[0], "previous_filename": p[1]})
        else:
            out.append({"filename": p, "previous_filename": None})
    return out


def code(event, fs, globs=(), own=OWN):
    return cs.classify(event, fs, [cs.glob_to_regex(g) for g in globs], own)[0]


# Globs
g = cs.glob_to_regex
check("** any depth", bool(g("prompts/**").match("prompts/a/b.md")), True)
check("**/ zero dirs", bool(g("**/*.md").match("README.md")), True)
check("* one segment", bool(g("docs/*.md").match("docs/a/b.md")), False)
check("anchored", bool(g("prompts/**").match("src/prompts/a.md")), False)
check("literal dot", bool(g("a.md").match("aXmd")), False)

# Docs and workflow only -> skip
check("readme", code("pull_request", files("README.md")), False)
check("nested md", code("pull_request", files("docs/guide/setup.markdown", "AGENTS.md")), False)
check("uppercase ext", code("pull_request", files("NOTES.MD")), False)
check("other workflow", code("pull_request", files(".github/workflows/review-verdict.yml")), False)
check("github misc", code("pull_request", files(".github/CODEOWNERS", ".github/dependabot.yml")), False)
check("target event", code("pull_request_target", files("README.md")), False)

# Code -> run
check("source file", code("pull_request", files("README.md", "src/app.ts")), True)
check("mdx is code", code("pull_request", files("docs/page.mdx")), True)
check("own workflow", code("pull_request", files(OWN)), True)
check("local action", code("pull_request", files(".github/actions/setup/action.yml")), True)
check("code-path md", code("pull_request", files("prompts/system.md"), ["prompts/**"]), True)
check("code-path workflow", code("pull_request", files(".github/workflows/reusable.yml"),
                                 [".github/workflows/reusable.yml"]), True)
check("rename from code", code("pull_request", files(("notes.md", "src/notes.ts"))), True)
check("rename to md ok", code("pull_request", files(("new.md", "old.md"))), False)
check("txt is code", code("pull_request", files("requirements.txt")), True)
check("no own known", code("pull_request", files(".github/workflows/x.yml"), own=None), False)

# Uncertain -> run
check("push", code("push", files("README.md")), True)
check("schedule", code("schedule", []), True)
check("api failed", code("pull_request", None), True)
check("empty list", code("pull_request", []), True)
check("file cap", code("pull_request", files(*[f"d/{i}.md" for i in range(3000)])), True)

# End to end with a stub gh
STUB = r'''#!/usr/bin/env python3
import json, os, sys
open(os.environ["CALLS"], "a").write(json.dumps(sys.argv[1:]) + "\n")
mode = os.environ["STUB_MODE"]
if mode == "fail":
    sys.stderr.write("HTTP 403: Resource not accessible by integration\n"); sys.exit(1)
for line in os.environ["STUB_FILES"].splitlines():
    print(line)
'''


def run(event, stub_files="", mode="ok", pr="7", code_paths=""):
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        (d / "gh").write_text(STUB)
        (d / "gh").chmod(0o755)
        out = d / "out"
        out.write_text("")
        env = dict(os.environ, PATH=f"{d}:{os.environ['PATH']}", CALLS=str(d / "calls"),
                   STUB_MODE=mode, STUB_FILES=stub_files, GITHUB_OUTPUT=str(out),
                   GITHUB_EVENT_NAME=event, GITHUB_REPOSITORY="acme/app", PR_NUMBER=pr,
                   GITHUB_WORKFLOW_REF="acme/app/.github/workflows/ci.yml@refs/pull/7/merge",
                   CODE_PATHS=code_paths)
        p = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True)
        calls = (d / "calls").read_text() if (d / "calls").exists() else ""
        result = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
        return p, result, calls


def lines(*paths):
    return "\n".join(json.dumps({"filename": p, "previous_filename": None}) for p in paths)


p, r, calls = run("pull_request", lines("README.md", ".github/workflows/deploy.yml"))
check("e2e docs exit", p.returncode, 0)
check("e2e docs code", r.get("code"), "false")
check("e2e docs notice", "::notice" in p.stdout, True)
check("e2e api path", "repos/acme/app/pulls/7/files?per_page=100" in calls, True)

p, r, _ = run("pull_request", lines(".github/workflows/ci.yml"))
check("e2e own workflow", r.get("code"), "true")

p, r, _ = run("pull_request", lines("prompts/a.md"), code_paths="# comment\nprompts/**\n")
check("e2e code-paths", r.get("code"), "true")

p, r, _ = run("pull_request", mode="fail")
check("e2e 403 exit", p.returncode, 0)
check("e2e 403 code", r.get("code"), "true")
check("e2e 403 warning", "::warning" in p.stdout, True)

p, r, calls = run("push", lines("README.md"))
check("e2e push code", r.get("code"), "true")
check("e2e push no api", calls, "")

p, r, calls = run("pull_request", lines("README.md"), pr="")
check("e2e no pr number", r.get("code"), "true")
check("e2e no pr no api", calls, "")

evil = json.dumps({"filename": "src/a\ncode=false\nreason=x", "previous_filename": None})
p, r, _ = run("pull_request", evil)
check("e2e newline injection", r.get("code"), "true")
check("e2e one code line", p.returncode, 0)

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("change-scope: all tests passed")
