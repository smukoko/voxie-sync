"""Reading fonts the one-button setup installs over the cable.

They go in the device's own fonts/ folder, not KOReader's. KOReader scans it
-- platform/kobo/koreader.sh exports EXT_FONT_DIR=/mnt/onboard/fonts, and
fontlist.lua reads that -- and it survives a KOReader upgrade, which extracts
over .adds/koreader. Nickel reads sideloaded fonts from the same folder, so the
faces work in the stock reader too. Flat, because Nickel does not descend into
subfolders.

Each family is four static files -- Regular, Italic, Bold, BoldItalic --
because crengine finds emphasis by separate files. A family missing its
italic renders every <em> in a novel as a mechanically slanted Regular.

Bitter was on the original list and is not here: its upstream now ships only
variable fonts, and instancing them would make a Modified Version under the
OFL's naming rules to add a fourth face nobody asked for by name.
"""
import hashlib
import json
import os
import shutil
import subprocess

MANIFEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts.json")
CACHE = os.path.expanduser(os.getenv("LIBRASYNC_CACHE", "~/.librasync_cache"))
DEVICE_FONT_DIR = "fonts"


class ChecksumMismatch(Exception):
    pass


def families(ids=None, path=MANIFEST):
    with open(path, encoding="utf-8") as f:
        all_fams = json.load(f)["families"]
    if ids is None:
        return [f for f in all_fams if f.get("default")]
    by_id = {f["id"]: f for f in all_fams}
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise ValueError(f"unknown font family id(s): {', '.join(unknown)}. Known: {', '.join(by_id)}")
    return [f for f in all_fams if f["id"] in set(ids)]


def _fetch(item, dest_dir, log):
    path = os.path.join(dest_dir, item["file"])
    if not os.path.exists(path):
        import requests
        os.makedirs(dest_dir, exist_ok=True)
        log(f"   ⬇️  Downloading {item['file']}...")
        r = requests.get(item["url"], timeout=120)
        r.raise_for_status()
        with open(path + ".part", "wb") as f:
            f.write(r.content)
        os.replace(path + ".part", path)
    with open(path, "rb") as f:
        actual = hashlib.sha256(f.read()).hexdigest()
    if actual != item["sha256"]:
        raise ChecksumMismatch(f"{item['file']} does not match its pinned checksum "
                               f"(expected {item['sha256'][:12]}, got {actual[:12]})")
    return path


def stage(fams, log=print):
    """One flat folder holding exactly what will be copied to the device's fonts/."""
    src = os.path.join(CACHE, "fonts", "src")
    staged = os.path.join(CACHE, "fonts", "staged")
    if os.path.exists(staged):
        shutil.rmtree(staged)
    os.makedirs(staged)
    for fam in fams:
        for item in fam["files"] + [fam["license_file"]]:
            shutil.copy2(_fetch(item, src, log), os.path.join(staged, item["file"]))
    return staged


def fat_steps(staged):
    from libra_cli.fat_check import Step
    return [Step("tree", staged, DEVICE_FONT_DIR)]


def install(staged, kobo_root, log=print):
    """Merge the staged fonts into the device's fonts/ folder.

    Merged, not replaced: the folder is the user's as much as ours -- anyone
    who has sideloaded a font for Nickel keeps it. ditto merges into an
    existing directory and overwrites only files of the same name.
    """
    dest = os.path.join(kobo_root, DEVICE_FONT_DIR)
    os.makedirs(dest, exist_ok=True)
    wanted = {n: os.path.getsize(os.path.join(staged, n)) for n in os.listdir(staged)}
    if all(os.path.exists(os.path.join(dest, n)) and os.path.getsize(os.path.join(dest, n)) == size
           for n, size in wanted.items()):
        log("   🔤 Fonts already installed.")
        return
    r = subprocess.run(["ditto", staged, dest], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"could not copy fonts to the device: {r.stderr.strip()}")
    for name in os.listdir(staged):
        target = os.path.join(dest, name)
        if not os.path.exists(target) or os.path.getsize(target) != os.path.getsize(os.path.join(staged, name)):
            raise RuntimeError(f"fonts/{name} did not arrive intact on the device")
    log(f"   🔤 Fonts installed: {', '.join(sorted({n.split('-')[0] for n in os.listdir(staged) if n.endswith('.ttf')}))}")
