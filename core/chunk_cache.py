"""Content-addressed cache for generated chunks.

A chunk's audio is a pure function of the text, the narrator and the model. So
the same chunk never needs paying for twice -- and until now it always was,
because every run started from nothing.

What that costs, concretely. The Roman-numeral fix changes the text of about 30
of The Screwtape Letters' 153 chunks; regenerating the book to hear it is 41
minutes of L40S at full price, of which 80% reproduces audio that already
exists byte for byte. The two chunks quarantined on 2026-09-15 were a two-chunk
problem that could only be fixed by a 153-chunk run. Trying a different narrator
on one chapter is not affordable at all.

With a cache each of those becomes proportional to what actually changed, which
is the difference between a failure costing a run and costing a retry.

The key is everything that can change the audio:

    sha256(provider class + provider identity + normalised text)

`provider identity` is the provider's own business -- the model name, voice and
speed for Voxtral; the speaker id (itself a hash of the reference clip) and its
transcript for CosyVoice. A provider that gains a parameter affecting its output
must add it there, or the cache will serve the old audio for the new setting.
That is the one way this module can be silently wrong, so `cache_identity` is
defined on the base class as an abstract-ish default rather than inferred here.

Two invariants matter more than speed:

**Writes are atomic.** Audio is written to a temp file in the same directory and
renamed into place, because rename is atomic within a filesystem while a copy is
not. A half-written entry would be indistinguishable from a good one on every
subsequent read, and would go on silently substituting truncated audio into
finished books -- exactly the class of failure this project keeps finding months
later.

**Reads are verified.** The stored size is checked against the file before a hit
is served. Cheap, and it catches a truncation that happened outside this module
(a full disk during an earlier version, a partial copy, a filesystem that lied).
"""
import hashlib
import json
import os
import shutil
import tempfile
import time

# Deliberately outside both the repo and mfa_workspace. The engine wipes
# mfa_workspace at the start of every run, which is where the chunks live now --
# a cache there would be destroyed by the very next book.
DEFAULT_DIR = os.path.expanduser("~/.librasync_chunk_cache")


def cache_dir():
    return os.environ.get("CHUNK_CACHE_DIR", DEFAULT_DIR)


def enabled():
    """Off with CHUNK_CACHE=0. A correctness escape hatch, not a preference.

    If a provider ever changes its output without changing its identity string,
    every cached chunk for it is wrong and there has to be a way to bypass the
    cache without deleting it.
    """
    return os.environ.get("CHUNK_CACHE", "1").strip().lower() not in ("0", "false", "no")


def key_for(provider, text):
    """The identity of this chunk's audio.

    Normalising only trailing whitespace: the text is what the model is sent, so
    anything else that differs is a genuinely different request and must miss.
    """
    identity = provider.cache_identity()
    h = hashlib.sha256()
    h.update(provider.__class__.__name__.encode("utf-8"))
    h.update(b"\x00")
    h.update(identity.encode("utf-8"))
    h.update(b"\x00")
    h.update(text.strip().encode("utf-8"))
    return h.hexdigest()


def _paths(key):
    # Sharded by the first two hex characters. A library of novels reaches tens
    # of thousands of entries, and some filesystems degrade badly on a single
    # directory that large.
    d = os.path.join(cache_dir(), key[:2])
    return os.path.join(d, key + ".wav"), os.path.join(d, key + ".json")


def get(key, dest_path):
    """Copy a cached chunk to dest_path. True on a verified hit.

    Never raises: a cache that fails is a cache miss, and a miss costs GPU time
    rather than the run.
    """
    if not enabled():
        return False
    audio, meta = _paths(key)
    try:
        if not (os.path.exists(audio) and os.path.exists(meta)):
            return False
        with open(meta) as f:
            recorded = json.load(f)
        actual = os.path.getsize(audio)
        if actual != recorded.get("bytes"):
            # Truncated or damaged. Drop it rather than serving it: a short
            # chunk is narration missing from a finished book, and it would be
            # served every time from here on.
            print(f"   🗑️  Cache entry {key[:12]} is {actual}B, recorded "
                  f"{recorded.get('bytes')}B — discarding it.")
            _discard(key)
            return False
        shutil.copyfile(audio, dest_path)
        return True
    except (OSError, ValueError) as e:
        print(f"   ⚠️  Cache read failed for {key[:12]} ({e}); regenerating.")
        return False


def put(key, src_path, meta=None):
    """Store a chunk. Never raises -- a cache that cannot write is not an error."""
    if not enabled():
        return False
    audio, meta_path = _paths(key)
    try:
        os.makedirs(os.path.dirname(audio), exist_ok=True)
        size = os.path.getsize(src_path)

        # Temp file in the destination directory, then rename. Atomic within a
        # filesystem, which a straight copy is not -- and a partially copied
        # entry would read as a valid hit forever after.
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(audio), suffix=".tmp")
        os.close(fd)
        shutil.copyfile(src_path, tmp)
        os.replace(tmp, audio)

        record = {"bytes": size, "stored_at": time.time()}
        record.update(meta or {})
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(meta_path), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(record, f)
        os.replace(tmp, meta_path)
        return True
    except OSError as e:
        print(f"   ⚠️  Could not cache {key[:12]} ({e}); the run is unaffected.")
        return False


def _discard(key):
    for p in _paths(key):
        try:
            os.remove(p)
        except OSError:
            pass


def stats():
    """(entries, bytes) currently stored."""
    root = cache_dir()
    n = total = 0
    for dirpath, _, files in os.walk(root):
        for f in files:
            if f.endswith(".wav"):
                n += 1
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
    return n, total


def max_bytes():
    """The cache's budget, in bytes. 0 disables pruning entirely.

    Default 20GB: comfortably more than one long novel's chunks (a 16-hour book
    is ~5.5GB) so a re-run of the last book generated is always free, while
    still being a number rather than "grows until the disk is full".

    It needs a ceiling because this cache doubles the disk a book costs -- the
    working copy in mfa_workspace plus the cached original -- and that doubling
    is invisible until a run dies at ENOSPC after the GPU has been paid for,
    which has happened here before for other reasons.
    """
    return int(float(os.environ.get("CHUNK_CACHE_MAX_GB", "20")) * 1e9)


def enforce_budget(verbose=True):
    """Prune to the configured budget. Called after a run, never during one.

    After, deliberately: evicting mid-run could drop a chunk this very book is
    about to ask for, turning a cache hit into a GPU bill while five pods are
    already running.
    """
    limit = max_bytes()
    if limit <= 0:
        return 0
    n, total = stats()
    if total <= limit:
        return 0
    freed = prune(limit)
    if verbose and freed:
        after_n, after_total = stats()
        print(f"   🧹 Chunk cache pruned: {n} -> {after_n} entries, "
              f"{total/1e9:.1f} -> {after_total/1e9:.1f} GB "
              f"(budget {limit/1e9:.0f} GB).")
    return freed


def prune(max_bytes):
    """Evict least-recently-stored entries until under max_bytes. Returns freed bytes.

    Stored-at rather than accessed-at on purpose: tracking reads would mean a
    write on every cache hit, and the ordering barely differs for this workload
    -- a book is generated once and its chunks are read together or not at all.
    """
    entries = []
    for dirpath, _, files in os.walk(cache_dir()):
        for f in files:
            if not f.endswith(".json"):
                continue
            key = f[:-len(".json")]
            audio = os.path.join(dirpath, key + ".wav")
            try:
                with open(os.path.join(dirpath, f)) as fh:
                    stored = json.load(fh).get("stored_at", 0)
                entries.append((stored, key, os.path.getsize(audio)))
            except (OSError, ValueError):
                continue
    total = sum(e[2] for e in entries)
    freed = 0
    for stored, key, size in sorted(entries):
        if total - freed <= max_bytes:
            break
        _discard(key)
        freed += size
    return freed
