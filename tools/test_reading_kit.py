#!/usr/bin/env python3
"""The reading kit writes nothing to a Kobo until the whole payload is proven.

Offline: staging and the FAT gate are replaced with fakes, because what is
under test is the ORDER -- stage, gate, then write -- and what happens to the
device when the gate says no.

    python tools/test_reading_kit.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from libra_cli import reading_kit as K  # noqa: E402
from libra_cli.fat_check import FatResult  # noqa: E402

# The Kobo installer is macOS-only for now: it extracts with Apple's ditto
# (core/platform_support.py), which other systems do not have.
if sys.platform != "darwin":
    print("SKIP  the Kobo installer runs on macOS only (it needs ditto)")
    sys.exit(0)

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  ({detail})" if detail else ""))


check("'default' means the manifest's defaults", K.parse_dict_spec("default") is None)
check("'none' means none", K.parse_dict_spec("none") == [])
check("a list is split and trimmed", K.parse_dict_spec(" en-en, fr-fr ") == ["en-en", "fr-fr"])

with tempfile.TemporaryDirectory() as tmp:
    staged_dict = os.path.join(tmp, "staged", "en-en")
    staged_fonts = os.path.join(tmp, "staged_fonts")
    for d in (staged_dict, staged_fonts):
        os.makedirs(d)
    open(os.path.join(staged_dict, "en-en.ifo"), "w").write("bookname=English\n")
    open(os.path.join(staged_fonts, "Zed-Regular.ttf"), "wb").write(b"font")

    K.D.stage = lambda e, log=None: staged_dict
    K.F.stage = lambda fams, log=None: staged_fonts
    K.D.entries = lambda ids=None: [{"id": "en-en"}] if ids != [] else []
    K.F.families = lambda ids=None: [{"family": "Zed"}]
    silent = lambda *a, **k: None

    def device():
        d = os.path.join(tmp, "device")
        if os.path.exists(d):
            import shutil
            shutil.rmtree(d)
        os.makedirs(d)
        return d

    def written(dev):
        return sorted(os.path.relpath(os.path.join(r, f), dev) for r, _, fs in os.walk(dev) for f in fs)

    seen = {}
    def refusing_gate(steps, seed_root=None):
        seen["seed"] = seed_root
        seen["steps"] = [(s.kind, s.dest) for s in steps]
        return FatResult(ok=False, duplicates=["/fonts/ZED-RE~1.TTF"])
    K.check = refusing_gate

    dev = device()
    try:
        K.install_reading_kit(dev, log=silent)
        check("a failed gate raises", False)
    except K.GateRefused as exc:
        check("a failed gate raises", "Nothing was written" in str(exc))
    check("...and NOTHING reached the device", written(dev) == [], str(written(dev)))
    check("the gate saw the whole payload at once",
          ("tree", ".adds/koreader/data/dict/en-en") in seen["steps"] and ("tree", "fonts") in seen["steps"],
          str(seen["steps"]))
    check("the gate was seeded from the device itself", seen["seed"] == dev)

    K.check = lambda steps, seed_root=None: FatResult(ok=False, reason="Docker is not running; ...")
    dev = device()
    try:
        K.install_reading_kit(dev, log=silent)
        check("no Docker means no install, not an unchecked one", False)
    except K.GateRefused as exc:
        check("no Docker means no install, not an unchecked one",
              "Start Docker" in str(exc) and written(dev) == [])

    K.check = lambda steps, seed_root=None: FatResult(ok=True, files_written=2)
    dev = device()
    out = K.install_reading_kit(dev, log=silent)
    check("a passed gate installs both", written(dev) == [".adds/koreader/data/dict/en-en/en-en.ifo", "fonts/Zed-Regular.ttf"],
          str(written(dev)))
    check("and reports what it installed", out == {"dictionaries": ["en-en"], "fonts": ["Zed"]}, str(out))

    called = []
    K.check = lambda *a, **k: called.append(1) or FatResult(ok=True)
    dev = device()
    K.install_reading_kit(dev, fat_gate=False, log=silent)
    check("the escape hatch really skips the gate", called == [] and len(written(dev)) == 2)

    dev = device()
    out = K.install_reading_kit(dev, dict_ids=[], with_fonts=False, log=silent)
    check("nothing selected writes nothing", out == {"dictionaries": [], "fonts": []} and written(dev) == [])

print("\n" + "=" * 60)
passed = sum(1 for r in results if r)
print(f"{len(results)} checks: {passed} passed, {len(results) - passed} FAILED")
sys.exit(0 if passed == len(results) else 1)
