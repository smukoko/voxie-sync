"""The voice clip check: trim to the typed words, refuse a wrong transcript, warn on the rest.

The clone copies the whole clip (a test clip's trailing sigh reached every sentence
of a book, 2026-10-06), so a clip is listened to before it is saved. Offline:
generated audio, and a fake listener standing in for Whisper with known word
times. The real Whisper path was checked by hand on two real clips (one
French, one English): both pass with no warning.
"""
import json
import os
import shutil
import sys
import tempfile

import wave

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from libra_cli import clip_check as cc, voice  # noqa: E402

passed = failed = 0
SR = 16000


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


work = tempfile.mkdtemp()


def clip(words, seconds=8.0, pause_noise=0.0, amp=0.3, name="c.wav"):
    """`words`: [(word, start, end, hz)]. A tone per word, silence (or noise) between."""
    rng = np.random.default_rng(1)
    x = rng.normal(0, pause_noise, int(seconds * SR)).astype("float32") if pause_noise else \
        np.zeros(int(seconds * SR), dtype="float32")
    for _, a, b, hz in words:
        t = np.arange(int((b - a) * SR)) / SR
        x[int(a * SR):int(a * SR) + len(t)] = amp * np.sin(2 * np.pi * hz * t)
    path = os.path.join(work, name)
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    return path


def listener(words):
    return lambda path, language: [(" " + w, a, b) for w, a, b, _ in words]


SENTENCE = "Every year approximately five million women become pregnant"
spoken = [(w, 1.0 + i * 0.5, 1.0 + i * 0.5 + 0.38, 180) for i, w in enumerate(SENTENCE.split())]

# --- matching the transcript ------------------------------------------------
heard = [(" " + w, a, b) for w, a, b, _ in spoken]
share, first, last = cc.match_typed(SENTENCE + ".", heard, "en")
check("the typed sentence matches what was said", share == 1.0 and (first, last) == (0, 7))
with_digits = [(" 5" if w.strip() == "five" else w, a, b) for w, a, b in heard]
check("Whisper's '5' is the typed 'five'", cc.match_typed(SENTENCE, with_digits, "en")[0] == 1.0)
split = [(" Est", 0, .2), ("-ce", .2, .3), (" que", .3, .5), (" vous", .5, .7), (" avez", .7, .9),
         (" essayé", .9, 1.2), (" d", 1.2, 1.3), ("'acheter", 1.3, 1.7), (" des", 1.7, 1.9), (" places", 1.9, 2.3)]
check("an elision Whisper splits (\" d\" + \"'acheter\") still matches",
      cc.match_typed("Est-ce que vous avez essayé d'acheter des places", split, "fr")[0] == 1.0)
check("a different sentence is far below the refusal line",
      cc.match_typed("The quick brown fox jumps over the lazy dog", heard, "en")[0] < cc.REFUSE_BELOW)
more = heard + [(" and", 5.2, 5.4), (" then", 5.4, 5.6), (" she", 5.6, 5.8), (" laughed", 5.8, 6.3)]
share, first, last = cc.match_typed(SENTENCE, more, "en")
check("speech beyond the transcript is outside the matched words", share == 1.0 and last == 7)
missing = cc.match_typed("Every year five million women become pregnant", heard, "en")[0]
check("a transcript missing a word is warned about, not refused",
      cc.REFUSE_BELOW <= missing < cc.WARN_BELOW, f"({missing:.2f})")

# --- the report -------------------------------------------------------------
path = clip(spoken)
r = cc.check(path, SENTENCE, "en", lufs=-20, listen=listener(spoken))
check("the clip is trimmed to the first word -0.15s and the last +0.2s",
      r["trim"] == (round(1.0 - cc.LEAD_IN, 3), round(spoken[-1][2] + cc.TAIL, 3)), str(r["trim"]))
check("a clean clip has no warning", r["warnings"] == [], str(r["warnings"]))

sigh = spoken + [("[sigh]", 5.4, 6.4, 90)]
r = cc.check(clip(sigh), SENTENCE, "en", lufs=-20,
             listen=lambda p, l: [(" " + w, a, b) for w, a, b, _ in spoken])
check("a trailing sigh after the last word is cut", r["trim"][1] < 5.4, str(r["trim"]))

try:
    cc.check(path, "The quick brown fox jumps over the lazy dog", "en", lufs=-20, listen=listener(spoken))
    refused = False
except cc.ClipMismatch as e:
    refused = "Whisper heard" in str(e)
check("a wrong transcript is refused, saying what was heard", refused)

# A comma's pause after the fourth word: the only place the background is heard alone.
paused = [(w, a + (0.6 if i >= 4 else 0), b + (0.6 if i >= 4 else 0), hz) for i, (w, a, b, hz) in enumerate(spoken)]
r = cc.check(clip(paused, pause_noise=0.05, name="noisy.wav"), SENTENCE, "en", lufs=-20, listen=listener(paused))
check("noise or music in the pauses is warned about", any("pauses" in w for w in r["warnings"]))
r = cc.check(clip(paused, pause_noise=0.0003, name="hiss.wav"), SENTENCE, "en", lufs=-20, listen=listener(paused))
check("a faint room tone is not", not any("pauses" in w for w in r["warnings"]))
# A breath in a comma pause, in an otherwise quiet recording: the pause's
# average is within 15 dB of the speech (the old rule warned), its floor is not.
breath = clip(paused, name="breath.wav")
x, _ = cc.read_wav(breath)
at = int((paused[3][2] + 0.2) * SR)
x[at:at + int(0.15 * SR)] += np.random.default_rng(2).normal(0, 0.08, int(0.15 * SR))
with wave.open(breath, "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
    w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
a, b = paused[3][2] + 0.05, paused[4][1] - 0.05
mean_margin = cc._db(np.concatenate([x[int(s * SR):int(e * SR)] for _, s, e, _ in paused])) - \
    cc._db(x[int(a * SR):int(b * SR)])
r = cc.check(breath, SENTENCE, "en", lufs=-20, listen=listener(paused))
check("a breath in a clean pause is not noise", mean_margin < cc.PAUSE_MARGIN_DB
      and not any("pauses" in w for w in r["warnings"]), f"(average {mean_margin:.1f} dB under the speech)")

r = cc.check(clip(spoken, amp=1.4, name="loud.wav"), SENTENCE, "en", lufs=-8, listen=listener(spoken))
check("a clipped recording is warned about", any("clips" in w for w in r["warnings"]))
r = cc.check(path, SENTENCE, "en", lufs=-48, listen=listener(spoken))
check("a very quiet recording is warned about", any("quiet" in w for w in r["warnings"]))

# A real voice's transcript (2026-10-06) ran on past its 10 s clip.
past = cc.match_typed(SENTENCE + " and they all moved to the city in the spring", heard, "en")[0]
try:
    cc.check(path, SENTENCE + " and they all moved to the city in the spring that year", "en", lufs=-20,
             listen=listener(spoken))
    said = ""
except cc.ClipMismatch as e:
    said = str(e)
check("a transcript that runs past the clip's end says so", "goes on after the clip ends" in said, f"({past:.2f})")

# --- through add_voice ------------------------------------------------------
vdir = os.path.join(work, "voices")
sentence_clip = clip(spoken, name="source.wav")
r = voice.add_voice(text=SENTENCE, consent="self", file=sentence_clip, name="checked", voices_dir=vdir,
                    listen=listener(spoken))
side = json.load(open(os.path.join(vdir, "checked.json")))
expected = (spoken[-1][2] + cc.TAIL) - (1.0 - cc.LEAD_IN)
check("add_voice saves the trimmed clip", abs(r["seconds"] - expected) < 0.05, f"({r['seconds']}s)")
check("and records what the check heard", side["check"]["match"] == 1.0 and side["check"]["trimmed_to"])
try:
    voice.add_voice(text="The quick brown fox jumps over the lazy dog", consent="self", file=sentence_clip,
                    name="wrong", voices_dir=vdir, listen=listener(spoken))
    refused = False
except voice.VoiceError:
    refused = True
check("a refused clip leaves nothing behind", refused and not os.path.exists(os.path.join(vdir, "wrong.wav")))
short = [(w, a, b, hz) for w, a, b, hz in spoken[:3]]
try:
    voice.add_voice(text="Every year approximately", consent="self", file=sentence_clip, name="short",
                    voices_dir=vdir, listen=listener(short))
    refused = False
except voice.VoiceError as e:
    refused = "of the clip is speech" in str(e)
check("a clip with under 3s of speech once trimmed is refused", refused)

# --- how the clip is paced -------------------------------------------------
# The first install's own clip (2026-10-22), word times as Whisper gave them:
# two sentences with 0.34 s between "AI." and "So". The narrator started every
# sentence that quickly after a full stop.
FIRST_CLIP = "Hey there, today I'm recording my voice because I want my voice to be cloned by AI.\r\nSo I have to speak very clearly."
times = [(" Hey", 0.00, 0.44), (" there,", 0.44, 0.74), (" today", 1.02, 1.26), (" I'm", 1.26, 1.74),
         (" recording", 1.74, 2.02), (" my", 2.02, 2.22), (" voice", 2.22, 2.62), (" because", 2.62, 3.46),
         (" I", 3.46, 3.90), (" want", 3.90, 4.12), (" my", 4.12, 4.32), (" voice", 4.32, 4.64),
         (" to", 4.64, 4.80), (" be", 4.80, 4.90), (" cloned", 4.90, 5.22), (" by", 5.22, 5.42),
         (" AI,", 5.42, 5.86), (" so", 6.20, 6.72), (" I", 6.72, 6.90), (" have", 6.90, 7.08),
         (" to", 7.08, 7.20), (" speak", 7.20, 7.48), (" very", 7.48, 7.84), (" clearly.", 7.84, 8.14)]
gaps = cc.sentence_gaps(FIRST_CLIP, times, "en")
check("the gap after the first sentence is found where the typed text ends it",
      gaps == [("ai", 0.34)], str(gaps))
report = cc.pace_report(FIRST_CLIP, times, "en")
check("a clip that starts the next sentence 0.34 s after a full stop is advised a retake",
      report["retake"] and any("0.3 s after a full stop" in w and "one second" in w.replace("a second", "one second")
                               for w in report["warnings"]), str(report["warnings"]))
check("its speed (about 170 words a minute) is not the problem", report["wpm"] < cc.FAST_WPM, f"({report['wpm']} wpm)")


def stretched(gap):
    """The same words with `gap` seconds of silence after the first sentence."""
    out = []
    for w, a, b in times:
        out.append((w, a + (gap - 0.34 if a >= 6.0 else 0), b + (gap - 0.34 if a >= 6.0 else 0)))
    return out


good = cc.pace_report(FIRST_CLIP, stretched(1.0), "en")
check("a full second after the full stop is not warned about", not good["retake"] and not good["warnings"],
      str(good))
fast = cc.pace_report("One two three four five six seven eight nine ten eleven twelve thirteen.",
                      [(f" w{i}", i * 0.25, i * 0.25 + 0.2) for i in range(13)], "en")
check("a clip read at 230 words a minute is advised a retake", fast["retake"] and "words a minute" in fast["warnings"][0],
      str(fast))
slow = cc.pace_report("One two three four five six seven eight nine ten eleven twelve thirteen.",
                      [(f" w{i}", i * 0.9, i * 0.9 + 0.5) for i in range(13)], "en")
check("a very slow clip is warned about, not sent back", slow["warnings"] and not slow["retake"], str(slow))
one = cc.pace_report("Hey there today I am recording my voice for the narrator.",
                     [(f" w{i}", i * 0.4, i * 0.4 + 0.3) for i in range(11)], "en")
check("a single sentence has no sentence gap to judge", one["sentence_gaps"] == [] and not one["warnings"], str(one))
check("a heard word out of order is skipped, not guessed",
      cc.sentence_gaps("Hello there. Goodbye now.", [(" Nothing", 0, 1), (" else", 1.1, 2)], "en") == [])
# Through check(): the report carries it and add_voice's warnings show it.
quick = [(w.strip(), a, b, 180) for w, a, b in times]
path = clip(quick, seconds=9.0, name="quick.wav")
rep = cc.check(path, FIRST_CLIP, "en", lufs=-20, listen=lambda p, lang: times)
check("check() reports the retake and the advice", rep["retake"] and any("full stop" in w for w in rep["warnings"]),
      str(rep["warnings"]))

shutil.rmtree(work, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
