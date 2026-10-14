#!/usr/bin/env python3
"""Check a narrated book against what Voxie claims about it. Read-only.

CLAUDE.md's after-run assertion, and the rest of the claims a finished book
has to meet, checked against the real files, the real Audiobookshelf and the
real gateway. Nothing is written: no reading position is pushed, so the
reader's own place is never touched.

    python tools/verify_book.py "Twisted"

  1. the manifest's chunks are all there, and the stitched files add up to them;
  2. each chapter file is loudness-normalised (-19 LUFS, a few sampled);
  3. Audiobookshelf has one item for it, with the manifest's duration and a
     chapter marker where every file starts;
  4. the gateway serves the device's map, intact (its sha256), covering the
     whole book;
  5. positions translate to times and back with the device's own rules
     (core/landing.py): a page lands at or before its paragraph's audio,
     never across a chapter start.
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from dotenv import dotenv_values  # noqa: E402

from core import accounts, landing, library  # noqa: E402
from core.fingerprint import koreader_partial_md5  # noqa: E402
from core.gateway_address import abs_url, gateway_url  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    return float(r.stdout.strip() or 0)


def lufs(path):
    err = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-af", "ebur128", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    found = re.findall(r"I:\s*(-?[\d.]+) LUFS", err)
    return float(found[-1]) if found else None


def main(title):
    import httpx
    cfg = json.load(open(os.path.expanduser("~/.librasync.json")))
    env = dotenv_values(os.path.join(ROOT, ".env"))
    folder = library.book_dir(title)
    manifest = json.load(open(os.path.join(folder, "manifest.json")))
    chunks, nodes = manifest["chapters"], manifest["sync_nodes"]
    book_title = manifest["book"].get("title") or title

    # 1. Files ----------------------------------------------------------------
    total = sum(c["duration"] or 0 for c in chunks)
    files = sorted(f for f in os.listdir(folder) if f.endswith(".mp3"))
    durations = [probe(os.path.join(folder, f)) for f in files]
    check("the manifest has narration and a map", chunks and nodes,
          f"{len(chunks)} chunks, {len(nodes)} paragraphs, {total / 3600:.2f} h")
    check("the chapter files add up to the manifest", files and abs(sum(durations) - total) < 1.0,
          f"{len(files)} files, {sum(durations):.1f}s vs {total:.1f}s")

    # 2. Loudness ---------------------------------------------------------------
    sample = [files[0], files[len(files) // 2], files[-1]] if files else []
    levels = {f: lufs(os.path.join(folder, f)) for f in dict.fromkeys(sample)}
    check("chapters are loudness-normalised to about -19 LUFS",
          levels and all(v is not None and abs(v + 19) <= 1.5 for v in levels.values()),
          ", ".join(f"{v}" for v in levels.values()))

    # 3. Audiobookshelf -------------------------------------------------------
    abs_base, token = abs_url(cfg), cfg.get("ABS_API_TOKEN") or env.get("ABS_API_TOKEN")
    head = {"Authorization": f"Bearer {token}"}
    items = []
    for lib in httpx.get(f"{abs_base}/api/libraries", headers=head, timeout=30).json().get("libraries", []):
        r = httpx.get(f"{abs_base}/api/libraries/{lib['id']}/search", params={"q": book_title, "limit": 10},
                      headers=head, timeout=60)
        items += [b["libraryItem"] for b in r.json().get("book", [])
                  if (b["libraryItem"]["media"]["metadata"].get("title") or "").lower().startswith(book_title.lower())]
    check("Audiobookshelf shows the book once", len(items) == 1, f"{len(items)} item(s)")
    if items:
        item = httpx.get(f"{abs_base}/api/items/{items[0]['id']}", params={"expanded": 1},
                         headers=head, timeout=30).json()
        abs_duration = item["media"].get("duration") or 0
        check("its duration equals the manifest's (CLAUDE.md's after-run assertion)",
              abs(abs_duration - total) < 1.0, f"{abs_duration:.1f}s vs {total:.1f}s")
        starts = [round(sum(durations[:i]), 1) for i in range(len(durations))]
        marks = [round(c["start"], 1) for c in item["media"].get("chapters", [])]
        off = [m for m in marks if not any(abs(m - s) <= 0.5 for s in starts)]
        # One file is one chapter: markers only matter between files.
        check("every chapter marker sits where a file starts",
              (marks or len(starts) == 1) and not off,
              f"{len(marks)} markers, {len(starts)} files" + (f", off: {off[:5]}" if off else ""))
        names = [c.get("title") for c in item["media"].get("chapters", [])]
        generic = [n for n in names if re.fullmatch(r"(Part|Chapter) \d+", n or "")]
        print(f"INFO  chapter titles: {names[:6]}{' ...' if len(names) > 6 else ''}"
              + (f" ({len(generic)} of {len(names)} are generic)" if generic else ""))

    # 4. The device's map -----------------------------------------------------
    kepub = os.path.join(folder, f"{title}.kepub.epub")
    # What the device sends: the manifest records it at narration time.
    fp = manifest["book"].get("koreader_md5") or koreader_partial_md5(kepub)
    user, pw = cfg["ACCOUNT_USERNAME"], cfg["ACCOUNT_PASSWORD"]
    auth = {"x-auth-user": user, "x-auth-key": accounts.userkey_for(pw)}
    r = httpx.get(f"{gateway_url(cfg)}/librarian/map/{fp}", headers=auth, timeout=60)
    if r.status_code == 404 and "<html" in r.text.lower():
        check("the gateway serves the book's map (offline translation)", False,
              "the server has no /librarian/map route: it runs code from before 2026-10-04; deploy main")
        return
    check("the gateway serves the book's map to its owner", r.status_code == 200,
          f"HTTP {r.status_code} for {fp}" + ("" if fp == koreader_partial_md5(kepub)
                                              else f"; the kepub here hashes to {koreader_partial_md5(kepub)}"))
    if r.status_code != 200:
        return
    check("the map is intact (its sha256)", hashlib.sha256(r.content).hexdigest() == r.headers.get("X-Voxie-Map-Sha256"))
    book_map = json.loads(zlib.decompress(r.content))
    map_nodes = [tuple(n) for n in book_map["nodes"]]
    map_chapters = [tuple(c) for c in book_map["chapters"]]
    check("it covers the whole book", map_chapters and abs(map_chapters[-1][2] - total) < 1.0
          and len(map_nodes) == len(nodes), f"{len(map_nodes)} paragraphs, ends at {map_chapters[-1][2] if map_chapters else 0:.1f}s")
    nobody = httpx.get(f"{gateway_url(cfg)}/librarian/map/{fp}", timeout=30).status_code
    check("and refuses anyone without the login", nobody == 401, f"HTTP {nobody}")

    # 5. Translation, with the device's own rules ------------------------------
    picks = [map_nodes[i] for i in (len(map_nodes) // 7, len(map_nodes) // 3, len(map_nodes) // 2,
                                    2 * len(map_nodes) // 3, len(map_nodes) - 2)]
    good = 0
    for path, start, end in picks:
        span = landing.time_for_position(map_nodes, path)
        back = landing.landing_position(map_nodes, map_chapters, start + 1.0)
        landed = landing.time_for_position(map_nodes, back) if back else None
        chapter_start = map_chapters[landing.chapter_at(map_chapters, start + 1.0)[0] - 1][1]
        ok = (span and abs(span[0] - start) < 0.01 and landed is not None and landed[0] <= start + 1.0
              and landed[0] >= min(start, chapter_start) - landing.CHAPTER_SLACK)
        good += bool(ok)
    check("a page translates to its paragraph's time, and back to the same place or just before",
          good == len(picks), f"{good}/{len(picks)}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "Twisted")
    print(f"\n{sum(results)}/{len(results)} claims hold")
    sys.exit(0 if all(results) else 1)
