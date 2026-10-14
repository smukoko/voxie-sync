#!/usr/bin/env python3
"""Pack a book that is already built on this Mac and ship it to the gateway.

For a book repaired by tools/repair_book.py, whose engine never reached the
ghost's packaging step. Builds the Sync Pack exactly as mac_ghost does -- its
own pack_write, so audio is stored and the manifest deflated -- uploads it with
the ghost's resumable, retrying upload, asks Audiobookshelf to rescan, and then
checks the one assertion this project makes after every run: the audiobook
item's duration in ABS equals the manifest's.

Refuses to ship a book repair_book did not verify, because the only thing worse
than a missing book is a wrong one replacing a right one.

    python tools/ship_book.py "Redeeming Love"
"""
import json
import os
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import mac_ghost as ghost  # noqa: E402  (guarded by __main__; import has no side effects)
from core import library  # noqa: E402


def abs_duration(title):
    import httpx
    cfg = json.load(open(os.path.expanduser("~/.librasync.json")))
    h = {"Authorization": f"Bearer {cfg['ABS_API_TOKEN']}"}
    base = f"http://{cfg['GATEWAY_IP']}:13378"
    lib = httpx.get(f"{base}/api/libraries", headers=h, timeout=30).json()["libraries"][0]["id"]
    res = httpx.get(f"{base}/api/libraries/{lib}/search", params={"q": title}, headers=h, timeout=30).json()
    for b in res.get("book", []):
        item = httpx.get(f"{base}/api/items/{b['libraryItem']['id']}", params={"expanded": 1},
                         headers=h, timeout=30).json()
        if "/audiobooks/" in (item.get("path") or ""):
            return item["media"].get("duration") or 0.0, len(item["media"].get("audioFiles", []))
    return None, 0


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    ghost.configure()
    folder = sys.argv[1]
    audio_dir = library.book_dir(folder)
    # A book built before the one-folder layout keeps its kepub and manifest in
    # library/ebooks/; the gateway moves them beside the audio on extraction.
    legacy_dir = os.path.join(library.LEGACY_EBOOKS, folder)
    ebook_dir = audio_dir if os.path.exists(os.path.join(audio_dir, "manifest.json")) else legacy_dir
    manifest = os.path.join(ebook_dir, "manifest.json")
    if not (os.path.isdir(audio_dir) and os.path.exists(manifest)):
        sys.exit(f"missing {audio_dir} or its manifest")
    if any(f.endswith(".wav") for f in os.listdir(audio_dir)):
        sys.exit(f"{audio_dir} still holds WAV chunks -- the book was not stitched; repair it first")

    total = sum(c.get("duration") or 0 for c in json.load(open(manifest)).get("chapters", []))
    print(f"📚 {folder}: manifest says {total:.2f}s")

    zip_filename = f"{folder}_SyncPack.zip"
    started = time.time()
    with zipfile.ZipFile(zip_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
        for base in sorted({ebook_dir, audio_dir}):
            for root, _, files in os.walk(base):
                for f in files:
                    ghost.pack_write(zipf, os.path.join(root, f))
    print(f"📦 Sync Pack: {os.path.getsize(zip_filename) // 1024 // 1024} MB in {time.time() - started:.0f}s")

    if not ghost.upload_with_retries(zip_filename):
        print(f"❌ Upload failed; the pack is kept. Retry with: python mac_ghost.py --retry-uploads")
        sys.exit(1)
    os.remove(zip_filename)
    ghost.trigger_abs_scan()

    for attempt in range(18):
        dur, files = abs_duration(folder)
        if dur and abs(dur - total) < 5:
            print(f"✅ ABS: {dur:.2f}s over {files} files; manifest {total:.2f}s; drift {dur - total:+.2f}s")
            return
        time.sleep(10)
    print(f"‼️  ABS reports {dur}s over {files} files against {total:.2f}s -- check the rescan before trusting it")
    sys.exit(2)


if __name__ == "__main__":
    main()
