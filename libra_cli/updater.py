"""Update Voxie from the setup page or `voxie update`: the checkout, its
dependencies, the settings that changed meaning, and the server.

In the first stage Voxie changes often, and the way to update was to know to run
`git pull` in ~/voxie. On 2026-10-22 that failed for the first install's user
anyway ("divergent branches": the public history had been rewritten). So:

  - the checkout is fast-forwarded when it can be;
  - a copy that diverged from the PUBLIC repository (a rewritten history) and has
    no edits of its own is set to the public one, since nothing there is the user's;
  - a copy with edits, or one that is not the public repository's clone (the
    maintainer's own), is never touched, and the reason is said;
  - dependencies are installed only when requirements.txt or setup.py changed;
  - .env settings whose default changed are moved once (core/envfile, VOXIE_ENV_VERSION);
  - the server (when Voxie set one up under /opt/voxie) gets the new code and a
    rebuild; its .env and data stay.

Every step takes `run` and `on_line` so it is tested without a network.
"""
import os
import subprocess
import sys

PUBLIC_REPO = "smukoko/voxie-sync"
ENV_VERSION = 2


class UpdateError(Exception):
    """Something the person can act on; the message says what."""


def _git(root, *args, run=subprocess.run, timeout=60):
    return run(["git", "-C", root, *args], capture_output=True, text=True, timeout=timeout)


def _out(r):
    return (r.stdout or "").strip()


def is_public_clone(root, run=subprocess.run):
    """True when `origin` is the public Voxie repository: a user's install, not
    the maintainer's working copy (whose history is its own)."""
    # The URL as written, not as `remote get-url` rewrites it through insteadOf.
    return PUBLIC_REPO in _out(_git(root, "config", "--get", "remote.origin.url", run=run))


def check(root, run=subprocess.run):
    """Ask GitHub what is new. Returns a dict:

    current / latest   short commit ids; `date` of the latest
    behind / ahead     commits only they / only this copy has
    dirty              True when tracked files here were edited
    can_update         True when update() will do something and may
    note               why not, in words, when it will not
    error              set (and the rest empty) when GitHub could not be asked
    """
    info = {"current": "", "latest": "", "behind": 0, "ahead": 0, "dirty": False,
            "can_update": False, "note": "", "error": "", "date": ""}
    try:
        fetched = _git(root, "fetch", "--quiet", "origin", run=run, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        info["error"] = f"GitHub could not be reached ({type(e).__name__})"
        return info
    if fetched.returncode != 0:
        info["error"] = "GitHub could not be reached: " + (fetched.stderr.strip().splitlines() or ["fetch failed"])[-1]
        return info
    branch = _out(_git(root, "rev-parse", "--abbrev-ref", "HEAD", run=run))
    upstream = f"origin/{branch if branch != 'HEAD' else 'main'}"
    info["current"] = _out(_git(root, "rev-parse", "--short", "HEAD", run=run))
    info["latest"] = _out(_git(root, "rev-parse", "--short", upstream, run=run))
    info["date"] = _out(_git(root, "log", "-1", "--format=%cs", upstream, run=run))
    counts = _out(_git(root, "rev-list", "--left-right", "--count", f"HEAD...{upstream}", run=run)).split()
    if len(counts) == 2:
        info["ahead"], info["behind"] = int(counts[0]), int(counts[1])
    info["dirty"] = bool(_out(_git(root, "status", "--porcelain", "--untracked-files=no", run=run)))
    if not info["behind"]:
        return info
    public = is_public_clone(root, run=run)
    if info["ahead"] == 0:
        info["can_update"] = True
    elif info["dirty"]:
        info["note"] = ("This copy of Voxie has edits of its own, so it is left alone. "
                        f"In {root}: git stash, then update again.")
    elif public:
        info["can_update"] = True          # a rewritten public history, nothing here is the user's
    else:
        info["note"] = (f"This is not the public Voxie repository's clone and has commits of its own; "
                        f"update it with git ({root}).")
    return info


def _changed(root, old, new, names, run):
    r = _git(root, "diff", "--name-only", f"{old}..{new}", run=run)
    return any(n in names for n in _out(r).splitlines())


def update_checkout(root, on_line=print, run=subprocess.run):
    """Bring the checkout to the latest. Returns {"from", "to", "changed": bool, "deps": bool}.

    Raises UpdateError when it must not, or cannot.
    """
    info = check(root, run=run)
    if info["error"]:
        raise UpdateError(info["error"])
    if not info["behind"]:
        on_line(f"Voxie is up to date ({info['current']}).")
        return {"from": info["current"], "to": info["current"], "changed": False, "deps": False}
    if not info["can_update"]:
        raise UpdateError(info["note"])
    old = _out(_git(root, "rev-parse", "HEAD", run=run))
    branch = _out(_git(root, "rev-parse", "--abbrev-ref", "HEAD", run=run))
    upstream = f"origin/{branch if branch != 'HEAD' else 'main'}"
    if info["ahead"] == 0:
        on_line(f"Updating {info['current']} to {info['latest']} ({info['date']}) ...")
        r = _git(root, "merge", "--ff-only", upstream, run=run)
    else:
        on_line("Voxie's public history was rewritten; this copy follows it ...")
        r = _git(root, "reset", "--hard", upstream, run=run)
    if r.returncode != 0:
        raise UpdateError("git could not update: " + (r.stderr.strip().splitlines() or ["?"])[-1])
    new = _out(_git(root, "rev-parse", "HEAD", run=run))
    # After a reset the old commit may no longer be in the new history: diff the trees.
    deps = _changed(root, old, new, ("requirements.txt", "setup.py"), run)
    return {"from": info["current"], "to": info["latest"], "changed": True, "deps": deps}


def install_dependencies(root, on_line=print, run=subprocess.run):
    """pip install -e . -r requirements.txt in the environment running Voxie."""
    on_line("Installing what changed (this takes a minute) ...")
    r = run([sys.executable, "-m", "pip", "install", "--quiet", "-e", root, "-r",
             os.path.join(root, "requirements.txt")], capture_output=True, text=True)
    if r.returncode != 0:
        raise UpdateError("pip could not install the new requirements: "
                          + (r.stderr.strip().splitlines() or ["?"])[-1])


def migrate_env(env_path):
    """Move settings whose meaning changed, once. Returns the list of what was done.

    v2 (2026-10-22): the junk filter and its paragraph filter became "auto": JEV
    for someone with an OpenRouter key, the local classifier for someone without.
    Only the old defaults (ai_brain / off, as .env.example wrote them) are moved;
    a choice made on purpose, shadow or jev, stays.
    """
    from core import envfile
    if not os.path.exists(env_path):
        return []
    env = envfile.read(env_path)
    if int(env.get("VOXIE_ENV_VERSION") or 1) >= ENV_VERSION:
        return []
    changes, done = {"VOXIE_ENV_VERSION": str(ENV_VERSION)}, []
    if (env.get("JUNK_PROVIDER") or "ai_brain") == "ai_brain":
        changes["JUNK_PROVIDER"] = "auto"
        done.append("the junk filter follows your OpenRouter key (JEV with one, local without)")
    if (env.get("JUNK_BLOCK_FILTER") or "off") == "off":
        changes["JUNK_BLOCK_FILTER"] = "auto"
        done.append("the paragraph filter is on with an OpenRouter key")
    envfile.update(env_path, changes)
    return done


def update_server(root, target, on_line=print, run=subprocess.run):
    """New code and a rebuild on the Voxie server at /opt/voxie. Its .env and data stay.

    A server whose gateway runs from another folder (set up by hand) is left alone.
    """
    from libra_cli import server_setup
    folder = server_setup.stack_dir(target, run=run)
    if folder and folder != server_setup.SERVER_DIR:
        raise UpdateError(f"Your server runs Voxie from {folder}, not {server_setup.SERVER_DIR}: "
                          "update it there by hand (git fetch, git merge --ff-only origin/main, "
                          "docker compose up -d --build).")
    on_line(f"Sending the new code to {target.split('@')[-1]} ...")
    server_setup.push_code(root, target, run=run)
    on_line("Rebuilding the server (a minute or two) ...")
    cmd = server_setup.ssh_command(target, *server_setup._sudo(target), "sh", "-c",
                                   f"'cd {server_setup.SERVER_DIR} && docker compose up -d --build "
                                   "&& for i in $(seq 1 30); do curl -fsS http://127.0.0.1:3000/health "
                                   ">/dev/null && exit 0; sleep 2; done; exit 1'")
    rc, _ = server_setup._stream(cmd, "", on_line)
    if rc != 0:
        raise UpdateError("the server did not come back: on it, docker compose logs librarian")
    on_line("The server is up to date.")
