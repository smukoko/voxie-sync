#!/usr/bin/env python3
"""A pull request's tests must fail on the code from before it (docs/TESTING.md, layer 1).

CLAUDE.md's rule, made binding: "a test that cannot fail on the old code is
not a test". For a change to the product's code:

  - it must add or change at least one test (tools/test_*.py or .lua);
  - those tests, run against the code as it was at BASE, must not all pass.
    If every one of them passes there too, they do not test the change.

Documentation-only changes pass. A pure refactor, whose tests are meant to
pass both before and after, says so with the `no-behaviour-change` label
(--labels), and the check says it was skipped and why.

    python tools/regression_proof.py --base origin/main [--labels a,b]
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST = re.compile(r"^tools/test_[^/]+\.(py|lua)$")
# Data the tests read, copied with them: a new example belongs to the test.
TEST_DATA = re.compile(r"^tools/.*\.(json|txt|epub|xhtml|html|wav)$")
# The product's code: what a test must prove. Docs, CI and dev tools are not.
CODE = re.compile(r"^(core/|libra_cli/|server/|librarian_sync\.koplugin/|koreader_patches/|librerian_[^/]+\.py$|"
                  r"mac_ghost\.py$|install\.sh$|docker/|tools/(export_public\.sh|leak_scan\.py|"
                  r"gateway_admin\.py|rebuild_map\.py|repair_book\.py)$)")
LABEL = "no-behaviour-change"


def git(*args, cwd=ROOT):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def changed(base, repo=ROOT):
    """{path: status letter} for the files that differ between BASE and HEAD."""
    out = git("diff", "--name-status", f"{base}...HEAD", cwd=repo)
    result = {}
    for line in out.splitlines():
        status, *paths = line.split("\t")
        result[paths[-1]] = status[0]
    return result


def run_test(path, cwd):
    cmd = ["luajit", path] if path.endswith(".lua") else [sys.executable, path]
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600,
                           stdin=subprocess.DEVNULL)
        return r.returncode == 0, (r.stdout + r.stderr)[-600:]
    except subprocess.TimeoutExpired:
        return False, "timed out"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--labels", default="")
    ap.add_argument("--repo", default=ROOT, help=argparse.SUPPRESS)   # tests use a scratch repo
    args = ap.parse_args(argv)
    repo = args.repo

    files = changed(args.base, repo)
    code = sorted(p for p, s in files.items() if CODE.match(p))
    tests = sorted(p for p, s in files.items() if TEST.match(p) and s != "D")
    if not code:
        print("Regression proof: no product code changed; nothing to prove.")
        return 0
    if LABEL in [l.strip() for l in args.labels.split(",")]:
        print(f"Regression proof: skipped by the '{LABEL}' label (a refactor: its tests must "
              "pass before and after). Code changed: " + ", ".join(code))
        return 0
    print("Code changed: " + ", ".join(code))
    if not tests:
        print("FAIL  the code changed and no test did. Add a test that fails without this "
              f"change (or label the pull request '{LABEL}' if behaviour is unchanged).")
        return 1

    tmp = tempfile.mkdtemp(prefix="voxie-regression-")
    old = os.path.join(tmp, "old")
    try:
        git("worktree", "add", "--detach", "-q", old, args.base, cwd=repo)
        for p in files:
            if (TEST.match(p) or TEST_DATA.match(p)) and files[p] != "D":
                os.makedirs(os.path.dirname(os.path.join(old, p)), exist_ok=True)
                shutil.copy2(os.path.join(repo, p), os.path.join(old, p))
        caught = []
        for t in tests:
            passed, tail = run_test(t, old)
            print(f"   {'passes' if passed else 'FAILS '} on the old code: {t}")
            if not passed:
                caught.append(t)
        if caught:
            print(f"Regression proof: {len(caught)} of {len(tests)} test file(s) fail without "
                  "this change, so they test it.")
            return 0
        print("FAIL  every changed test also passes on the code from before this change, so "
              "none of them tests it. Add an assertion the old code fails.")
        return 1
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", old], cwd=repo, capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
