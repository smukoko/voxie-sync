"""The files the code loads must survive the public export (2026-10-22).

The first install from the public repo ran with no junk filter: every section
logged "the junk filter failed (XGBoostError: ... safe_models/audiobook_xgb...)"
and was kept, so a publisher's "Thank you for downloading this ebook... click
here to sign up" page was narrated as the book's first words. The model file
was tracked here, but `.gitignore` says `*.json`: `git archive` exports tracked
files whatever .gitignore says, then the export's `git add -A` honours it and
drops the file. Nothing noticed, because the file existed on the author's disk.

So: a tracked file that .gitignore also matches is a file the public repo will
not have, and the files ai_brain loads must be neither ignored nor missing.
Offline; needs git (skips loudly outside a checkout).
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


def git(*args):
    return subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True)


if git("rev-parse", "--is-inside-work-tree").returncode != 0:
    print("SKIP  not a git checkout")
    sys.exit(0)

from core import ai_brain  # noqa: E402

# The classifier's two files exist, are tracked, and are not ignored.
for path in (ai_brain.MODEL_JSON, ai_brain.SCALER_PKL):
    rel = os.path.relpath(path, ROOT)
    check(f"{rel} exists", os.path.isfile(path))
    check(f"{rel} is tracked", git("ls-files", "--error-unmatch", rel).returncode == 0)
    check(f"{rel} is not gitignored", git("check-ignore", "-q", "--no-index", rel).returncode == 1)

# The general form of the same failure: nothing tracked is also ignored.
both = git("ls-files", "-ci", "--exclude-standard").stdout.split()
check("no tracked file is also gitignored (the export would drop it)", not both, str(both))

# A missing file is said in words, once, before any model is touched.
real = ai_brain.MODEL_JSON
ai_brain.MODEL_JSON = os.path.join(ROOT, "safe_models", "nope.json")
try:
    ai_brain._brain()
    check("a missing model file is named in the error", False)
except FileNotFoundError as e:
    check("a missing model file is named in the error", "nope.json" in str(e) and "missing" in str(e), str(e))
finally:
    ai_brain.MODEL_JSON = real

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
