#!/usr/bin/env python3
"""The FAT gate must be able to fail, and must pass what the installer writes.

macOS can give two long filenames the same 8.3 short name; the Kobo's fsck
renames one of them at boot and KOReader dies. libra_cli/fat_check.py writes a
payload into a FAT32 image the installer's way and runs the real fsck.vfat.

The seeding logic is tested with no Docker at all. The end-to-end checks need
Docker and the cached KOReader archive, and skip -- loudly -- without them,
because a silent skip reads exactly like a pass.

    python tools/test_fat_check.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from libra_cli import fat_check  # noqa: E402
from libra_cli.fat_check import Step, check  # noqa: E402

results = []


def check_(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  ({detail})" if detail else ""))


# --- seeding mirrors names, never contents, never deeper than the path ------
with tempfile.TemporaryDirectory() as device, tempfile.TemporaryDirectory() as image:
    os.makedirs(os.path.join(device, ".adds/koreader/fonts/noto"))
    os.makedirs(os.path.join(device, "KEbooks/private"))
    with open(os.path.join(device, ".adds/koreader/fonts/README.md"), "w") as f:
        f.write("real content that must not be copied")
    with open(os.path.join(device, "KEbooks/private/book.epub"), "w") as f:
        f.write("a personal book")
    open(os.path.join(device, "._junk"), "w").close()

    fat_check._seed_names(device, ".adds/koreader/fonts", image)

    readme = os.path.join(image, ".adds/koreader/fonts/README.md")
    check_("names on the destination path are mirrored", os.path.exists(readme))
    check_("...as empty placeholders, never contents", os.path.getsize(readme) == 0)
    check_("sibling directories are mirrored as directories",
           os.path.isdir(os.path.join(image, ".adds/koreader/fonts/noto")))
    check_("nothing off the path is descended into",
           not os.path.exists(os.path.join(image, "KEbooks/private")),
           "KEbooks is named at the root but never opened")
    check_("AppleDouble files are skipped", not os.path.exists(os.path.join(image, "._junk")))

# --- end to end: the instrument must be seen to fail ------------------------
ZIP = os.path.expanduser("~/.librasync_cache/koreader/koreader-kobo-v2026.07.1.zip")
if not fat_check.docker_available():
    print("⚠️  SKIPPED end-to-end: Docker is not running")
elif not os.path.exists(ZIP):
    print(f"⚠️  SKIPPED end-to-end: no cached archive at {ZIP}")
else:
    good = check([Step("zip", ZIP, ".adds")])
    check_("ditto, as the installer extracts, passes", good.ok and good.files_written == 1045, str(good))

    bad = check([Step("zipfile", ZIP, ".adds")])
    check_("NEGATIVE CONTROL: zipfile is caught", not bad.ok and len(bad.duplicates) == 8,
           f"{len(bad.duplicates)} duplicates")
    check_("...and names the files the bricked installs lost",
           any(d.endswith("filemanager.lua") for d in bad.duplicates)
           and any(d.endswith("dis_arm64.lua") for d in bad.duplicates))

print("\n" + "=" * 60)
passed = sum(1 for r in results if r)
print(f"{len(results)} checks: {passed} passed, {len(results) - passed} FAILED")
sys.exit(0 if passed == len(results) else 1)
