"""Prove that what we are about to write to a Kobo survives the Kobo's own fsck.

macOS's FAT driver can hand the same 8.3 short name to two different long
filenames in one directory. macOS's fsck_msdos does not look for that; the
Linux fsck.vfat the Kobo runs at every boot does, and "repairs" it by renaming
the second file of each pair to FSCK0000.000. The contents survive, the name
does not, and KOReader dies on launch looking for a module that is no longer
there. It cost two bricked installs, and nothing on the Mac can see it: on the
Mac every file is present.

So the only honest check is the Kobo's own: write the payload into a FAT32
disk image *exactly as the installer writes it to the device*, then run the
real Linux fsck.vfat against that image in Docker.

Two things this version adds over the single-archive rig it replaces:

  - It takes a list of steps, not one zip. Dictionaries, fonts and a UI
    plugin each add hundreds of filenames, and a collision can only be found
    by checking them together with what they land beside.

  - It can seed the image with the NAMES already present on the device along
    each destination path. A short name is chosen against the names already in
    its directory, so a payload that is clean on an empty volume can still
    collide with what the device already holds. Only names are copied -- empty
    placeholders -- never contents.

    That seeding is a faithful model, not a replica: the placeholders get short
    names assigned afresh, which may not be the exact ~N numbers the device's
    own files carry. It is the closest this can get without raw access to the
    device, which Docker Desktop on macOS cannot pass through.

And a negative control. Step kind "zipfile" extracts with Python's zipfile,
which is measured to produce eight duplicates on the KOReader archive. A check
that has never been seen to fail proves nothing, so the tests run that too.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field

# Pinned rather than :latest -- the check is only as stable as the fsck it runs.
DOCKER_IMAGE = "alpine:3.20"

# macOS creates these on any volume it mounts. They are not ours to model.
_SYSTEM_NAMES = {".Trashes", ".Spotlight-V100", ".fseventsd", ".TemporaryItems",
                 ".DS_Store", ".metadata_never_index"}


@dataclass
class Step:
    """One write the installer will perform.

    kind: "zip"     -- `ditto -x -k src dest`, how every archive is installed
          "tree"    -- `ditto src dest`, how a prepared directory is copied
          "zipfile" -- Python's extractall; the negative control, never used
                       by the installer
    dest: relative to the volume root, e.g. ".adds" or "fonts".
    """
    kind: str
    src: str
    dest: str


@dataclass
class FatResult:
    ok: bool
    duplicates: list = field(default_factory=list)       # paths fsck flagged
    seed_duplicates: list = field(default_factory=list)  # present before the payload
    files_written: int = 0
    image_mb: int = 0
    output: str = ""
    reason: str = ""

    def __str__(self):
        if self.reason and not self.duplicates:
            return f"FAT check could not run: {self.reason}"
        if self.ok:
            return (f"FAT check PASSED: {self.files_written} files, 0 duplicate "
                    f"short names ({self.image_mb}MB image)")
        head = ", ".join(self.duplicates[:4])
        more = f" and {len(self.duplicates) - 4} more" if len(self.duplicates) > 4 else ""
        return (f"FAT check FAILED: {len(self.duplicates)} duplicate short name(s) "
                f"-- the Kobo would rename these at boot: {head}{more}")


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def docker_available():
    if shutil.which("docker") is None:
        return False
    return _run(["docker", "info"]).returncode == 0


def _payload_bytes(step):
    if step.kind in ("zip", "zipfile"):
        with zipfile.ZipFile(step.src) as z:
            return sum(i.file_size for i in z.infolist())
    total = 0
    for root, _, files in os.walk(step.src):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
    return total


def _seed_names(seed_root, dest, image_root):
    """Recreate, as empty placeholders, the names already on the device along `dest`.

    Walks from the volume root down to `dest`, and at each directory that
    exists on the device, mirrors its immediate entries by name. Nothing is
    ever read from inside a file, and nothing below the path is descended into.
    """
    parts = [p for p in dest.strip("/").split("/") if p]
    rel = ""
    for depth in range(len(parts) + 1):
        real = os.path.join(seed_root, rel)
        if not os.path.isdir(real):
            return
        mirror = os.path.join(image_root, rel)
        os.makedirs(mirror, exist_ok=True)
        for name in os.listdir(real):
            if name in _SYSTEM_NAMES or name.startswith("._"):
                continue
            target = os.path.join(mirror, name)
            if os.path.lexists(target):
                continue
            if os.path.isdir(os.path.join(real, name)):
                os.makedirs(target, exist_ok=True)
            else:
                open(target, "wb").close()
        if depth < len(parts):
            rel = os.path.join(rel, parts[depth])


def _apply(step, image_root):
    dest = os.path.join(image_root, step.dest.strip("/"))
    os.makedirs(dest, exist_ok=True)
    if step.kind == "zip":
        r = _run(["ditto", "-x", "-k", step.src, dest])
    elif step.kind == "tree":
        r = _run(["ditto", step.src, dest])
    elif step.kind == "zipfile":
        with zipfile.ZipFile(step.src) as z:
            z.extractall(dest)
        return
    else:
        raise ValueError(f"unknown step kind {step.kind!r}")
    if r.returncode != 0:
        raise RuntimeError(f"ditto failed on {step.src}: {r.stderr.strip()}")


def _count_files(root, dests):
    n = 0
    for d in set(dests):
        for _, _, files in os.walk(os.path.join(root, d.strip("/"))):
            n += len(files)
    return n


def _fsck(workdir):
    """Run the Kobo's filesystem check against the image. Returns (output, dup paths)."""
    r = _run(["docker", "run", "--rm", "-v", f"{workdir}:/w", DOCKER_IMAGE, "sh", "-c",
              "apk add --no-cache dosfstools >/dev/null 2>&1 && fsck.vfat -n /w/img.dmg 2>&1"])
    out = (r.stdout or "") + (r.stderr or "")
    if "fsck.fat" not in out and "fsck.vfat" not in out and "dosfsck" not in out:
        raise RuntimeError(f"fsck.vfat did not run (exit {r.returncode}): {out.strip()[:300]}")
    lines = out.splitlines()
    dups = []
    for i, line in enumerate(lines):
        if "Duplicate directory entry" in line and i > 0:
            dups.append(lines[i - 1].strip())
    return out, dups


class _Image:
    """A FAT32 disk image, attached and mounted at a private path.

    Mounted under the work directory rather than /Volumes, so a leftover from
    an interrupted run can never be mistaken for -- or collide with -- a real
    volume, and the cleanup below cannot unmount anything it did not create.
    """

    def __init__(self, workdir, size_mb):
        self.workdir = workdir
        self.size_mb = size_mb
        self.dev = None
        self.mnt = os.path.join(workdir, "mnt")

    def create(self):
        r = _run(["hdiutil", "create", "-size", f"{self.size_mb}m", "-layout", "NONE",
                  "-fs", "MS-DOS FAT32", "-volname", "KFATCHK",
                  "-o", os.path.join(self.workdir, "img")])
        if r.returncode != 0:
            raise RuntimeError(f"hdiutil create failed: {r.stderr.strip()}")

    def attach(self):
        r = _run(["hdiutil", "attach", "-nomount", os.path.join(self.workdir, "img.dmg")])
        if r.returncode != 0:
            raise RuntimeError(f"hdiutil attach failed: {r.stderr.strip()}")
        self.dev = r.stdout.split()[0]
        os.makedirs(self.mnt, exist_ok=True)
        r = _run(["diskutil", "mount", "-mountPoint", self.mnt, self.dev])
        if r.returncode != 0:
            raise RuntimeError(f"diskutil mount failed: {r.stderr.strip() or r.stdout.strip()}")

    def detach(self):
        if self.dev:
            _run(["sync"])
            r = _run(["hdiutil", "detach", self.dev])
            if r.returncode != 0:
                _run(["hdiutil", "detach", "-force", self.dev])
            self.dev = None


def check(steps, seed_root=None):
    """Write `steps` into a FAT32 image the installer's way and fsck it.

    Never raises for an ordinary failure -- a missing Docker, a bad archive --
    because the caller is an installer that must decide what to tell a person.
    The reason is in the result.
    """
    if not steps:
        return FatResult(ok=True, reason="nothing to check")
    if not docker_available():
        return FatResult(ok=False, reason="Docker is not running; the Kobo's fsck runs in a Linux container")

    total = sum(_payload_bytes(s) for s in steps)
    # FAT32 wastes up to a cluster per file, and 1045 small files is a lot of
    # clusters. 40% plus a floor keeps the image from filling up, which would
    # read as an extraction error rather than as the thing being tested.
    size_mb = max(128, int(total * 1.4 / 1e6) + 64)

    workdir = tempfile.mkdtemp(prefix="kfat_")
    image = _Image(workdir, size_mb)
    try:
        image.create()
        image.attach()

        seed_dups = []
        if seed_root:
            for s in steps:
                _seed_names(seed_root, s.dest, image.mnt)
            image.detach()
            _, seed_dups = _fsck(workdir)
            image.attach()

        for s in steps:
            _apply(s, image.mnt)
        written = _count_files(image.mnt, [s.dest for s in steps])
        image.detach()

        out, dups = _fsck(workdir)
        new = [d for d in dups if d not in set(seed_dups)]
        return FatResult(ok=not new, duplicates=new, seed_duplicates=seed_dups,
                         files_written=written, image_mb=size_mb, output=out)
    except (RuntimeError, OSError, zipfile.BadZipFile) as exc:
        return FatResult(ok=False, reason=str(exc), image_mb=size_mb)
    finally:
        image.detach()
        shutil.rmtree(workdir, ignore_errors=True)


def _parse_step(kind, spec):
    src, _, dest = spec.rpartition(":")
    if not src:
        raise argparse.ArgumentTypeError(f"expected SRC:DEST, got {spec!r}")
    return Step(kind, os.path.expanduser(src), dest)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--zip", action="append", default=[], metavar="SRC:DEST",
                    help="an archive the installer extracts with ditto -x -k")
    ap.add_argument("--tree", action="append", default=[], metavar="SRC:DEST",
                    help="a directory the installer copies with ditto")
    ap.add_argument("--zipfile", action="append", default=[], metavar="SRC:DEST",
                    help="NEGATIVE CONTROL: extract with Python zipfile, expected to fail")
    ap.add_argument("--seed", metavar="VOLUME", help="mirror names already on this volume")
    a = ap.parse_args(argv)

    steps = ([_parse_step("zip", s) for s in a.zip] + [_parse_step("tree", s) for s in a.tree]
             + [_parse_step("zipfile", s) for s in a.zipfile])
    result = check(steps, seed_root=a.seed)
    for d in result.duplicates:
        print(f"   duplicate: {d}")
    if result.seed_duplicates:
        print(f"   ({len(result.seed_duplicates)} duplicate(s) came from the seeded names, not the payload)")
    print(("✅ " if result.ok else "❌ ") + str(result))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
