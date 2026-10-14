#!/usr/bin/env python3
"""Chunks are placed as APFS clones, and anywhere that is impossible, copied.

    python tools/test_clone_or_copy.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.tts_worker as tw  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  ({detail})" if detail else ""))


d = tempfile.mkdtemp(dir=os.path.expanduser("~"))   # on the same APFS volume as the repo
try:
    src = os.path.join(d, "chunk.wav")
    with open(src, "wb") as f:
        f.write(os.urandom(64 << 20))
    body = open(src, "rb").read()

    free = shutil.disk_usage(d).free
    how = tw.clone_or_copy(src, os.path.join(d, "placed.wav"))
    subprocess.run(["sync"])
    used = free - shutil.disk_usage(d).free
    check("placed byte-identical", open(os.path.join(d, "placed.wav"), "rb").read() == body)
    if sys.platform == "darwin":
        check("on APFS it is a clone that costs ~no space", how == "clone" and used < 8 << 20,
              f"{how}, {used / 1e6:.1f} MB for 64 MB")

    with open(os.path.join(d, "placed.wav"), "r+b") as f:
        f.write(b"XXXX")
    check("writing to the placed file never touches the source", open(src, "rb").read() == body)

    open(os.path.join(d, "stale.wav"), "wb").write(b"old")
    tw.clone_or_copy(src, os.path.join(d, "stale.wav"))
    check("an existing destination is replaced", open(os.path.join(d, "stale.wav"), "rb").read() == body)

    real_run = tw.subprocess.run
    tw.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 1)
    try:
        how = tw.clone_or_copy(src, os.path.join(d, "fallback.wav"))
    finally:
        tw.subprocess.run = real_run
    check("where cloning fails it falls back to a copy",
          how == "copy" and open(os.path.join(d, "fallback.wav"), "rb").read() == body)
finally:
    shutil.rmtree(d, ignore_errors=True)

print("\n" + "=" * 60)
passed = sum(1 for r in results if r)
print(f"{len(results)} checks: {passed} passed, {len(results) - passed} FAILED")
sys.exit(0 if passed == len(results) else 1)
