"""The mutation tester itself (tools/mutation_test.py): what it plants, and where."""
import ast
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import mutation_test as mt  # noqa: E402

passed = failed = 0
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.invalid"}


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


SRC = "def f(a, b):\n    if a < b and not a == 0:\n        return a + 1\n    return True\n"
tree = ast.parse(SRC)
places = mt.candidates(tree)
kinds = sorted({k[1][0] for k in places})
check("every kind of bug is found", kinds == ["bool", "cmp", "not", "num", "op"], str(kinds))
mutants = {ast.unparse(mt.mutate(tree, i, k)[0]) for i, k in places}
check("each planted bug changes the code, and every mutant still parses",
      len(mutants) == len(places) and all(ast.parse(m) for m in mutants) and ast.unparse(tree) not in mutants)
check("the original is never touched", ast.unparse(tree) == ast.unparse(ast.parse(SRC)))

repo = tempfile.mkdtemp()
run = lambda *c: subprocess.run(c, cwd=repo, env=ENV, check=True, capture_output=True)  # noqa: E731
os.makedirs(os.path.join(repo, "core"))
open(os.path.join(repo, "core", "m.py"), "w").write("a = 1\nb = 2\n")
run("git", "init", "-q", "-b", "main"); run("git", "add", "-A"); run("git", "commit", "-qm", "base")
run("git", "checkout", "-qb", "change")
open(os.path.join(repo, "core", "m.py"), "w").write("a = 1\nb = 3\nc = 4\n")
run("git", "add", "-A"); run("git", "commit", "-qm", "change")
check("a pull request's changed lines are the ones mutated",
      mt.changed_lines("main", "core/m.py", repo) == {2, 3}, str(mt.changed_lines("main", "core/m.py", repo)))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
