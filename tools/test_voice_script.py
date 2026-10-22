"""The text read to make a narrator, and the check that holds the reader to it (2026-10-22).

A first install's clip left 0.34 s between its two sentences and the narrator
started every sentence that quickly after a full stop. The setup now gives a
text to read; this checks that the text fits the model's 8-15 s window when read
calmly, that a calm reading passes the pace check and a hurried one is sent back,
and that the terminal route (`voice-add --script`) uses the same words.
Offline: the reading is simulated as word times, the way Whisper reports them.
"""
import os
import sys

from click.testing import CliRunner

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from libra_cli import clip_check as cc, voice_script as vs  # noqa: E402
from libra_cli.main import cli  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


def read(words_per_second, pause):
    """The script read at a pace, with `pause` seconds after each full stop, as word times."""
    out, t = [], 0.4
    for i, sentence in enumerate(vs.SENTENCES):
        for w in sentence.split():
            out.append((" " + w, t, t + 0.8 / words_per_second))
            t += 1.0 / words_per_second
        t += pause if i < len(vs.SENTENCES) - 1 else 0
    return out


words = cc.tokens(vs.TEXT)
check("every word is easy: short, and no sentence runs long",
      all(len(w) <= 6 for w in words) and all(len(cc.tokens(x)) <= 9 for x in vs.SENTENCES),
      str([w for w in words if len(w) > 6]))
check("the script is three sentences of plain words, no digits",
      len(vs.SENTENCES) == 3 and 20 <= len(words) <= 35 and not any(c.isdigit() for c in vs.TEXT), f"({len(words)} words)")
for label, wps in (("a calm reader (150 words a minute)", 2.5), ("a slow one (110)", 1.85)):
    heard = read(wps, 1.0)
    seconds = heard[-1][2] - heard[0][1]
    check(f"{label} fills the model's 8-15 s window", 8 <= seconds <= 15, f"({seconds:.1f}s)")
calm = cc.pace_report(vs.TEXT, read(2.5, 1.0), "en")
check("a calm reading with a second after each full stop is not sent back",
      not calm["retake"] and not calm["warnings"] and len(calm["sentence_gaps"]) == 2, str(calm))
hasty = cc.pace_report(vs.TEXT, read(2.5, 0.3), "en")
check("the same reading with 0.3 s after each full stop is sent back, with the advice",
      hasty["retake"] and "count" in hasty["warnings"][0], str(hasty["warnings"]))
check("the typed text and the heard words line up on both full stops",
      [w for w, _ in calm["sentence_gaps"]] == ["hills", "trees"], str(calm["sentence_gaps"]))

# --- The terminal route says the same words --------------------------------------------
runner = CliRunner()
out = runner.invoke(cli, ["voice-script"]).output
check("voxie voice-script prints every sentence and the pause advice",
      all(s in out for s in vs.SENTENCES) and "pause" in out and "voice-add --script" in out)
seen = {}


def fake_add(**kw):
    seen.update(kw)
    return {"name": "x", "seconds": 12.0, "lufs": -20, "warnings": [], "retake": True, "heard": None}


import libra_cli.voice as voice_mod  # noqa: E402
real = voice_mod.add_voice
voice_mod.add_voice = fake_add
try:
    r = runner.invoke(cli, ["voice-add", "--script", "--file", "memo.m4a", "--name", "me", "--consent", "self",
                            "--url", "https://youtu.be/x", "--start", "5", "--text", "ignored"])
    check("--script reads the file as the script, whatever else was given",
          r.exit_code == 0 and seen["text"] == vs.TEXT and seen["url"] is None and seen["start"] is None
          and seen["file"] == "memo.m4a", r.output + str(seen))
    check("a retake is said in the terminal too", "again" in r.output.lower(), r.output)
    r = runner.invoke(cli, ["voice-add", "--consent", "self", "--file", "memo.m4a"])
    check("without --text or --script it says what is missing", r.exit_code == 1 and "--script" in r.output, r.output)
finally:
    voice_mod.add_voice = real

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
