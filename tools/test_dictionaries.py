#!/usr/bin/env python3
"""Dictionaries: pinned, prepared honestly, and installed intact.

Runs offline. The manifest is checked as data; staging and installing run
against a synthetic archive built here, so nothing is downloaded.

    python tools/test_dictionaries.py
"""
import hashlib
import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from libra_cli import dictionaries as D  # noqa: E402

# The Kobo installer is macOS-only for now: it extracts with Apple's ditto
# (core/platform_support.py), which other systems do not have.
if sys.platform != "darwin":
    print("SKIP  the Kobo installer runs on macOS only (it needs ditto)")
    sys.exit(0)

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  ({detail})" if detail else ""))


# --- the manifest ------------------------------------------------------------
m = D.load_manifest()["dictionaries"]
ids = [e["id"] for e in m]
# The nine asked for: ENG, ENG-FRENCH, SPANISH-FRENCH, SPANISH-ENGLISH, FRENCH,
# DUTCH, FRENCH-DUTCH, DUTCH-FRENCH, GERMAN-DUTCH.
required = {"en-en", "en-fr", "es-fr", "es-en", "fr-fr", "nl-nl", "fr-nl", "nl-fr", "de-nl"}
check("every requested dictionary is in the manifest", required <= set(ids), str(sorted(required - set(ids))))
check("ids are unique", len(ids) == len(set(ids)))
check("every archive is pinned by sha256",
      all(len(e["sha256"]) == 64 and all(c in "0123456789abcdef" for c in e["sha256"]) for e in m))
check("every entry names its licence", all(e.get("license") for e in m))
check("koreader-dicts are pinned to a release, not 'latest'",
      all("/latest/" not in e["url"] for e in m))
check("display names are human, not codes", all(e["name"] != e["id"] for e in m))

check("the defaults are what setup installs", [e["id"] for e in D.entries()] == [e["id"] for e in m if e.get("default")])
try:
    D.entries(["en-en", "xx-yy"])
    check("an unknown id is refused, not skipped", False)
except ValueError as exc:
    check("an unknown id is refused, not skipped", "xx-yy" in str(exc))

# --- staging and installing, against a synthetic archive ----------------------
with tempfile.TemporaryDirectory() as tmp:
    D.CACHE = os.path.join(tmp, "cache")
    os.makedirs(os.path.join(D.CACHE, "dicts", "zips"))

    ifo = "StarDict's dict ifo file\nversion=3.0.0\nbookname=zz-zz\nwordcount=2\nidxfilesize=10\n"
    zpath = os.path.join(D.CACHE, "dicts", "zips", "zz-zz.zip")
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("zz-zz/zz-zz.ifo", ifo)
        z.writestr("zz-zz/zz-zz.dict", b"\x00\x01definition bytes")
        z.writestr("zz-zz/zz-zz.idx", b"index")
    sha = hashlib.sha256(open(zpath, "rb").read()).hexdigest()
    entry = {"id": "zz-zz", "name": "Zed → Zed", "url": "https://example.invalid/zz-zz.zip",
             "sha256": sha, "archive_root": "zz-zz", "license": "CC BY-SA 4.0",
             "ships_attribution": False, "archive_bytes": 1}

    staged = D.stage(entry, log=lambda *_: None)
    got_ifo = open(os.path.join(staged, "zz-zz.ifo"), encoding="utf-8").read()
    check("the display name replaces the code", "bookname=Zed → Zed" in got_ifo)
    check("...and nothing else in the .ifo moves",
          got_ifo.replace("bookname=Zed → Zed", "bookname=zz-zz") == ifo)
    check("dictionary data is byte-identical",
          open(os.path.join(staged, "zz-zz.dict"), "rb").read() == b"\x00\x01definition bytes")
    check("attribution is written where the archive has none",
          os.path.exists(os.path.join(staged, "ATTRIBUTION")) and os.path.exists(os.path.join(staged, "LICENSE")))
    changes = open(os.path.join(staged, "CHANGES.txt"), encoding="utf-8").read()
    check("the change is recorded, as share-alike requires", '"zz-zz"' in changes and "Zed → Zed" in changes)

    bad = dict(entry, sha256="0" * 64)
    try:
        D.stage(bad, log=lambda *_: None)
        check("a checksum mismatch is refused", False)
    except D.ChecksumMismatch as exc:
        check("a checksum mismatch is refused", "re-pin" in str(exc))

    device = os.path.join(tmp, "device")
    D.install({"zz-zz": staged}, device, log=lambda *_: None)
    on_device = os.path.join(device, D.DEVICE_DICT_DIR, "zz-zz")
    check("install puts every staged file on the device",
          sorted(os.listdir(on_device)) == sorted(os.listdir(staged)))
    open(os.path.join(on_device, "stale.txt"), "w").close()
    D.install({"zz-zz": staged}, device, log=lambda *_: None)
    check("reinstalling replaces, it does not accumulate", not os.path.exists(os.path.join(on_device, "stale.txt")))

# ---- re-running setup does not rewrite what is already on the device ----------
import time as _t
_src = tempfile.mkdtemp(); _dev = tempfile.mkdtemp()
open(os.path.join(_src, "d.ifo"), "w").write("x" * 10)
D.install({"demo": _src}, _dev, log=lambda *a, **k: None)
_dest = os.path.join(_dev, D.DEVICE_DICT_DIR, "demo", "d.ifo")
_before = os.path.getmtime(_dest); _t.sleep(1.1)
_said = []
D.install({"demo": _src}, _dev, log=_said.append)
check("an identical dictionary is left alone on a re-run",
      os.path.getmtime(_dest) == _before and any("already installed" in m for m in _said))
open(_dest, "w").write("short")
D.install({"demo": _src}, _dev, log=lambda *a, **k: None)
check("a damaged one is replaced", os.path.getsize(_dest) == 10)

print("\n" + "=" * 60)
passed = sum(1 for r in results if r)
print(f"{len(results)} checks: {passed} passed, {len(results) - passed} FAILED")
sys.exit(0 if passed == len(results) else 1)
