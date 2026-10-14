#!/usr/bin/env python3
"""Reading fonts: four real styles per family, pinned, merged into the device.

Offline: the manifest is checked as data, and staging/installing run against a
synthetic family in a temporary cache.

    python tools/test_fonts.py
"""
import hashlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from libra_cli import fonts as F  # noqa: E402

# The Kobo installer is macOS-only for now: it extracts with Apple's ditto
# (core/platform_support.py), which other systems do not have.
if sys.platform != "darwin":
    print("SKIP  the Kobo installer runs on macOS only (it needs ditto)")
    sys.exit(0)

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  ({detail})" if detail else ""))


fams = F.families()
check("three families ship by default", [f["family"] for f in fams] == ["Literata", "Crimson Pro", "EB Garamond"])
# crengine finds emphasis by separate files; a family without its italic fakes it.
check("every family has Regular, Italic, Bold and BoldItalic",
      all(sorted(x["file"].split("-")[-1] for x in f["files"]) ==
          ["Bold.ttf", "BoldItalic.ttf", "Italic.ttf", "Regular.ttf"] for f in fams))
check("every file is pinned to a commit, not a branch",
      all(f["commit"] in x["url"] and len(f["commit"]) == 40 for f in fams for x in f["files"]))
check("every file carries a sha256", all(len(x["sha256"]) == 64 for f in fams for x in f["files"]))
check("every family ships its OFL", all(f["license_file"]["file"].startswith("OFL-") for f in fams))
check("fonts go to the device's own fonts/ folder", F.DEVICE_FONT_DIR == "fonts")

with tempfile.TemporaryDirectory() as tmp:
    F.CACHE = os.path.join(tmp, "cache")
    src = os.path.join(F.CACHE, "fonts", "src")
    os.makedirs(src)

    def item(name, body):
        open(os.path.join(src, name), "wb").write(body)
        return {"file": name, "url": "https://example.invalid/" + name,
                "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}

    fam = {"id": "Zed", "family": "Zed",
           "files": [item(f"Zed-{s}.ttf", f"font {s}".encode()) for s in ("Regular", "Italic", "Bold", "BoldItalic")],
           "license_file": item("OFL-Zed.txt", b"OFL")}

    staged = F.stage([fam], log=lambda *_: None)
    check("staging is one flat folder of fonts and licence",
          sorted(os.listdir(staged)) == ["OFL-Zed.txt", "Zed-Bold.ttf", "Zed-BoldItalic.ttf", "Zed-Italic.ttf", "Zed-Regular.ttf"])

    device = os.path.join(tmp, "device")
    os.makedirs(os.path.join(device, "fonts"))
    open(os.path.join(device, "fonts", "MySideloaded-Regular.ttf"), "wb").write(b"mine")
    F.install(staged, device, log=lambda *_: None)
    on_device = sorted(os.listdir(os.path.join(device, "fonts")))
    check("install puts every staged file on the device", all(n in on_device for n in os.listdir(staged)))
    check("a font the user sideloaded is kept", "MySideloaded-Regular.ttf" in on_device)

    fam_bad = dict(fam, files=[dict(fam["files"][0], sha256="0" * 64)] + fam["files"][1:])
    try:
        F.stage([fam_bad], log=lambda *_: None)
        check("a checksum mismatch is refused", False)
    except F.ChecksumMismatch:
        check("a checksum mismatch is refused", True)

try:
    F.families(["Comic-Sans"])
    check("an unknown family is refused", False)
except ValueError:
    check("an unknown family is refused", True)

print("\n" + "=" * 60)
passed = sum(1 for r in results if r)
print(f"{len(results)} checks: {passed} passed, {len(results) - passed} FAILED")
sys.exit(0 if passed == len(results) else 1)
