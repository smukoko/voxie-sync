"""What Voxie is doing with a book, for the person who dropped it.

On 2026-10-22 a first book took 75 minutes and said nothing the whole time: the
person could not tell whether the book was readable, whether the audiobook was
coming, or how long. The engine knows all of it. It writes the stage to one
small file; the watcher (mac_ghost.py) turns changes of stage into desktop
notices, and `voxie status` prints it.

A file, not a pipe: the engine is a child process of the watcher, a book can be
resumed by another process, and `voxie status` has to work from a terminal that
started neither. Written whole to a temporary name and renamed, so a reader
never sees half of it.

Every function here is best-effort. Progress is a courtesy: nothing in it may
ever stop a book.
"""
import json
import os
import tempfile
import time

from core import platform_support

# What the person is told, in the order a book passes through them.
LABELS = {
    "reading": "Reading the book",
    "ebook_ready": "The ebook is ready to read; planning the narration",
    "waiting_gpu": "Waiting for a narration GPU to be free",
    "starting_gpu": "Starting the narration GPU",
    "narrating": "Narrating",
    "aligning": "Matching the audio to the text (this is what keeps your place in sync)",
    "stitching": "Putting the chapters together",
    "uploading": "Sending the audiobook to your server",
    "done": "Ready",
    "failed": "Stopped",
}

# After the last chunk is narrated: word timings, the retakes, the stitch and
# the upload. Measured on Winning (6.9 h): about 5 + 2.5 + 4.5 minutes.
FINISHING_BASE_MINUTES = 3.0
FINISHING_PER_HOUR = 1.2


def status_path():
    return os.getenv("VOXIE_STATUS_FILE") or os.path.join(platform_support.log_dir(), "voxie-status.json")


def read():
    try:
        with open(status_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def finishing_minutes(audio_hours):
    return FINISHING_BASE_MINUTES + FINISHING_PER_HOUR * max(0.0, audio_hours or 0.0)


def narration_eta(done_chars, total_chars, elapsed_seconds):
    """Minutes of narrating left, from the pace so far; None until it can be told.

    Counted in characters, not chunks: the longest chunks go first, so a count
    of chunks would promise the end far too early.
    """
    if total_chars <= 0 or done_chars <= 0 or elapsed_seconds < 60 or done_chars < 0.02 * total_chars:
        return None
    rate = done_chars / elapsed_seconds
    return max(0.0, (total_chars - done_chars) / rate / 60.0)


def duration_text(minutes):
    """"about 25 minutes", "about 1 h 10 min", "under a minute"."""
    if minutes is None:
        return ""
    m = int(round(minutes / 5.0) * 5) if minutes >= 10 else int(round(minutes))
    if minutes < 1:
        return "under a minute"
    if m < 60:
        return f"about {m} minute{'s' if m != 1 else ''}"
    h, rest = divmod(m, 60)
    return f"about {h} h" + (f" {rest} min" if rest else "")


def update(stage, book=None, say=True, **fields):
    """Record the stage (and any fields: eta_minutes, done, total, audio_hours...).

    The book's name and the fields of the same book are kept from one call to
    the next; a new book starts clean. With say=True a change of stage also
    prints one timestamped line, which is what the watcher's log lacked.
    """
    try:
        old = read() or {}
        # Reading a book starts it afresh, even the same book dropped again.
        same = book is None or (old.get("book") == book and stage != "reading")
        status = dict(old) if same else {}
        if book is not None:
            status["book"] = book
        previous = status.get("stage")
        status.update(stage=stage, updated=time.time(), **fields)
        if stage != previous or not same:
            status["since"] = status["updated"]
        path = status_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".voxie-status-")
        with os.fdopen(fd, "w") as f:
            json.dump(status, f)
        os.replace(tmp, path)
        if say and stage != previous:
            print(f"   🕒 {time.strftime('%H:%M')}  {describe(status)}", flush=True)
        return status
    except Exception:
        return None


def describe(status):
    """One line a person can read: what is happening and how long is left."""
    if not status:
        return "Voxie is not working on a book."
    book = status.get("book") or "your book"
    stage = status.get("stage")
    label = LABELS.get(stage, stage or "Working")
    line = f"{book}: {label}"
    if stage == "narrating" and status.get("total"):
        line += f", {status.get('done', 0) * 100 // status['total']}% of the text"
    eta = status.get("eta_minutes")
    if eta is not None and stage not in ("done", "failed"):
        line += f", {duration_text(eta)} to go"
    if stage == "done" and status.get("audio_hours"):
        line += f" ({status['audio_hours']:.1f} h of audio)"
    return line


def notice(status, told):
    """[(key, title, message)] the person should be told now, given what they were told.

    `told` is a set the caller keeps for the book; a notice is returned once.
    Only the moments that change what the person can do: the ebook can be read,
    the wait has a length, the wait is a queue for a GPU, the audiobook is here.
    """
    if not status:
        return []
    book = status.get("book") or "Your book"
    stage = status.get("stage")
    out = []

    def once(key, title, message):
        if key not in told:
            told.add(key)
            out.append((key, title, message))

    if stage in ("starting_gpu", "narrating") and status.get("eta_minutes") is not None:
        hours = status.get("audio_hours")
        once("eta", f"Voxie is narrating '{book}'",
             f"The audiobook {('(' + format(hours, '.0f') + ' h) ') if hours and hours >= 1 else ''}"
             f"will be ready in {duration_text(status['eta_minutes'])}. You can already read the ebook "
             "on your Kobo.")
    if stage == "waiting_gpu" and time.time() - status.get("since", time.time()) > 120:
        once("waiting_gpu", f"Voxie is waiting for a GPU for '{book}'",
             "Every narration GPU on RunPod is busy right now. Voxie keeps trying, and nothing is "
             "billed while it waits. The ebook is readable already.")
    if stage == "done":
        hours = status.get("audio_hours")
        once("done", f"'{book}' is ready to listen to",
             (f"{hours:.1f} hours of audiobook" if hours else "The audiobook")
             + " is on your server. Open Audiobookshelf to listen; on your Kobo, sync to keep your "
             "place in step.")
    return out
