"""A reading profile: a KOReader that feels like a library, not a Linux box.

Applied by setup_kobo to settings.reader.lua, once per profile version. Every
change stays reversible on the device, and nothing the reader chose is
overwritten: a value is only changed while it is still KOReader's own default.

v1: developer plugins tucked away (Tools > Plugin management brings any back),
    and Literata as the default font when the reading fonts were installed.
v2: a calmer page, verified on screen in KOReader 2026.07.1 at a Clara 2E's
    300dpi -- Literata 20, line spacing 120%, margins 15/20/15, strokes +0.5
    for e-ink, and word expansion so justified lines stretch letters a little
    rather than opening rivers between words. Same page count as before (776
    against 783), so the text is no bigger, just easier to read. The reader
    now opens on History, a grid of covers, instead of the drive's root.
    Wireless Calibre and cloud storage are no longer disabled: they are how
    books reach the device when the gateway does not serve them.

v3: KOReader's quick-start guide is marked as seen. A first launch from the
    Voxie button used to open its manual instead of the reader's covers
    (user, 2026-10-22, a from-scratch install). Only a device that never
    recorded the guide gets the mark; one that has seen it keeps its own.

What it does NOT touch: the sleep screen, the status bar, page refresh,
statistics. Those are taste.

Books already opened keep their own settings in their .sdr sidecar, which is
why v1's font never showed: every book had saved KOReader's Noto Serif. So v2
also restyles the sidecars of books at the volume root -- the ones Voxie
delivered -- again only where a value is still the default. KEbooks, the
reader's own library, is never touched.
"""
import glob
import os
import shutil
import time

from libra_cli import luatable

SETTINGS = ".adds/koreader/settings.reader.lua"
PLUGINS_DIR = ".adds/koreader/plugins"
BACKUP_SUFFIX = ".before-librasync"
MARKER = "librasync_profile_applied"          # v1, kept for devices that have it
VERSION_KEY = "librasync_profile_version"
PROFILE_VERSION = 3
# KOReader opens its quick-start manual while quickstart_shown_version is below
# its own; tools/kosim/setup.sh uses the same value for the pinned 2026.07.1.
QUICKSTART_SEEN = 202607010000

# Keys are plugin folder names without ".koplugin" -- that is what
# pluginloader.lua indexes plugins_disabled by, and it is case-sensitive.
DISABLE = {
    "terminal": "a Linux shell",
    "SSH": "an SSH server",
    "texteditor": "a plain-text editor",
    "hello": "KOReader's example plugin",
    "httpinspector": "a developer HTTP inspector",
    "wallabag": "a read-it-later service",
    "newsdownloader": "an RSS downloader",
}

DEFAULT_FONT = "Literata"
KOREADER_DEFAULT_FONT = "Noto Serif"

# (ours, KOReader's default). A value still at the default is not a choice.
TYPOGRAPHY = {
    "copt_font_size": (20, 22),
    "copt_line_spacing": (120, 100),
    "copt_h_page_margins": ({1: 15, 2: 15}, {1: 10, 2: 10}),
    "copt_t_page_margin": (20, 15),
    "copt_b_page_margin": (15, 15),
    "copt_font_base_weight": (0.5, 0),
    "copt_word_expansion": (5, 0),
}


def _plugins_on_device(kobo_path):
    d = os.path.join(kobo_path, PLUGINS_DIR)
    if not os.path.isdir(d):
        return set()
    return {e[:-len(".koplugin")] for e in os.listdir(d) if e.endswith(".koplugin")}


def _load(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return luatable.loads(f.read())


def _write(path, table, header):
    # In place, on the existing directory entry: a new file beside it is what
    # can collide with an 8.3 short name on the Kobo's FAT.
    text = luatable.dumps(table, header=header)
    mode = "r+" if os.path.exists(path) else "w"
    with open(path, mode, encoding="utf-8") as f:
        f.seek(0)
        f.write(text)
        f.truncate()


def apply_profile(kobo_path, fonts_installed, log=print, keep_plugins=False):
    """Bring settings.reader.lua up to PROFILE_VERSION. Returns what changed.

    keep_plugins: KOReader was already on the device. Its owner may use SSH,
    the terminal or the RSS downloader, and nothing here can tell, so their
    plugins are left as they are; only default-valued typography changes.
    """
    path = os.path.join(kobo_path, SETTINGS)
    settings = _load(path)
    version = settings.get(VERSION_KEY) or (1 if settings.get(MARKER) else 0)
    if version >= PROFILE_VERSION:
        log("   ℹ️  Reading profile already applied — leaving your settings alone.")
        return {"applied": False, "disabled": [], "font": None, "typography": []}

    if os.path.exists(path) and not os.path.exists(path + BACKUP_SUFFIX):
        shutil.copy2(path, path + BACKUP_SUFFIX)

    newly, font, styled = [], None, []
    if version < 1 and not keep_plugins:
        present = _plugins_on_device(kobo_path)
        disabled = settings.setdefault("plugins_disabled", {})
        newly = [p for p in DISABLE if p in present and not disabled.get(p)]
        for p in newly:
            disabled[p] = True
        if not disabled:
            settings.pop("plugins_disabled")

    if version < 2:
        if fonts_installed and settings.get("cre_font") in (None, KOREADER_DEFAULT_FONT):
            settings["cre_font"] = font = DEFAULT_FONT
        for key, (ours, _) in TYPOGRAPHY.items():
            if key not in settings:
                settings[key] = ours
                styled.append(key)
        settings.setdefault("start_with", "history")

    if version < 3:
        settings.setdefault("quickstart_shown_version", QUICKSTART_SEEN)

    settings[MARKER] = True
    settings[VERSION_KEY] = PROFILE_VERSION
    _write(path, settings, "./settings.reader.lua")

    if newly:
        log(f"   🧹 Reading profile: tucked away {len(newly)} developer plugins "
            f"({', '.join(newly)}). Tools > Plugin management brings any back.")
    if font:
        log(f"   🔤 Reading profile: {font} is the default reading font.")
    if styled:
        log("   📖 Reading profile: calmer pages (spacing, margins, weight) and a cover-grid home.")
    return {"applied": True, "disabled": newly, "font": font, "typography": styled}


def restyle_books(kobo_path, fonts_installed, backup_dir, log=print):
    """Give books already opened the profile's typography, where untouched.

    Only sidecars at the volume root, never KEbooks. A value the reader set is
    kept; only KOReader's own defaults are replaced. Each changed sidecar is
    copied to `backup_dir` on the Mac first. Returns the books restyled.
    """
    restyled = []
    for meta in sorted(glob.glob(os.path.join(kobo_path, "*.sdr", "metadata.*.lua"))):
        table = _load(meta)
        changed = False
        if fonts_installed and table.get("font_face") in (None, KOREADER_DEFAULT_FONT):
            table["font_face"] = DEFAULT_FONT
            changed = True
        for key, (ours, default) in TYPOGRAPHY.items():
            if key in table and table[key] == default and table[key] != ours:
                table[key] = ours
                changed = True
        if not changed:
            continue
        rel = os.path.relpath(meta, kobo_path)
        dest = os.path.join(backup_dir, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(meta, dest)
        _write(meta, table, "./" + os.path.basename(meta))
        restyled.append(os.path.basename(os.path.dirname(meta))[:-4])
    if restyled:
        log(f"   📖 Restyled {len(restyled)} book(s) you had already opened; "
            f"backups in {backup_dir}")
    return restyled


def default_backup_dir():
    return os.path.expanduser(f"~/.librasync_backups/{time.strftime('%Y%m%d-%H%M%S')}")
