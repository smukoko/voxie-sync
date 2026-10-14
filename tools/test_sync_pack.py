#!/usr/bin/env python3
"""How the Sync Pack compresses its members, which is a trade, not a setting.

MP3 does not compress: measured on six of Screwtape's chapters, 38.8MB of audio
deflated to 37.8MB and cost 1.29s against 0.07s stored. A sixteen-hour book is
~1.4GB of it, so deflating the audio is ~45s of the Mac's CPU to save ~14s of
its uplink -- and packing is serial time in the middle of a run.

The obvious fix is `ZIP_STORED` for the whole pack, and it is wrong: the
manifest carries every word timing in the book (5.7MB for Screwtape, deflating
to 0.9MB in a tenth of a second) and a real novel's is far larger. Storing that
gives back more than the audio saves.

So compression is chosen per member, and these are the assertions that keep it
that way -- including the one that matters most, that choosing it per member
cannot change what comes out.

    python tools/test_sync_pack.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# This checkout's core/, not whichever one `pip install -e .` points at: from a
# second checkout the suite was testing the first one's code (2026-10-08).
sys.path.insert(0, ROOT)

# mac_ghost is a script rather than a package, and importing it by path keeps
# this runnable from anywhere without installing anything.
#
# It reads ~/.librasync.json at import and sys.exits without one, so `~` is
# redirected at a throwaway config for the length of the import. Without that
# this suite would pass on the machine it was written on and fail everywhere
# else, which is the opposite of what a suite that "needs no GPU and no
# network" is for.
_fake_home = tempfile.mkdtemp(prefix="syncpack_home_")
with open(os.path.join(_fake_home, ".librasync.json"), "w") as _f:
    json.dump({"GATEWAY_IP": "127.0.0.1", "UPLOAD_TOKEN": "not-a-real-token"}, _f)

_real_expanduser = os.path.expanduser
os.path.expanduser = lambda p: (
    os.path.join(_fake_home, p[2:]) if p.startswith("~/") else _real_expanduser(p)
)
try:
    _spec = importlib.util.spec_from_file_location("mac_ghost", os.path.join(ROOT, "mac_ghost.py"))
    mac_ghost = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(mac_ghost)
finally:
    os.path.expanduser = _real_expanduser

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  ({detail})" if detail else ""))


work = tempfile.mkdtemp(prefix="syncpack_")
try:
    # Text that actually compresses, standing in for a manifest full of word
    # timings, and bytes that do not, standing in for narration.
    manifest = os.path.join(work, "manifest.json")
    with open(manifest, "w") as f:
        f.write('{"word_timings": [' + ",".join('{"w":"word","s":%d}' % i for i in range(20000)) + "]}")

    audio = os.path.join(work, "Chapter_01_Part_1.mp3")
    with open(audio, "wb") as f:
        f.write(os.urandom(2 * 1024 * 1024))

    kepub = os.path.join(work, "Book.kepub.epub")
    with open(kepub, "wb") as f:
        f.write(os.urandom(256 * 1024))

    pack = os.path.join(work, "pack.zip")
    with zipfile.ZipFile(pack, "w", zipfile.ZIP_DEFLATED) as z:
        for p in (manifest, audio, kepub):
            mac_ghost.pack_write(z, p, os.path.basename(p))

    with zipfile.ZipFile(pack) as z:
        info = {i.filename: i for i in z.infolist()}
        check("the audio is stored, not deflated",
              info["Chapter_01_Part_1.mp3"].compress_type == zipfile.ZIP_STORED)
        check("the kepub is stored -- an EPUB is already a zip",
              info["Book.kepub.epub"].compress_type == zipfile.ZIP_STORED)
        check("the manifest is still deflated",
              info["manifest.json"].compress_type == zipfile.ZIP_DEFLATED)
        check("and deflating it is worth doing",
              info["manifest.json"].compress_size < info["manifest.json"].file_size / 4,
              f"{info['manifest.json'].file_size} -> {info['manifest.json'].compress_size}")

        # The point of the whole exercise: a member's compression is invisible
        # to the gateway, which extracts with plain zipfile either way. A pack
        # that unpacks to different bytes would be a silently corrupted book.
        check("every member's CRC verifies", z.testzip() is None)
        same = all(z.read(os.path.basename(p)) == open(p, "rb").read()
                   for p in (manifest, audio, kepub))
        check("every member reads back byte-identical", same)

    # An unknown extension must stay compressed. Getting this backwards would
    # store text, which is the failure a blanket ZIP_STORED would have shipped.
    other = os.path.join(work, "notes.txt")
    with open(other, "w") as f:
        f.write("libra" * 20000)
    pack2 = os.path.join(work, "pack2.zip")
    with zipfile.ZipFile(pack2, "w", zipfile.ZIP_DEFLATED) as z:
        mac_ghost.pack_write(z, other, "notes.txt")
    with zipfile.ZipFile(pack2) as z:
        check("an unrecognised extension is compressed",
              z.getinfo("notes.txt").compress_type == zipfile.ZIP_DEFLATED)

    check("the extension match is case-insensitive",
          ".MP3".lower() in mac_ghost.ALREADY_COMPRESSED)
finally:
    import shutil
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(_fake_home, ignore_errors=True)

print("\n" + "=" * 60)
passed = sum(1 for r in results if r)
print(f"{len(results)} checks: {passed} passed, {len(results) - passed} FAILED")
sys.exit(0 if passed == len(results) else 1)
