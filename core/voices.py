"""The narrator library: reference clips that CosyVoice clones from.

A voice is two files in voices/:

    voices/narrator.wav     16kHz mono, 3-30s (8-15s is the sweet spot)
    voices/narrator.json    {"text": "<exact transcript>", "language": "en",
                             "label": "Narrator"}

Two files rather than one registry, so adding a voice is dropping a pair in and
nothing else -- no central file to merge, and a half-added voice is visibly
half-added rather than quietly breaking the ones beside it.

WHAT A REFERENCE CLIP CAN AND CANNOT CHANGE

It carries accent, timbre, pace and delivery. It does NOT teach the model a
language: a Nigerian-accented clip reading English text works, because the
accent comes free with the clip while the words come from the model's training.

Voxie narrates English only (core/languages.py, user 2026-10-07), so a voice
is an English voice. `language` stays in the sidecar for the voices other
languages will need later, and choose() warns when a voice is not English.
"""
import json
import os

# Anchored to the checkout, never to the current directory: `voxie init` is run
# from wherever the reader's shell happens to be (their home, on a from-scratch
# install) while the watcher runs from the checkout, so a relative "voices"
# saved the voice in one place and looked for it in another (2026-10-22: the
# first book was set aside for "no narrator voice" with one just added).
VOICES_DIR = os.getenv("COSY_VOICES_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "voices")

# What Voxie narrates. CosyVoice 3 itself reads nine languages; only English has
# been tested end to end, so only English is offered.
SUPPORTED_LANGUAGES = {"en"}


class Voice:
    def __init__(self, name, wav, text, language, label=None, note=None, consent=None):
        self.name = name
        self.wav = wav
        self.text = text
        self.language = language
        self.label = label or name.replace("_", " ").title()
        self.note = note
        # {"basis": "self"|"permission", ...} from `voxie voice-add`; None
        # for a clip added any other way, which the engine says out loud.
        self.consent = consent

    def __repr__(self):
        return f"<Voice {self.name} ({self.language})>"


def _sidecar(wav_path):
    return os.path.splitext(wav_path)[0] + ".json"


def available(directory=None):
    """Every usable voice, keyed by name. A clip with no sidecar is skipped.

    Skipped rather than guessed at: the sidecar carries the transcript, and an
    absent or wrong transcript degrades the clone quietly instead of failing,
    which is the hardest kind of fault to notice months later.
    """
    directory = directory or VOICES_DIR
    found = {}
    if not os.path.isdir(directory):
        return found
    for entry in sorted(os.listdir(directory)):
        if not entry.endswith(".wav"):
            continue
        wav = os.path.join(directory, entry)
        side = _sidecar(wav)
        if not os.path.exists(side):
            continue
        try:
            meta = json.load(open(side, encoding="utf-8"))
        except (ValueError, OSError):
            continue
        text = (meta.get("text") or "").strip()
        if not text:
            continue
        name = os.path.splitext(entry)[0]
        found[name] = Voice(name, wav, text, meta.get("language", "en"),
                            meta.get("label"), meta.get("note"), meta.get("consent"))
    return found


def choose(name=None, book_language=None, directory=None):
    """Resolve the voice to narrate with.

    Precedence: explicit name (a --voice flag) > COSY_VOICE > the only voice
    whose language matches the book > error listing what is available.

    A voice whose language is not the book's is a warning, not a refusal: the
    engine only narrates English books, and a voice recorded in another
    language still reads them, with its accent.
    """
    voices = available(directory)
    if not voices:
        raise FileNotFoundError(
            f"No voices in {directory or VOICES_DIR}/. No voice ships with Voxie.\n  "
            + HOW_TO_ADD)

    wanted = name or os.getenv("COSY_VOICE") or None
    if wanted:
        if wanted not in voices:
            raise KeyError(
                f"Unknown voice '{wanted}'. Available: {', '.join(sorted(voices))}")
        voice = voices[wanted]
    else:
        matching = [v for v in voices.values() if v.language == book_language]
        if len(matching) == 1:
            voice = matching[0]
        elif len(voices) == 1:
            voice = next(iter(voices.values()))
        else:
            raise KeyError(
                f"Several voices available and none chosen. Set COSY_VOICE or pass "
                f"--voice. Available: {', '.join(sorted(voices))}")

    if voice.consent is None:
        print(f"   ⚠️  Voice '{voice.name}' has no consent record. Clone only voices whose "
              "speaker agreed; `voxie voice-add` records it.")
    if book_language and voice.language != book_language:
        print(f"   ⚠️  Voice '{voice.name}' is {voice.language} but the book is "
              f"{book_language}; the accent will not match the text.")
    return voice


HOW_TO_ADD = ("Add one from an 8-15 second clip of someone who agreed to it: the Voxie "
              "setup page (run `voxie init`, then Narration > Your narrator's voice), or\n"
              "  voxie voice-add --file memo.m4a --start 0:05 --end 0:17 "
              "--text \"exactly what is said\" --name me --consent self")


def missing_voice(name=None, book_language=None, directory=None):
    """Why choose() would fail for this run, as advice, or None if it would not.

    Checked before any GPU is asked for. Without it, a fresh install's first
    drop got as far as building the provider and died there in a traceback,
    which the watcher reported as "the Engine crashed". A book with no
    language yet cannot rule out the "several voices" case, because the
    language detected later may pick one; that case is left to choose().
    """
    voices = available(directory)
    if not voices:
        return (f"No narrator voice yet. No voice ships with Voxie.\n  {HOW_TO_ADD}")
    wanted = name or os.getenv("COSY_VOICE") or None
    if wanted and wanted not in voices:
        return (f"The voice '{wanted}' is not in {directory or VOICES_DIR}/. "
                f"Available: {', '.join(sorted(voices))}.")
    if not wanted and len(voices) > 1 and book_language and \
            sum(v.language == book_language for v in voices.values()) != 1:
        return (f"Several voices and none chosen for a '{book_language}' book. Set "
                f"COSY_VOICE in .env or pass --voice. Available: {', '.join(sorted(voices))}.")
    return None


def describe(directory=None):
    """One line per voice, for a --list flag."""
    voices = available(directory)
    if not voices:
        return f"No voices in {directory or VOICES_DIR}/.\n  {HOW_TO_ADD}"
    width = max(len(n) for n in voices)
    lines = [f"{len(voices)} voice(s) in {directory or VOICES_DIR}/:"]
    for name, v in sorted(voices.items()):
        flag = "" if v.language in SUPPORTED_LANGUAGES else "  (not English: Voxie narrates English only)"
        if v.consent is None:
            flag += "  (no consent record)"
        lines.append(f"  {name:<{width}}  {v.language}  {v.label}{flag}")
    if any(v.consent is None for v in voices.values()):
        lines.append("Clone only voices whose speaker agreed. `voxie voice-add` records "
                     "that consent; a voice added by hand has none.")
    return "\n".join(lines)
