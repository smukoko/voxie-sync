"""The public export, first and after: a fresh history once, then one commit on top.

After the public repository exists its history must never be replaced, so
`--onto` lays the filtered, leak-checked tree over a clone of it as one new
commit. Runs the real script against this repository's HEAD.
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "tools", "export_public.sh")
passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


# A CI runner has no git identity, and the export commits.
ENV = {**os.environ, "GIT_AUTHOR_NAME": "Voxie test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
       "GIT_COMMITTER_NAME": "Voxie test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}


def run(*args, cwd=ROOT):
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, env=ENV)


def git(repo, *args):
    return run("git", "-C", repo, *args).stdout.strip()


# The export is git's HEAD: a copy without .git (the mutation gate's) has none,
# and its export would be empty, which the checks below would half pass.
if run("git", "rev-parse", "--git-dir").returncode != 0:
    print("SKIP  not a git checkout: the export is HEAD")
    sys.exit(0)
if run("git", "status", "--porcelain").stdout.strip() or not shutil.which("rsync"):
    print("SKIP  the export is HEAD: commit first (and it needs rsync)")
    sys.exit(0)

tmp = tempfile.mkdtemp()
# The export of a clone of HEAD, not of HEAD: a README sample in a placeholder
# voice (assets/samples/SAMPLES.json, voice_consent null) rightly stops the
# real export until the maintainer's own voice replaces it. Here it is marked
# consented, so the export's mechanics are still tested meanwhile.
source = os.path.join(tmp, "source")
run("git", "clone", "-q", ROOT, source)
listing = os.path.join(source, "assets", "samples", "SAMPLES.json")
if os.path.exists(listing):
    import json
    samples = json.load(open(listing))
    if any((v or {}).get("voice_consent") is None for v in samples.values()):
        for v in samples.values():
            v["voice_consent"] = v.get("voice_consent") or "self"
        json.dump(samples, open(listing, "w"))
        run("git", "-C", source, "commit", "-qam", "test: placeholder samples marked consented")
SCRIPT = os.path.join(source, "tools", "export_public.sh")
first = os.path.join(tmp, "first")
r = run("bash", SCRIPT, first)
check("the first export is one commit", r.returncode == 0 and git(first, "rev-list", "--count", "HEAD") == "1",
      r.stderr[-300:])
check("it leaves the private files out", not os.path.exists(os.path.join(first, "docs", "LAUNCH_PLAN.md"))
)
exported = open(os.path.join(first, "CLAUDE.md")).read().splitlines()
check("CLAUDE.md's lines marked private stay here (the maintainer's voices, server, device)",
      not any(line.rstrip().endswith("<!-- private -->") for line in exported))
check("and the lines around them are kept", any(line.startswith("- **No voice is bundled**") for line in exported))

public = os.path.join(tmp, "public")
run("git", "clone", "-q", first, public)
r = run("bash", SCRIPT, "--onto", public, "nothing new")
check("an export with nothing new makes no commit",
      r.returncode == 0 and "Nothing changed" in r.stdout and git(public, "rev-list", "--count", "HEAD") == "1")

# The public repo drifted from HEAD: a file edited there, one added there.
with open(os.path.join(public, "README.md"), "a") as f:
    f.write("\nedited by hand\n")
open(os.path.join(public, "stray.txt"), "w").write("not in the source")
run("git", "-C", public, "add", "-A")
run("git", "-C", public, "commit", "-qm", "drift")
before = git(public, "rev-parse", "HEAD")
r = run("bash", SCRIPT, "--onto", public, "Update from the source")
check("--onto adds one commit on top of the public history",
      r.returncode == 0 and git(public, "rev-list", "--count", "HEAD") == "3"
      and git(public, "rev-parse", "HEAD~1") == before, r.stderr[-300:])
check("the tree is HEAD again: edits undone, strays removed",
      "edited by hand" not in open(os.path.join(public, "README.md")).read()
      and not os.path.exists(os.path.join(public, "stray.txt")))
check("and identical to a fresh export", git(public, "rev-parse", "HEAD^{tree}") == git(first, "rev-parse", "HEAD^{tree}"))
check("its message is the one given", git(public, "log", "-1", "--format=%s") == "Update from the source")
open(os.path.join(public, "dirty.txt"), "w").write("x")
check("a clone with uncommitted changes is refused",
      run("bash", SCRIPT, "--onto", public, "m").returncode != 0)

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
