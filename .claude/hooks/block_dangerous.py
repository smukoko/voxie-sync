#!/usr/bin/env python3
"""Refuse the commands CLAUDE.md already forbids in prose.

Deliberately narrow. A hook that fires on work you actually do is a hook you
learn to route around, so this blocks only things with no legitimate use in
this repo -- not every command that looks alarming. `rm -rf node_modules` and a
force-push to your own feature branch both stay allowed, because CLAUDE.md
allows them.

Exit 2 blocks the call and returns the message to Claude as feedback.
"""
import json
import os
import re
import sys

MAIN = r"(main|master)"

RULES: list[tuple[str, str]] = [
    (
        rf"git\s+push\b[^|;&]*--force(-with-lease)?\b[^|;&]*\b{MAIN}\b|git\s+push\b[^|;&]*\b{MAIN}\b[^|;&]*--force",
        "Force-pushing to main is forbidden (CLAUDE.md: no history rewrites on main).",
    ),
    (
        rf"git\s+push\b[^|;&]*\s-f\b[^|;&]*\b{MAIN}\b",
        "Force-pushing to main is forbidden (CLAUDE.md: no history rewrites on main).",
    ),
    (
        r"--no-verify\b",
        "--no-verify is forbidden (CLAUDE.md). If a gate is failing, fix the gate.",
    ),
    (
        rf"git\s+reset\s+--hard\b[^|;&]*\b(origin/)?{MAIN}\b",
        "Hard-resetting main discards work irrecoverably. Branch first.",
    ),
    (
        rf"git\s+(rebase|filter-branch|filter-repo)\b[^|;&]*\b{MAIN}\b",
        "Rewriting main's history is forbidden (CLAUDE.md).",
    ),
    (
        r"chmod\s+(-R\s+)?777\b",
        "chmod 777 makes a path world-writable. Use a narrower mode.",
    ),
    (
        r"curl\b[^|]*\|\s*(sudo\s+)?(ba)?sh\b",
        "Piping a downloaded script straight into a shell runs unreviewed code. Download, read, then run.",
    ),
    # ---- Voxie specific, below this line ----------------------------
    #
    # CLAUDE.md: "Never rewrite history, force-push, or delete branches without
    # explicit confirmation." Deleting main is the unrecoverable member of that
    # set, and unlike the others it has no legitimate use at all.
    (
        rf"git\s+branch\b[^|;&]*\s-[dD]\b[^|;&]*\b{MAIN}\b|git\s+push\b[^|;&]*--delete\b[^|;&]*\b{MAIN}\b",
        "Deleting main is forbidden (CLAUDE.md: main stays a pristine, always-working reference).",
    ),
    # CLAUDE.md names this exact accident: "Secrets never get committed -- check
    # anything touching training_lab/ extra carefully, since it currently has
    # plaintext API keys on disk (gitignored, but easy to accidentally git add -f)."
    # -f is the only way past .gitignore, so it is the only thing to catch.
    (
        r"git\s+add\b[^|;&]*(\s-[a-zA-Z]*f|--force)\b[^|;&]*(\.env\b|training_lab)",
        "git add -f on .env or training_lab/ would commit plaintext API keys past .gitignore "
        "(CLAUDE.md). If a file there genuinely belongs in git, remove the secret first.",
    ),
]

# rm -rf is only blocked against paths that would take the machine, the home
# directory, the reader's device, or the library with them. Clearing a build
# dir, or one book's folder under library/, is ordinary work.
#
# Parsed rather than matched with one regex: the regex version required a space
# both before and after the flags, so `rm -rf /` -- the exact case it existed
# for -- slipped straight through while the tests looked plausible.
DEADLY_TARGETS = {"/", "/*", "~", "~/", "~/*", "$HOME", "$HOME/", "$HOME/*"}

# Matched on the whole target, never a prefix, so `rm -rf library/audiobooks/Some Book`
# stays allowed while `rm -rf library` does not. library/ holds database.db --
# every sync_node ever generated, and the Mac is the master copy.
PROTECTED_DIRS = {"library", "./library", "library/", "./library/"}

# The Kobo is a mounted volume with 200-odd personal books on it that this
# project did not put there and cannot restore. Nothing in a build or a test
# needs to delete from it.
KOBO_PATH = re.compile(r"/Volumes/[^/\s]+/?(\s|$)|/Volumes/[^\s]*(KEbooks|\.kobo)", re.IGNORECASE)


def catastrophic_rm_reason(command: str) -> str | None:
    """Return why this rm is refused, or None if it is ordinary."""
    home = os.path.expanduser("~")
    for segment in re.split(r"[|;&]+", command):
        tokens = segment.split()
        if "rm" not in tokens:
            continue
        rest = tokens[tokens.index("rm") + 1 :]
        if not any("r" in tok.lower() for tok in rest if tok.startswith("-")):
            continue
        for target in (tok for tok in rest if not tok.startswith("-")):
            if target in DEADLY_TARGETS:
                return "this rm would delete the filesystem root or your home directory."
            if target.rstrip("/") in ("", "~", "$HOME", home):
                return "this rm would delete the filesystem root or your home directory."
            if target in PROTECTED_DIRS:
                return ("this would delete library/ whole, taking database.db and every "
                        "sync_node with it. Delete a single book's folder instead.")
            if KOBO_PATH.search(target):
                return ("this would delete from a mounted volume that may be the Kobo. "
                        "Its personal books are not ours to remove.")
    return None


# A heredoc body is data -- a commit message, a file being written, a doc that
# discusses these very commands -- not something the shell executes. Scanning it
# refused this repo's own commit describing the rm bug below, and would refuse
# any attempt to document a dangerous command at all.
#
# The exception is a heredoc fed to a shell, where the body IS executed. This
# does mean a body fed to some other interpreter is no longer scanned: accepted
# deliberately, because this is a guardrail against slips, not a sandbox, and a
# guardrail that fires on ordinary writing is one you learn to disable.
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1\s*\n(.*?)\n\2", re.DOTALL)
SHELL_FED = re.compile(r"(^|\|)\s*(sudo\s+)?(ba|z)?sh\b")


def strip_heredoc_bodies(command: str) -> str:
    if SHELL_FED.search(command):
        return command
    return HEREDOC.sub(lambda m: f"<<{m.group(2)}\n(body elided)\n{m.group(2)}", command)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    command = (payload.get("tool_input") or {}).get("command") or ""
    if not command:
        return 0
    command = strip_heredoc_bodies(command)

    reason = catastrophic_rm_reason(command)
    if reason:
        print(f"Refusing: {reason}", file=sys.stderr)
        return 2

    for pattern, message in RULES:
        if re.search(pattern, command, re.IGNORECASE):
            print(f"Refusing: {message}", file=sys.stderr)
            return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
