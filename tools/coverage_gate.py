#!/usr/bin/env python3
"""Coverage may only go up, and what a change adds must be tested (docs/TESTING.md, layer 1).

Runs every tools/test_*.py under coverage, then two checks:

  - the total may not fall below the recorded baseline
    (tools/coverage_baseline.json; --update raises it, never lowers it);
  - with --diff BASE, the executable lines changed since BASE must be covered
    (at least --min-diff of them, 90% by default).

    python tools/coverage_gate.py                       # run, report, check the total
    python tools/coverage_gate.py --diff origin/main    # and the changed lines
    python tools/coverage_gate.py --update              # after raising coverage

The test results are reported too: a failing test fails this gate as well.
"""
import argparse
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE = os.path.join(ROOT, "tools", "coverage_baseline.json")
RCFILE = os.path.join(ROOT, "tools", "coveragerc")
# Run by tools/run_all_tests.sh without coverage: one is a shell test, and
# test_fat_check needs Docker; they still run in the main test job.
SKIP = set()


def run_tests(data_dir, only=None):
    """Every test file under coverage. Returns {test file: passed?}."""
    env = {**os.environ, "COVERAGE_FILE": os.path.join(data_dir, ".coverage")}
    results = {}
    for test in sorted(glob.glob(os.path.join(ROOT, "tools", "test_*.py"))):
        name = os.path.relpath(test, ROOT)
        if name in SKIP or (only and name not in only):
            continue
        r = subprocess.run([sys.executable, "-m", "coverage", "run", "--rcfile", RCFILE, "-p", test],
                           cwd=ROOT, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        results[name] = r.returncode == 0
        print(f"{'ok  ' if r.returncode == 0 else 'FAIL'}  {name}", flush=True)
        if r.returncode:
            print("\n".join((r.stdout + r.stderr).splitlines()[-12:]))
    subprocess.run([sys.executable, "-m", "coverage", "combine", "--rcfile", RCFILE, "-q"],
                   cwd=ROOT, env=env, check=True)
    out = os.path.join(data_dir, "coverage.json")
    subprocess.run([sys.executable, "-m", "coverage", "json", "--rcfile", RCFILE, "-q", "-o", out],
                   cwd=ROOT, env=env, check=True)
    return results, json.load(open(out))


def _code(line):
    """A line without its comment, or None when it cannot be tokenized alone."""
    try:
        toks = [t for t in tokenize.generate_tokens(io.StringIO(line).readline)
                if t.type not in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
                                  tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER)]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    return " ".join(t.string for t in toks)


def changed_lines(base, repo=ROOT):
    """{repo-relative .py path: {line numbers added or changed since base}}.

    A line whose code is unchanged and only gained or lost a comment (a
    `# nosec` for the security scan, say) is not a change in behaviour: no test
    can tell the two apart, so it is not counted.
    """
    diff = subprocess.run(["git", "diff", "--unified=0", f"{base}...HEAD", "--", "*.py"],
                          cwd=repo, capture_output=True, text=True, check=True).stdout
    lines, path, removed, at = {}, None, set(), 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = None if line.endswith("/dev/null") else line[6:]
        elif line.startswith("--- "):
            continue
        elif line.startswith("@@") and path:
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            at, removed = int(m.group(1)), set()
            lines.setdefault(path, set())
        elif line.startswith("-") and path:
            code = _code(line[1:])
            if code:
                removed.add(code)
        elif line.startswith("+") and path:
            code = _code(line[1:])
            if code is None or code not in removed:
                lines[path].add(at)
            at += 1
    return {p: ls for p, ls in lines.items() if ls}


def diff_coverage(report, changed):
    """(covered, total, {path: [uncovered changed lines]}) over measured files only."""
    files = {os.path.relpath(p, ROOT) if os.path.isabs(p) else p: f for p, f in report["files"].items()}
    covered = total = 0
    missing = {}
    for path, lines in changed.items():
        f = files.get(path)
        if f is None:
            continue    # not measured: tests, tools, docs (tools/coveragerc)
        executed, missed = set(f["executed_lines"]), set(f["missing_lines"])
        hit, miss = lines & executed, lines & missed
        covered += len(hit)
        total += len(hit) + len(miss)
        if miss:
            missing[path] = sorted(miss)
    return covered, total, missing


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--diff", metavar="BASE", help="also check the lines changed since BASE")
    ap.add_argument("--min-diff", type=float, default=90.0, help="percent of changed lines covered")
    ap.add_argument("--update", action="store_true", help="raise the baseline to this run's total")
    args = ap.parse_args(argv)

    data_dir = tempfile.mkdtemp(prefix="voxie-coverage-")
    results, report = run_tests(data_dir)
    total = round(report["totals"]["percent_covered"], 2)
    baseline = json.load(open(BASELINE))["total"] if os.path.exists(BASELINE) else 0.0
    failing = [t for t, ok in results.items() if not ok]
    ok = True

    print(f"\nCoverage: {total}% of {report['totals']['num_statements']} statements "
          f"(baseline {baseline}%)")
    if failing:
        ok = False
        print(f"FAIL  {len(failing)} test file(s) fail: {', '.join(failing)}")
    if total < baseline - 0.05:
        ok = False
        print(f"FAIL  coverage fell from {baseline}% to {total}%: test what you added, or "
              "explain in the pull request why code was left untested.")
    if args.diff:
        covered, measured, missing = diff_coverage(report, changed_lines(args.diff))
        if measured:
            pct = round(100.0 * covered / measured, 1)
            print(f"Changed lines covered: {covered}/{measured} = {pct}% (need {args.min_diff}%)")
            for path, lines in sorted(missing.items()):
                print(f"   not covered: {path}: {', '.join(map(str, lines[:40]))}")
            if pct < args.min_diff:
                ok = False
                print("FAIL  the changed lines are not tested enough.")
        else:
            print("Changed lines covered: no measured code changed.")
    if args.update:
        if total > baseline:
            json.dump({"total": total}, open(BASELINE, "w"), indent=1)
            print(f"Baseline raised to {total}%.")
        else:
            print("Baseline unchanged: it only goes up.")
    shutil.rmtree(data_dir, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
