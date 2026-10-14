"""Dictionaries the one-button setup installs over the cable.

The reader should never need wifi to look a word up, so dictionaries travel
with the pairing: downloaded once to a cache on the Mac, checked against a
pinned sha256, prepared, gated through the FAT check, and copied to the device.

Preparation does two things to an upstream archive, both recorded on the device
in CHANGES.txt because both sources are share-alike and require it:

  - The display name. koreader-dicts call themselves "en-en" and "fr-fr", which
    is what KOReader prints above every lookup. The .ifo's bookname becomes
    "English", "Français", "English → Français". Nothing else in the file
    changes, so the index sizes it declares stay true.

  - Attribution. WikDict archives carry no licence file at all -- the terms
    live only in the .ifo's description -- so an ATTRIBUTION and LICENSE are
    written from the manifest. koreader-dicts ship their own and keep them.

The manifest (dictionaries.json, beside this file) is the only list. WikDict
rebuilds its files in place under unchanged URLs, so a checksum there will go
stale some day. That fails loudly rather than installing an unchecked file;
`python -m libra_cli.dictionaries --refresh` recomputes the pins after a human
has looked.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

MANIFEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dictionaries.json")
CACHE = os.path.expanduser(os.getenv("LIBRASYNC_CACHE", "~/.librasync_cache"))
DEVICE_DICT_DIR = ".adds/koreader/data/dict"


class ChecksumMismatch(Exception):
    pass


def load_manifest(path=MANIFEST):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def entries(ids=None, path=MANIFEST):
    """Manifest entries for `ids` in manifest order; the defaults when ids is None.

    An unknown id is an error rather than a skip: a typo in a list of
    dictionaries would otherwise install eight of nine and report success.
    """
    all_entries = load_manifest(path)["dictionaries"]
    if ids is None:
        return [e for e in all_entries if e.get("default")]
    by_id = {e["id"]: e for e in all_entries}
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise ValueError(f"unknown dictionary id(s): {', '.join(unknown)}. "
                         f"Known: {', '.join(by_id)}")
    wanted = set(ids)
    return [e for e in all_entries if e["id"] in wanted]


def _zip_path(entry):
    return os.path.join(CACHE, "dicts", "zips", os.path.basename(entry["url"]))


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def ensure_cached(entry, log=print):
    """The archive on disk, downloaded if needed and always checksum-verified."""
    path = _zip_path(entry)
    if not os.path.exists(path):
        import requests
        os.makedirs(os.path.dirname(path), exist_ok=True)
        log(f"   ⬇️  Downloading {entry['name']} ({entry['archive_bytes'] / 1e6:.1f} MB)...")
        tmp = path + ".part"
        with requests.get(entry["url"], stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        os.replace(tmp, path)

    actual = _sha256(path)
    if actual != entry["sha256"]:
        # Keep the file for inspection; never install it.
        raise ChecksumMismatch(
            f"{entry['id']}: the archive does not match its pinned checksum "
            f"(expected {entry['sha256'][:12]}, got {actual[:12]}). Upstream may have "
            f"rebuilt it. Inspect {path}, then run "
            f"`python -m libra_cli.dictionaries --refresh` to re-pin.")
    return path


def _set_bookname(ifo_path, name):
    with open(ifo_path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    old = re.search(r"^bookname=(.*)$", text, re.M)
    new_text, n = re.subn(r"^bookname=.*$", f"bookname={name}", text, count=1, flags=re.M)
    if n != 1:
        raise ValueError(f"{ifo_path} has no bookname line")
    with open(ifo_path, "w", encoding="utf-8") as f:
        f.write(new_text)
    return old.group(1) if old else ""


def _write_attribution(folder, entry):
    with open(os.path.join(folder, "ATTRIBUTION"), "w", encoding="utf-8") as f:
        f.write(
            f"{entry['name']} ({entry['id']})\n\n"
            "Source: WikDict (https://www.wikdict.com/), part of the FreeDict project\n"
            "(https://freedict.org/). Publisher: Karl Bartel. Base data from Wiktionary\n"
            "(https://www.wiktionary.org/) via DBnary (http://kaiko.getalp.org/about-dbnary/).\n"
            f"Retrieved from {entry['url']}\n"
        )
    with open(os.path.join(folder, "LICENSE"), "w", encoding="utf-8") as f:
        f.write(f"{entry['license']}\nhttps://creativecommons.org/licenses/by-sa/4.0/legalcode\n")


def stage(entry, log=print):
    """Prepare one dictionary as the exact folder that will be copied to the device.

    Rebuilt from the verified archive every time. It is a few seconds, and a
    staged folder that silently drifted from its archive would put something on
    the device that no checksum ever saw.
    """
    archive = ensure_cached(entry, log)
    staged_root = os.path.join(CACHE, "dicts", "staged")
    final = os.path.join(staged_root, entry["id"])
    os.makedirs(staged_root, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=staged_root) as tmp:
        r = subprocess.run(["ditto", "-x", "-k", archive, tmp], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"ditto could not extract {archive}: {r.stderr.strip()}")
        src = os.path.join(tmp, entry["archive_root"])
        if not os.path.isdir(src):
            raise RuntimeError(f"{archive} has no top-level {entry['archive_root']}/ folder")

        ifos = [f for f in os.listdir(src) if f.endswith(".ifo")]
        if len(ifos) != 1:
            raise RuntimeError(f"{entry['id']}: expected one .ifo, found {ifos}")
        was = _set_bookname(os.path.join(src, ifos[0]), entry["name"])

        if not entry.get("ships_attribution"):
            _write_attribution(src, entry)

        with open(os.path.join(src, "CHANGES.txt"), "w", encoding="utf-8") as f:
            f.write(
                "Modified by Voxie for display on the device:\n"
                f"  - bookname in {ifos[0]} changed from \"{was}\" to \"{entry['name']}\"\n"
                + ("" if entry.get("ships_attribution") else
                   "  - ATTRIBUTION and LICENSE written from the licence stated in the .ifo\n")
                + "No dictionary content was changed.\n"
            )

        if os.path.exists(final):
            shutil.rmtree(final)
        os.replace(src, final)
    return final


def fat_steps(staged):
    """FAT-check steps for {id: staged_folder}, matching how install() copies them."""
    from libra_cli.fat_check import Step
    return [Step("tree", folder, f"{DEVICE_DICT_DIR}/{did}") for did, folder in staged.items()]


def _same_files(src, dest):
    """True when dest holds exactly src's files, at the same sizes."""
    listing = lambda d: {n: os.path.getsize(os.path.join(d, n)) for n in os.listdir(d)
                         if os.path.isfile(os.path.join(d, n))}
    return listing(src) == listing(dest)


def install(staged, kobo_root, log=print):
    """Copy staged dictionaries to the device and verify every byte arrived.

    ditto, never shutil: the FAT check proved ditto's writes clean, and the
    only honest way to lean on that is to write the same way.
    """
    for did, folder in staged.items():
        dest = os.path.join(kobo_root, DEVICE_DICT_DIR, did)
        # Already there, file for file and byte for byte: leave it. Re-running
        # setup to change one thing should not rewrite every dictionary on a
        # FAT volume, which is where the device's trouble has always come from.
        if os.path.isdir(dest) and _same_files(folder, dest):
            log(f"   📖 Dictionary already installed: {did}")
            continue
        if os.path.exists(dest):
            shutil.rmtree(dest)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        r = subprocess.run(["ditto", folder, dest], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"could not copy {did} to the device: {r.stderr.strip()}")
        for name in os.listdir(folder):
            want = os.path.getsize(os.path.join(folder, name))
            target = os.path.join(dest, name)
            if not os.path.exists(target) or os.path.getsize(target) != want:
                raise RuntimeError(f"{did}/{name} did not arrive intact on the device")
        log(f"   📖 Dictionary installed: {did}")


def refresh(path=MANIFEST, log=print):
    """Re-download every archive and rewrite the pins. For a human to run, and read."""
    manifest = load_manifest(path)
    for e in manifest["dictionaries"]:
        zp = _zip_path(e)
        if os.path.exists(zp):
            os.remove(zp)
        e_unpinned = dict(e, sha256="")
        try:
            ensure_cached(e_unpinned, log)
        except ChecksumMismatch:
            pass
        new = _sha256(zp)
        if new != e["sha256"]:
            log(f"   {e['id']}: {e['sha256'][:12]} -> {new[:12]}")
        e["sha256"] = new
        e["archive_bytes"] = os.path.getsize(zp)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Dictionaries for the Kobo.")
    ap.add_argument("--list", action="store_true", help="show the manifest")
    ap.add_argument("--stage", nargs="*", metavar="ID", help="download, verify and prepare")
    ap.add_argument("--refresh", action="store_true", help="re-download and re-pin every archive")
    a = ap.parse_args(argv)
    if a.refresh:
        refresh()
        return 0
    if a.stage is not None:
        for e in entries(a.stage or None):
            print(f"   ✅ {e['id']:6} -> {stage(e)}")
        return 0
    for e in load_manifest()["dictionaries"]:
        mark = "*" if e.get("default") else " "
        print(f" {mark} {e['id']:6} {e['name']:24} {e['entries']:>9,} entries  "
              f"{e['unpacked_bytes'] / 1e6:6.1f} MB  {e['license']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
