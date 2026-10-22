"""The page a user sees when a book's run fails (user, 2026-10-22).

The watcher runs in the background and its log is read by nobody; a
notification says only *that* it failed. A first-time user then has nothing to
act on. So a failed drop opens one local HTML page: what stopped, the end of
the log for that book, and two ways to get help (a prefilled GitHub issue, or
the whole report copied for an AI assistant or a forum). It is a file in the
log folder, never a hosted page: nothing leaves the machine until the user
chooses to paste it somewhere, which is why the log is scrubbed first.

stdlib only: the watcher imports this, and must not load the engine's stack.
"""
import html
import os
import platform
import re
import sys
import time
import urllib.parse

from core import platform_support

ISSUES_URL = "https://github.com/smukoko/voxie-sync/issues/new?template=bug.yml"
REPORT_NAME = "voxie-last-failure.html"
# A backlog of failed books at start must not open a tab each: the page is one
# file, rewritten, and opened again only after this many seconds. Counted from
# the last time it was *opened* (a stamp file per kind), not from its last
# write: a watcher that cannot start is restarted by launchd every few seconds
# and rewrites the page each time, which must not hide the next real failure.
REOPEN_AFTER = 60
MAX_LINES = 120

# Causes the engine already names in its own words (librerian_engine_v3's
# prerequisite check), said plainly. Add a line here only for a message that
# really occurs.
KNOWN_CAUSES = [
    ("Docker is installed but not running",
     "Docker Desktop is not running. Open Docker Desktop, wait until it says it is running, "
     "then drop the book into Kobo_Dropzone again."),
    ("No space left on device",
     "The disk is full. A book needs about twice its audio in free space; free some up and drop it again."),
]

_SECRETS = [
    # Names that hold a secret, in KEY=value, KEY: value or "key": "value" form.
    (re.compile(r"(?i)((?:api[_-]?key|token|secret|password|authorization)[\"']?\s*[=:]\s*[\"']?(?:(?:bearer|basic)\s+)?)[^\s\"',]+"),
     r"\1[hidden]"),
    # Key shapes: RunPod, OpenRouter, Groq, Hugging Face.
    (re.compile(r"\b(?:rpa_|sk-or-|gsk_|hf_)[A-Za-z0-9_\-]{8,}"), "[hidden]"),
]


def scrub(text, home=None):
    """Take secrets and the user's home folder out of a log before it is shown or shared."""
    home = home or os.path.expanduser("~")
    if home and home != "/":
        text = text.replace(home, "~")
    for pattern, repl in _SECRETS:
        text = pattern.sub(repl, text)
    return text


def excerpt_for(log_path, filename, max_lines=MAX_LINES):
    """The lines of the watcher's log from this book's last start, at most max_lines (the end of them).

    The log holds the engine's output too, since the engine inherits the
    watcher's stdout. Empty when the log is unreadable (a watcher run in a
    terminal has no log file; the terminal has it).
    """
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 400_000))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    marker = f"Detected {filename}"
    at = text.rfind(marker)
    if at != -1:
        text = text[at:]
    return text.splitlines()[-max_lines:]


def known_cause(lines):
    joined = "\n".join(lines)
    for needle, plain in KNOWN_CAUSES:
        if needle in joined:
            return plain
    return None


def _version():
    try:
        from importlib.metadata import version
        return version("voxie-sync")
    except Exception:
        return "unknown"


def plain_report(book, why, lines):
    """The text to paste into an issue, a forum or an AI chat."""
    return scrub("\n".join([
        f"Voxie {_version()} on {platform.platform()}, Python {sys.version.split()[0]}",
        f"Book: {book}",
        f"What stopped: {why}",
        "",
        "End of the log:",
        *lines,
    ]))


def build_html(book, why, lines, headline="A run did not finish"):
    cause = known_cause(lines)
    report = plain_report(book, why, lines)
    log = html.escape(scrub("\n".join(lines))) or "(no log file: the watcher was started in a terminal; look there)"
    issue = ISSUES_URL + "&" + urllib.parse.urlencode({"title": f"A run failed: {book}"[:120]})
    ask = ("My Voxie narration run failed. Here is its report. What went wrong, and what do I do?\n\n"
           + report)
    cause_html = (f'<div class="cause"><b>Most likely cause</b><p>{html.escape(cause)}</p></div>'
                  if cause else "")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Voxie: a run failed</title>
<style>
:root{{--bg:#fff;--fg:#1c1c1e;--mut:#6b6b70;--line:#e2e2e5;--amber:#c9791a;--code:#f5f5f7}}
@media(prefers-color-scheme:dark){{:root{{--bg:#1c1c1e;--fg:#f2f2f4;--mut:#9a9aa0;--line:#3a3a3e;--code:#2a2a2d}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:32px 16px 64px}}
h1{{font-size:24px;margin:0 0 4px}} .mut{{color:var(--mut)}}
.cause{{border-left:4px solid var(--amber);padding:4px 16px;margin:20px 0;background:var(--code)}}
.row{{display:flex;gap:12px;flex-wrap:wrap;margin:20px 0}}
a.btn,button{{font:inherit;padding:10px 16px;border-radius:8px;border:1px solid var(--line);
background:var(--code);color:var(--fg);text-decoration:none;cursor:pointer}}
a.btn.main{{background:var(--amber);border-color:var(--amber);color:#fff}}
pre{{background:var(--code);padding:12px;border-radius:8px;overflow:auto;font-size:13px;max-height:420px}}
textarea{{position:absolute;left:-9999px}}
</style></head><body><main>
<h1>{html.escape(headline)}</h1>
<p class="mut">{html.escape(book)} &middot; {html.escape(time.strftime("%Y-%m-%d %H:%M"))}</p>
<p>{html.escape(why)}</p>
{cause_html}
<h2>Get help</h2>
<p>Paste this report to an AI assistant, or post it where people can help: a GitHub issue, or the forum of the app you were using.
Passwords and keys are hidden. Read it before you share it: it names the book.</p>
<div class="row">
<button id="copy">Copy the report</button>
<a class="btn main" href="{html.escape(issue)}" target="_blank" rel="noopener">Open a GitHub issue</a>
</div>
<p id="done" class="mut"></p>
<textarea id="t">{html.escape(ask)}</textarea>
<h2>The end of the log</h2>
<pre>{log}</pre>
</main>
<script>
document.getElementById("copy").onclick=function(){{
 var t=document.getElementById("t");t.select();
 var ok=false;try{{ok=document.execCommand("copy")}}catch(e){{}}
 document.getElementById("done").textContent=ok?"Copied. Paste it into your message.":"Select the log below and copy it by hand.";
}};
</script></body></html>
"""


def report_path():
    return os.path.join(platform_support.log_dir(), REPORT_NAME)


def show_failure(filename, why, log_path=None, opener=platform_support.open_path,
                 headline="A run did not finish", key="run", reopen_after=REOPEN_AFTER):
    """Write the report page and open it. Returns its path, or None when it could not be written.

    `why` says what stopped and what to do next. `key` names the kind of
    failure whose open-throttle applies ("run" for a book, "startup" for a
    watcher that cannot start, which gets a much longer one). Never raises:
    this runs inside failure handlers, which must go on to move the book and
    notify.
    """
    if os.getenv("VOXIE_NO_REPORT"):
        return None
    path = report_path()
    stamp = os.path.join(os.path.dirname(path), f".voxie-failure-{key}.opened")
    book = os.path.splitext(filename)[0]
    try:
        fresh = time.time() - os.path.getmtime(stamp) < reopen_after
    except OSError:
        fresh = False
    try:
        lines = excerpt_for(log_path or platform_support.ghost_log_path(), filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(build_html(book, why, lines, headline))
        if not fresh:
            with open(stamp, "w"):
                pass
    except OSError:
        return None
    if not fresh:
        opener(path)
    return path
