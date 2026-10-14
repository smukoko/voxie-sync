"""Read and write KOReader's settings files exactly.

KOReader stores settings as a Lua chunk, `return { ["key"] = value, ... }`,
written by its own dump (frontend/dump.lua): string and integer keys in
brackets, and values that are tables, strings, numbers or booleans. Editing
those with regular expressions is what made the installer skip an existing
opds.lua outright rather than add its catalogue to it. This parses that subset
into Python and writes it back in the same shape, so a change touches only the
keys it means to.
"""
import re

_TOKEN = re.compile(r"""
    (?P<ws>\s+|--[^\n]*)
  | (?P<str>"(?:\\[\s\S]|[^"\\])*")
  | (?P<num>-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)
  | (?P<word>[A-Za-z_][A-Za-z_0-9]*)
  | (?P<sym>[{}\[\]=,;])
""", re.X)

# KOReader's dump writes a newline inside a string as a backslash followed by
# a real line break, which Lua reads as a newline.
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "'": "'", "0": "\0", "\n": "\n"}


def _tokens(text):
    pos = 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise ValueError(f"cannot parse Lua settings at offset {pos}: {text[pos:pos + 30]!r}")
        pos = m.end()
        if m.lastgroup != "ws":
            yield m.lastgroup, m.group()


def _unquote(s):
    body = s[1:-1]
    return re.sub(r"\\([\s\S])", lambda m: _ESCAPES.get(m.group(1), m.group(1)), body)


def loads(text):
    toks = list(_tokens(text))
    i = 0

    def peek():
        return toks[i] if i < len(toks) else (None, None)

    def take(expected=None):
        nonlocal i
        kind, val = toks[i]
        if expected and val != expected:
            raise ValueError(f"expected {expected!r}, got {val!r}")
        i += 1
        return kind, val

    def value():
        kind, val = take()
        if kind == "str":
            return _unquote(val)
        if kind == "num":
            return float(val) if any(c in val for c in ".eE") else int(val)
        if kind == "word":
            if val in ("true", "false"):
                return val == "true"
            if val == "nil":
                return None
            raise ValueError(f"unexpected {val!r}")
        if val == "{":
            table, n = {}, 1
            while peek()[1] != "}":
                if peek()[1] == "[":
                    take("[")
                    key = value()
                    take("]")
                    take("=")
                    table[key] = value()
                elif peek()[0] == "word" and i + 1 < len(toks) and toks[i + 1][1] == "=":
                    key = take()[1]
                    take("=")
                    table[key] = value()
                else:
                    table[n] = value()
                    n += 1
                if peek()[1] in (",", ";"):
                    take()
            take("}")
            return table
        raise ValueError(f"unexpected {val!r}")

    if peek()[1] == "return":
        take()
    result = value()
    return result


def _quote(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _key(k):
    return f"[{k}]" if isinstance(k, int) and not isinstance(k, bool) else f"[{_quote(str(k))}]"


def _dump(v, indent):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return _quote(v)
    if isinstance(v, dict):
        if not v:
            return "{}"
        pad = "    " * (indent + 1)
        # Integer keys in order, then string keys sorted: KOReader's own order.
        keys = sorted((k for k in v if isinstance(k, int)), key=int) + \
            sorted(k for k in v if not isinstance(k, int))
        lines = [f"{pad}{_key(k)} = {_dump(v[k], indent + 1)}," for k in keys if v[k] is not None]
        return "{\n" + "\n".join(lines) + "\n" + "    " * indent + "}"
    raise TypeError(f"cannot write {type(v).__name__} to a Lua settings file")


def dumps(table, header=None):
    return (f"-- {header}\n" if header else "") + "return " + _dump(table, 0) + "\n"
