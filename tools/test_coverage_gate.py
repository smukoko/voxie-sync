"""The coverage gate's arithmetic (tools/coverage_gate.py): which lines changed, which ran."""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import coverage_gate  # noqa: E402

passed = failed = 0
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.invalid"}


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


repo = tempfile.mkdtemp()
run = lambda *c: subprocess.run(c, cwd=repo, env=ENV, check=True, capture_output=True)  # noqa: E731
os.makedirs(os.path.join(repo, "core"))
open(os.path.join(repo, "core", "a.py"), "w").write("x = 1\ny = 2\nz = 3\n")
open(os.path.join(repo, "notes.md"), "w").write("hi\n")
run("git", "init", "-q", "-b", "main"); run("git", "add", "-A"); run("git", "commit", "-qm", "base")
run("git", "checkout", "-qb", "change")
open(os.path.join(repo, "core", "a.py"), "w").write("x = 1\ny = 20\nz = 3\nw = 4\nv = 5\n")
open(os.path.join(repo, "core", "b.py"), "w").write("q = 1\n")
open(os.path.join(repo, "notes.md"), "w").write("changed\n")
run("git", "add", "-A"); run("git", "commit", "-qm", "change")

lines = coverage_gate.changed_lines("main", repo)
check("changed and added lines are found, Python only",
      lines == {"core/a.py": {2, 4, 5}, "core/b.py": {1}}, str(lines))

# A comment added to an unchanged line is not a change; a changed line is,
# even when a comment comes with it.
run("git", "checkout", "-qb", "comments")
open(os.path.join(repo, "core", "a.py"), "w").write(
    "x = 1  # nosec B101\ny = 20\nz = 30  # why it is 30\nw = 4\nv = 5\n")
run("git", "add", "-A"); run("git", "commit", "-qm", "comments")
lines2 = coverage_gate.changed_lines("change", repo)
check("a comment-only edit is not counted, a code edit with a comment is",
      lines2 == {"core/a.py": {3}}, str(lines2))
run("git", "checkout", "-q", "change")

report = {"files": {"core/a.py": {"executed_lines": [1, 2, 3, 4], "missing_lines": [5]},
                    "tools/x.py": {"executed_lines": [], "missing_lines": [1]}}}
covered, total, missing = coverage_gate.diff_coverage(report, {"core/a.py": {2, 4, 5}, "core/b.py": {1}})
check("only measured files count, and only executable lines", (covered, total) == (2, 3), f"{covered}/{total}")
check("the uncovered changed lines are named", missing == {"core/a.py": [5]})

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
