"""The setup page (`voxie init` in the browser): locked to this computer and tab.

Any website open in the same browser can send requests to 127.0.0.1, so the
page's API must refuse every request without the session key, and any request
whose Host is not this machine (DNS rebinding). Then the steps themselves:
the login rules, narration written to .env, the server's address saved, the
Kobo installer's codes turned into sentences. Offline: Flask's test client,
with SSH, RunPod and the Kobo faked.
"""
import json
import io
import os
import shutil
import sys
import tempfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import envfile  # noqa: E402
from libra_cli import main as cli  # noqa: E402
from libra_cli.setup_web import server  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


tmp = tempfile.mkdtemp()
checkout = os.path.join(tmp, "checkout")
os.makedirs(checkout)
shutil.copyfile(os.path.join(ROOT, ".env.example"), os.path.join(checkout, ".env.example"))

patches = [
    mock.patch.object(cli, "CONFIG_FILE", os.path.join(tmp, ".librasync.json")),
    mock.patch.object(cli, "DROPZONE", os.path.join(tmp, "Kobo_Dropzone")),
    mock.patch.object(cli, "ARCHIVE", os.path.join(tmp, "Kobo_Archive")),
    mock.patch.object(cli, "repo_root_dir", lambda: checkout),
    mock.patch.object(cli, "_runpod_key_works", lambda k: k == "good-key"),
    mock.patch.object(cli, "_openrouter_key_works", lambda k: k == "sk-or-good"),
    mock.patch.object(cli, "_docker_ready", lambda: True),
    mock.patch.object(cli, "verify_abs_token", lambda base, t: (t == "abs-ok", "rejected")),
]
for p in patches:
    p.start()

KEY = "session-key"
app = server.create_app(KEY, {"stop": False})
c = app.test_client()
H = {"X-Voxie-Key": KEY}

# --- 1. Locked to this tab and this machine ---------------------------------
check("the page itself loads without a key", c.get("/").status_code == 200)
check("an API call without the key is refused", c.get("/api/state").status_code == 403)
check("an API call with a wrong key is refused", c.get("/api/state", headers={"X-Voxie-Key": "guess"}).status_code == 403)
check("a foreign Host is refused (DNS rebinding)",
      c.get("/api/state", headers={**H, "Host": "evil.example.com"}).status_code == 403)
r = c.get("/api/state", headers=H)
check("with the key, the state answers", r.status_code == 200 and "links" in r.get_json())
check("the last screen gets the donate link", r.get_json()["links"].get("donate", "").startswith("https://ko-fi.com/"))
csp = r.headers.get("Content-Security-Policy", "")
check("no outside scripts, styles or frames", "script-src 'self'" in csp and "frame-ancestors 'none'" in csp)
check("the state never carries a secret",
      not any(k in json.dumps(r.get_json()).lower() for k in ("password\": \"", "upload_token", "abs_api")))

# --- 2. Login --------------------------------------------------------------
bad = c.post("/api/account", headers=H, json={"username": "anna", "password": "short"}).get_json()
check("a short password is refused, on its field", not bad["ok"] and bad["field"] == "password")
bad = c.post("/api/account", headers=H, json={"username": "an na", "password": "long enough"}).get_json()
check("a username with a space is refused", not bad["ok"] and bad["field"] == "username")
check("server steps wait for a login", c.post("/api/server/install", headers=H, json={}).status_code == 409)
ok = c.post("/api/account", headers=H, json={"username": "anna", "password": "correct horse"}).get_json()
check("a good login is kept for the next steps", ok["ok"] and app.config["session"]["user"] == "anna")

# --- 3. A server you already run -----------------------------------------
r = c.post("/api/server/existing", headers=H, json={"address": "https://voxie.example.com", "absKey": "nope"}).get_json()
check("a rejected ABS key is said on its field", not r["ok"] and r["field"] == "absKey")
r = c.post("/api/server/existing", headers=H, json={"address": "https://voxie.example.com", "absKey": "abs-ok"}).get_json()
cfg = json.load(open(os.path.join(tmp, ".librasync.json")))
check("an existing server is saved", r["ok"] and cfg["GATEWAY_URL"] == "https://voxie.example.com"
      and cfg["ACCOUNT_USERNAME"] == "anna")
check("its two server lines carry the same upload token",
      r["envLine"] == f"UPLOAD_TOKEN={cfg['UPLOAD_TOKEN']}" and "add-user anna --userkey" in r["addUser"])
with mock.patch.object(server.server_setup, "check_ssh", lambda t: (True, "")), \
        mock.patch.object(server.server_setup, "link_existing",
                          lambda t, u, k: {"upload_token": "server-tok", "abs_token": "abs-ok"}):
    r = c.post("/api/server/existing", headers=H, json={"address": "https://voxie.example.com",
                                                         "ssh": "root@203.0.113.9"}).get_json()
cfg = json.load(open(os.path.join(tmp, ".librasync.json")))
check("with the server's login, nothing is left to type on it", r["ok"] and r["linked"]
      and cfg["UPLOAD_TOKEN"] == "server-tok" and cfg["ABS_API_TOKEN"] == "abs-ok", str(r)[:120])
with mock.patch.object(server.server_setup, "check_ssh", lambda t: (False, "Permission denied (publickey).")):
    r = c.post("/api/server/existing", headers=H, json={"address": "https://voxie.example.com",
                                                         "ssh": "root@203.0.113.9"}).get_json()
check("a login that does not work is said on its field", not r["ok"] and r["field"] == "ssh")
def no_stack(t, u, k):
    raise RuntimeError("No Voxie server (a container called librarian_server) runs there.")


with mock.patch.object(server.server_setup, "check_ssh", lambda t: (True, "")), \
        mock.patch.object(server.server_setup, "link_existing", no_stack):
    r = c.post("/api/server/existing", headers=H, json={"address": "https://voxie.example.com",
                                                         "ssh": "root@203.0.113.9"}).get_json()
check("a server where the link fails says why, on the login field", not r["ok"] and r["field"] == "ssh"
      and "No Voxie server" in r["error"])

# --- 4. Narration -----------------------------------------------------------
env_path = os.path.join(checkout, ".env")
check("a RunPod key RunPod rejects is said so",
      not c.post("/api/runpod/check", headers=H, json={"key": "bad"}).get_json()["ok"])
r = c.post("/api/narration", headers=H, json={"provider": "runpod", "key": "good-key", "template": "tmpl1"}).get_json()
env = envfile.read(env_path)
check("RunPod is written to .env, with a GPU token",
      r["ok"] and env["TTS_PROVIDER"] == "cosyvoice" and env["RUNPOD_API_KEY"] == "good-key"
      and env["RUNPOD_TEMPLATE_ID"] == cli.RUNPOD_PUBLIC_TEMPLATE_ID and len(env["TTS_API_TOKEN"]) == 64)
check("the public template is used without asking",
      cli.RUNPOD_PUBLIC_TEMPLATE_ID and c.get("/api/state", headers=H).get_json()["publicTemplate"] is True)
token = env["TTS_API_TOKEN"]
c.post("/api/narration", headers=H, json={"provider": "runpod", "template": "tmpl1"})
check("saving again keeps the key and the GPU token",
      envfile.read(env_path)["RUNPOD_API_KEY"] == "good-key" and envfile.read(env_path)["TTS_API_TOKEN"] == token)
# The voice director's OpenRouter key: checked, saved once, kept after.
st = c.get("/api/state", headers=H).get_json()
check("no OpenRouter key yet, and the page knows", st["hasOpenRouterKey"] is False
      and st["links"]["openrouter"].startswith("https://openrouter.ai"))
check("a key OpenRouter rejects is said so",
      not c.post("/api/openrouter/check", headers=H, json={"key": "sk-or-bad"}).get_json()["ok"])
c.post("/api/narration", headers=H, json={"provider": "runpod", "template": "tmpl1", "openrouterKey": "sk-or-good"})
check("the OpenRouter key is written to .env", envfile.read(env_path)["OPENROUTER_API_KEY"] == "sk-or-good")
st = c.get("/api/state", headers=H)
check("a second run sees it, without ever being sent it",
      st.get_json()["hasOpenRouterKey"] is True and "sk-or-good" not in st.get_data(as_text=True))
c.post("/api/narration", headers=H, json={"provider": "runpod", "template": "tmpl1"})
check("saving again without a new key keeps it", envfile.read(env_path)["OPENROUTER_API_KEY"] == "sk-or-good")
envfile.update(env_path, {"OPENROUTER_API_KEY": "", "OPEN_ROUTER_API_KEY": "sk-or-old"})
check("the older spelling in .env counts as a key", c.get("/api/state", headers=H).get_json()["hasOpenRouterKey"])
envfile.update(env_path, {"OPENROUTER_API_KEY": "sk-or-good", "OPEN_ROUTER_API_KEY": ""})
c.post("/api/narration", headers=H, json={"provider": "kokoro"})
check("this Mac, free: TTS_PROVIDER=kokoro", envfile.read(env_path)["TTS_PROVIDER"] == "kokoro")

# --- 5. The Kobo ------------------------------------------------------------
check("the installer's codes become sentences",
      "Plug it in" in server._kobo_error("kobo_not_detected") and server._kobo_error("weird") == "weird")
looks_asked = []


def fake_setup_kobo(on_line, look=None):
    looks_asked.append(look)
    on_line("Downloading KOReader")
    return {"success": True, "ejected": True}


with mock.patch.object(server, "setup_kobo", fake_setup_kobo):
    job = c.post("/api/kobo/install", headers=H, json={"look": "kobo"}).get_json()["job"]
    import time
    for _ in range(50):
        v = c.get(f"/api/jobs/{job}", headers=H).get_json()
        if v["status"] != "running":
            break
        time.sleep(0.05)
check("a Kobo job reports its log and its result",
      v["status"] == "done" and v["result"]["ejected"] and v["lines"] == ["Downloading KOReader"])
check("the look chosen on the page reaches the installer", looks_asked == ["kobo"], looks_asked)
with mock.patch.object(server, "setup_kobo", fake_setup_kobo):
    c.post("/api/kobo/install", headers=H, json={"look": "kindle"})
    for _ in range(50):
        if len(looks_asked) == 2:
            break
        time.sleep(0.05)
check("an unknown look is never passed on", looks_asked[-1:] == [None], looks_asked)


class FakeProc:
    def __init__(self, args, **kw):
        FakeProc.args = args
        self.stdout = io.StringIO('{"success": true}\n')
        self.stderr = io.StringIO("")

    def wait(self):
        return 0


with mock.patch.object(server.subprocess, "Popen", FakeProc):
    server.setup_kobo(lambda line: None, "koreader")
check("the installer is asked for that look", FakeProc.args[-2:] == ["--look", "koreader"], FakeProc.args)
with mock.patch.object(server.subprocess, "Popen", FakeProc):
    server.setup_kobo(lambda line: None)
check("no look chosen: the installer keeps the Kobo's", "--look" not in FakeProc.args, FakeProc.args)
check("the page offers both looks, with their pictures",
      all(s in open(os.path.join(os.path.dirname(server.__file__), "static", "index.html")).read()
          for s in ('value="kobo"', 'value="koreader"', "look-kobo.png", "look-koreader.png"))
      and all(os.path.exists(os.path.join(os.path.dirname(server.__file__), "static", f))
              for f in ("look-kobo.png", "look-koreader.png")))

# --- 6. A voice: checked into staging, heard, then kept or dropped ----------
from core import voices as voices_mod  # noqa: E402
vdir = os.path.join(tmp, "voices")


def fake_add_voice(**kw):
    os.makedirs(kw["voices_dir"], exist_ok=True)
    name = kw["name"].replace(" ", "_")
    open(os.path.join(kw["voices_dir"], name + ".wav"), "wb").write(b"RIFF....WAVE")
    open(os.path.join(kw["voices_dir"], name + ".json"), "w").write("{}")
    return {"name": name, "seconds": 9.1, "warnings": ["There is noise or music in the pauses."],
            "heard": "hello", "advice": "calm"}


def run_voice(name):
    job = c.post("/api/voice", headers=H, data={"url": "https://youtu.be/x", "text": "hello",
                                                "name": name, "consent": "self"}).get_json()["job"]
    for _ in range(50):
        v = c.get(f"/api/jobs/{job}", headers=H).get_json()
        if v["status"] != "running":
            return v
        time.sleep(0.05)


with mock.patch.object(voices_mod, "VOICES_DIR", vdir), \
        mock.patch("libra_cli.voice.add_voice", fake_add_voice):
    v = run_voice("grandad")
    staged = v["result"]["staged"]
    check("a checked clip waits in staging, not in voices/",
          v["status"] == "done" and not os.path.exists(os.path.join(vdir, "grandad.wav")))
    check("its warnings come back for the page", v["result"]["warnings"] == ["There is noise or music in the pauses."])
    check("the preview needs the key", c.get(f"/api/voice/preview/{staged}").status_code == 403)
    pre = c.get(f"/api/voice/preview/{staged}", headers=H)
    check("with it, the trimmed clip plays", pre.status_code == 200 and pre.mimetype == "audio/wav")
    check("the page may play it from a blob", "media-src 'self' blob:" in pre.headers["Content-Security-Policy"])
    r = c.post("/api/voice/keep", headers=H, json={"staged": staged}).get_json()
    check("Keep puts the voice in voices/", r["ok"] and os.path.exists(os.path.join(vdir, "grandad.wav"))
          and os.path.exists(os.path.join(vdir, "grandad.json")))
    check("and the staging copy is gone", c.get(f"/api/voice/preview/{staged}", headers=H).status_code == 404)
    r = c.post("/api/voice", headers=H, data={"url": "https://youtu.be/x", "text": "hi", "name": "grandad",
                                              "consent": "self"}).get_json()
    check("a name already taken is refused before any work", not r.get("job") and "already exists" in r["error"])
    v = run_voice("other")
    staged = v["result"]["staged"]
    c.post("/api/voice/discard", headers=H, json={"staged": staged})
    check("Try again leaves nothing behind", not os.path.exists(os.path.join(vdir, "other.wav"))
          and c.get(f"/api/voice/preview/{staged}", headers=H).status_code == 404)

for p in patches:
    p.stop()
shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
