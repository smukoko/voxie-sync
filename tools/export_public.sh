#!/bin/bash
# Build the public repository: this tree at HEAD, one fresh commit, no history.
#
#   tools/export_public.sh ~/Projects/libra-sync-public
#
# Once the public repository exists, its history must not be replaced (people
# clone it). Then export onto a clone of it: the same tree, filtered and
# leak-checked the same way, lands as one new commit on top of what is there.
#
#   tools/export_public.sh --onto ~/Projects/voxie-public "What changed, in a line"
#
# Why a fresh history: earlier commits carry book text, operational details and
# keys that were rotated but are still in the objects. Only tracked files at
# HEAD are exported (git archive), so nothing gitignored -- library/, voices/,
# .env, training_lab/ -- can come along.
#
# It never pushes. It refuses to finish if the result still contains the live
# gateway address, a home directory, or anything shaped like an API key.
set -euo pipefail
cd "$(dirname "$0")/.."

onto=""
if [ "${1:-}" = "--onto" ]; then
    onto="${2:?usage: tools/export_public.sh --onto <clone of the public repo> \"commit message\"}"
    message="${3:?usage: tools/export_public.sh --onto <clone of the public repo> \"commit message\"}"
    if [ ! -d "$onto/.git" ]; then
        echo "refusing: $onto is not a git clone of the public repository" >&2; exit 1
    fi
    if [ -n "$(git -C "$onto" status --porcelain)" ]; then
        echo "refusing: $onto has uncommitted changes" >&2; exit 1
    fi
    work="$(mktemp -d)"
    trap 'rm -rf "$work"' EXIT
    dest="$work/export"
else
    dest="${1:?usage: tools/export_public.sh <empty destination folder>}"
    if [ -e "$dest" ] && [ -n "$(ls -A "$dest" 2>/dev/null)" ]; then
        echo "refusing: $dest exists and is not empty" >&2; exit 1
    fi
fi
if [ -n "$(git status --porcelain --ignore-submodules=none)" ]; then
    echo "refusing: commit or stash your changes first (the export is HEAD, not the working tree)" >&2; exit 1
fi

# Not published: private operations and strategy. LESSONS.md is the evidence
# behind CLAUDE.md's rules, but it quotes books and names the live server; a
# scrubbed version can follow.
EXCLUDE=(
    docs/LESSONS.md
    docs/history
    docs/LAUNCH_PLAN.md
    docs/COMPETITION.md
    docs/pricing
    docs/grants
    .gitmodules
)

mkdir -p "$dest"
git archive HEAD | tar -x -C "$dest"
for p in "${EXCLUDE[@]}"; do rm -rf "${dest:?}/$p"; done

# CLAUDE.md's last section is this machine's live state (server, account,
# template); everything above it is the project's conventions.
python3 - "$dest/CLAUDE.md" <<'EOF'
import re, sys
p = sys.argv[1]
s = open(p).read()
s = s.split("\n## Where things stand", 1)[0].rstrip() + "\n"
# A line ending in <!-- private --> is this machine's: the maintainer's own
# voices, server, device and files. Useful here, nothing to a reader there.
s = "\n".join(l for l in s.split("\n") if not l.rstrip().endswith("<!-- private -->"))
# "Open items" is the private to-do list (keys to rotate, the trademark
# search, the maintainer's own device); the sections after it are conventions.
if "\n## Open items" in s:
    head, rest = s.split("\n## Open items", 1)
    after = rest.split("\n## ", 1)
    s = head + ("\n## " + after[1] if len(after) > 1 else "\n")
s = s.replace("`docs/LESSONS.md` (the old CLAUDE.md, verbatim) and in git history",
              "the commit messages")
s = s.replace("One line each; the evidence is in `docs/LESSONS.md` or the commit that made the rule.",
              "One line each; the evidence is in the commit that made the rule.")
open(p, "w").write(s)
EOF

# --- Placeholders the maintainer has to fill in -------------------------------
# The Code of Conduct needs a real contact before anyone can rely on it.
# Only that file: this script itself is exported and names the placeholder.
if grep -q "\[INSERT CONTACT METHOD\]" "$dest/.github/CODE_OF_CONDUCT.md" 2>/dev/null; then
    echo "Export stopped: .github/CODE_OF_CONDUCT.md still says [INSERT CONTACT METHOD]." >&2
    echo "Put the address that receives conduct reports there, commit, and export again." >&2
    exit 1
fi

# The referral links (core/links.py, and the README which repeats them) hold a
# placeholder until the maintainer has the real codes: a public link that 404s
# or credits nobody is worse than none.
if grep -q "ref=REFERRAL_CODE_HERE" "$dest/core/links.py" "$dest/README.md" "$dest/DEPLOY.md" 2>/dev/null; then
    echo "Export stopped: core/links.py, README.md or DEPLOY.md still says REFERRAL_CODE_HERE." >&2
    echo "Put the Hetzner and RunPod referral links in all three, commit, and export again." >&2
    exit 1
fi

# --- Dropped files ------------------------------------------------------------
# `git archive` exports tracked files whatever .gitignore says; the commit below
# is made with `git add -A`, which honours it. A tracked file that .gitignore
# also matches therefore silently goes missing from the public repo: on
# 2026-10-22 that was the junk classifier's model, and every public install ran
# without a junk filter. Ask git what it would drop, in a throwaway index.
probe="$(mktemp -d)"
git --git-dir="$probe" init -q
git --git-dir="$probe" --work-tree="$dest" add -A
dropped="$(git --git-dir="$probe" --work-tree="$dest" ls-files --others --ignored --exclude-standard)"
rm -rf "$probe"
if [ -n "$dropped" ]; then
    echo "Export stopped: .gitignore would drop these tracked files from the public repo:" >&2
    echo "$dropped" | sed 's/^/    /' >&2
    echo "Add a '!path' exception to .gitignore, commit, and export again." >&2
    exit 1
fi

# --- Leak check ---------------------------------------------------------------
if ! python3 tools/leak_scan.py "$dest"; then
    echo "Export stopped: fix the above in the source repo, commit, and export again." >&2
    exit 1
fi

if [ -n "$onto" ]; then
    # Everything but .git is replaced, so a file deleted here is deleted there.
    rsync -a --delete --exclude .git "$dest"/ "$onto"/
    cd "$onto"
    git add -A
    if git diff --cached --quiet; then
        echo "Nothing changed since the last export; no commit made."
        exit 0
    fi
    git -c commit.gpgsign=false commit -q -m "$message"
    echo "✅ One new commit on $(git rev-parse --abbrev-ref HEAD) in $onto: $(git diff --stat HEAD~1 | tail -1)"
    echo "   Nothing was pushed. Review it (git -C $onto show --stat), then push by hand."
    exit 0
fi
cd "$dest"
git init -q -b main
git add -A
git -c commit.gpgsign=false commit -q -m "Voxie: EPUB to audiobook, in sync with your e-reader

First public release. AGPL-3.0-or-later; the map format spec (docs/SYNC_FORMAT.md)
is CC-BY-4.0. Contributions are made under the CLA (.github/CLA.md)."
echo "✅ $(git ls-files | wc -l | tr -d ' ') files in one commit at $dest"
echo "   Nothing was pushed. Review it, then create the GitHub repo and push by hand."
