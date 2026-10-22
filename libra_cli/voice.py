"""Add a narrator voice: a short clip, its exact transcript, and a consent record.

No voice ships with libra-sync. Cloning a voice is the speaker's decision, so
every clip added here records who consented and on what basis, and the engine
says so when it narrates with a clip that carries no record.

Sources, in the order people actually have them:

    a YouTube link   https://youtu.be/ID?t=90  (the ?t= is taken as the start)
    a local file     any audio or video ffmpeg reads: MP3, M4A, WAV, MP4, MOV...
                     (from a video only the sound is kept)

The clip is cut to the start/end given, checked and trimmed to speech
(libra_cli/clip_check.py), normalised to 16kHz mono at -20 LUFS
(the reference clip's loudness and noise go straight into the clone, without
ever failing), and written to voices/<name>.wav with voices/<name>.json.

The transcript is not optional and cannot be guessed: a wrong one degrades
the clone rather than erroring.
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import tempfile
from urllib.parse import parse_qs, urlparse

from core.platform_support import install_hint
from libra_cli import clip_check
from core.voices import SUPPORTED_LANGUAGES, VOICES_DIR

TARGET_LUFS = -20
MIN_SECONDS, BEST_MIN, MAX_SECONDS = 3.0, 8.0, 30.0

# What the person adding the voice is confirming. Stored verbatim in the sidecar.
CONSENT = {
    "self": "This is my own voice, and I agree to it being cloned.",
    "permission": "The speaker has given me permission to clone their voice for narration.",
}


class VoiceError(Exception):
    """A problem the person adding the voice can fix; the message says how."""


def parse_time(value):
    """Seconds from '90', '1:30', '00:01:30.5' or YouTube's '1m30s'. None if empty."""
    if value is None or str(value).strip() == "":
        return None
    v = str(value).strip().lower()
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s?)?", v)
    if m and any(m.groups()) and not re.fullmatch(r"\d+(?:\.\d+)?", v):
        h, mi, s = m.groups()
        return int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)
    parts = v.split(":")
    if len(parts) > 3 or not all(re.fullmatch(r"\d+(?:\.\d+)?", p) for p in parts):
        raise VoiceError(f"'{value}' is not a time. Use seconds (90), 1:30 or 00:01:30.")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total


def start_from_url(url):
    """The ?t= (or #t=) of a YouTube link, in seconds, or None."""
    parsed = urlparse(url)
    t = parse_qs(parsed.query).get("t") or parse_qs(parsed.fragment).get("t")
    return parse_time(t[0]) if t else None


def slug(text):
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s[:40] or None


def _hms(seconds):
    return f"{int(seconds // 3600):02d}:{int(seconds % 3600 // 60):02d}:{seconds % 60:06.3f}"


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def _require(tool, hint):
    if shutil.which(tool) is None:
        raise VoiceError(f"{tool} is not installed. {hint}")


def probe_seconds(path):
    r = _run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path])
    if r.returncode != 0:
        raise VoiceError(f"ffprobe could not read {path}")
    return float(json.loads(r.stdout)["format"]["duration"])


def loudness(path):
    err = _run(["ffmpeg", "-hide_banner", "-i", path, "-af", "ebur128", "-f", "null", "-"]).stderr
    found = re.findall(r"I:\s*(-?[\d.]+) LUFS", err)
    return float(found[-1]) if found else None


def fetch_youtube(url, start, end, dest):
    """Download only [start, end] of a YouTube video's audio, as WAV.

    Cut at exactly those times (--force-keyframes-at-cuts). Without it yt-dlp
    cuts where the stream allows: asked for 3.6 s from 35:48.4 it gave 12 s
    starting earlier, asked for 1:01.7 it gave audio from about 1:08
    (2026-10-12, yt-dlp 2026.08.19), so a clip held other words than the
    ones chosen and typed. With it, 1:01.70 measured against the whole video
    was 1:01.70.
    """
    _require("yt-dlp", f"Install it with `{install_hint('yt-dlp')}`.")
    r = _run(["yt-dlp", "--no-update", "--no-playlist", "-f", "ba", "-x", "--audio-format", "wav",
              "--download-sections", f"*{_hms(start)}-{_hms(end)}", "--force-keyframes-at-cuts",
              "-o", dest, url])
    if r.returncode != 0:
        tail = (r.stderr or r.stdout)[-400:]
        if "403" in tail or "Forbidden" in tail:
            # YouTube rotates its signature scheme; an old yt-dlp gets a 403
            # that looks like a permissions problem and is not.
            raise VoiceError("yt-dlp got a 403, which almost always means it is out of date: "
                             "`brew upgrade yt-dlp` (or `pip install -U --pre yt-dlp`).")
        raise VoiceError(f"yt-dlp could not fetch that link: {tail.strip()}")
    if not os.path.exists(dest):
        raise VoiceError("yt-dlp finished but wrote no audio.")


def add_voice(*, text, consent, url=None, file=None, start=None, end=None,
              name=None, label=None, language="en", voices_dir=None, by=None, overwrite=False,
              source_name=None, listen=None):
    """Create voices/<name>.wav and .json. Returns a summary dict.

    Raises VoiceError for anything the caller should show the person as-is.
    The clip is checked first (libra_cli/clip_check.py): trimmed to the typed
    words, refused if it says something else, warned about otherwise. `listen`
    replaces Whisper (tests).
    """
    text = (text or "").strip()
    if not text:
        raise VoiceError("Type exactly what is said in the clip. The clone copies how "
                         "those words sound, so a wrong transcript makes a worse voice.")
    if consent not in CONSENT:
        raise VoiceError("Confirm that this is your own voice or that the speaker gave "
                         "permission. Voices are only added with consent.")
    if bool(url) == bool(file):
        raise VoiceError("Give either a YouTube link or an audio file.")
    if language not in SUPPORTED_LANGUAGES:
        raise VoiceError("Voxie narrates English only for now, so a voice reads English: "
                         "use a clip of someone speaking English.")
    _require("ffmpeg", f"Install it with `{install_hint('ffmpeg')}`.")

    start_s = parse_time(start)
    if start_s is None and url:
        start_s = start_from_url(url)
    start_s = start_s or 0.0
    end_s = parse_time(end)
    if url and end_s is None:
        end_s = start_s + 12.0      # the middle of the 8-15s sweet spot
    if end_s is not None:
        if end_s <= start_s:
            raise VoiceError("The end must come after the start.")
        if end_s - start_s > MAX_SECONDS:
            raise VoiceError(f"Keep the clip under {MAX_SECONDS:.0f} seconds; the model ignores the rest.")
        if end_s - start_s < MIN_SECONDS:
            raise VoiceError(f"The clip needs at least {MIN_SECONDS:.0f} seconds of speech.")

    name = slug(name or label or "") or None
    if not name:
        raise VoiceError("Give the voice a name, e.g. 'my voice' or 'grandad'.")
    voices_dir = voices_dir or VOICES_DIR
    os.makedirs(voices_dir, exist_ok=True)
    dest = os.path.join(voices_dir, f"{name}.wav")
    if os.path.exists(dest) and not overwrite:
        raise VoiceError(f"A voice called '{name}' already exists. Pick another name.")

    with tempfile.TemporaryDirectory() as tmp:
        if url:
            source = os.path.join(tmp, "clip.wav")
            fetch_youtube(url, start_s, end_s, source)
            cut = []                    # yt-dlp already cut the section
        else:
            if not os.path.exists(file):
                raise VoiceError(f"No file at {file}.")
            source = file
            cut = ["-ss", str(start_s)] + (["-to", str(end_s)] if end_s is not None else [])
        # Cut first, without normalising: clipping and a faint recording are
        # measured on what the person gave, and Whisper hears it as it is.
        raw = os.path.join(tmp, "raw.wav")
        r = _run(["ffmpeg", "-y", "-v", "error", *cut, "-i", source, "-vn",
                  "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", raw])
        if r.returncode != 0:
            if "does not contain any stream" in r.stderr or "matches no streams" in r.stderr:
                raise VoiceError("That file has no sound track.")
            raise VoiceError(f"ffmpeg could not read the sound in that file: {r.stderr.strip()[-300:]}")
        seconds = probe_seconds(raw)
        if seconds < MIN_SECONDS:
            raise VoiceError(f"The clip is {seconds:.1f}s; it needs at least {MIN_SECONDS:.0f}s of speech.")
        if seconds > MAX_SECONDS + 0.5:
            raise VoiceError(f"The clip is {seconds:.0f}s; keep it under {MAX_SECONDS:.0f}s (set an end time).")
        try:
            report = clip_check.check(raw, text, language, lufs=loudness(raw), listen=listen)
        except clip_check.ClipMismatch as e:
            raise VoiceError(str(e)) from None
        # Trimmed to the typed words (a trailing sigh or breath goes), then
        # normalised in the same encode.
        trim = report.get("trim")
        window = ["-ss", f"{trim[0]:.3f}", "-to", f"{trim[1]:.3f}"] if trim else []
        out = os.path.join(tmp, "voice.wav")
        r = _run(["ffmpeg", "-y", "-v", "error", "-i", raw, *window,
                  "-af", f"loudnorm=I={TARGET_LUFS}:TP=-2:LRA=11", "-ar", "16000", "-ac", "1",
                  "-c:a", "pcm_s16le", out])
        if r.returncode != 0:
            raise VoiceError(f"ffmpeg could not write the clip: {r.stderr.strip()[-300:]}")
        seconds = probe_seconds(out)
        if seconds < MIN_SECONDS:
            raise VoiceError(f"Only {seconds:.1f}s of the clip is speech; it needs at least "
                             f"{MIN_SECONDS:.0f}s. Choose a longer stretch.")
        lufs = loudness(out)
        shutil.move(out, dest)

    sidecar = {
        "text": text,
        "language": language,
        "label": label or name.replace("_", " ").title(),
        "source": url or source_name or os.path.basename(file),
        "clip": {"start": round(start_s, 3), "end": round(end_s, 3) if end_s is not None else None},
        # What the clip check heard, so a voice that narrates oddly can be
        # traced to its clip later.
        "check": {"by": report.get("checked_by"), "heard": report.get("heard"),
                  "match": report.get("match"), "trimmed_to": report.get("trim"),
                  "pace": report.get("pace")},
        "consent": {"basis": consent, "statement": CONSENT[consent],
                    "confirmed_by": by or os.environ.get("USER") or "unknown",
                    "confirmed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")},
    }
    with open(os.path.join(voices_dir, f"{name}.json"), "w", encoding="utf-8") as f:
        json.dump(sidecar, f, indent=1, ensure_ascii=False)

    warnings = list(report.get("warnings") or [])
    if report.get("note"):
        warnings.append(report["note"])
    if seconds < BEST_MIN:
        warnings.append(f"{seconds:.1f}s works, but 8-15 seconds captures accent and rhythm better.")
    return {"name": name, "label": sidecar["label"], "seconds": round(seconds, 2),
            "lufs": lufs, "language": language, "path": dest, "warnings": warnings,
            "heard": report.get("heard"), "match": report.get("match"), "advice": clip_check.MOOD_ADVICE,
            # True when the clip would teach the narrator to hurry (read too fast, or
            # the next sentence started under half a second after a full stop).
            "retake": bool(report.get("retake")), "pace": report.get("pace")}
