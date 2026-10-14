"""The regression-proof gate itself (tools/regression_proof.py), on scratch git repos.

It must accept a change whose test fails on the old code, and refuse a change
whose tests would pass without it, or that has no test at all.
"""
import io
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import regression_proof  # noqa: E402

passed = failed = 0
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.invalid"}


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


def sh(repo, *cmd):
    subprocess.run(cmd, cwd=repo, env=ENV, check=True, capture_output=True)


def write(repo, path, text):
    os.makedirs(os.path.dirname(os.path.join(repo, path)) or repo, exist_ok=True)
    open(os.path.join(repo, path), "w").write(text)


TEST = '''import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.price import total
sys.exit(0 if {cond} else 1)
'''


def scenario(change, labels=""):
    """A repo whose base has core/price.py and a test; `change` edits it on a branch."""
    repo = tempfile.mkdtemp()
    sh(repo, "git", "init", "-q", "-b", "main")
    write(repo, "core/__init__.py", "")
    write(repo, "core/price.py", "def total(a, b):\n    return a + b\n")
    write(repo, "tools/test_price.py", TEST.format(cond="total(2, 3) == 5"))
    write(repo, "README.md", "x\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-qm", "base")
    sh(repo, "git", "checkout", "-qb", "change")
    change(repo)
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-qm", "change")
    out = io.StringIO()
    with redirect_stdout(out):
        code = regression_proof.main(["--base", "main", "--repo", repo, "--labels", labels])
    return code, out.getvalue()


def real_fix(repo):     # a discount, with a test that needs it
    write(repo, "core/price.py", "def total(a, b, off=0):\n    return a + b - off\n")
    write(repo, "tools/test_discount.py", TEST.format(cond="total(2, 3, off=1) == 4"))


def hollow_test(repo):  # the same change, but the new test passes on the old code too
    write(repo, "core/price.py", "def total(a, b, off=0):\n    return a + b - off\n")
    write(repo, "tools/test_discount.py", TEST.format(cond="total(2, 3) == 5"))


def no_test(repo):
    write(repo, "core/price.py", "def total(a, b, off=0):\n    return a + b - off\n")


def docs_only(repo):
    write(repo, "README.md", "more\n")


code, out = scenario(real_fix)
check("a change whose new test fails on the old code is accepted", code == 0 and "FAILS" in out, out[-200:])
code, out = scenario(hollow_test)
check("a test that passes on the old code too is refused", code == 1 and "none of them tests it" in out, out[-200:])
code, out = scenario(no_test)
check("code changed without any test is refused", code == 1 and "no test did" in out, out[-200:])
code, out = scenario(docs_only)
check("documentation alone needs no test", code == 0 and "nothing to prove" in out)
code, out = scenario(no_test, labels="no-behaviour-change")
check("a refactor label skips it, and says so", code == 0 and "skipped" in out)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
