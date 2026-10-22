"""What a voice clip will teach the narrator, checked before it is saved (LAUNCH_PLAN 5b).

The clone copies everything in the reference clip, not only the voice: a test
clip ended on a sigh and every sentence of the book got one (2026-10-06). So a
clip is listened to before it becomes a narrator:

  - Whisper's word times trim it to speech: the first typed word -0.15 s, the
    last +0.2 s. A breath, a sigh or applause after the last word is cut, and so
    is speech beyond what the person typed (the transcript decides the clip).
  - What Whisper hears is compared with the typed transcript. A clear mismatch
    is refused, because a wrong transcript degrades the clone without failing.
  - Warnings, never refusals: clipping, a very quiet recording, noise or music
    in the pauses.

A second voice is not detected: per-word pitch and spectrum were tried on the
user's six real clips joined in pairs (2026-10-06), and a single speaker scored
the same as two. It needs a speaker-recognition model; until then the advice
says "one voice only".

Whisper is optional. mlx-whisper on a Mac (Apple silicon), faster-whisper
elsewhere; with neither, the clip is saved as before with a note saying how to
have it checked. Voices are only used by the GPU narrator, never by the free
route (Kokoro), so nothing here may become a hard dependency.
"""
import difflib
import os
import re
import unicodedata

import numpy as np

LEAD_IN = 0.15            # seconds kept before the first typed word
TAIL = 0.20               # and after the last
REFUSE_BELOW = 0.6        # share of typed words Whisper heard, in order
WARN_BELOW = 0.9
QUIET_LUFS = -40.0        # the raw recording, before normalising
CLIPPED_SHARE = 0.0005    # of samples at full scale
PAUSE_MARGIN_DB = 15.0    # speech must stand this far above the pauses' floor
MIN_PAUSE = 0.25          # seconds between words that count as a pause
# What the narrator copies from how the clip is read, not only from the voice:
# a clip read quickly after each full stop gave a narrator that starts quickly
# after every full stop (2026-10-22: 0.34 s between the two sentences of the
# first install's clip). An audiobook leaves about a second.
SENTENCE_PAUSE_MIN = 0.5  # median seconds between sentences under which a retake is advised
FAST_WPM = 190            # an audiobook is about 150
SLOW_WPM = 90
MLX_MODEL = os.environ.get("VOICE_CHECK_MODEL", "mlx-community/whisper-large-v3-turbo")
FASTER_MODEL = os.environ.get("VOICE_CHECK_MODEL_FASTER", "large-v3-turbo")

PAUSE_ADVICE = ("Leave about a second after every full stop: count “one” in your head "
                "before the next sentence.")
MOOD_ADVICE = ("The clip's mood becomes the narrator's: calm, clear, one voice only, "
               "no laughs, sighs or music.")
INSTALL_HINT = ("Install mlx-whisper (Mac) or faster-whisper to have clips checked "
                "and trimmed to speech: pip install mlx-whisper")


class ClipMismatch(Exception):
    """The typed transcript is not what the clip says."""


def _mlx(path, language):
    import mlx_whisper
    from core.hf_cache import model_path
    r = mlx_whisper.transcribe(path, path_or_hf_repo=model_path(MLX_MODEL), language=language,
                               word_timestamps=True, verbose=None)
    return [(w["word"], float(w["start"]), float(w["end"]))
            for s in r.get("segments", []) for w in s.get("words", [])]


def _faster(path, language):
    from faster_whisper import WhisperModel
    model = WhisperModel(FASTER_MODEL, device="auto", compute_type="int8")
    segments, _ = model.transcribe(path, language=language, word_timestamps=True)
    return [(w.word, float(w.start), float(w.end)) for s in segments for w in (s.words or [])]


def transcriber():
    """(name, fn(path, language) -> [(word, start, end)]) or None when no Whisper is installed."""
    import importlib.util
    if importlib.util.find_spec("mlx_whisper"):
        return "mlx-whisper " + MLX_MODEL.split("/")[-1], _mlx
    if importlib.util.find_spec("faster_whisper"):
        return "faster-whisper " + FASTER_MODEL, _faster
    return None


_TOKEN = re.compile(r"[^\W_]+(?:'[^\W_]+)*")


def _spelled(token, language):
    """Whisper writes "5" where a person types "five": compare words with words."""
    if not token.isdigit() or len(token) > 4:
        return [token]
    from core.structure import spell_number
    said = spell_number(int(token))
    return tokens(said) if said else [token]


def tokens(text, language=None, spans=False):
    """Words to compare: lower case, accents kept, punctuation gone, digits spelled.

    With spans=True, [(token, start offset in text)].
    """
    norm = unicodedata.normalize("NFC", text).lower().replace("\u2019", "'")
    out = []
    for m in _TOKEN.finditer(norm):
        for t in (_spelled(m.group(), language) if language else [m.group()]):
            out.append((t, m.start()))
    return out if spans else [t for t, _ in out]


def _heard_tokens(heard_words, language=None):
    """(tokens Whisper heard, the index of the heard word each one came from).

    Tokenised as one string: Whisper hands "d'acheter" over as " d" + "'acheter".
    """
    joined, starts = "", []
    for w, _, _ in heard_words:
        starts.append(len(joined))
        joined += w
    heard_t, owner = [], []
    for t, at in tokens(joined, language, spans=True):
        heard_t.append(t)
        owner.append(max(i for i, s0 in enumerate(starts) if s0 <= at))
    return heard_t, owner


def match_typed(typed, heard_words, language=None):
    """Where the typed transcript sits in what Whisper heard.

    Returns (share of typed words heard in order, index of the first and last
    heard word that matched). Whisper writes "5" where a person types "five"
    and splits "e-mails"; the share tolerates that, a different sentence does not.
    """
    typed_t = tokens(typed, language)
    heard_t, owner = _heard_tokens(heard_words, language)
    if not typed_t or not heard_t:
        return 0.0, None, None
    sm = difflib.SequenceMatcher(None, typed_t, heard_t, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size]
    if not blocks:
        return 0.0, None, None
    matched = sum(b.size for b in blocks)
    first = owner[blocks[0].b]
    last = owner[blocks[-1].b + blocks[-1].size - 1]
    # A word Whisper heard that nobody typed counts against the transcript too
    # (a typed sentence missing a clause is wrong), but a substitution only once.
    span = blocks[-1].b + blocks[-1].size - blocks[0].b
    share = matched / max(len(typed_t), span)
    return share, first, last


_SENTENCE_END = re.compile(r"[.!?\u2026\n]")


def sentence_gaps(typed, heard_words, language=None):
    """[(last word of a sentence, seconds of silence before the next sentence)].

    Sentences are the typed transcript's (a full stop, a question or exclamation
    mark, a line break), found in what Whisper heard by matching words; a word
    Whisper did not hear in order is skipped. The gap is from the end of that
    word to the start of the next one.
    """
    norm = unicodedata.normalize("NFC", typed).lower().replace("\u2019", "'")
    spans = [(m.start(), m.end()) for m in _TOKEN.finditer(norm)]
    typed_t = tokens(typed, language)
    if len(spans) != len(typed_t) or len(spans) < 2:
        return []           # digits spelled out change the count: no guess then
    heard_t, owner = _heard_tokens(heard_words, language)
    sm = difflib.SequenceMatcher(None, typed_t, heard_t, autojunk=False)
    where = {}
    for b in sm.get_matching_blocks():
        for k in range(b.size):
            where[b.a + k] = owner[b.b + k]
    out = []
    for k in range(len(spans) - 1):
        between = norm[spans[k][1]:spans[k + 1][0]]
        if not _SENTENCE_END.search(between):
            continue
        i, j = where.get(k), where.get(k + 1)
        if i is None or j is None or j <= i:
            continue
        out.append((typed_t[k], round(max(0.0, heard_words[j][1] - heard_words[i][2]), 2)))
    return out


def runs_past(typed, heard_words, language=None):
    """True when what Whisper heard is the start of the transcript and the transcript goes on."""
    heard_t = tokens("".join(w for w, _, _ in heard_words), language)
    typed_t = tokens(typed, language)
    if len(heard_t) < 4 or len(typed_t) <= len(heard_t):
        return False
    head = difflib.SequenceMatcher(None, typed_t[:len(heard_t) + 3], heard_t, autojunk=False)
    return sum(b.size for b in head.get_matching_blocks()) >= 0.7 * len(heard_t)


def _db(x):
    return 20 * np.log10(np.sqrt(np.mean(np.square(x))) + 1e-9)


def read_wav(path):
    """16-bit PCM WAV as float samples in [-1, 1], mono. The standard library, no soundfile."""
    import wave
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"{path}: expected 16-bit PCM")
        sr, channels = w.getframerate(), w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype("float32") / 32768.0
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1)
    return x, sr


def pace_report(text, spoken, language=None):
    """How the clip is paced, and what to tell the reader: a dict with "wpm",
    "sentence_gaps" ([(word, seconds)]), "warnings" and "retake" (True when the
    clip would teach the narrator to hurry).

    `spoken` is the heard words from the first typed word to the last.
    """
    words = len(tokens(text, language))
    span = spoken[-1][2] - spoken[0][1]
    wpm = 60.0 * words / span if span > 0 and words else 0.0
    gaps = sentence_gaps(text, spoken, language)
    warnings, retake = [], False
    if gaps:
        ordered = sorted(g for _, g in gaps)
        median = ordered[len(ordered) // 2]
        if median < SENTENCE_PAUSE_MIN:
            retake = True
            warnings.append(
                f"You began the next sentence {median:.1f} s after a full stop. The narrator copies "
                "that: it would run its sentences together after every full stop of the book. "
                + PAUSE_ADVICE)
    if wpm > FAST_WPM:
        retake = True
        warnings.append(f"You read at about {wpm:.0f} words a minute; an audiobook is about 150, and "
                        "the narrator copies the speed. Slower, as if reading aloud to someone.")
    elif words >= 12 and 0 < wpm < SLOW_WPM:
        warnings.append(f"You read at about {wpm:.0f} words a minute, slower than an audiobook "
                        "(about 150); the narrator would drag.")
    return {"wpm": round(wpm), "sentence_gaps": gaps, "warnings": warnings, "retake": retake}


def check(raw_path, text, language, lufs=None, listen=None):
    """Listen to a cut, un-normalised clip. Returns a report dict.

    `listen` is the transcriber (tests pass a fake); by default the installed
    Whisper, if any. Raises ClipMismatch when the transcript is clearly not
    what is said. The report's "trim" is (start, end) in the clip's seconds, or
    None when there are no word times to trim by.
    """
    samples, sr = read_wav(raw_path)
    duration = len(samples) / sr
    warnings, report = [], {"trim": None, "heard": None, "match": None, "checked_by": None}

    peak = np.abs(samples)
    if np.mean(peak >= 0.99) > CLIPPED_SHARE and np.sum(peak >= 0.99) >= 10:
        warnings.append("The recording is distorted (it clips at full volume); the narrator "
                        "would copy the distortion. A quieter take is better.")
    if lufs is not None and lufs < QUIET_LUFS:
        warnings.append(f"The recording is very quiet ({lufs:.0f} LUFS); raising it raises its "
                        "background noise too. Record closer to the microphone if you can.")

    found = (None, listen) if listen else transcriber()
    if not found:
        report["note"] = INSTALL_HINT
        report["warnings"] = warnings
        return report
    report["checked_by"] = found[0]
    words = [(w, a, b) for w, a, b in found[1](raw_path, language) if b >= a]
    report["heard"] = "".join(w for w, _, _ in words).strip()
    share, first, last = match_typed(text, words, language)
    report["match"] = round(share, 2)
    if share < REFUSE_BELOW and runs_past(text, words, language):
        raise ClipMismatch(
            f"Your transcript goes on after the clip ends: the clip stops at "
            f"“…{report['heard'][-80:]}”. Type only what the clip says, or set a later end time.")
    if share < REFUSE_BELOW:
        heard = report["heard"] or "nothing at all"
        raise ClipMismatch(
            f"The clip does not say what you typed. Whisper heard: “{heard[:300]}”. "
            "Type exactly what is said in the clip (or move its start and end).")
    if share < WARN_BELOW:
        warnings.append(f"Whisper heard “{report['heard'][:200]}”, which differs from what you "
                        "typed. Check the transcript word for word: the clone copies how "
                        "those words sound.")

    spoken = words[first:last + 1]
    start = max(0.0, spoken[0][1] - LEAD_IN)
    end = min(duration, spoken[-1][2] + TAIL)
    report["trim"] = (round(start, 3), round(end, 3))
    pace = pace_report(text, spoken, language)
    report["pace"] = {"wpm": pace["wpm"], "sentence_gaps": pace["sentence_gaps"]}
    report["retake"] = pace["retake"]
    warnings += pace["warnings"]

    gaps = [(a[2], b[1]) for a, b in zip(spoken, spoken[1:]) if b[1] - a[2] >= MIN_PAUSE]
    if gaps:
        speech = np.concatenate([samples[int(a * sr):int(b * sr)] for _, a, b in spoken])
        # The floor of the pauses, not their average: a pause opens on the
        # last word's tail and may hold a breath, but noise or music fills its
        # quietest moments too. Measured on real clips (2026-10-06): a clean
        # voice's comma pause averages 13 dB under its speech and its floor 29;
        # other clean recordings' floors sit 32-34 dB under, a music bed 4-12, hiss 14.
        frame, hop = int(0.02 * sr), int(0.01 * sr)
        frames = []
        for a, b in gaps:
            seg = samples[int((a + 0.05) * sr):int((b - 0.05) * sr)]
            frames += [_db(seg[i:i + frame]) for i in range(0, len(seg) - frame, hop)]
        if len(frames) >= 5 and _db(speech) - float(np.percentile(frames, 10)) < PAUSE_MARGIN_DB:
            warnings.append("There is noise or music in the pauses; the narrator would carry "
                            "it under every sentence. A clip with silence between words is better.")
    report["warnings"] = warnings
    return report
