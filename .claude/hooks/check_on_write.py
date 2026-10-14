#!/usr/bin/env python3
"""Check any file Claude writes, and format it if this repo ever adopts a formatter.

The reference version of this hook ran Prettier, because that repo asks for
Prettier. **This repo has no formatter and no formatter config** -- no
pyproject.toml, no ruff.toml, no black. Pointing a formatter at it anyway would
reformat a codebase that never chose a style, turning every small edit into a
large diff and contradicting CLAUDE.md's instruction to write code that reads
like the code around it. Adopting one is a real decision, and the user's to make.

So this does the thing this repo does need. Editing Python through sed, heredocs
and patch scripts -- which is how a lot of this codebase gets changed -- breaks
syntax in ways that stay invisible until the next engine run, and an engine run
costs a GPU boot. Parsing the file the moment it is written closes that gap.

Never blocks. A file that does not parse is usually a file mid-edit, and
refusing the write would be worse than reporting it: the next edit is probably
the one that fixes it.
"""
import ast
import json
import os
import shutil
import subprocess
import sys

CHECKED = {".py"}

# Filled in if this repo ever adopts a formatter. Add the tool and the
# extensions it owns; the syntax check below stays either way.
#   ("ruff",  ["ruff", "format"],  {".py"})
#   ("black", ["black", "-q"],     {".py"})
FORMATTERS: list[tuple[str, list[str], set[str]]] = []


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    path = (payload.get("tool_input") or {}).get("file_path")
    if not path:
        return 0

    root = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
    root = os.path.realpath(root)
    real = os.path.realpath(path)

    # Only files inside the project. A scratchpad file is not ours to touch.
    if not real.startswith(root + os.sep):
        return 0
    ext = os.path.splitext(real)[1]
    if not os.path.isfile(real):
        return 0

    for name, cmd, extensions in FORMATTERS:
        if ext in extensions and shutil.which(cmd[0]):
            try:
                subprocess.run(cmd + [real], cwd=root, capture_output=True,
                               timeout=60, check=False)
            except Exception:
                pass  # a formatter failure must never block a write
            break

    if ext in CHECKED:
        try:
            with open(real, "rb") as handle:
                ast.parse(handle.read(), filename=real)
        except SyntaxError as e:
            rel = os.path.relpath(real, root)
            print(f"Note: {rel} does not parse yet — {e.msg} at line {e.lineno}.",
                  file=sys.stderr)
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
