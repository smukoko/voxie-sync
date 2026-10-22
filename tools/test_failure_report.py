"""The page a failed run opens: what it shows, what it hides, and when it opens.

Offline: the module's own functions with a temp log folder and a fake opener.
"""
import os
import sys
import tempfile
import time
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from core import failure_report as fr  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


home = os.path.expanduser("~")
raw = (f"{home}/library/x.epub\nTTS_API_TOKEN=abc123secret\nrunpod key rpa_ABCDEFGH12345678\n"
       '{"api_key": "sk-live-zzz"}\nAuthorization: Bearer tok.en\nnothing to hide here')
out = fr.scrub(raw)
check("the log is scrubbed: home folder, tokens, key shapes",
      home not in out and "abc123secret" not in out and "rpa_ABCDEFGH12345678" not in out
      and "sk-live-zzz" not in out and "tok.en" not in out and "~/library/x.epub" in out
      and "nothing to hide here" in out, out)

d = tempfile.mkdtemp()
log = os.path.join(d, "ghost.out")
with open(log, "w") as f:
    f.write("old book line\nGHOST WOKE UP: Detected Twisted.epub\nline a\n")
    f.write("GHOST WOKE UP: Detected Other.epub\nOther 1\n")
    f.write("GHOST WOKE UP: Detected Twisted.epub\nnew 1\n❌ Docker is installed but not running (start Docker Desktop)\n")
lines = fr.excerpt_for(log, "Twisted.epub")
check("the excerpt starts at this book's LAST start", lines[1] == "new 1" and "line a" not in "\n".join(lines),
      str(lines))
check("a log that is not there gives no lines, not an error", fr.excerpt_for(os.path.join(d, "none"), "x.epub") == [])
many = os.path.join(d, "many")
open(many, "w").write("Detected B.epub\n" + "\n".join(f"l{i}" for i in range(500)))
check("a long run is cut to its end", len(fr.excerpt_for(many, "B.epub")) == fr.MAX_LINES
      and fr.excerpt_for(many, "B.epub")[-1] == "l499")

check("Docker not running is said plainly", "Docker Desktop is not running" in (fr.known_cause(lines) or ""))
check("an unknown failure claims no cause", fr.known_cause(["something odd"]) is None)

page = fr.build_html("<script>alert(1)</script>", "Its narration stopped.", ["<img onerror=x>", "TOKEN=zzzz"])
check("a title or log line cannot inject markup", "<script>alert(1)</script>" not in page
      and "<img onerror=x>" not in page and "&lt;img onerror=x&gt;" in page)
check("the page hides secrets too", "zzzz" not in page)
check("the page offers the GitHub issue, prefilled", "issues/new?template=bug.yml&amp;title=" in page)

opened = []
with mock.patch.object(fr.platform_support, "log_dir", lambda: d):
    p = fr.show_failure("Twisted.epub", "Its narration stopped.", log_path=log, opener=opened.append)
    check("a failure writes the page and opens it", p == os.path.join(d, fr.REPORT_NAME)
          and os.path.exists(p) and opened == [p], str(opened))
    check("the page holds this book's log", "Docker Desktop is not running" in open(p).read())
    fr.show_failure("Other.epub", "Again.", log_path=log, opener=opened.append)
    check("a second failure within a minute rewrites the page but opens no second tab",
          len(opened) == 1 and "Other" in open(p).read(), str(opened))
    fr.show_failure("x.epub", "Cannot start.", log_path=log, opener=opened.append, key="startup",
                    reopen_after=3600, headline="The watcher could not start")
    check("another kind has its own throttle, and its own headline",
          len(opened) == 2 and "The watcher could not start" in open(p).read(), str(opened))
    for k in ("run",):
        stamp = os.path.join(d, f".voxie-failure-{k}.opened")
        old = time.time() - fr.REOPEN_AFTER - 5
        os.utime(stamp, (old, old))
    fr.show_failure("Other.epub", "Again.", log_path=log, opener=opened.append)
    check("a later failure opens it again, even after other kinds rewrote the page", len(opened) == 3, str(opened))
    with mock.patch.dict(os.environ, {"VOXIE_NO_REPORT": "1"}):
        check("VOXIE_NO_REPORT turns it off", fr.show_failure("x.epub", "y", log_path=log, opener=opened.append) is None)

with mock.patch.object(fr.platform_support, "log_dir", lambda: "/proc/nope/never"):
    check("an unwritable folder returns None, never raises",
          fr.show_failure("x.epub", "y", log_path=log, opener=opened.append) is None)

check("a platform with no opener is not an error", fr.platform_support.open_command("/x") is not None
      or fr.platform_support.open_path("/x") is False)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
