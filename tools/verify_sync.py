#!/usr/bin/env python3
"""Prove that a page and a second of audio point at the same words, both ways. Read-only.

tools/verify_book.py checks the map against itself. This checks it against
the book and the audio, with nothing taken from the engine but the map:

  1. every sampled paragraph's xpointer, resolved in the KEPUB the way
     KOReader numbers it (DocFragment[N] is the Nth spine item; an element's
     index counts its same-named siblings), is the text the map was made for;
  2. page to audio: Whisper, with word times, around the second the map gives
     a paragraph: its first words must be heard there, within --tolerance;
  3. audio to page: random seconds across the book, landed with the device's
     own rules (core/landing.py); what is being said there must be on the
     page it opens (that paragraph and what follows, to about a page). How
     far into that paragraph the audio
     already is, is reported: a long paragraph opens at its start (positions
     are per paragraph until EMIT_TEXT_OFFSETS).

    python tools/verify_sync.py library/audiobooks/Twisted [--samples 40] [--seed 1]
    python tools/verify_sync.py library/audiobooks/Twisted --shift 4   # the control: must fail

Needs ffmpeg and mlx-whisper (Apple silicon). Nothing is written but a
scratch folder of clips, removed at the end.
"""
import argparse
import difflib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import landing, library  # noqa: E402
from core.parser import parse_xhtml  # noqa: E402

WHISPER = "mlx-community/whisper-large-v3-turbo"
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)


def words(text):
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", (text or "").lower().replace("’", "'"))


# --- 1. the KEPUB, as KOReader reads it ---------------------------------------------
def spine(kepub):
    """[content document bytes] in spine order: KOReader's DocFragment[1..N]."""
    with zipfile.ZipFile(kepub) as z:
        opf_path = re.search(r'full-path="([^"]+)"', z.read("META-INF/container.xml").decode()).group(1)
        opf = z.read(opf_path).decode("utf-8")
        base = os.path.dirname(opf_path)
        items = {m.group(2): m.group(1) for m in re.finditer(r'<item\b[^>]*?href="([^"]+)"[^>]*?id="([^"]+)"', opf)}
        items.update({m.group(1): m.group(2) for m in re.finditer(r'<item\b[^>]*?id="([^"]+)"[^>]*?href="([^"]+)"', opf)})
        order = re.findall(r'<itemref\b[^>]*?idref="([^"]+)"', opf)
        from urllib.parse import unquote
        return [z.read(os.path.normpath(os.path.join(base, unquote(items[i]))).replace("\\", "/")) for i in order]


def resolve(fragments, path):
    """The element a KOReader path without offsets names, or None.

    `/body/DocFragment[47]/body/div/div/p[3]`: the 47th spine document, then
    each step by tag, 1-based among siblings of that tag (no index = 1).
    """
    m = re.match(r"^/body/DocFragment\[(\d+)\]/(.*)$", path)
    if not m or int(m.group(1)) > len(fragments):
        return None
    node = parse_xhtml(fragments[int(m.group(1)) - 1])     # the <html> element
    for step in m.group(2).split("/"):
        s = re.match(r"^([\w:-]+)(?:\[(\d+)\])?$", step)
        if not s:
            return None
        tag, n = s.group(1), int(s.group(2) or 1)
        same = [c for c in node if isinstance(c.tag, str) and c.tag.split("}")[-1] == tag]
        if len(same) < n:
            return None
        node = same[n - 1]
    return node


def similar(a, b):
    """Share of a's words found, in order, in b."""
    a, b = words(a), words(b)
    if not a:
        return 0.0
    m = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return sum(blk.size for blk in m.get_matching_blocks()) / len(a)


# --- the audio ----------------------------------------------------------------------
def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


class Audio:
    """The stitched files as one timeline, in book seconds."""

    def __init__(self, folder, work):
        self.files = sorted(os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".mp3"))
        self.starts, t = [], 0.0
        for f in self.files:
            self.starts.append(t)
            t += probe(f)
        self.total, self.work, self.n = t, work, 0

    def clip(self, start, seconds, anchor=None):
        """A 16 kHz WAV of [start, start + seconds] in the file holding `anchor`
        (default `start`), and the clip's real start.

        The file holding the anchor, not the start: 5 s before a chapter's
        first paragraph is the previous chapter's file, and a clip cut there
        ended with it, before the paragraph was ever said.
        """
        anchor = max(0.0, start if anchor is None else anchor)
        i = max(k for k, s in enumerate(self.starts) if s <= anchor)
        start = max(self.starts[i], start)
        self.n += 1
        out = os.path.join(self.work, f"clip{self.n}.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start - self.starts[i]:.3f}", "-t", f"{seconds:.3f}",
                        "-i", self.files[i], "-ar", "16000", "-ac", "1", out], check=True)
        return out, start


def heard(path):
    """[(word, start seconds into the clip)] as Whisper hears them."""
    import mlx_whisper
    from core.hf_cache import model_path
    out = mlx_whisper.transcribe(path, path_or_hf_repo=model_path(WHISPER), language="en", word_timestamps=True,
                                 condition_on_previous_text=False)
    said = []
    for seg in out.get("segments", []):
        for w in seg.get("words", []):
            for token in words(w["word"]):
                said.append((token, w["start"]))
    return said


def onset(said, text):
    """Seconds into the clip where `text`'s first words are heard, or None.

    The first run of two or more of its first eight words heard in order,
    timed from the run's second word less one word's worth (0.3 s), and moved
    back 0.3 s per word the run skipped. From the second word because Whisper
    stretches a word that follows a pause back into the silence ("I" timed
    2 s before "had scarcely laid").
    """
    want = words(text)[:8]
    got = [w for w, _ in said]
    m = difflib.SequenceMatcher(None, got, want, autojunk=False)
    for blk in m.get_matching_blocks():
        if blk.size >= 2:
            return said[blk.a + 1][1] - 0.3 * (blk.b + 1)
    return None


# About one page of the reader's: the reading profile shows ~250 words.
PAGE_WORDS = 200


def page_text(texts, k):
    """The text a reader sees opening at paragraph k: it and what follows, to
    about a page. Short dialogue fits several paragraphs in the 6 s heard."""
    out, n = [], 0
    for t in texts[k:]:
        out.append(t)
        n += len(words(t))
        if n >= PAGE_WORDS and len(out) >= 2:
            break
    return " ".join(out)


def recognisable(text):
    """Enough distinct words at its start to be found by ear: not "Ugh! ugh!
    ugh!" or "He! he! he!", which match any laugh or cough nearby."""
    first = words(text)[:8]
    return len(first) >= 6 and len(set(first)) >= 6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", help="the book's folder (library/audiobooks/<title>), or its title")
    ap.add_argument("--samples", type=int, default=40)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--tolerance", type=float, default=1.5, help="seconds a paragraph's start may be off")
    ap.add_argument("--shift", type=float, default=0.0,
                    help="move every paragraph this many seconds: a control, the checks must then fail")
    args = ap.parse_args()
    folder = args.folder if os.path.isdir(args.folder) else library.book_dir(args.folder)
    manifest = json.load(open(os.path.join(folder, "manifest.json")))
    kepub = next(os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".kepub.epub"))
    chunk_rows = sorted(manifest["chapters"], key=lambda c: c["chapter_index"])
    node_rows = [(n["xpath_string"], n["start_time"] + args.shift, n["end_time"] + args.shift)
                 for n in manifest["sync_nodes"]]
    book_map = landing.build_map(node_rows, [(c["audio_filename"], c["duration"]) for c in chunk_rows])
    nodes = [tuple(n) for n in book_map["nodes"]]
    chapters = [tuple(c) for c in book_map["chapters"]]
    text_of = {}
    for n in manifest["sync_nodes"]:
        text_of.setdefault(landing.kobo_path(n["xpath_string"]), n.get("text_content") or "")
    rng = random.Random(args.seed)
    work = tempfile.mkdtemp(prefix="verify_sync_")
    try:
        audio = Audio(folder, work)
        bounds = [round(s, 1) for s in audio.starts]
        off = [c for c in chapters if not any(abs(c[1] - b) <= 0.5 for b in bounds)]
        check("the map's chapters start where the audio files start, so a map second is a file second",
              len(chapters) == len(audio.files) and not off and abs(chapters[-1][2] - audio.total) < 1.0,
              f"{len(chapters)} chapters, {len(audio.files)} files, {audio.total:.1f}s"
              + (f"; off: {off[:3]}" if off else ""))

        # One node per paragraph (its first), long enough to recognise.
        first, seen = [], set()
        for path, start, end in nodes:
            if path not in seen:
                seen.add(path)
                first.append((path, start, end))
        usable = [i for i, (p, s, e) in enumerate(first) if recognisable(text_of.get(p)) and e - s >= 2.0]
        picks = sorted(rng.sample(usable, min(args.samples, len(usable))))

        # 1. Each xpointer names the text it was made for -----------------------
        fragments = spine(kepub)
        bad = []
        for i in picks:
            el = resolve(fragments, first[i][0])
            if el is None or similar(text_of[first[i][0]], el.text_content()) < 0.95:
                bad.append(first[i][0])
        check("every sampled xpointer resolves in the KEPUB to the paragraph it was mapped for",
              not bad, f"{len(picks) - len(bad)}/{len(picks)}" + (f"; first bad: {bad[0]}" if bad else ""))

        # 2. Page to audio --------------------------------------------------------
        errors, missed = [], []
        for i in picks:
            path, start, _ = first[i]
            clip, at = audio.clip(start - 5.0, 13.0, anchor=start)
            t = onset(heard(clip), text_of[path])
            if t is None:
                missed.append(path)
                continue
            errors.append(at + t - start)
        ok = [e for e in errors if abs(e) <= args.tolerance]
        errs = sorted(abs(e) for e in errors)
        print(f"INFO  page to audio: {len(errors)} heard, median {errs[len(errs) // 2] if errs else 0:.2f} s off, "
              f"worst {errs[-1] if errs else 0:.2f} s; not heard: {missed[:3]}")
        check(f"a page's paragraph is heard within {args.tolerance} s of the second the map gives it",
              len(ok) >= 0.9 * len(picks), f"{len(ok)}/{len(picks)}")

        # 3. Audio to page ----------------------------------------------------------
        index = {p: k for k, (p, _, _) in enumerate(first)}
        found, into = 0, []
        times = sorted(rng.uniform(10.0, audio.total - 15.0) for _ in range(args.samples))
        for t in times:
            path = landing.landing_position(nodes, chapters, t)
            k = index[path]
            shown = page_text([text_of.get(p, "") for p, _, _ in first], k)
            clip, at = audio.clip(t, 6.0)
            said = " ".join(w for w, _ in heard(clip))
            if said and similar(said, shown) >= 0.6:
                found += 1
            into.append(t - first[k][1])
        into.sort()
        print(f"INFO  audio to page: the audio was a median {into[len(into) // 2]:.0f} s into the opened paragraph, "
              f"{sum(1 for s in into if s <= 30)}/{len(into)} within 30 s, the most {into[-1]:.0f} s")
        check("what is being said at a second is on the page that second opens",
              found >= 0.9 * len(times), f"{found}/{len(times)}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} claims hold")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
