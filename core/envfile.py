"""Read and update a .env file in place, keeping its comments and order.

`voxie init` writes the narration settings into the Mac's .env and the server
setup writes the gateway's; both files are mostly comments a reader relies on
(.env.example is the documentation), so a rewrite that dropped them would cost
more than it saved. Only uncommented KEY=value lines are touched; a key that is
not there yet is appended.
"""
import os
import re

_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def read(path):
    """{key: value} for every uncommented KEY=value line; {} if there is no file."""
    values = {}
    if not os.path.exists(path):
        return values
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = _LINE.match(line.rstrip("\n"))
            if m:
                values[m.group(1)] = _strip_comment(m.group(2))
    return values


def _strip_comment(value):
    # python-dotenv reads `KEY=abc #note` as "abc": a trailing comment needs a
    # space before the #, which no token or URL here contains.
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def update(path, changes, mode=0o600):
    """Set each key in `changes` (None removes the line). Creates the file if needed."""
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    pending = dict(changes)
    out = []
    for line in lines:
        m = _LINE.match(line)
        if m and m.group(1) in pending:
            value = pending.pop(m.group(1))
            if value is not None:
                out.append(f"{m.group(1)}={value}")
            continue
        out.append(line)
    for key, value in pending.items():
        if value is not None:
            out.append(f"{key}={value}")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)
