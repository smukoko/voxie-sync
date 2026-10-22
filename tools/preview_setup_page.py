"""The setup page with every real-world effect faked: look, click, change nothing.

    python tools/preview_setup_page.py

Opens the page in your browser. Your config, .env, LaunchAgent and Kobo are
never touched: settings go to a temporary folder, SSH and RunPod answer from
fakes (the IP 203.0.113.10 connects, anything else is refused; a domain ending
in ok.example.com resolves), and a pretend Kobo connects after six seconds.
For designing and reviewing the page, and for screenshots of the launch video.
"""
import os, shutil, sys, tempfile, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from libra_cli import main as cli, server_setup

tmp = tempfile.mkdtemp(prefix="voxie-preview-")
checkout = os.path.join(tmp, "checkout")
os.makedirs(checkout)
shutil.copyfile(os.path.join(ROOT, ".env.example"), os.path.join(checkout, ".env.example"))
os.symlink(os.path.join(ROOT, "assets"), os.path.join(checkout, "assets"))
cli.CONFIG_FILE = os.path.join(tmp, ".librasync.json")
cli.DROPZONE = os.path.join(tmp, "Kobo_Dropzone")
# "Keep this voice" moves the clip into voices/: never into the real one.
import core.voices
core.voices.VOICES_DIR = os.path.join(tmp, "voices")
cli.ARCHIVE = os.path.join(tmp, "Kobo_Archive")
cli.repo_root_dir = lambda: checkout
cli.verify_abs_token = lambda base, t: (bool(t) and t != "bad", "Audiobookshelf rejected this key")
cli._runpod_key_works = lambda k: k != "bad"
cli._docker_ready = lambda: True
cli.IS_MAC = True

KOBO = {"since": None}
def fake_mount():
    if KOBO["since"] is None:
        KOBO["since"] = time.time()
    # Named so the page says it: "KOBOeReader (preview) is connected" is this
    # fake, six seconds after the page opens, not a device.
    return "/Volumes/KOBOeReader (preview)" if time.time() - KOBO["since"] > float(os.getenv("KOBO_AFTER", "6")) else None
cli.find_kobo_mount = fake_mount

server_setup.public_key = lambda: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGx0bW9ja2tleWZvcnByZXZpZXdvbmx5bm90cmVhbA voxie"
server_setup.check_ssh = lambda t: (time.sleep(1.2), (t.endswith("203.0.113.10"), "Permission denied (publickey)."))[1]
server_setup.foreign_stack = lambda t: None
server_setup.link_existing = lambda t, u, k: (time.sleep(1.5), {"upload_token": "preview-token", "abs_token": "fake-abs-key"})[1]
server_setup.resolves_to = lambda d, ip: d.endswith("ok.example.com")
server_setup.push_code = lambda r, t: time.sleep(1)

def fake_install(target, settings, on_line=None, **kw):
    for line in ["🐳 Installing Docker (get.docker.com, Docker's own installer) ...",
                 "🚀 Starting the gateway and Audiobookshelf (the first build takes a few minutes) ...",
                 " Container audiobookshelf  Started", " Container librarian_server  Started",
                 "📚 Setting up Audiobookshelf (admin, library, API key) ...",
                 f"👤 Account '{settings['account_username']}' created.",
                 "✅ Server ready."]:
        time.sleep(1.1)
        on_line(line)
    return "fake-abs-key"
server_setup.install = fake_install

from libra_cli.setup_web import server
server.start_watcher = lambda: (time.sleep(0.6), (True, None))[1]

def fake_kobo(on_line):
    for line in ["🔌 Kobo Auto-Injector Initialized...", "✅ Kobo detected at /Volumes/KOBOeReader!",
                 "   ⬇️  Downloading KOReader v2026.07.1 (pinned, sha256-verified)...",
                 "   📂 Extracting (this writes ~1000 files, give it a moment)...",
                 "   ⬇️  Downloading NickelMenu v0.6.0 (pinned, sha256-verified)...",
                 "   ✅ NickelMenu bootloader installed: 'Voxie' in the Kobo's bottom bar.",
                 "\n   💉 Injecting Voxie Protocols...", "   ✅ Sidecar Plugin injected! (Targeting https://voxie.ok.example.com)",
                 "\n   💾 Flushing all writes to the device..."]:
        time.sleep(1.3)
        on_line(line)
    return {"success": True, "ejected": True}
server.setup_kobo = fake_kobo

import libra_cli.voice as voice
def fake_add_voice(**kw):
    # The page plays the staged clip before "Keep": a second of a soft tone.
    import math, struct, wave
    time.sleep(1.5)
    name = voice.slug(kw.get("name") or "my voice")
    os.makedirs(kw["voices_dir"], exist_ok=True)
    with wave.open(os.path.join(kw["voices_dir"], name + ".wav"), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * 220 * i / 16000)))
                               for i in range(16000)))
    open(os.path.join(kw["voices_dir"], name + ".json"), "w").write("{}")
    return {"name": name, "seconds": 11.4, "lufs": -19.0, "advice": "",
            "warnings": ["There is noise or music in the pauses; the narrator would carry it under "
                         "every sentence. A clip with silence between words is better."]}
voice.add_voice = fake_add_voice

# The Update button: a newer version is "ready", and updating only pretends.
from libra_cli import updater
updater.check = lambda root, run=None: {"current": "a1b2c3d", "latest": "e4f5a6b", "behind": 4, "ahead": 0,
                                        "dirty": False, "can_update": True, "note": "", "error": "",
                                        "date": "2026-10-23"}
def fake_update(on_line, with_server=True):
    for line in ["Updating a1b2c3d to e4f5a6b (2026-10-23) ...", "Installing what changed (this takes a minute) ...",
                 "Settings: the junk filter follows your OpenRouter key (JEV with one, local without).",
                 "Restarting the watcher ...", "Sending the new code to 203.0.113.10 ...",
                 "Rebuilding the server (a minute or two) ...", "The server is up to date."]:
        time.sleep(1.0)
        on_line(line)
    return {"changed": True, "to": "e4f5a6b", "watcher": "restarted", "server": "updated", "from": "a1b2c3d", "deps": False}
cli.run_update = fake_update

server.serve(open_browser=os.getenv("NO_BROWSER") is None, port=int(os.getenv("PORT", "0")))
