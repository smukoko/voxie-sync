#!/usr/bin/env python3
"""Does the stitcher put the right audio in the right chapter, in parallel?

Stitching is twenty minutes of a sixteen-hour book and the work is per chapter,
so it runs on every core. That introduced a hazard the serial version did not
have: the chunk list handed to ffmpeg used to be a single file, "concat.txt",
written into the audiobook folder and overwritten by each chapter in turn. In
parallel, every chapter writes that same path, and each ffmpeg encodes whatever
happened to be in it when it read -- producing a valid MP3 of another chapter's
audio. Nothing downstream can catch that: the file plays, the duration is
plausible, and the sync map is silently describing different words.

So this asserts identity, not just success: each chapter's finished MP3 has to
be as long as ITS OWN chunks and nobody else's. Chunks are generated at
distinct lengths so a swap shows up as a duration mismatch.

    python tools/test_stitcher.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.tts_worker import stitch_audio_chapters, probe_duration   # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"   {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def make_tone(path, seconds, hz):
    """A chunk of audio at the engine's own format: 24kHz mono PCM."""
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         f"sine=frequency={hz}:duration={seconds}:sample_rate=24000",
         "-ac", "1", "-c:a", "pcm_s16le", path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


def main():
    work = tempfile.mkdtemp(prefix="stitch_test_")
    try:
        # Three chapters whose chunk counts and durations all differ, so that
        # any cross-contamination changes a duration rather than hiding inside
        # an equal-length swap.
        plan = {
            "Chapter One":   [1.0, 2.0, 3.0],          # 6.0s
            "Chapter Two":   [4.0, 1.0],               # 5.0s
            "Chapter Three": [2.0, 2.0, 2.0, 1.0],     # 7.0s
        }
        expected = {t: sum(d) for t, d in plan.items()}

        index = 1
        for title, durations in plan.items():
            for i, secs in enumerate(durations):
                label = chr(ord("A") + i)
                make_tone(os.path.join(work, f"{index:03d}_{label}_{title}.wav"),
                          secs, 220 + index * 40)
                index += 1

        stitch_audio_chapters(work, "Test Book", "Test Author", "LBR-TESTTEST")

        mp3s = sorted(f for f in os.listdir(work) if f.endswith(".mp3"))
        check("every chapter produced one MP3", len(mp3s) == len(plan),
              f"{len(mp3s)} of {len(plan)}: {mp3s}")

        for title, want in expected.items():
            match = [m for m in mp3s if m.endswith(f"_{title}.mp3")]
            if not match:
                check(f"'{title}' was stitched", False, "no MP3 for it")
                continue
            got = probe_duration(os.path.join(work, match[0]))
            # MP3 granularity plus the encoder's own padding; a swapped chapter
            # here would be out by a whole second or more.
            ok = abs(got - want) < 0.30
            check(f"'{title}' contains its own chunks and no others",
                  ok, f"{got:.2f}s, expected {want:.2f}s")

        leftover_wavs = [f for f in os.listdir(work) if f.endswith(".wav")]
        check("chunk WAVs are removed once stitched", not leftover_wavs,
              f"{len(leftover_wavs)} left")

        scratch = [f for f in os.listdir(work) if "concat" in f]
        check("no concat scratch file is left behind", not scratch, str(scratch))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # Past chunk 999 (2026-10-07): the engine names chunk 1,000 "1000_A_...", and
    # a stitcher that matched three digits dropped every chunk from there on,
    # 6.6 of Twisted's 13.5 hours. As text "1000" also sorts before "101".
    work = tempfile.mkdtemp(prefix="stitch_test_")
    try:
        plan = [(998, "Late", 1.0), (999, "Late", 1.5), (1000, "Late", 2.0), (1001, "Last", 2.5),
                (100, "Early", 0.5), (101, "Early", 0.75)]
        for n, title, secs in plan:
            make_tone(os.path.join(work, f"{n:03d}_A_{title}.wav"), secs, 200 + n % 300)
        stitch_audio_chapters(work, "Long Book", "Author", "LBR-TESTTEST")
        mp3s = sorted(f for f in os.listdir(work) if f.endswith(".mp3"))
        late = [m for m in mp3s if m.endswith("_Late.mp3")]
        got = probe_duration(os.path.join(work, late[0])) if late else 0
        check("chunks past 999 are stitched, not dropped", late and abs(got - 4.5) < 0.3, f"{got:.2f}s of 4.5s")
        check("and in numeric order: 100 and 101 come first, 1001 last",
              mp3s and mp3s[0].endswith("_Early.mp3") and mp3s[-1].endswith("_Last.mp3"), str(mp3s))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # A chapter set that does not add up to the narration fails the run.
    work = tempfile.mkdtemp(prefix="stitch_test_")
    try:
        make_tone(os.path.join(work, "001_A_One.wav"), 1.0, 300)
        make_tone(os.path.join(work, "002_A_One.wav"), 1.0, 320)
        make_tone(os.path.join(work, "003-A-unrecognised.wav"), 3.0, 340)
        try:
            stitch_audio_chapters(work, "Short Book", "Author", "LBR-TESTTEST")
            said = ""
        except RuntimeError as e:
            said = str(e)
        check("a chunk the stitcher cannot place stops the run, saying so",
              "not recognised" in said and "Nothing was uploaded" in said, said[:100])
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n" + "=" * 60)
    passed = sum(1 for r in results if r)
    print(f"{len(results)} checks: {passed} passed, {len(results) - passed} FAILED")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
