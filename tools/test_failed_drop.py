"""A book whose run did not finish leaves the drop folder, so it is not narrated again by itself.

A book left in ~/Kobo_Dropzone is narrated again by the watcher's next start
(process_backlog). A new run begins by wiping mfa_workspace, the only copy of
the narration when the chunk cache is off, and bills the GPU again: at every
restart, for a book that fails every time (Twisted, 2026-10-11, moved out by
hand). After a failed upload it was worse: the next start sent the kept pack,
then narrated the book a second time. Offline: the watcher's real handler with
a fake engine.
"""
import contextlib
import os
import sys
import tempfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(tempfile.mkdtemp())
import mac_ghost as mg  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


class Engine:
    """The engine process, finished with `code`."""

    def __init__(self, code):
        self.returncode = code

    def poll(self):
        return self.returncode


def drop(code, upload_ok=True, built=True, raises=False):
    """Run one drop. Returns (dropzone, failed, archive, notices)."""
    home = tempfile.mkdtemp()
    zone, failed_dir, archive = (os.path.join(home, d) for d in ("drop", "failed", "archive"))
    os.makedirs(zone)
    book = os.path.join(zone, "Twisted.epub")
    open(book, "wb").write(b"epub")
    folder = os.path.join(home, "library", "audiobooks", "Twisted")
    os.makedirs(folder)
    open(os.path.join(folder, "manifest.json"), "w").write("{}")
    notices = []
    patches = [
        mock.patch.object(mg, "FAILED", failed_dir), mock.patch.object(mg, "ARCHIVE", archive),
        mock.patch.object(mg, "notify", lambda *a: notices.append(a)),
        mock.patch.object(mg, "report_failed", lambda *a: None),
        mock.patch.object(mg, "keep_awake", contextlib.nullcontext),
        mock.patch.object(mg.subprocess, "Popen", lambda *a, **k: Engine(code)),
        mock.patch.object(mg.time, "sleep", lambda s: None),
        mock.patch.object(mg, "read_build_marker", lambda: folder if built else None),
        mock.patch.object(mg.library, "BOOKS", os.path.join(home, "empty")),
        mock.patch.object(mg, "upload_with_retries", lambda z: upload_ok),
        mock.patch.object(mg, "trigger_abs_scan", lambda: None),
    ]
    handler = mg.GhostHandler()
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        if raises:
            stack.enter_context(mock.patch.object(handler, "_process", side_effect=RuntimeError("boom")))
        handler.on_created(type("E", (), {"is_directory": False, "src_path": book})())
    ls = lambda d: sorted(os.listdir(d)) if os.path.isdir(d) else []  # noqa: E731
    return ls(zone), ls(failed_dir), ls(archive), notices


zone, failed_dir, archive, notices = drop(code=1)
check("an engine that fails: the book leaves the drop folder", zone == [], str(zone))
check("for the folder of books that did not finish, with a notice saying how to retry",
      failed_dir == ["Twisted.epub"] and notices and "drop it into Kobo_Dropzone again" in notices[-1][1],
      f"({failed_dir}, {notices[-1:]})")

zone, failed_dir, archive, notices = drop(code=mg.NO_VOICE_EXIT)
check("no narrator voice: the book is set aside too, so it can be dropped again",
      zone == [] and failed_dir == ["Twisted.epub"], f"({zone}, {failed_dir})")

zone, failed_dir, archive, notices = drop(code=0, upload_ok=False)
check("a failed upload: the book goes to the archive, its audiobook is in the kept pack",
      zone == [] and archive == ["Twisted.epub"] and os.path.exists("Twisted_SyncPack.zip"),
      f"({zone}, {archive})")
os.remove("Twisted_SyncPack.zip")

zone, failed_dir, archive, notices = drop(code=0, raises=True)
check("a watcher error mid-run: the book leaves the drop folder too",
      zone == [] and failed_dir == ["Twisted.epub"], f"({zone}, {failed_dir})")

zone, failed_dir, archive, notices = drop(code=0)
check("a run that finishes and uploads: archived as before", zone == [] and archive == ["Twisted.epub"]
      and failed_dir == [], f"({zone}, {archive}, {failed_dir})")

zone, failed_dir, archive, notices = drop(code=0, built=False)
check("an engine that finishes but leaves no book to send: set aside, not narrated again",
      zone == [] and failed_dir == ["Twisted.epub"], f"({zone}, {failed_dir})")

home = tempfile.mkdtemp()
stuck = os.path.join(home, "Twisted.epub")
open(stuck, "wb").write(b"epub")
with mock.patch.object(mg, "FAILED", os.path.join(home, "failed")), \
        mock.patch.object(mg.shutil, "move", side_effect=OSError("read-only")), \
        mock.patch.object(mg, "notify", lambda *a: None):
    moved = mg.set_aside(stuck, "Test.")
check("a book that cannot be moved stays, and says it will be narrated again",
      moved is None and os.path.exists(stuck))

# A second failure of the same book does not overwrite the first.
home = tempfile.mkdtemp()
fd = os.path.join(home, "failed")
os.makedirs(fd)
open(os.path.join(fd, "Twisted.epub"), "wb").write(b"first")
src = os.path.join(home, "Twisted.epub")
open(src, "wb").write(b"second")
with mock.patch.object(mg, "FAILED", fd), mock.patch.object(mg, "notify", lambda *a: None):
    mg.set_aside(src, "Test.")
kept = sorted(os.listdir(fd))
check("a book set aside twice keeps both copies", len(kept) == 2 and "Twisted.epub" in kept, str(kept))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
