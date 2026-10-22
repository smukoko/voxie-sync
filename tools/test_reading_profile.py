#!/usr/bin/env python3
"""The reading profile edits a reader's settings, so it is tested on what it keeps.

Offline, on a synthetic device. When LuaJIT is present the edited file is also
loaded as Lua -- a settings.reader.lua KOReader cannot parse is a reader who
loses every preference they have -- and compared key by key with the original.

    python tools/test_reading_profile.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from libra_cli import reading_profile as RP  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  ({detail})" if detail else ""))


ORIGINAL = '''return {
    ["footer"] = {
        ["text_font_size"] = 14,
        ["all_at_once"] = true,
    },
    ["plugins_disabled"] = {
        ["japanese"] = true,
    },
    ["screensaver_type"] = "disable",
    ["wifi_enable_action"] = "turn_on",
}'''


def device(settings=ORIGINAL, plugins=("terminal", "SSH", "coverbrowser", "japanese", "kosync")):
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, RP.PLUGINS_DIR))
    for p in plugins:
        os.makedirs(os.path.join(root, RP.PLUGINS_DIR, p + ".koplugin"))
    if settings is not None:
        with open(os.path.join(root, RP.SETTINGS), "w") as f:
            f.write(settings)
    return root


def read(root, suffix=""):
    return open(os.path.join(root, RP.SETTINGS + suffix)).read()


silent = lambda *a, **k: None

root = device()
out = RP.apply_profile(root, fonts_installed=True, log=silent)
text = read(root)
check("only plugins present on the device are disabled", sorted(out["disabled"]) == ["SSH", "terminal"], str(out["disabled"]))
check("a plugin the reader had already disabled stays disabled", '["japanese"] = true' in text)
check("plugins the reader uses are left alone", "coverbrowser" not in text and "kosync" not in text)
check("Literata becomes the default font", '["cre_font"] = "Literata"' in text)
check("the sleep screen is not touched", '["screensaver_type"] = "disable"' in text)
check("the original is kept beside it", read(root, RP.BACKUP_SUFFIX) == ORIGINAL)

# Applied once: a reader who turns terminal back on must not lose it on re-run.
reenabled = text.replace('\n        ["terminal"] = true,', "")
with open(os.path.join(root, RP.SETTINGS), "w") as f:
    f.write(reenabled)
again = RP.apply_profile(root, fonts_installed=True, log=silent)
check("a re-run never re-disables what the reader turned back on",
      again["applied"] is False and '["terminal"]' not in read(root))
check("...and never overwrites the pristine backup", read(root, RP.BACKUP_SUFFIX) == ORIGINAL)

root2 = device(settings=ORIGINAL.replace("return {", 'return {\n    ["cre_font"] = "EB Garamond",'))
RP.apply_profile(root2, fonts_installed=True, log=silent)
check("a font the reader chose is kept", '"EB Garamond"' in read(root2) and "Literata" not in read(root2))

root3 = device()
RP.apply_profile(root3, fonts_installed=False, log=silent)
check("no default font is set when the fonts were not installed", "cre_font" not in read(root3))

root4 = device(settings=ORIGINAL.replace('    ["plugins_disabled"] = {\n        ["japanese"] = true,\n    },\n', ""))
RP.apply_profile(root4, fonts_installed=True, log=silent)
check("a settings file with no plugins_disabled gets one", '["plugins_disabled"] = {' in read(root4))

root5 = device(settings=None)
RP.apply_profile(root5, fonts_installed=True, log=silent)
from libra_cli import luatable as _lt
_owned = {"plugins_disabled", "cre_font", "start_with", "quickstart_shown_version", RP.MARKER, RP.VERSION_KEY, *RP.TYPOGRAPHY}
check("a brand-new device gets only the profile's own keys, not a guessed file",
      set(_lt.loads(read(root5))) <= _owned, str(set(_lt.loads(read(root5))) - _owned))

# --- the edited files must still be Lua that KOReader can load --------------------
lua = shutil.which("luajit")
if not lua:
    print("⚠️  SKIPPED Lua validation: luajit not installed")
else:
    probe = os.path.join(tempfile.mkdtemp(), "probe.lua")
    with open(probe, "w") as f:
        f.write('local t = dofile(arg[1]); assert(type(t) == "table"); '
                'local b = arg[2] and dofile(arg[2]); '
                'if b then for k in pairs(b) do assert(t[k] ~= nil, "lost " .. k) end end; print("ok")')
    for label, r in (("edited", root), ("fresh plugins_disabled", root4), ("brand-new", root5)):
        backup = os.path.join(r, RP.SETTINGS + RP.BACKUP_SUFFIX)
        args = [lua, probe, os.path.join(r, RP.SETTINGS)] + ([backup] if os.path.exists(backup) and r != root5 else [])
        res = subprocess.run(args, capture_output=True, text=True)
        check(f"{label} file loads in LuaJIT and keeps every key", res.stdout.strip() == "ok", res.stderr.strip()[:80])


# ---- v2 ---------------------------------------------------------------------
t = _lt.loads(read(root))
check("v2: calmer page defaults are set", t.get("copt_line_spacing") == 120 and t.get("copt_font_size") == 20
      and t.get("copt_h_page_margins") == {1: 15, 2: 15} and t.get("copt_word_expansion") == 5)
check("v2: opens on History, the cover grid", t.get("start_with") == "history")
check("v2: cloud storage and wireless Calibre stay enabled",
      "cloudstorage" not in RP.DISABLE and "calibre" not in RP.DISABLE)

v1 = device(settings='''return {
    ["librasync_profile_applied"] = true,
    ["plugins_disabled"] = {},
    ["copt_line_spacing"] = 130,
    ["cre_font"] = "Noto Serif",
}''')
res = RP.apply_profile(v1, fonts_installed=True, log=silent)
t = _lt.loads(read(v1))
check("a v1 device gets v2 without plugins being disabled again", res["disabled"] == [] and not t.get("plugins_disabled"))
check("a line spacing the reader chose is kept", t["copt_line_spacing"] == 130)
check("KOReader's default font becomes Literata", t["cre_font"] == "Literata")
check("v2 applies once", RP.apply_profile(v1, fonts_installed=True, log=silent)["applied"] is False)

# ---- v3: the quick-start manual ---------------------------------------------
check("v3: a first launch does not open KOReader's manual",
      _lt.loads(read(root)).get("quickstart_shown_version") == RP.QUICKSTART_SEEN)
check("v3: a v1 device is marked too", t.get("quickstart_shown_version") == RP.QUICKSTART_SEEN)
v2 = device(settings='''return {
    ["librasync_profile_version"] = 2,
    ["start_with"] = "history",
}''')
res = RP.apply_profile(v2, fonts_installed=True, log=silent)
check("v3: a v2 device gets only the mark", res["applied"] and _lt.loads(read(v2)).get("quickstart_shown_version") == RP.QUICKSTART_SEEN
      and _lt.loads(read(v2)).get("copt_font_size") is None)
seen = device(settings='''return {
    ["librasync_profile_version"] = 2,
    ["quickstart_shown_version"] = 202701010000,
}''')
RP.apply_profile(seen, fonts_installed=True, log=silent)
check("v3: a guide KOReader recorded as seen keeps its own number",
      _lt.loads(read(seen))["quickstart_shown_version"] == 202701010000)

kobo = tempfile.mkdtemp()
def sidecar(where, body):
    d = os.path.join(kobo, where); os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "metadata.epub.lua"), "w") as f: f.write(body)
sidecar("A.sdr", 'return {\n    ["font_face"] = "Noto Serif",\n    ["copt_line_spacing"] = 100,\n    ["copt_font_size"] = 24,\n    ["last_xpointer"] = "/body/p[3]",\n}')
sidecar("KEbooks/B.sdr", 'return {\n    ["font_face"] = "Noto Serif",\n    ["copt_line_spacing"] = 100,\n}')
backup = tempfile.mkdtemp()
done = RP.restyle_books(kobo, fonts_installed=True, backup_dir=backup, log=silent)
a = _lt.loads(open(os.path.join(kobo, "A.sdr", "metadata.epub.lua")).read())
check("an opened book's default font and spacing are restyled", done == ["A"] and a["font_face"] == "Literata"
      and a["copt_line_spacing"] == 120)
check("...its chosen font size and reading position are kept", a["copt_font_size"] == 24 and a["last_xpointer"] == "/body/p[3]")
check("...and the original is backed up on the Mac", os.path.exists(os.path.join(backup, "A.sdr", "metadata.epub.lua")))
check("KEbooks is never touched", "Noto Serif" in open(os.path.join(kobo, "KEbooks", "B.sdr", "metadata.epub.lua")).read())

print("\n" + "=" * 60)
passed = sum(1 for r in results if r)
print(f"{len(results)} checks: {passed} passed, {len(results) - passed} FAILED")
sys.exit(0 if passed == len(results) else 1)
