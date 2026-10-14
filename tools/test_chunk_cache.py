"""The chunk cache, and the ways it could silently ruin a book.

Speed is not what these check. A cache that is merely slow costs GPU time; a
cache that is wrong substitutes the wrong audio into a finished audiobook and
does it identically on every subsequent run, which is the failure this project
keeps discovering weeks later. So the assertions are about identity and
integrity:

  * anything that changes the audio must change the key -- text, narrator,
    narrator's transcript, provider, model, speed
  * a truncated entry must be refused and dropped, not served
  * a write must be atomic, so a crash mid-store cannot leave a valid-looking
    entry
  * a cache that cannot be read or written must degrade to a miss, never to an
    exception, because a failed cache must not be able to fail a paid run

    python tools/test_chunk_cache.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import chunk_cache

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"   {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


class FakeProvider:
    """Stands in for a real provider so the test needs no model or network."""
    def __init__(self, identity="model|voice=a|speed=1.0"):
        self._identity = identity

    def cache_identity(self):
        return self._identity


def main():
    work = tempfile.mkdtemp()
    os.environ["CHUNK_CACHE_DIR"] = os.path.join(work, "cache")
    os.environ["CHUNK_CACHE"] = "1"

    print("chunk cache\n" + "=" * 60)

    p = FakeProvider()
    text = "The most brilliant feature of the Screwtape Letters."

    # --- identity -------------------------------------------------------
    k = chunk_cache.key_for(p, text)
    check("the same chunk gives the same key", k == chunk_cache.key_for(p, text))
    check("trailing whitespace does not change the key",
          k == chunk_cache.key_for(p, text + "\n  "))
    check("different text -> different key",
          k != chunk_cache.key_for(p, text + " And one more sentence."))
    check("different voice -> different key",
          k != chunk_cache.key_for(FakeProvider("model|voice=b|speed=1.0"), text))
    check("different speed -> different key",
          k != chunk_cache.key_for(FakeProvider("model|voice=a|speed=0.93"), text))
    check("different model -> different key",
          k != chunk_cache.key_for(FakeProvider("other|voice=a|speed=1.0"), text))

    class OtherProvider(FakeProvider):
        pass
    check("same identity, different provider class -> different key",
          k != chunk_cache.key_for(OtherProvider(), text),
          "two servers can produce different audio from the same inputs")

    # --- round trip -----------------------------------------------------
    src = os.path.join(work, "chunk.wav")
    payload = b"RIFF" + os.urandom(40000)
    open(src, "wb").write(payload)

    check("a miss returns False", not chunk_cache.get(k, os.path.join(work, "out.wav")))
    check("put succeeds", chunk_cache.put(k, src, {"chunk": "042_B_IX"}))

    dest = os.path.join(work, "restored.wav")
    check("a hit returns True", chunk_cache.get(k, dest))
    check("the restored bytes are identical",
          open(dest, "rb").read() == payload,
          f"{os.path.getsize(dest)}B restored")

    # --- integrity: the part that protects a book -----------------------
    audio_path, meta_path = chunk_cache._paths(k)
    with open(audio_path, "r+b") as f:
        f.truncate(1000)
    check("a truncated entry is refused",
          not chunk_cache.get(k, os.path.join(work, "bad.wav")),
          "a short chunk is narration missing from the book")
    check("and the bad entry is dropped, not left to be served again",
          not os.path.exists(audio_path) and not os.path.exists(meta_path))

    # --- degradation: a broken cache must not break a run ---------------
    chunk_cache.put(k, src)
    os.remove(meta_path)
    check("an entry with no metadata is a miss, not a crash",
          not chunk_cache.get(k, os.path.join(work, "x.wav")))

    check("put on a missing source returns False rather than raising",
          not chunk_cache.put("deadbeef" * 8, os.path.join(work, "nope.wav")))

    os.environ["CHUNK_CACHE_DIR"] = "/proc/nonexistent/cannot/create"
    check("an unwritable cache dir degrades to a miss",
          not chunk_cache.put(k, src) and not chunk_cache.get(k, dest))
    os.environ["CHUNK_CACHE_DIR"] = os.path.join(work, "cache")

    # --- the off switch -------------------------------------------------
    chunk_cache.put(k, src)
    os.environ["CHUNK_CACHE"] = "0"
    check("CHUNK_CACHE=0 bypasses a populated cache",
          not chunk_cache.get(k, os.path.join(work, "y.wav")))
    os.environ["CHUNK_CACHE"] = "1"
    check("and does not delete it", chunk_cache.get(k, os.path.join(work, "z.wav")))

    # --- housekeeping ---------------------------------------------------
    n, total = chunk_cache.stats()
    check("stats counts what was stored", n >= 1 and total >= len(payload),
          f"{n} entries, {total}B")

    for i in range(4):
        chunk_cache.put(chunk_cache.key_for(FakeProvider(f"m{i}"), text), src)
    before, _ = chunk_cache.stats()
    freed = chunk_cache.prune(max_bytes=len(payload) * 2)
    after, after_bytes = chunk_cache.stats()
    check("prune evicts down to the budget",
          after < before and after_bytes <= len(payload) * 2,
          f"{before} -> {after} entries, freed {freed}B")

    shutil.rmtree(work, ignore_errors=True)
    print("=" * 60)
    failed = [r for r in results if not r[1]]
    print(f"{len(results)} checks: {len(results) - len(failed)} passed, {len(failed)} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
