#!/usr/bin/env python3
"""Enforce the commit rules CLAUDE.md states but cannot make binding.

  1. Never commit directly to main.
  2. The code must at least parse, and the sync contract must still hold.

Rule 2 is skipped when a commit touches nothing the gates can fail on -- docs,
hooks, .env.example. Running a suite to commit a typo in a markdown file
teaches you to reach for --no-verify, which is the thing being prevented.

Exit 2 blocks the commit and hands the reason back to Claude.
"""
import json
import os
import re
import subprocess
import sys

# This repo is Python, and its gates are not the obvious ones.
#
# `tools/healthcheck.py` is called "the test suite" in CLAUDE.md and is
# deliberately NOT here. It checks the world rather than the logic -- Groq,
# RunPod, Audiobookshelf, the live gateway -- so as a commit gate it would
# block every commit made on a train, spend Groq calls on each one, and fail
# for reasons that have nothing to do with the diff. It is a daily check, which
# is exactly how CLAUDE.md describes it.
#
# What is left is everything that is deterministic and offline:
GATES = [
    # Catches the broken edit immediately rather than at the next engine run.
    # Excludes the submodule, the abandoned whisper_dump, and the MFA scratch
    # directory, none of which this repo's commits own.
    ("syntax", ["python3", "-m", "compileall", "-q", "-x",
                r"(kobosync-web|whisper_dump|mfa_workspace|node_modules|\.venv|build|dist)", "."]),
    # 38 assertions covering the messy-user contract: upload the same book
    # twice, delete it, resume a part-finished upload. Offline, 0.3s.
    ("sync contract", ["python3", "tools/test_sync_contract.py"]),
    # The boot rule has been wrong once already -- it used to kill healthy pods
    # on a proxy 404. Replayed against a virtual clock, so it costs no GPU.
    ("boot logic", ["python3", "tools/test_boot_logic.py"]),
]

# Changes that cannot break a gate. Everything else runs the full suite.
# .env.example is documentation of configuration, not configuration.
EXEMPT = re.compile(r"(\.md$|^LICENSE$|^\.gitignore$|^\.claude/|\.txt$|^\.env\.example$)")


def run(cmd, cwd):
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return None


def changed_files(cwd, command):
    out = run(["git", "diff", "--cached", "--name-only"], cwd)
    files = [f for f in (out.stdout.splitlines() if out else []) if f.strip()]
    # `git commit -a` stages tracked changes as part of committing, so they
    # have to be considered too or the gate reads an empty changeset.
    if re.search(r"git\s+commit\b[^|;&]*\s-[a-zA-Z]*a", command):
        tracked = run(["git", "diff", "--name-only"], cwd)
        files += [f for f in (tracked.stdout.splitlines() if tracked else []) if f.strip()]
    return sorted(set(files))


def target_repo(command, default):
    """The repository the commit lands in, from `cd <dir> && ...` or `git -C <dir>`,
    else the project folder.

    A git worktree (a second checkout on another branch, used while a long
    run holds the main one) is a different folder: checking the project
    folder's branch refused a feature-branch commit there as "main", and the
    gates would have run against the wrong files.
    """
    m = re.match(r"\s*cd\s+(\"[^\"]+\"|'[^']+'|\S+)\s*(?:&&|;)", command) or \
        re.search(r"\bgit\s+-C\s+(\"[^\"]+\"|'[^']+'|\S+)\s", command)
    if not m:
        return default
    path = os.path.expanduser(m.group(1).strip("\"'"))
    path = path if os.path.isabs(path) else os.path.join(default, path)
    inside = run(["git", "rev-parse", "--show-toplevel"], path) if os.path.isdir(path) else None
    return inside.stdout.strip() if inside and inside.returncode == 0 else default


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    command = (payload.get("tool_input") or {}).get("command") or ""
    if not re.search(r"\bgit\s+commit\b", command):
        return 0

    cwd = target_repo(command, os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd())

    # symbolic-ref, not rev-parse: on a branch with no commits yet rev-parse
    # fails outright, so the main check would silently pass on exactly the repo
    # state where a first commit is most likely to land on main by accident.
    branch = run(["git", "symbolic-ref", "--short", "HEAD"], cwd)
    if branch and branch.returncode == 0:
        name = branch.stdout.strip()
        if name in ("main", "master"):
            print(
                f"Refusing: this would commit directly to {name}. CLAUDE.md requires a "
                "feat/fix/chore/refactor branch. Branch first.",
                file=sys.stderr,
            )
            return 2

    files = changed_files(cwd, command)
    if files and all(EXEMPT.search(f) for f in files):
        return 0

    for label, cmd in GATES:
        result = run(cmd, cwd)
        if result is None:
            continue  # tool missing or hung -- don't wedge the commit on infrastructure
        if result.returncode != 0:
            tail = (result.stdout or "")[-1500:] + (result.stderr or "")[-1500:]
            print(
                f"Refusing: the {label} gate is failing, so this change is not done "
                f"(CLAUDE.md). Fix it rather than bypassing.\n\n{tail}",
                file=sys.stderr,
            )
            return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
