#!/usr/bin/env python3
"""Mutation testing for the logic modules (docs/TESTING.md, layer 1).

A test suite can run every line and still check nothing. So this changes the
code on purpose, one small bug at a time (a `<` becomes `<=`, `and` becomes
`or`, a `not` disappears, a number moves by one), runs the tests that cover
that module, and counts the bugs they catch. A bug they miss ("a survivor")
is a place where the tests do not look.

Two modes:

  - a pull request (--changed BASE): every bug that can be planted in the
    lines it changed is tried, none sampled, and most must be caught
    (--min-changed, 70%). The question is "are the lines you wrote tested?",
    and the answer does not drift with the rest of the module.
  - nightly: a seeded sample per module, compared with the module's recorded
    score (tools/mutation_baseline.json, measured in CI); a fall of more than
    --tolerance points fails. A sample moves when the module's code changes,
    so this is a trend, not an exact gate.

It works on a copy of the repository, so an interrupted run leaves no bug
behind, and it is reproducible: the same seed tries the same mutants.

    python tools/mutation_test.py                       # every module, checked against the baseline
    python tools/mutation_test.py core/landing.py -n 60 # one module, more mutants
    python tools/mutation_test.py --update              # raise the baseline after better tests
    python tools/mutation_test.py --changed origin/main # a pull request: the modules it touches
"""
import argparse
import ast
import copy
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE = os.path.join(ROOT, "tools", "mutation_baseline.json")

# Logic modules and the tests that are meant to catch their bugs.
MODULES = {
    "core/landing.py": ["tools/test_landing.py", "tools/test_gateway_sync.py"],
    "core/director.py": ["tools/test_director.py"],
    "core/languages.py": ["tools/test_english_only.py"],
    "core/chunking.py": ["tools/test_chunk_split.py", "tools/test_chunking.py"],
    "core/narration.py": ["tools/test_narration.py", "tools/test_stray_text.py"],
    "core/structure.py": ["tools/test_block_text.py", "tools/test_chapters_and_pictures.py",
                          "tools/test_narration.py", "tools/test_stray_text.py", "tools/test_epub3_toc.py"],
    "core/gateway_address.py": ["tools/test_gateway_address.py"],
    "core/ai_brain.py": ["tools/test_junk_windows.py", "tools/test_free_routes.py"],
    "core/block_filter.py": ["tools/test_block_filter.py"],
    "core/junk_provider.py": ["tools/test_junk_provider.py", "tools/test_block_filter.py"],
    "core/chunk_cache.py": ["tools/test_chunk_cache.py"],
    "core/drm.py": ["tools/test_drm.py"],
    "core/pod_pool.py": ["tools/test_pod_pool.py", "tools/test_pod_sweep.py", "tools/test_pod_release.py",
                         "tools/test_deploy_request.py", "tools/test_pool_acquisition.py"],
    "libra_cli/clip_check.py": ["tools/test_clip_check.py", "tools/test_voice_add.py"],
}

SWAPS = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.Eq: ast.NotEq,
         ast.NotEq: ast.Eq, ast.In: ast.NotIn, ast.NotIn: ast.In, ast.Is: ast.IsNot, ast.IsNot: ast.Is,
         ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.FloorDiv, ast.And: ast.Or, ast.Or: ast.And}


def candidates(tree):
    """[(node index in ast.walk order, kind)] for every place a bug can be planted."""
    found = []
    for i, node in enumerate(ast.walk(tree)):
        if isinstance(node, ast.Compare):
            found += [(i, ("cmp", j)) for j, op in enumerate(node.ops) if type(op) in SWAPS]
        elif isinstance(node, (ast.BinOp, ast.BoolOp)) and type(node.op) in SWAPS:
            found.append((i, ("op", None)))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            found.append((i, ("not", None)))
        elif isinstance(node, ast.Constant) and type(node.value) in (bool, int) and not isinstance(node.value, bool) \
                and abs(node.value) < 10_000:
            found.append((i, ("num", None)))
        elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
            found.append((i, ("bool", None)))
    return found


def mutate(tree, index, kind):
    """A copy of `tree` with one bug planted, and a line describing it."""
    tree = copy.deepcopy(tree)
    node = list(ast.walk(tree))[index]
    what, j = kind
    line = getattr(node, "lineno", "?")
    if what == "cmp":
        old = node.ops[j]
        node.ops[j] = SWAPS[type(old)]()
        desc = f"{type(old).__name__} -> {type(node.ops[j]).__name__}"
    elif what == "op":
        old = node.op
        node.op = SWAPS[type(old)]()
        desc = f"{type(old).__name__} -> {type(node.op).__name__}"
    elif what == "not":
        parent = next(p for p in ast.walk(tree) for c in ast.iter_child_nodes(p) if c is node)
        for field, value in ast.iter_fields(parent):
            if value is node:
                setattr(parent, field, node.operand)
            elif isinstance(value, list):
                setattr(parent, field, [node.operand if v is node else v for v in value])
        desc = "drop `not`"
    elif what == "num":
        desc = f"{node.value} -> {node.value + 1}"
        node.value += 1
    else:
        desc = f"{node.value} -> {not node.value}"
        node.value = not node.value
    return ast.fix_missing_locations(tree), f"line {line}: {desc}"


def tests_pass(workdir, tests, timeout):
    for t in tests:
        try:
            r = subprocess.run([sys.executable, t], cwd=workdir, capture_output=True,
                               timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return False       # a hang is caught, not survived
        if r.returncode != 0:
            return False
    return True


def copy_repo():
    work = tempfile.mkdtemp(prefix="voxie-mutants-")
    ignore = shutil.ignore_patterns(".git", "library", "mfa_workspace", "training_lab", "voices",
                                    "__pycache__", ".venv", "node_modules", "*.wav", "*.mp3")
    shutil.copytree(ROOT, os.path.join(work, "repo"), ignore=ignore, symlinks=True)
    return work, os.path.join(work, "repo")


def run_module(repo, module, tests, n, seed, timeout, lines=None):
    """(score, survivors). With `lines`, every candidate on those lines, unsampled."""
    path = os.path.join(repo, module)
    source = open(path, encoding="utf-8").read()
    tree = ast.parse(source)
    if not tests_pass(repo, tests, timeout):
        return None, ["the tests fail without any mutant: fix them first"]
    places = candidates(tree)
    if lines is not None:
        nodes = list(ast.walk(tree))
        places = [p for p in places if getattr(nodes[p[0]], "lineno", None) in lines]
        if not places:
            return 100.0, []
        n = len(places)
    else:
        random.Random(f"{seed}:{module}").shuffle(places)
    caught, survivors = 0, []
    for index, kind in places[:n]:
        mutant, desc = mutate(tree, index, kind)
        try:
            open(path, "w", encoding="utf-8").write(ast.unparse(mutant))
            if tests_pass(repo, tests, timeout):
                survivors.append(desc)
            else:
                caught += 1
        finally:
            open(path, "w", encoding="utf-8").write(source)
    tried = min(n, len(places))
    return (round(100.0 * caught / tried, 1) if tried else 100.0), survivors


def changed_lines(base, module, repo=ROOT):
    """Line numbers of `module` (in the new version) added or changed since base."""
    import re
    diff = subprocess.run(["git", "diff", "--unified=0", f"{base}...HEAD", "--", module], cwd=repo,
                          capture_output=True, text=True, check=True).stdout
    lines = set()
    for m in re.finditer(r"^@@ .*?\+(\d+)(?:,(\d+))? @@", diff, re.M):
        start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
        lines.update(range(start, start + count))
    return lines


def changed_mode(args):
    changed = subprocess.run(["git", "diff", "--name-only", f"{args.changed}...HEAD"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.split()
    modules = [m for m in MODULES if m in changed]
    if not modules:
        print("Mutation testing: no logic module changed.")
        return 0
    # The pull request's own new or changed tests run too: a test written for
    # this change is the one meant to catch bugs in it, and it is not in
    # MODULES yet (#48's first run: 0% caught, its new test never ran).
    own_tests = [t for t in changed if t.startswith("tools/test_") and t.endswith(".py")
                 and os.path.exists(os.path.join(ROOT, t))]
    work, repo = copy_repo()
    ok = True
    try:
        for module in modules:
            lines = changed_lines(args.changed, module)
            tests = list(dict.fromkeys(MODULES[module] + own_tests))
            score, survivors = run_module(repo, module, tests, 0, args.seed, args.timeout, lines)
            if score is None:
                ok = False
                print(f"FAIL  {module}: {survivors[0]}")
                continue
            verdict = "ok  " if score >= args.min_changed else "FAIL"
            ok &= verdict == "ok  "
            print(f"{verdict}  {module}: {score}% of the bugs planted in its changed lines were caught "
                  f"(need {args.min_changed}%)", flush=True)
            for s in survivors[:12]:
                print(f"        survived: {s}  <- no test notices this change")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("modules", nargs="*", help="default: every module in MODULES")
    ap.add_argument("-n", type=int, default=25, help="mutants per module")
    ap.add_argument("--seed", default="voxie")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--changed", metavar="BASE", help="a pull request: the lines changed since BASE")
    ap.add_argument("--min-changed", type=float, default=70.0)
    ap.add_argument("--tolerance", type=float, default=15.0, help="nightly: points a module may fall")
    ap.add_argument("--report", action="store_true", help="nightly: print the scores, warn, never fail")
    args = ap.parse_args(argv)
    if args.changed:
        return changed_mode(args)

    baseline = json.load(open(BASELINE)) if os.path.exists(BASELINE) else {}
    work, repo = copy_repo()
    ok, scores = True, {}
    try:
        for module in args.modules or MODULES:
            score, survivors = run_module(repo, module, MODULES[module], args.n, args.seed, args.timeout)
            if score is None:
                ok = False
                print(f"FAIL  {module}: {survivors[0]}")
                continue
            scores[module] = score
            floor = baseline.get(module, 0.0)
            verdict = "ok  " if score >= floor - args.tolerance else ("WARN" if args.report else "FAIL")
            ok &= verdict != "FAIL"
            print(f"{verdict}  {module}: {score}% of mutants caught (baseline {floor}%)", flush=True)
            for s in survivors[:8]:
                print(f"        survived: {s}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if args.update:
        for m, s in scores.items():
            baseline[m] = max(s, baseline.get(m, 0.0))
        json.dump(baseline, open(BASELINE, "w"), indent=1, sort_keys=True)
        print("Baseline updated (it only goes up).")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
