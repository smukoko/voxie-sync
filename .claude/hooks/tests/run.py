#!/usr/bin/env python3
"""Exercise the hooks against fixtures.

The cases live in cases.json rather than in this file's own shell commands,
because a hook that works would block the test that tries to run `rm -rf /`
through a Bash tool call. Feeding the hook a JSON payload on stdin is how
Claude Code invokes it anyway, so this is the real interface, not a mock.

    python3 .claude/hooks/tests/run.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HOOKS = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(HOOKS))

passed, failed = 0, 0


def report(ok, name, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}{('  — ' + detail) if detail else ''}")


def call(hook, payload, cwd=None, project_dir=None):
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = project_dir or REPO
    return subprocess.run(
        [sys.executable, os.path.join(HOOKS, hook)],
        input=json.dumps(payload), capture_output=True, text=True,
        cwd=cwd or REPO, env=env, timeout=120,
    )


def bash_payload(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}}


# ------------------------------------------------------- block_dangerous ---
print("\nblock_dangerous.py — command fixtures")
for case in json.load(open(os.path.join(HERE, "cases.json"))):
    result = call("block_dangerous.py", bash_payload(case["command"]))
    blocked = result.returncode == 2
    report(blocked == case["block"], case["name"],
           f"expected {'block' if case['block'] else 'allow'}, "
           f"got {'block' if blocked else 'allow'}")


# --------------------------------------------------- gate_before_commit ---
print("\ngate_before_commit.py — branch detection")


def temp_repo(branch, commit=False):
    """A repo on `branch`. With commit=False it has NO commits, which is the
    state where `git rev-parse --abbrev-ref HEAD` fails and the main check
    used to silently pass."""
    d = tempfile.mkdtemp()
    run = lambda *a: subprocess.run(a, cwd=d, capture_output=True, text=True)
    run("git", "init", "-q")
    run("git", "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    run("git", "config", "user.email", "t@t"); run("git", "config", "user.name", "t")
    if commit:
        open(os.path.join(d, "f.txt"), "w").write("x")
        run("git", "add", "f.txt"); run("git", "commit", "-qm", "init")
    return d


# The trap: prove rev-parse really does fail on a commitless branch, so the
# comment in the hook is describing a real thing and not folklore.
empty_main = temp_repo("main", commit=False)
rp = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=empty_main, capture_output=True, text=True)
sr = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"],
                    cwd=empty_main, capture_output=True, text=True)
report(rp.returncode != 0 and sr.returncode == 0,
       "rev-parse fails on a commitless branch where symbolic-ref works",
       f"rev-parse={rp.returncode} symbolic-ref={sr.returncode}")

r = call("gate_before_commit.py", bash_payload("git commit -m x"),
         cwd=empty_main, project_dir=empty_main)
report(r.returncode == 2, "blocks a first commit landing on main (no commits yet)")

main_with_commits = temp_repo("main", commit=True)
r = call("gate_before_commit.py", bash_payload("git commit -m x"),
         cwd=main_with_commits, project_dir=main_with_commits)
report(r.returncode == 2, "blocks a commit on main")

master = temp_repo("master", commit=True)
r = call("gate_before_commit.py", bash_payload("git commit -m x"),
         cwd=master, project_dir=master)
report(r.returncode == 2, "blocks a commit on master")

r = call("gate_before_commit.py", bash_payload("ls -la"),
         cwd=main_with_commits, project_dir=main_with_commits)
report(r.returncode == 0, "ignores a command that is not a commit")

print("\ngate_before_commit.py — gates, in the real repo")
r = call("gate_before_commit.py", bash_payload("git commit -m 'test'"))
report(r.returncode == 0, "allows a commit on this feature branch with gates green",
       (r.stderr or "")[-300:])

# Both gates must actually be runnable here; a gate that silently no-ops
# because its command is missing is worse than no gate.
sys.path.insert(0, HOOKS)
import gate_before_commit as gate  # noqa: E402
for label, cmd in gate.GATES:
    out = gate.run(cmd, REPO)
    report(out is not None and out.returncode == 0,
           f"gate '{label}' runs and passes here",
           "not runnable" if out is None else f"exit {out.returncode}")


# -------------------------------------------------------- check_on_write ---
print("\ncheck_on_write.py")
good = os.path.join(REPO, ".claude", "hooks", "tests", "_tmp_ok.py")
bad = os.path.join(REPO, ".claude", "hooks", "tests", "_tmp_bad.py")
open(good, "w").write("x = 1\n")
open(bad, "w").write("def broken(:\n")
try:
    r = call("check_on_write.py", {"tool_name": "Write", "tool_input": {"file_path": good}})
    report(r.returncode == 0 and "does not parse" not in r.stderr, "clean file passes quietly")

    r = call("check_on_write.py", {"tool_name": "Write", "tool_input": {"file_path": bad}})
    report(r.returncode == 0, "a syntax error NEVER blocks the write")
    report("does not parse" in r.stderr, "...but it is reported", r.stderr.strip()[:120])

    r = call("check_on_write.py",
             {"tool_name": "Write", "tool_input": {"file_path": "/tmp/outside.py"}})
    report(r.returncode == 0, "a file outside the project is left alone")
finally:
    for f in (good, bad):
        if os.path.exists(f):
            os.remove(f)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
