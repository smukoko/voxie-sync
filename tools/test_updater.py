"""Updating Voxie from the page or `voxie update` (2026-10-22).

The first install's `git pull` failed ("divergent branches": the public history
had been rewritten), and every first-stage fix needs one. Real git, local
repositories: a "public" origin reached through insteadOf, so its URL names the
public repository while the bytes come from a folder. What must be true:
a copy behind is fast-forwarded; a copy whose public history was rewritten and
has no edits of its own follows it; a copy with edits, or the maintainer's own
(not the public clone), is never touched and says why; dependencies are installed
only when they changed; old default settings move once, a choice made stays.
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from libra_cli import updater  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def git(cwd, *args):
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, env=ENV)
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


def commit(repo, name, text, msg):
    with open(os.path.join(repo, name), "w") as f:
        f.write(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)


work = tempfile.mkdtemp()
PUBLIC_URL = f"https://github.com/{updater.PUBLIC_REPO}.git"


def world(public=True):
    """(origin work tree, install clone): the install follows `origin`."""
    origin = os.path.join(work, f"origin{len(os.listdir(work))}")
    os.makedirs(origin)
    git(origin, "init", "-q", "-b", "main")
    commit(origin, "README", "one", "first")
    commit(origin, "requirements.txt", "flask", "deps")
    clone = origin + "-clone"
    subprocess.run(["git", "clone", "-q", origin, clone], check=True, env=ENV, capture_output=True)
    if public:
        # The URL names the public repository; the bytes come from the folder.
        git(clone, "config", f"url.{origin}.insteadOf", PUBLIC_URL)
        git(clone, "remote", "set-url", "origin", PUBLIC_URL)
    return origin, clone


lines = []
say = lines.append

# --- up to date -----------------------------------------------------------------------
origin, clone = world()
info = updater.check(clone)
check("a copy that is current says so", not info["behind"] and not info["can_update"] and not info["error"], str(info))
r = updater.update_checkout(clone, say)
check("and updating it changes nothing", not r["changed"] and "up to date" in lines[-1])

# --- behind: fast-forward ----------------------------------------------------------------
commit(origin, "README", "two", "second")
info = updater.check(clone)
check("a copy behind is told how far, and may update", info["behind"] == 1 and info["can_update"] and info["latest"], str(info))
r = updater.update_checkout(clone, say)
check("it is fast-forwarded", r["changed"] and open(os.path.join(clone, "README")).read() == "two")
check("without installing anything when no requirement changed", r["deps"] is False)
commit(origin, "requirements.txt", "flask\nrequests", "new dependency")
r = updater.update_checkout(clone, say)
check("a changed requirements.txt asks for the install", r["changed"] and r["deps"] is True)

# --- a rewritten public history -------------------------------------------------------------
origin, clone = world()
git(origin, "checkout", "-q", "--orphan", "new")
commit(origin, "README", "rewritten", "the whole history, once")
git(origin, "branch", "-q", "-D", "main")
git(origin, "branch", "-q", "-m", "main")
info = updater.check(clone)
check("a rewritten public history is diverged, and a clean copy of the public clone follows it",
      info["ahead"] >= 1 and info["behind"] >= 1 and info["can_update"], str(info))
r = updater.update_checkout(clone, say)
check("it is set to the public one", r["changed"] and open(os.path.join(clone, "README")).read() == "rewritten")

origin, clone = world()
git(origin, "checkout", "-q", "--orphan", "new")
commit(origin, "README", "rewritten", "the whole history, once")
git(origin, "branch", "-q", "-D", "main")
git(origin, "branch", "-q", "-m", "main")
with open(os.path.join(clone, "README"), "w") as f:
    f.write("my own edit")
info = updater.check(clone)
check("a copy with edits of its own is left alone, with the reason",
      not info["can_update"] and info["dirty"] and "edits of its own" in info["note"], str(info))
try:
    updater.update_checkout(clone, say)
    check("and update refuses", False)
except updater.UpdateError as e:
    check("and update refuses, in words", "edits of its own" in str(e) and open(os.path.join(clone, "README")).read() == "my own edit")

origin, clone = world(public=False)
git(origin, "checkout", "-q", "--orphan", "new")
commit(origin, "README", "rewritten", "the whole history, once")
git(origin, "branch", "-q", "-D", "main")
git(origin, "branch", "-q", "-m", "main")
info = updater.check(clone)
check("the maintainer's own clone (not the public repository) is never reset",
      not info["can_update"] and "not the public Voxie repository" in info["note"], str(info))
before = git(clone, "rev-parse", "HEAD")
try:
    updater.update_checkout(clone, say)
except updater.UpdateError:
    pass
check("its history is untouched", git(clone, "rev-parse", "HEAD") == before)

# --- no network ------------------------------------------------------------------------------
origin, clone = world()
git(clone, "remote", "set-url", "origin", os.path.join(work, "nowhere"))
info = updater.check(clone)
check("GitHub unreachable is an error in words, not a crash", info["error"].startswith("GitHub could not be reached"), str(info))

# --- dependencies ----------------------------------------------------------------------------
calls = []
updater.install_dependencies(ROOT, say, run=lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
check("dependencies go in with the environment's own pip",
      calls and calls[0][:4] == [sys.executable, "-m", "pip", "install"] and "-e" in calls[0] and "requirements.txt" in calls[0][-1])
try:
    updater.install_dependencies(ROOT, say, run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "no network\nERROR: no matching"))
    check("a failed install is an error", False)
except updater.UpdateError as e:
    check("a failed install is an error, with pip's last line", "no matching" in str(e))

# --- settings ---------------------------------------------------------------------------------
env = os.path.join(work, ".env")
open(env, "w").write("# my settings\nJUNK_PROVIDER=ai_brain\nJUNK_BLOCK_FILTER=off\nRUNPOD_API_KEY=abc\n")
done = updater.migrate_env(env)
text = open(env).read()
check("the old defaults move to auto, comments and other keys kept",
      "JUNK_PROVIDER=auto" in text and "JUNK_BLOCK_FILTER=auto" in text and "# my settings" in text
      and "RUNPOD_API_KEY=abc" in text and "VOXIE_ENV_VERSION=2" in text and len(done) == 2, text)
check("once: a second run does nothing", updater.migrate_env(env) == [])
open(env, "w").write("JUNK_PROVIDER=shadow\nJUNK_BLOCK_FILTER=on\n")
updater.migrate_env(env)
text = open(env).read()
check("a choice made on purpose stays", "JUNK_PROVIDER=shadow" in text and "JUNK_BLOCK_FILTER=on" in text, text)
check("no .env, nothing to do", updater.migrate_env(os.path.join(work, "none.env")) == [])

# --- the server -------------------------------------------------------------------------------
asked = []


def fake_run(cmd, **kw):
    asked.append(cmd)
    return subprocess.CompletedProcess(cmd, 0, "/srv/elsewhere\n" if "inspect" in " ".join(cmd) else "", "")


try:
    updater.update_server(ROOT, "root@1.2.3.4", say, run=fake_run)
    check("a server running Voxie from another folder is not touched", False)
except updater.UpdateError as e:
    check("a server running Voxie from another folder is not touched, and told how",
          "/srv/elsewhere" in str(e) and "by hand" in str(e) and not any("tar" in " ".join(c) for c in asked))

# --- run_update: the order and what it says ---------------------------------------------------
from unittest import mock  # noqa: E402
from libra_cli import main as cli  # noqa: E402


def run_update(changed=True, deps=False, watcher=True, busy=False, server="root@1.2.3.4", server_error=None):
    log, steps = [], []
    res = {"from": "aaa", "to": "bbb", "changed": changed, "deps": deps}
    with mock.patch.object(updater, "update_checkout", lambda root, on_line: res), \
            mock.patch.object(updater, "install_dependencies", lambda *a, **k: steps.append("deps")), \
            mock.patch.object(updater, "migrate_env", lambda p: ["the junk filter follows your OpenRouter key"]), \
            mock.patch.object(cli, "ensure_env_file", lambda: "/nope/.env"), \
            mock.patch.object(cli, "_watcher_installed", lambda: watcher), \
            mock.patch.object(cli, "_book_running", lambda: busy), \
            mock.patch.object(cli, "_load_config", lambda: {"SERVER_SSH": server} if server else {}), \
            mock.patch.object(cli.start_ghost, "callback", lambda: steps.append("watcher")), \
            mock.patch.object(updater, "update_server",
                              lambda root, target, on_line: (_ for _ in ()).throw(updater.UpdateError(server_error))
                              if server_error else steps.append("server")):
        done = cli.run_update(log.append)
    return done, steps, log


done, steps, log = run_update(deps=True)
check("an update installs what changed, restarts the watcher, then updates the server",
      steps == ["deps", "watcher", "server"] and done["watcher"] == "restarted" and done["server"] == "updated", str(steps))
check("and says the settings it moved", any(l.startswith("Settings:") for l in log), str(log))
done, steps, log = run_update(busy=True)
check("a book being narrated keeps the old watcher until it is done, and says so",
      "watcher" not in steps and done["watcher"] == "later" and any("voxie start-ghost" in l for l in log), str(log))
done, steps, log = run_update(changed=False)
check("nothing new: nothing restarted, nothing sent", steps == [] and done["server"] == "" and done["watcher"] == "")
done, steps, log = run_update(server=None)
check("no server set up: none is touched", "server" not in steps and done["server"] == "")
done, steps, log = run_update(server_error="Your server runs Voxie from /srv/x")
check("a server that cannot be updated does not fail the update, and is told",
      done["server"] == "skipped" and any("was not updated" in l and "/srv/x" in l for l in log), str(log))
done, steps, log = run_update(watcher=False)
check("no watcher installed: none is started", "watcher" not in steps)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
