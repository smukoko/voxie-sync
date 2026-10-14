"""How KOReader looks on the Kobo: as KOReader, or as the Kobo's own software.

Chosen as the last step of `voxie setup-kobo` (`--look kobo|koreader`, or the
setup page), and changed at any time with `voxie look kobo|koreader`, Kobo
plugged in. Both ways leave the device exactly as the other found it.

    koreader  KOReader as it ships, with Voxie's reading profile.
    kobo      The Kobo's look (photographed on a Clara 2E, firmware 4.38):
              a home screen with the books you are reading, a status bar,
              tabs along the bottom (Home, My Books, Discover, More), My Books
              as covers with titles and authors, and while reading the
              chapter above the text, the book and a hairline of progress
              below it, and a toolbar on a tap in the middle of the page.

What `kobo` puts on the device, and `koreader` takes off again:

  - Simple UI (github.com/doctorhetfield-cmd/simpleui.koplugin, MIT), pinned
    by sha256 like KOReader itself, in .adds/koreader/plugins/: the home
    screen, status bar and tabs. Its settings file is written whole, with the
    Kobo's layout, and its once-a-day update check off (an update would
    replace the pinned code; its own menu can still update it by hand).
  - koreader_patches/2-kobo-look.lua in .adds/koreader/patches/: the reading
    screen, and My Books without file sizes (in memory only).
  - the icons in .adds/voxie/look/.
  - two of KOReader's settings, each recorded first under
    librasync_look_saved in settings.reader.lua: open on the home screen
    instead of History, and KOReader's footer off (the patch draws the
    Kobo's). `koreader` puts back exactly what was there.

A reader who already installed Simple UI keeps it: the Kobo look then refuses,
rather than overwrite a home screen they set up themselves.

Separately, and for both looks, koreader_patches/2-wifi-in-background.lua is
installed: progress sync never holds the screen while Wi-Fi connects.

Everything is staged on the Mac and passes the Kobo's own filesystem check
(libra_cli/fat_check.py) before a byte is written, like the reading kit.
Nothing here opens a KOReader database: on a FAT volume the Mac's sqlite
leaves -wal and -shm files beside a WAL database (seen 2026-10-14).
"""
import os
import shutil

from libra_cli import luatable
from libra_cli.reading_profile import _load, _write

LOOKS = ("kobo", "koreader")

SIMPLEUI_TAG = "2.7.5"
SIMPLEUI_URL = ("https://github.com/doctorhetfield-cmd/simpleui.koplugin/releases/download/"
                f"{SIMPLEUI_TAG}/simpleui.koplugin.zip")
SIMPLEUI_SHA256 = "d09143a3c1d6867d8cbcf2f18f8262dde894e11d7412156d50c8784c87beea12"

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REPO_PATCHES = os.path.join(REPO, "koreader_patches")
REPO_ICONS = os.path.join(REPO, "assets", "kobo_look")
# Simple UI's release zip carries no licence; MIT wants it beside the code.
REPO_SIMPLEUI_LICENSE = os.path.join(REPO, "assets", "simpleui", "LICENSE")
CACHE = os.path.expanduser(os.getenv("LIBRASYNC_CACHE", "~/.librasync_cache"))

SETTINGS = ".adds/koreader/settings.reader.lua"
PATCH_DIR = ".adds/koreader/patches"
PLUGIN_DIR = ".adds/koreader/plugins"
SIMPLEUI_DIR = ".adds/koreader/plugins/simpleui.koplugin"
SIMPLEUI_SETTINGS_DIR = ".adds/koreader/settings/simpleui"
ICON_DIR = ".adds/voxie/look"
DEVICE_ICONS = "/mnt/onboard/" + ICON_DIR

WIFI_PATCH = "2-wifi-in-background.lua"
LOOK_PATCH = "2-kobo-look.lua"

LOOK_KEY = "librasync_look"
SAVED_KEY = "librasync_look_saved"
ABSENT = "__absent__"   # a Lua table cannot hold nil: "this key was not set"

# settings.reader.lua while the Kobo look is on. A dotted key is a nested one.
KOBO_SETTINGS = {
    "start_with": "homescreen_simpleui",
    "footer.disabled": True,
}


def _lua_arrays(value):
    """Lists as luatable writes arrays: tables keyed 1..n."""
    if isinstance(value, list):
        return {i + 1: _lua_arrays(v) for i, v in enumerate(value)}
    if isinstance(value, dict):
        return {k: _lua_arrays(v) for k, v in value.items()}
    return value


def kobo_simpleui_settings(icons=DEVICE_ICONS):
    """Simple UI's whole settings file for the Kobo's layout (Simple UI 2.7.5's keys)."""
    return _lua_arrays({
        "simpleui_onboarding_done": True,
        "simpleui_updater_auto_check": False,
        "simpleui_enabled": True,
        # The Kobo's status bar: the time on the left, light, Wi-Fi and battery on the right.
        "simpleui_topbar_enabled": True,
        "simpleui_topbar_swipe_indicator": False,
        "simpleui_topbar_config": {
            "side": {"clock": "left", "frontlight": "right", "wifi": "right", "battery": "right"},
            "order_left": ["clock"],
            "order_right": ["frontlight", "wifi", "battery"],
        },
        # The Kobo's tabs, icon over label.
        "simpleui_bar_enabled": True,
        "simpleui_bar_mode": "both",
        "simpleui_bar_style": "default",
        "simpleui_bar_tabs": ["homescreen", "home", "custom_qa_1", "custom_qa_2"],
        "simpleui_action_homescreen_label": "Home",
        "simpleui_action_homescreen_icon": f"{icons}/home.svg",
        "simpleui_action_home_label": "My Books",
        "simpleui_action_home_icon": f"{icons}/books.svg",
        "simpleui_qa_list": ["custom_qa_1", "custom_qa_2"],
        # Discover is the catalogue, where Voxie's library is; More is KOReader's own menu.
        "simpleui_qa_custom_qa_1": {"label": "Discover", "icon": f"{icons}/discover.svg",
                                    "dispatcher_action": "opds_show_catalog"},
        "simpleui_qa_custom_qa_2": {"label": "More", "icon": f"{icons}/more.svg",
                                    "dispatcher_action": "show_menu"},
        "simpleui_qs_bar_enabled": False,
        # The Kobo's home: the books you are reading in a row, then what is new.
        "simpleui_layout": {"pages": [{"id": 1, "modules": ["recent", "new_books"]}]},
        "simpleui_hs_recent_enabled": True,
        "simpleui_hs_new_books_enabled": True,
        "simpleui_hs_currently_enabled": False,
        "simpleui_hs_collections_enabled": False,
        "simpleui_hs_action_list_enabled": False,
        "simpleui_hide_label_recent": True,
        "simpleui_hs_recent_show_frame": False,
        "simpleui_hs_recent_show_text": True,
        # The Kobo writes under a cover in its serif, not in bold.
        "simpleui_hs_recent_text_font_label": "Literata",
        "simpleui_hs_recent_text_variant_label": "regular",
        "simpleui_hs_new_books_text_font_label": "Literata",
        "simpleui_hs_new_books_text_variant_label": "regular",
    })


class LookRefused(Exception):
    pass


def current_look(kobo_path):
    settings = _load(os.path.join(kobo_path, SETTINGS))
    return "kobo" if settings.get(LOOK_KEY) == "kobo" else "koreader"


def _get(table, dotted):
    *path, last = dotted.split(".")
    for p in path:
        table = table.get(p)
        if not isinstance(table, dict):
            return ABSENT
    return table.get(last, ABSENT)


def _set(table, dotted, value):
    *path, last = dotted.split(".")
    if value != ABSENT:
        for p in path:
            table = table.setdefault(p, {})
        table[last] = value
        return
    # Taking a key away: also the tables that held only it, as the look found
    # no table there either (an empty one reads the same to KOReader).
    chain = [table]
    for p in path:
        if not isinstance(chain[-1].get(p), dict):
            return
        chain.append(chain[-1][p])
    chain[-1].pop(last, None)
    for parent, name, child in reversed(list(zip(chain, path, chain[1:]))):
        if child:
            break
        parent.pop(name)


def _simpleui_zip(log, download):
    """The pinned Simple UI release, cached on the Mac after the first download."""
    import hashlib
    path = os.path.join(CACHE, "look", f"simpleui-{SIMPLEUI_TAG}.zip")
    if os.path.exists(path):
        with open(path, "rb") as f:
            if hashlib.sha256(f.read()).hexdigest() == SIMPLEUI_SHA256:
                return path
    log(f"   ⬇️  Downloading Simple UI {SIMPLEUI_TAG} (pinned, sha256-verified)...")
    data = download(SIMPLEUI_URL, SIMPLEUI_SHA256)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".part", "wb") as f:
        f.write(data)
    os.replace(path + ".part", path)
    return path


def stage(look, log=print, download=None):
    """Everything this look writes, laid out on the Mac. Returns {name: path}."""
    root = os.path.join(CACHE, "look", "staged")
    if os.path.exists(root):
        shutil.rmtree(root)
    patches = os.path.join(root, "patches")
    os.makedirs(patches)
    shutil.copy2(os.path.join(REPO_PATCHES, WIFI_PATCH), patches)
    staged = {"patches": patches}
    if look == "kobo":
        shutil.copy2(os.path.join(REPO_PATCHES, LOOK_PATCH), patches)
        icons = os.path.join(root, "icons")
        shutil.copytree(REPO_ICONS, icons)
        sui = os.path.join(root, "simpleui")
        os.makedirs(sui)
        with open(os.path.join(sui, "sui_settings.lua"), "w", encoding="utf-8") as f:
            f.write(luatable.dumps(kobo_simpleui_settings(),
                                   header="/mnt/onboard/.adds/koreader/settings/simpleui/sui_settings.lua"))
        licence = os.path.join(root, "simpleui_licence")
        os.makedirs(licence)
        shutil.copy2(REPO_SIMPLEUI_LICENSE, licence)
        staged.update(icons=icons, simpleui_settings=sui, simpleui_licence=licence)
        if download is not None:
            staged["simpleui_zip"] = _simpleui_zip(log, download)
    return staged


def fat_steps(staged):
    from libra_cli.fat_check import Step
    steps = [Step("tree", staged["patches"], PATCH_DIR)]
    if "simpleui_zip" in staged:
        steps.append(Step("zip", staged["simpleui_zip"], PLUGIN_DIR))
        steps.append(Step("tree", staged["simpleui_licence"], SIMPLEUI_DIR))
    if "icons" in staged:
        steps.append(Step("tree", staged["icons"], ICON_DIR))
    if "simpleui_settings" in staged:
        steps.append(Step("tree", staged["simpleui_settings"], SIMPLEUI_SETTINGS_DIR))
    return steps


def _copy(src, dest, what, archive=False):
    """Merge a staged folder (or extract a zip) into `dest` on the device.

    ditto on the Mac, as every installer write; Python's own copy elsewhere,
    which only the tests ever reach (the installer itself is macOS-only).
    """
    os.makedirs(dest, exist_ok=True)
    if shutil.which("ditto"):
        import subprocess
        r = subprocess.run(["ditto", *(["-x", "-k"] if archive else []), src, dest],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"could not copy {what} to the device: {r.stderr.strip()}")
    elif archive:
        import zipfile
        with zipfile.ZipFile(src) as z:
            z.extractall(dest)
    else:
        shutil.copytree(src, dest, dirs_exist_ok=True)


def _write_settings(kobo_path, look, simpleui_ours):
    """Turn the look's KOReader settings on or off, keeping what they replaced."""
    path = os.path.join(kobo_path, SETTINGS)
    settings = _load(path)
    saved = settings.get(SAVED_KEY)
    if look == "kobo":
        if not isinstance(saved, dict):
            saved = {"settings": {k: _get(settings, k) for k in KOBO_SETTINGS},
                     "simpleui_installed": simpleui_ours}
        for key, value in KOBO_SETTINGS.items():
            _set(settings, key, value)
        disabled = settings.get("plugins_disabled")
        if isinstance(disabled, dict) and disabled.pop("simpleui", None) is not None and not disabled:
            settings.pop("plugins_disabled")
        settings[LOOK_KEY] = "kobo"
        settings[SAVED_KEY] = saved
    else:
        if isinstance(saved, dict):
            for key, value in (saved.get("settings") or {}).items():
                _set(settings, key, value)
        settings.pop(LOOK_KEY, None)
        settings.pop(SAVED_KEY, None)
    _write(path, settings, "./settings.reader.lua")
    return saved if isinstance(saved, dict) else {}


def apply(kobo_path, look, fat_gate=True, log=print, download=None):
    """Put `look` on the Kobo at `kobo_path`. Returns what happened, for the JSON line.

    `download(url, sha256) -> bytes` fetches the pinned Simple UI (main.download_pinned).
    """
    if look not in LOOKS:
        raise ValueError(f"look must be one of {', '.join(LOOKS)}, not {look!r}")
    was = current_look(kobo_path)
    simpleui_dir = os.path.join(kobo_path, SIMPLEUI_DIR)
    simpleui_ours = True
    if look == "kobo" and was != "kobo" and os.path.isdir(simpleui_dir):
        raise LookRefused("Simple UI is already installed on this Kobo, with your own home screen. "
                          "Voxie leaves it as it is; the Kobo look was not applied.")
    if download is None:
        from libra_cli.main import download_pinned as download

    staged = stage(look, log, download)
    if fat_gate:
        from libra_cli.fat_check import check
        log("   🛡️  Checking the look against the Kobo's own filesystem check...")
        result = check(fat_steps(staged), seed_root=kobo_path)
        if not result.ok:
            from libra_cli.reading_kit import GateRefused
            raise GateRefused(f"{result}\n   Nothing was written to the Kobo. "
                              + ("Start Docker Desktop and run setup again." if "Docker" in result.reason
                                 else "Report this: a file in the payload would be renamed at boot."))
        log(f"   ✅ {result}")
    else:
        log("   ⚠️  FAT check SKIPPED on request.")

    patch_dir = os.path.join(kobo_path, PATCH_DIR)
    _copy(staged["patches"], patch_dir, "the KOReader patches")

    if look == "kobo":
        _copy(staged["simpleui_zip"], os.path.join(kobo_path, PLUGIN_DIR), "Simple UI", archive=True)
        if not os.path.exists(os.path.join(simpleui_dir, "main.lua")):
            raise RuntimeError("Simple UI did not arrive intact on the device (no main.lua)")
        _copy(staged["simpleui_licence"], simpleui_dir, "Simple UI's licence")
        _copy(staged["icons"], os.path.join(kobo_path, ICON_DIR), "the look's icons")
        _copy(staged["simpleui_settings"], os.path.join(kobo_path, SIMPLEUI_SETTINGS_DIR), "Simple UI's settings")
        _write_settings(kobo_path, "kobo", simpleui_ours)
        log("   🎨 Kobo look: home screen, tabs, My Books and the reading screen of a Kobo. "
            "`voxie look koreader` takes it off again.")
    elif was == "kobo":
        saved = _write_settings(kobo_path, "koreader", False)
        patch = os.path.join(patch_dir, LOOK_PATCH)
        if os.path.exists(patch):
            os.remove(patch)
        if saved.get("simpleui_installed"):
            shutil.rmtree(simpleui_dir, ignore_errors=True)
            shutil.rmtree(os.path.join(kobo_path, SIMPLEUI_SETTINGS_DIR), ignore_errors=True)
        shutil.rmtree(os.path.join(kobo_path, ICON_DIR), ignore_errors=True)
        log("   🎨 KOReader's own look is back, with your settings as they were.")
    log("   📶 Progress sync waits for Wi-Fi in the background, never on screen.")
    return {"look": look, "was": was}
