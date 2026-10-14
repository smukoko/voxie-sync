#!/usr/bin/env python3
"""Measuring audio length, which this project has got wrong before.

CosyVoice returns 32-bit float PCM. The obvious `len(raw) / (rate * 2)` assumes
16-bit and therefore reports exactly DOUBLE -- and a doubled duration is a
doubled throughput, which is a measurement that flatters whatever it is pointed
at rather than failing visibly. CLAUDE.md records the last time: a float32 WAV
read as int16 gave "147s at 4.04x" for 73.5s at 2.02x, alongside two other
instrument errors from the same afternoon.

So the header is parsed, and this asserts it against files whose true duration
is known independently.

    python tools/test_wav_duration.py
"""
import os
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.tts_worker import wav_duration_from_bytes, probe_duration  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"   {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def make(path, seconds, rate, fmt, channels=1):
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         f"sine=frequency=440:duration={seconds}:sample_rate={rate}",
         "-ac", str(channels), "-c:a", fmt, path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


work = tempfile.mkdtemp(prefix="wavdur_")
try:
    cases = [
        ("32-bit float, 24kHz mono — what CosyVoice actually returns",
         "pcm_f32le", 24000, 1, 3.0),
        ("16-bit, 24kHz mono", "pcm_s16le", 24000, 1, 3.0),
        ("16-bit, 16kHz mono — a reference clip", "pcm_s16le", 16000, 1, 2.0),
        ("32-bit float, 24kHz stereo", "pcm_f32le", 24000, 2, 1.5),
        ("24-bit, 48kHz mono", "pcm_s24le", 48000, 1, 1.25),
    ]
    for label, fmt, rate, ch, secs in cases:
        p = os.path.join(work, f"{fmt}_{rate}_{ch}.wav")
        make(p, secs, rate, fmt, ch)
        raw = open(p, "rb").read()
        got = wav_duration_from_bytes(raw)
        check(label, abs(got - secs) < 0.02, f"{got:.3f}s, expected {secs:.3f}s")

    # The specific wrong answer, named, so a regression is recognisable rather
    # than merely numeric.
    p = os.path.join(work, "cosy.wav")
    make(p, 4.0, 24000, "pcm_f32le")
    raw = open(p, "rb").read()
    naive = (len(raw) - 44) / (24000 * 2)
    check("the naive 16-bit assumption would have doubled it",
          abs(naive - 8.0) < 0.1 and abs(wav_duration_from_bytes(raw) - 4.0) < 0.02,
          f"naive {naive:.2f}s vs correct {wav_duration_from_bytes(raw):.2f}s")

    check("it agrees with ffprobe",
          abs(wav_duration_from_bytes(raw) - probe_duration(p)) < 0.02)

    # Garbage must measure as nothing, not as something plausible. A throughput
    # number built from an error page is worse than no number at all.
    check("a non-WAV response measures 0, not a plausible number",
          wav_duration_from_bytes(b"<html>error</html>") == 0.0)
    check("a truncated WAV measures 0", wav_duration_from_bytes(b"RIFF") == 0.0)

    # A streamed WAV can carry a placeholder data size; what is present wins.
    p = os.path.join(work, "lying.wav")
    make(p, 2.0, 24000, "pcm_s16le")
    raw = bytearray(open(p, "rb").read())
    i = raw.find(b"data")
    raw[i + 4:i + 8] = struct.pack("<I", 0)
    check("a WAV whose data size says 0 is measured from what is there",
          abs(wav_duration_from_bytes(bytes(raw)) - 2.0) < 0.02,
          f"{wav_duration_from_bytes(bytes(raw)):.3f}s")
finally:
    import shutil
    shutil.rmtree(work, ignore_errors=True)

print("\n" + "=" * 60)
passed = sum(1 for r in results if r)
print(f"{len(results)} checks: {passed} passed, {len(results) - passed} FAILED")
sys.exit(0 if passed == len(results) else 1)
