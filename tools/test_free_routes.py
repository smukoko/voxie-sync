"""Every paid route has a free one that works (the user's rule, 2026-10-02).

- The junk filter without any API key: the local classifier still decides.
  Before this, a missing GROQ_API_KEY made the Groq client refuse to construct,
  and it was built together with the local models, so the free route was dead.
- Narration on your own GPU (--tts-url): no RunPod account is asked for.

Offline: the deep-learning stack and Docker are faked.
"""
import asyncio, os, sys, types

import numpy as np
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


# --- 1. the junk filter with no key --------------------------------------------
class FakeGroq:
    def __init__(self, api_key=None, **kw):
        if not api_key:   # what the real SDK does
            raise RuntimeError("The api_key client option must be set")


class FakeClf:
    p = 0.5
    def load_model(self, path): pass
    def predict_proba(self, X): return np.array([[1 - FakeClf.p, FakeClf.p]])


fakes = {
    "groq": types.SimpleNamespace(Groq=FakeGroq),
    "xgboost": types.SimpleNamespace(XGBClassifier=FakeClf),
    "joblib": types.SimpleNamespace(load=lambda p: types.SimpleNamespace(transform=lambda a: a)),
    "sentence_transformers": types.SimpleNamespace(
        SentenceTransformer=lambda name, **kw: types.SimpleNamespace(encode=lambda t, **k: [[0.0] * 4])),
}
with mock.patch.dict(sys.modules, fakes), mock.patch.dict(os.environ, {"GROQ_API_KEY": ""}):
    import core.ai_brain as brain
    brain.GROQ_API_KEY = None
    brain._BRAIN = None
    try:
        FakeClf.p = 0.5
        grey = asyncio.run(brain.evaluate_text_block("x " * 300, 300, 0.5, "grey"))
        FakeClf.p = 0.99
        junk = asyncio.run(brain.evaluate_text_block("x " * 300, 300, 0.99, "junk"))
        FakeClf.p = 0.01
        keep = asyncio.run(brain.evaluate_text_block("x " * 300, 300, 0.2, "keep"))
        ok, err = True, ""
    except Exception as e:
        ok, err = False, repr(e)
    check("the junk filter runs with no API key at all", ok, err)
    if ok:
        check("the local classifier still removes clear junk", junk == "JUNK", f"({junk})")
        check("and keeps clear text", keep == "KEEP", f"({keep})")
        check("its grey zone is kept, never guessed", grey == "KEEP", f"({grey})")
        check("the whole-book pass is skipped, not failed",
              asyncio.run(brain.classify_document_map([{"index": 0, "title": "t", "words": 1,
                                                        "position": 0.0, "snippet": "s"}])) == {})

# --- 2. narration on your own GPU needs no RunPod account ------------------------
os.environ.setdefault("TTS_PROVIDER", "cosyvoice")
import librerian_engine_v3 as engine  # noqa: E402
engine.RUNPOD_API_KEY = engine.RUNPOD_TEMPLATE_ID = engine.RUNPOD_URL_PATTERN = None
engine.TTS_API_TOKEN = "t"
docker_ok = mock.Mock(returncode=0)
with mock.patch.object(engine.shutil, "which", return_value="/usr/bin/docker"), \
     mock.patch.object(engine.subprocess, "run", return_value=docker_ok):
    try:
        engine.check_prerequisites("/tmp", own_server=True)
        own = True
    except SystemExit:
        own = False
    try:
        engine.check_prerequisites("/tmp", own_server=False)
        rented = True
    except SystemExit:
        rented = False
check("--tts-url starts with no RunPod account", own)
check("without it, a missing RunPod account is still refused", not rented)

# --- 3. narration on your own CPU (Kokoro, server/cpu_tts.py) ---------------------
import io, json, re, threading, urllib.request, urllib.error, wave  # noqa: E402
from core.tts_providers import KokoroProvider, get_provider  # noqa: E402

with mock.patch.dict(os.environ, {"TTS_PROVIDER": "kokoro", "TTS_API_TOKEN": "",
                                  "KOKORO_VOICE": ""}):
    engine.TTS_API_TOKEN = ""
    check("TTS_PROVIDER=kokoro is the CPU route", engine.cpu_route())
    with mock.patch.object(engine.shutil, "which", return_value="/usr/bin/docker"), \
         mock.patch.object(engine.subprocess, "run", return_value=docker_ok):
        try:
            engine.check_prerequisites("/tmp", own_server=engine.cpu_route())
            cpu_ok = True
        except SystemExit:
            cpu_ok = False
    check("it starts with no RunPod account and no TTS token", cpu_ok)
    kokoro = get_provider(language="en")
    check("the free route reads in Kokoro's English voice", isinstance(kokoro, KokoroProvider)
          and kokoro.voice == "af_heart")
    endpoint, payload = kokoro.build_request("Hello.")
    check("the request names the voice", endpoint == "/v1/audio/speech" and payload["voice"] == "af_heart")
engine.TTS_API_TOKEN = "t"

src = open(os.path.join(ROOT, "librerian_engine_v3.py"), encoding="utf-8").read()
main_src = src[src.index("async def main("):src.index('if __name__ == "__main__":')]
calls = re.findall(r"check_prerequisites\(([^)]*)\)", main_src)
check("every prerequisite check in main() knows a server needs no RunPod account",
      calls and all("own_server=" in c for c in calls), f"({calls})")
check("the engine starts its own CPU server when no --tts-url is given",
      "if cpu_route() and not tts_url:" in src and "start_cpu_server(tts_provider)" in src)
check("and stops it however the run ends", "stop_cpu_server(cpu_server)" in src
      and "atexit.register(stop_cpu_server, proc)" in src and '"--parent-pid"' in src)

# The server itself, with synthesis faked: health, audio, and the token.
sys.path.insert(0, os.path.join(ROOT, "server"))
import cpu_tts  # noqa: E402


def fake_wav(text, voice, speed, device):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(cpu_tts.SAMPLE_RATE)
        w.writeframes(b"\x00\x01" * cpu_tts.SAMPLE_RATE)
    return buf.getvalue()


def serve(token):
    srv = cpu_tts.ThreadingHTTPServer(("127.0.0.1", 0), cpu_tts.make_handler("cpu", token))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def call(url, body=None, token=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


with mock.patch.object(cpu_tts, "synthesize", fake_wav):
    srv, url = serve("")
    check("the CPU server answers the provider's health path",
          call(url + KokoroProvider.HEALTH_PATH)[0] == 200)
    status, wav = call(url + "/v1/audio/speech", {"input": "Hello.", "voice": "af_heart"})
    check("and returns audio the provider accepts",
          status == 200 and KokoroProvider(language="en").is_valid_response(wav))
    check("an empty chunk is a 400, not silence",
          call(url + "/v1/audio/speech", {"input": " "})[0] == 400)
    srv.shutdown()
    srv, url = serve("secret")
    check("with a token, /health still answers: the engine polls it before it authenticates",
          call(url + "/health")[0] == 200)
    check("with TTS_API_TOKEN set, no token is a 401",
          call(url + "/v1/audio/speech", {"input": "Hello."})[0] == 401)
    check("and the right token is served",
          call(url + "/v1/audio/speech", {"input": "Hello."}, token="secret")[0] == 200)
    srv.shutdown()

# --- 4. a copied .env.example is the free route ------------------------------------
from dotenv import dotenv_values  # noqa: E402
example = dotenv_values(os.path.join(ROOT, ".env.example"))
check(".env.example narrates on the CPU", example.get("TTS_PROVIDER") == "kokoro",
      f"({example.get('TTS_PROVIDER')})")
# JUNK_PROVIDER and JUNK_BLOCK_FILTER are "auto": the local classifier and no block
# filter without an OpenRouter key (the free route), JEV and the block filter with one.
from core import block_filter, junk_provider  # noqa: E402
check(".env.example leaves the junk filter to auto", example.get("JUNK_PROVIDER") == "auto"
      and example.get("JUNK_BLOCK_FILTER") == "auto")
no_key = {"JUNK_PROVIDER": "auto"}
check("with no key, auto is the local junk filter and no block filter",
      junk_provider.resolved_choice(no_key) == "ai_brain"
      and not junk_provider.has_openrouter_key(no_key))
saved = {k: os.environ.pop(k, None) for k in ("OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY", "JUNK_BLOCK_FILTER")}
check("...nor a block filter", block_filter.mode() == "off")
os.environ["OPENROUTER_API_KEY"] = "k"
check("with a key, auto is JEV and the block filter is on",
      junk_provider.resolved_choice({"OPENROUTER_API_KEY": "k"}) == "jev" and block_filter.mode() == "on")
check("either spelling of the key counts", junk_provider.resolved_choice({"OPEN_ROUTER_API_KEY": "k"}) == "jev")
check("an explicit choice is never overridden",
      junk_provider.resolved_choice({"JUNK_PROVIDER": "ai_brain", "OPENROUTER_API_KEY": "k"}) == "ai_brain")
os.environ["JUNK_BLOCK_FILTER"] = "off"
check("and neither is an explicit block filter", block_filter.mode() == "off")
for k, v in saved.items():
    os.environ.pop(k, None)
    if v is not None:
        os.environ[k] = v
keys = ("GROQ_API_KEY", "OPENROUTER_API_KEY", "RUNPOD_API_KEY", "RUNPOD_TEMPLATE_ID",
        "RUNPOD_URL_PATTERN", "TYPESAFE_API_KEY", "TTS_API_TOKEN")
filled = [k for k in keys if (example.get(k) or "").strip()]
check("and no placeholder key a paid service would be called with", not filled, f"({filled})")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
