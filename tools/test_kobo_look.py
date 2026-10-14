#!/usr/bin/env python3
"""The Kobo look is chosen at install and must come off without a trace.

Offline, on a synthetic device, with a stand-in for the pinned Simple UI
release. Each direction is checked on what it leaves behind: `kobo` then
`koreader` must give back the reader's settings key for key, and a Simple UI
the reader installed themselves is never touched. When LuaJIT is present every
written settings file is loaded as Lua, and every patch is compiled.

    python tools/test_kobo_look.py
"""
import copy
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from libra_cli import kobo_look as L  # noqa: E402
from libra_cli import luatable  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  ({detail})" if detail else ""))


ORIGINAL = {
    "footer": {"disabled": False, "text_font_size": 14, "all_at_once": True},
    "plugins_disabled": {"terminal": True},
    "start_with": "history",
    "cre_font": "Literata",
    "wifi_enable_action": "turn_on",
}


def fake_simpleui():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("simpleui.koplugin/main.lua", "return {}\n")
        z.writestr("simpleui.koplugin/_meta.lua", "return { name = 'simpleui' }\n")
    return buf.getvalue()


ZIP = fake_simpleui()
L.SIMPLEUI_SHA256 = hashlib.sha256(ZIP).hexdigest()
L.CACHE = tempfile.mkdtemp()
downloads = []


def download(url, sha):
    downloads.append(url)
    assert sha == L.SIMPLEUI_SHA256
    return ZIP


def device(settings=ORIGINAL):
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, ".adds/koreader/plugins/kosync.koplugin"))
    os.makedirs(os.path.join(root, ".adds/koreader/settings"))
    if settings is not None:
        with open(os.path.join(root, L.SETTINGS), "w") as f:
            f.write(luatable.dumps(settings, header="./settings.reader.lua"))
    return root


def settings(root):
    with open(os.path.join(root, L.SETTINGS)) as f:
        return luatable.loads(f.read())


def exists(root, rel):
    return os.path.exists(os.path.join(root, rel))


def lua_loads(path):
    if not shutil.which("luajit"):
        return True
    r = subprocess.run(["luajit", "-e", f"assert(type(dofile([[{path}]])) == 'table')"],
                       capture_output=True, text=True)
    return r.returncode == 0


silent = lambda *a, **k: None
apply = lambda root, look: L.apply(root, look, fat_gate=False, log=silent, download=download)

# --- koreader -> kobo ---------------------------------------------------------
root = device()
out = apply(root, "kobo")
s = settings(root)
check("reports the change", out == {"look": "kobo", "was": "koreader"}, out)
check("opens on the home screen", s.get("start_with") == "homescreen_simpleui")
check("KOReader's footer is off (the patch draws the Kobo's)", s["footer"]["disabled"] is True)
check("the rest of the footer is the reader's", s["footer"]["text_font_size"] == 14 and s["footer"]["all_at_once"])
check("records the look", s.get(L.LOOK_KEY) == "kobo")
check("records what it replaced",
      s[L.SAVED_KEY]["settings"] == {"start_with": "history", "footer.disabled": False}, s.get(L.SAVED_KEY))
check("other settings untouched", s["cre_font"] == "Literata" and s["plugins_disabled"] == {"terminal": True})
check("both patches installed", exists(root, f"{L.PATCH_DIR}/{L.LOOK_PATCH}") and exists(root, f"{L.PATCH_DIR}/{L.WIFI_PATCH}"))
check("Simple UI installed from the pinned release", exists(root, f"{L.SIMPLEUI_DIR}/main.lua") and len(downloads) == 1)
check("...with its MIT licence beside it", "MIT License" in open(os.path.join(root, L.SIMPLEUI_DIR, "LICENSE")).read())
check("the icons are on the device", exists(root, f"{L.ICON_DIR}/home.svg") and exists(root, f"{L.ICON_DIR}/knob.svg"))
sui_path = os.path.join(root, L.SIMPLEUI_SETTINGS_DIR, "sui_settings.lua")
sui = luatable.loads(open(sui_path).read())
check("Simple UI's update check is off (the code is pinned)", sui["simpleui_updater_auto_check"] is False)
check("Simple UI's welcome screen is skipped", sui["simpleui_onboarding_done"] is True)
check("the Kobo's four tabs", [sui["simpleui_action_homescreen_label"], sui["simpleui_action_home_label"],
                                sui["simpleui_qa_custom_qa_1"]["label"], sui["simpleui_qa_custom_qa_2"]["label"]]
      == ["Home", "My Books", "Discover", "More"])
check("Discover opens the catalogue, More KOReader's menu",
      sui["simpleui_qa_custom_qa_1"]["dispatcher_action"] == "opds_show_catalog"
      and sui["simpleui_qa_custom_qa_2"]["dispatcher_action"] == "show_menu")
check("written settings load as Lua", lua_loads(os.path.join(root, L.SETTINGS)) and lua_loads(sui_path))

# Every icon a setting or the patch names is one the device gets.
named = {v.rsplit("/", 1)[1] for v in re.findall(r'"(/mnt/onboard/[^"]+\.svg)"', open(sui_path).read())}
patch_text = open(os.path.join(L.REPO_PATCHES, L.LOOK_PATCH)).read()
named |= set(re.findall(r'file = "([\w.]+\.svg)"', patch_text))
shipped = set(os.listdir(L.REPO_ICONS))
check("every icon named is shipped", named and named <= shipped, sorted(named - shipped))
check("every icon path points where the installer puts them",
      all(p.startswith(L.DEVICE_ICONS + "/") for p in re.findall(r'"(/mnt/onboard/[^"]+\.svg)"', open(sui_path).read())))

# --- kobo again: refreshed, and the record of the reader's settings kept -------
apply(root, "kobo")
s2 = settings(root)
check("applying twice keeps the first record", s2[L.SAVED_KEY]["settings"]["start_with"] == "history")
check("cached release reused", len(downloads) == 1, len(downloads))

# --- kobo -> koreader -----------------------------------------------------------
out = apply(root, "koreader")
s3 = settings(root)
check("reports the change back", out == {"look": "koreader", "was": "kobo"}, out)
check("the reader's settings come back key for key", s3 == ORIGINAL, s3)
check("the look's patch is gone, the Wi-Fi patch stays",
      not exists(root, f"{L.PATCH_DIR}/{L.LOOK_PATCH}") and exists(root, f"{L.PATCH_DIR}/{L.WIFI_PATCH}"))
check("Simple UI and its settings are gone", not exists(root, L.SIMPLEUI_DIR) and not exists(root, L.SIMPLEUI_SETTINGS_DIR))
check("the icons are gone", not exists(root, L.ICON_DIR))
check("KOReader's own plugins untouched", exists(root, ".adds/koreader/plugins/kosync.koplugin"))

# --- a reader with no footer settings at all ----------------------------------------
bare = {k: v for k, v in ORIGINAL.items() if k != "footer"}
root = device(bare)
apply(root, "kobo")
apply(root, "koreader")
check("no footer before, no footer after", settings(root) == bare, settings(root))

# --- a Simple UI the reader installed (here turned off) is theirs -----------------
withoff = copy.deepcopy(ORIGINAL)
withoff["plugins_disabled"] = {"simpleui": True}
root = device(withoff)
os.makedirs(os.path.join(root, L.SIMPLEUI_DIR))
try:
    apply(root, "kobo")
    refused = False
except L.LookRefused:
    refused = True
check("a Simple UI the reader installed is never taken over", refused)
check("...and nothing was written", settings(root) == withoff and not exists(root, f"{L.PATCH_DIR}/{L.LOOK_PATCH}"))

# --- koreader on a device that never had the look -----------------------------------
root = device()
out = apply(root, "koreader")
check("only the Wi-Fi patch arrives", exists(root, f"{L.PATCH_DIR}/{L.WIFI_PATCH}")
      and not exists(root, f"{L.PATCH_DIR}/{L.LOOK_PATCH}") and not exists(root, L.SIMPLEUI_DIR))
check("settings untouched", settings(root) == ORIGINAL)

try:
    apply(root, "kindle")
    check("an unknown look is refused", False)
except ValueError:
    check("an unknown look is refused", True)

# --- the patches themselves compile ---------------------------------------------------
if shutil.which("luajit"):
    for name in sorted(os.listdir(L.REPO_PATCHES)):
        r = subprocess.run(["luajit", "-bl", os.path.join(L.REPO_PATCHES, name)], capture_output=True, text=True)
        check(f"{name} compiles", r.returncode == 0, r.stderr.strip()[:200])
else:
    print("skip  patch compilation (no luajit)")

print(f"\n{sum(results)} passed, {len(results) - sum(results)} failed")
sys.exit(0 if all(results) else 1)
