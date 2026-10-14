"""Adding a narrator voice: consent is recorded, the clip is cut and normalised.

No voice ships with Voxie, so this path is the first thing a new user
meets. Offline: a generated tone stands in for speech, and yt-dlp is mocked.
"""
import contextlib, io, json, os, shutil, subprocess, sys, tempfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from libra_cli import voice, clip_check  # noqa: E402
from core import voices  # noqa: E402

# A sine tone is not speech: these tests are about cutting and consent, so the
# clip check runs as on a computer without Whisper (tools/test_clip_check.py
# covers the check itself, with a fake listener).
clip_check.transcriber = lambda: None

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


def refused(fn, needle):
    try:
        fn()
    except voice.VoiceError as e:
        return needle.lower() in str(e).lower()
    return False


if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
    print("SKIP  ffmpeg not installed")
    sys.exit(0)

work = tempfile.mkdtemp()
vdir = os.path.join(work, "voices")
tone = os.path.join(work, "talk.mp3")
subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=220:duration=40",
                "-ac", "2", "-ar", "44100", tone], check=True)
base = dict(text="Hello there.", file=tone, voices_dir=vdir, language="en")

check("times parse in every form people type",
      [voice.parse_time(t) for t in ("90", "1:30", "00:01:30.5", "1m30s")] == [90, 90, 90.5, 90])
check("a YouTube ?t= becomes the start", voice.start_from_url("https://youtu.be/x?t=95") == 95)
check("no consent, no voice", refused(lambda: voice.add_voice(consent=None, name="a", **base), "consent"))
check("no transcript, no voice",
      refused(lambda: voice.add_voice(**{**base, "text": " "}, consent="self", name="a"), "exactly what is said"))
check("over 30 seconds is refused before any work",
      refused(lambda: voice.add_voice(consent="self", name="a", start="0", end="45", **base), "under 30"))
check("a voice in another language is refused: Voxie narrates English only",
      refused(lambda: voice.add_voice(**{**base, "language": "nl"}, consent="self", name="a"), "English only"))
check("nothing was written by a refused voice", not os.path.exists(vdir) or os.listdir(vdir) == [])

r = voice.add_voice(consent="permission", name="Grandad", start="0:05", end="0:17", **base)
side = json.load(open(os.path.join(vdir, "grandad.json")))
probe = json.loads(subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams",
                                   os.path.join(vdir, "grandad.wav")], capture_output=True, text=True).stdout)
st = probe["streams"][0]
check("the clip is cut to the chosen window", abs(r["seconds"] - 12) < 0.2, f"({r['seconds']}s)")
check("and normalised to 16kHz mono", st["sample_rate"] == "16000" and st["channels"] == 1)
check("the consent is recorded with its statement",
      side["consent"]["basis"] == "permission" and "permission" in side["consent"]["statement"]
      and side["consent"]["confirmed_at"])
check("the transcript is stored verbatim", side["text"] == "Hello there.")
check("the voice is usable by the engine", "grandad" in voices.available(vdir))
check("without Whisper the clip is saved, with a note saying how to have it checked",
      any("whisper" in w.lower() for w in r["warnings"]) and side["check"]["by"] is None)
check("the mood advice comes back for the page to show", "sighs" in r["advice"])
check("a name is never silently overwritten",
      refused(lambda: voice.add_voice(consent="self", name="grandad", end="5", **base), "already exists"))

# A voice added by hand, with no consent record, is named when it narrates.
shutil.copy(os.path.join(vdir, "grandad.wav"), os.path.join(vdir, "old.wav"))
json.dump({"text": "x", "language": "en"}, open(os.path.join(vdir, "old.json"), "w"))
out = io.StringIO()
with contextlib.redirect_stdout(out):
    voices.choose("old", "en", directory=vdir)
check("the engine says when a voice has no consent record", "no consent record" in out.getvalue())
out = io.StringIO()
with contextlib.redirect_stdout(out):
    voices.choose("grandad", "en", directory=vdir)
check("and stays quiet for one that has", "consent" not in out.getvalue())

# A video file: only its sound is kept, cut to the window.
video = os.path.join(work, "phone.mp4")
subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=20",
                "-f", "lavfi", "-i", "sine=frequency=330:duration=20", "-shortest",
                "-c:v", "mpeg4", "-c:a", "aac", video], check=True)
r = voice.add_voice(consent="self", name="from video", file=video, start="2", end="12", text="Hi.",
                    voices_dir=vdir, source_name="IMG_0042.MOV")
st = json.loads(subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams",
                                r["path"]], capture_output=True, text=True).stdout)["streams"]
check("an MP4 becomes a voice: sound only, cut to the window",
      len(st) == 1 and st[0]["codec_type"] == "audio" and abs(r["seconds"] - 10) < 0.2, f"({r['seconds']}s)")
check("the file's own name is recorded, not a temp path",
      json.load(open(os.path.join(vdir, "from_video.json")))["source"] == "IMG_0042.MOV")
silent = os.path.join(work, "silent.mp4")
subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=8",
                "-c:v", "mpeg4", silent], check=True)
check("a video with no sound says so",
      refused(lambda: voice.add_voice(consent="self", name="mute", file=silent, end="5", text="x", voices_dir=vdir),
              "no sound"))

# YouTube: only the chosen section is fetched, starting at the link's ?t=.
calls = []


def fake_run(cmd):
    if cmd[0] == "yt-dlp":
        calls.append(cmd)
        dest = cmd[cmd.index("-o") + 1]
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", tone, "-t", "12", dest], check=True)
        return mock.Mock(returncode=0, stdout="", stderr="")
    return subprocess.run(cmd, capture_output=True, text=True)


with mock.patch.object(voice, "_run", fake_run), mock.patch.object(voice.shutil, "which", lambda t: "/usr/bin/" + t):
    r = voice.add_voice(consent="self", name="me", url="https://youtu.be/abc?t=95", text="Hi.", voices_dir=vdir)
section = calls[0][calls[0].index("--download-sections") + 1] if calls else None
check("only the section from ?t= is downloaded, 12s by default",
      section == "*00:01:35.000-00:01:47.000", f"({section})")
check("and cut at exactly those times, not at the stream's nearest keyframe",
      bool(calls) and "--force-keyframes-at-cuts" in calls[0], f"({calls[:1]})")
check("the source link is recorded", json.load(open(os.path.join(vdir, "me.json")))["source"] == "https://youtu.be/abc?t=95")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
