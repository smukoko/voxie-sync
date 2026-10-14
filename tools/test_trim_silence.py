"""A chunk's dead air after its last word is cut to half a second, losslessly.

CosyVoice ended one line of a scene in 6.4 s and then 6.9 s of silence, in two
takes (2026-10-12), and it played as-is between two paragraphs. Offline:
generated WAVs, float32 as CosyVoice returns them and int16 as Kokoro does.
"""
import os
import struct
import sys
import tempfile
import types
import wave

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from core import tts_worker as tw  # noqa: E402

passed = failed = 0
RATE = 24000


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


def tone(seconds, dbfs):
    t = np.arange(int(seconds * RATE)) / RATE
    return (10 ** (dbfs / 20) * np.sqrt(2) * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def hiss(seconds, dbfs=-60):
    return (np.random.default_rng(1).normal(0, 10 ** (dbfs / 20), int(seconds * RATE))).astype(np.float32)


def write_f32(path, x):
    data = x.astype("<f4").tobytes()
    fmt = struct.pack("<HHIIHH", 3, 1, RATE, RATE * 4, 4, 32)
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 4 + 8 + 16 + 8 + len(data)) + b"WAVE"
                + b"fmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(data)) + data)


def write_i16(path, x):
    with wave.open(path, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(RATE)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


tmp = tempfile.mkdtemp()

# --- CosyVoice: float32, a line then 7 s of near-silence --------------------
p = os.path.join(tmp, "cosy.wav")
write_f32(p, np.concatenate([tone(2.0, -20), hiss(7.0)]))
before = open(p, "rb").read()
cut = tw.trim_trailing_silence(p)
after = open(p, "rb").read()
seconds = tw.wav_duration_from_bytes(after)
check("7 s of dead air after the last sound is cut to half a second",
      abs(seconds - 2.5) < 0.03 and abs(cut - 6.5) < 0.03, f"({seconds:.3f}s left, {cut:.3f}s cut)")
check("losslessly: the audio kept is the same bytes", after[44:] == before[44:len(after)])
check("and the header says the new length", struct.unpack("<I", after[40:44])[0] == len(after) - 44
      and struct.unpack("<I", after[4:8])[0] == len(after) - 8)

# --- Kokoro: int16, an ordinary short tail is left alone ---------------------
p = os.path.join(tmp, "kokoro.wav")
write_i16(p, np.concatenate([tone(2.0, -20), hiss(0.3)]))
before = open(p, "rb").read()
check("an ordinary tail is left alone, the file untouched",
      tw.trim_trailing_silence(p) == 0.0 and open(p, "rb").read() == before)
write_i16(p, np.concatenate([tone(2.0, -20), hiss(5.0)]))
tw.trim_trailing_silence(p)
with wave.open(p) as w:
    left = w.getnframes() / w.getframerate()
check("int16 is trimmed too, and the stdlib still reads it", abs(left - 2.5) < 0.03, f"({left:.3f}s)")

# --- What must stay ----------------------------------------------------------
p = os.path.join(tmp, "soft.wav")
write_f32(p, np.concatenate([tone(2.0, -20), hiss(0.5), tone(0.4, -35), hiss(5.0)]))
tw.trim_trailing_silence(p)
check("a soft last word (-35 dBFS) is kept", abs(tw.wav_duration_from_bytes(open(p, "rb").read()) - 3.4) < 0.03)
p = os.path.join(tmp, "silent.wav")
write_f32(p, hiss(3.0))
before = open(p, "rb").read()
check("an all-silent file is not this check's business: untouched",
      tw.trim_trailing_silence(p) == 0.0 and open(p, "rb").read() == before)
p = os.path.join(tmp, "junk.wav")
open(p, "wb").write(b"not a wav at all, not even close" * 4)
check("a file that is not a PCM WAV is left alone", tw.trim_trailing_silence(p) == 0.0)

# --- Every chunk goes through it, fresh or cached ----------------------------
p = os.path.join(tmp, "chunk.wav")
write_f32(p, np.concatenate([tone(2.0, -20), hiss(6.4)]))
tw.prepare_chunk_audio(p, types.SimpleNamespace(NEEDS_MFA_BOOST=False), 1, "002_B_scene")
check("prepare_chunk_audio trims a CosyVoice chunk",
      abs(tw.wav_duration_from_bytes(open(p, "rb").read()) - 2.5) < 0.03)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
