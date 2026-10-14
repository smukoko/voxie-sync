"""The reading kit: dictionaries and fonts, installed in one gated pass.

Order is the whole design. Everything is downloaded, checksum-verified and
staged on the Mac first; then the complete payload goes through the Kobo's own
filesystem check with the device's existing names seeded in; and only if that
passes is a single byte written to the device. A failure anywhere before the
gate leaves the Kobo exactly as it was.

The gate is on by default and refuses when Docker is not running, rather than
installing unchecked. That is deliberate even though it is inconvenient: the
failure it prevents is invisible from the Mac and only shows on the device at
its next boot, as a KOReader that will not start. The escape hatch exists, is
named for what it is, and says so when used.
"""
from libra_cli import dictionaries as D
from libra_cli import fonts as F
from libra_cli.fat_check import check


class GateRefused(Exception):
    pass


def parse_dict_spec(spec):
    """'default' -> None (the manifest's defaults), 'none' -> [], else a list of ids."""
    spec = (spec or "default").strip().lower()
    if spec == "default":
        return None
    if spec in ("none", ""):
        return []
    return [s.strip() for s in spec.split(",") if s.strip()]


def install_reading_kit(kobo_path, dict_ids=None, with_fonts=True, fat_gate=True, log=print):
    """Stage, gate, install. Returns what was installed, for the JSON result line."""
    dicts = D.entries(dict_ids) if dict_ids != [] else []
    fams = F.families() if with_fonts else []
    if not dicts and not fams:
        log("   ℹ️  Reading kit: nothing selected.")
        return {"dictionaries": [], "fonts": []}

    staged_dicts = {}
    if dicts:
        log(f"\n   📚 Preparing {len(dicts)} dictionar{'y' if len(dicts) == 1 else 'ies'}...")
        for e in dicts:
            staged_dicts[e["id"]] = D.stage(e, log)
    staged_fonts = None
    if fams:
        log(f"   🔤 Preparing {len(fams)} reading font{'s' if len(fams) != 1 else ''}...")
        staged_fonts = F.stage(fams, log)

    steps = D.fat_steps(staged_dicts) + (F.fat_steps(staged_fonts) if staged_fonts else [])
    if fat_gate:
        log("   🛡️  Checking the payload against the Kobo's own filesystem check...")
        result = check(steps, seed_root=kobo_path)
        if not result.ok:
            raise GateRefused(
                f"{result}\n   Nothing was written to the Kobo. "
                + ("Start Docker Desktop and run setup again."
                   if "Docker" in result.reason else
                   "Report this: a file in the payload would be renamed at boot."))
        log(f"   ✅ {result}")
    else:
        log("   ⚠️  FAT check SKIPPED on request. If KOReader fails to start after this, "
            "read .adds/koreader/crash.log for a missing module.")

    if staged_dicts:
        D.install(staged_dicts, kobo_path, log)
    if staged_fonts:
        F.install(staged_fonts, kobo_path, log)

    return {"dictionaries": list(staged_dicts), "fonts": [f["family"] for f in fams]}

