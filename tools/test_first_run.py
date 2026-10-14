"""A fresh install's first drop says what to do instead of crashing.

No voice ships with Voxie, so the very first book anyone drops meets an empty
voices/. Before this, the engine got as far as building the TTS provider and
died in a traceback that the watcher reported as "the Engine crashed".
"""
import contextlib, io, json, os, re, sys, tempfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("TTS_PROVIDER", "cosyvoice")
from core import voices  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


empty = tempfile.mkdtemp()
full = tempfile.mkdtemp()
for name, lang in (("anna", "en"), ("marc", "fr"), ("eve", "en")):
    open(os.path.join(full, f"{name}.wav"), "wb").write(b"RIFF")
    json.dump({"text": "Hello.", "language": lang}, open(os.path.join(full, f"{name}.json"), "w"))

with mock.patch.dict(os.environ, {"COSY_VOICE": ""}):
    msg = voices.missing_voice(directory=empty)
    check("no voices: the advice names both ways to add one",
          msg and "voice-add" in msg and "localhost:3005/voices" in msg, f"({msg!r})")
    check("and asks for the speaker's agreement", msg and "agreed" in msg)
    check("an unknown --voice is named",
          "'zed'" in (voices.missing_voice("zed", directory=full) or ""))
    check("several English voices, none chosen, English book: asked to choose",
          "COSY_VOICE" in (voices.missing_voice(None, "en", directory=full) or ""))
    check("one voice matches the book's language: fine",
          voices.missing_voice(None, "fr", directory=full) is None)
    check("no language yet: left to choose() after detection",
          voices.missing_voice(None, None, directory=full) is None)
    check("a named voice that exists: fine", voices.missing_voice("eve", "en", directory=full) is None)

import librerian_engine_v3 as engine  # noqa: E402

out = io.StringIO()
with mock.patch.object(voices, "VOICES_DIR", empty), \
     mock.patch.dict(os.environ, {"TTS_PROVIDER": "cosyvoice", "COSY_VOICE": "", "COSY_REF_WAV": ""}), \
     contextlib.redirect_stdout(out):
    try:
        engine.preflight_voice("en")
        code = None
    except SystemExit as e:
        code = e.code
check("the engine stops with its own exit code, not a traceback",
      code == engine.NO_VOICE_EXIT, f"(code {code})")
check("and says nothing was billed", "nothing was billed" in out.getvalue().lower())

with mock.patch.object(voices, "VOICES_DIR", empty), \
     mock.patch.dict(os.environ, {"TTS_PROVIDER": "kokoro"}):
    try:
        engine.preflight_voice("en")
        ok = True
    except SystemExit:
        ok = False
check("a preset-voice provider needs no clip", ok)

src = open(os.path.join(ROOT, "librerian_engine_v3.py"), encoding="utf-8").read()
pub = src.index("    publish_ebook_early(ebook_dir, librarian_id")
check("the check runs after the ebook is published and before the plan",
      pub < src.index("    preflight_voice(book_language)") < src.index("await cartographer_producer"))

ghost = open(os.path.join(ROOT, "mac_ghost.py"), encoding="utf-8").read()
ghost_code = int(re.search(r"^NO_VOICE_EXIT = (\d+)", ghost, re.M).group(1))
check("the watcher knows the same exit code", ghost_code == engine.NO_VOICE_EXIT)
check("and notifies instead of reporting a crash",
      "returncode == NO_VOICE_EXIT" in ghost and 'notify("Voxie needs a narrator voice"' in ghost)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
