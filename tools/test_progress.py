"""A book says what is happening to it (2026-10-22: 75 minutes, no word).

The engine writes the stage to a file (core/progress.py); the watcher turns a
change of stage into a desktop notice; `voxie status` prints it. Tested on the
pure parts (the words, the times, who is told what once) and on the watcher's
handling of a stale file left by the previous book. Offline.
"""
import io
import contextlib
import os
import sys
import tempfile
import time
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
work = tempfile.mkdtemp()
os.environ["VOXIE_STATUS_FILE"] = os.path.join(work, "logs", "voxie-status.json")
os.chdir(work)
from core import progress  # noqa: E402
import mac_ghost as mg  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


# --- the words ---------------------------------------------------------------------
check("minutes read as a person says them",
      [progress.duration_text(m) for m in (0.4, 1, 7, 23, 58, 70, 125)]
      == ["under a minute", "about 1 minute", "about 7 minutes", "about 25 minutes", "about 1 h",
          "about 1 h 10 min", "about 2 h 5 min"],
      str([progress.duration_text(m) for m in (0.4, 1, 7, 23, 58, 70, 125)]))
check("no estimate says nothing rather than a guess", progress.duration_text(None) == "")
check("nothing is promised before the pace is known",
      progress.narration_eta(0, 1000, 600) is None and progress.narration_eta(10, 1000, 600) is None
      and progress.narration_eta(500, 1000, 30) is None)
check("the time left follows the pace: half done in 20 min is 20 min to go",
      abs(progress.narration_eta(500, 1000, 1200) - 20) < 0.01)
check("finishing takes longer for a longer book",
      progress.finishing_minutes(1) < progress.finishing_minutes(16) and 10 < progress.finishing_minutes(6.9) < 13)

line = progress.describe({"book": "Winning", "stage": "narrating", "done": 400, "total": 1000, "eta_minutes": 31})
check("a status reads as a sentence with the share done and the time left",
      line == "Winning: Narrating, 40% of the text, about 30 minutes to go", line)
check("a finished book says its length",
      "7.0 h of audio" in progress.describe({"book": "W", "stage": "done", "audio_hours": 7.0}))
check("no status is not an error", "not working" in progress.describe(None))

# --- the file ----------------------------------------------------------------------
check("nothing written yet reads as None", progress.read() is None)
with contextlib.redirect_stdout(io.StringIO()) as out:
    progress.update("reading", book="Winning")
    progress.update("starting_gpu", audio_hours=7.0, eta_minutes=45)
    progress.update("narrating", say=False, done=10, total=100)
st = progress.read()
check("fields and the book carry from one stage to the next",
      st["book"] == "Winning" and st["audio_hours"] == 7.0 and st["eta_minutes"] == 45 and st["stage"] == "narrating")
check("a change of stage is printed once, with the time",
      out.getvalue().count("🕒") == 2 and time.strftime("%H:%M") in out.getvalue(), out.getvalue())
since = st["since"]
time.sleep(0.01)
progress.update("narrating", say=False, done=20, total=100)
check("a tick inside a stage keeps when the stage began", progress.read()["since"] == since)
with contextlib.redirect_stdout(io.StringIO()):
    progress.update("reading", book="Winning")
check("the same book dropped again starts clean",
      "eta_minutes" not in progress.read() and "done" not in progress.read())
leftovers = [f for f in os.listdir(os.path.dirname(progress.status_path())) if f.startswith(".voxie-status-")]
check("no temporary file is left beside it", not leftovers, str(leftovers))
os.environ["VOXIE_STATUS_FILE"] = "/proc/nope/never/status.json"
check("a status that cannot be written never raises", progress.update("reading", book="X", say=False) is None)
os.environ["VOXIE_STATUS_FILE"] = os.path.join(work, "logs", "voxie-status.json")

# --- who is told what, once --------------------------------------------------------
told = set()
check("nothing to say at the start", progress.notice({"book": "Winning", "stage": "reading"}, told) == [])
n = progress.notice({"book": "Winning", "stage": "starting_gpu", "eta_minutes": 47, "audio_hours": 6.9}, told)
check("the wait is told once it has a length, with the audio's length",
      len(n) == 1 and "about 45 minutes" in n[0][2] and "7 h" in n[0][2] and "read the ebook" in n[0][2], str(n))
check("and not told twice",
      progress.notice({"book": "Winning", "stage": "narrating", "eta_minutes": 40, "audio_hours": 6.9}, told) == [])
queue = {"book": "Winning", "stage": "waiting_gpu", "since": time.time() - 30}
check("a short wait for a GPU is not worth a notice", progress.notice(queue, set()) == [])
queue["since"] = time.time() - 300
n = progress.notice(queue, set())
check("a long one is, and says nothing is billing", len(n) == 1 and "nothing is billed" in n[0][2])
n = progress.notice({"book": "Winning", "stage": "done", "audio_hours": 6.9}, told)
check("the finished book is announced with its length", len(n) == 1 and "6.9 hours" in n[0][2] and "ready to listen" in n[0][1])

# --- the watcher --------------------------------------------------------------------
check("a file name becomes the book's name",
      mg.friendly_name("Winning The Unforgiving Race to Greatness (Tim S. Grover) (z-library.sk, 1lib.sk, z-lib.sk).epub")
      == "Winning The Unforgiving Race to Greatness" and mg.friendly_name("Walden.epub") == "Walden")
sent = []
with mock.patch.object(mg, "notify", lambda t, m: sent.append((t, m))):
    # The last book's "done" is on disk when this one starts: its news is not this book's.
    progress.update("done", book="Old Book", say=False, audio_hours=3.0)
    mg.tell_progress(time.time() + 1, set())
    check("a stale status from the previous book tells nothing", sent == [])
    progress.update("done", book="Winning", say=False, audio_hours=6.9)
    told2 = set()
    mg.tell_progress(time.time() - 5, told2)
    mg.tell_progress(time.time() - 5, told2)
    check("this book's finish is announced once", len(sent) == 1 and "'Winning' is ready" in sent[0][0], str(sent))

# the command
from click.testing import CliRunner  # noqa: E402
from libra_cli.main import cli  # noqa: E402
out = CliRunner().invoke(cli, ["status"]).output
check("voxie status prints it", "Winning: Ready" in out, out)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
