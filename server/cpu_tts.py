"""Narration on your own CPU: Kokoro-82M behind the same HTTP shape as the GPU server.

The free route (CLAUDE.md: every paid option has a free one). No GPU, no
RunPod, no API key: the engine starts this on 127.0.0.1 when
TTS_PROVIDER=kokoro, sends it one chunk at a time, and stops it afterwards.
Alignment is unchanged: the engine's local MFA reads the WAVs as it always has.

Kokoro (hexgrad/Kokoro-82M, Apache-2.0) has preset voices only. It cannot
clone, so a voice added with `voxie voice-add` is not used on this route.

Measured on an M1 (2026-10-02): 3.7x realtime on the CPU, 4.5x with
KOKORO_DEVICE=mps. A one-hour book is about 16 minutes.

    python server/cpu_tts.py --port 8765

    GET  /health                -> 200 once the model is loaded
    POST /v1/audio/speech       {"input": text, "voice": "af_heart", "speed": 1.0}
                                -> audio/wav, 24 kHz mono PCM16

TTS_API_TOKEN, when set, is required as a Bearer token on every request but
/health (as in server/custom_api.py); the engine binds this to 127.0.0.1
only, where nothing else can reach it.
"""
import argparse
import io
import json
import os
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The repo root, for core.kokoro_words: this file is run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import kokoro_words  # noqa: E402

SAMPLE_RATE = 24000
REPO = "hexgrad/Kokoro-82M"

_pipelines = {}
_lock = threading.Lock()   # one synthesis at a time: torch already uses every core


def pipeline(lang_code, device):
    """One KPipeline per language, loaded on first use (the weights are shared)."""
    if lang_code not in _pipelines:
        from kokoro import KPipeline
        _pipelines[lang_code] = KPipeline(lang_code=lang_code, repo_id=REPO, device=device)
        if lang_code in ("a", "b"):
            kokoro_words.teach(_pipelines[lang_code].g2p)
    return _pipelines[lang_code]


def synthesize(text, voice, speed, device):
    """PCM16 WAV bytes for `text`. Kokoro splits long text itself, segment by segment."""
    import numpy as np
    # Kokoro's own convention: a voice's first letter is its language.
    pipe = pipeline(voice[0], device)
    parts = []
    for result in pipe(text, voice=voice, speed=speed):
        audio = result.audio
        if audio is None:
            continue
        parts.append(audio.numpy() if hasattr(audio, "numpy") else np.asarray(audio))
    if not parts:
        return b""
    samples = np.concatenate(parts)
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def make_handler(device, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass                      # the engine prints its own line per chunk

        def _authorised(self):
            if not token:
                return True
            if self.headers.get("Authorization", "") == f"Bearer {token}":
                return True
            self._send(401, b'{"error": "unauthorised"}', "application/json")
            return False

        def _send(self, status, body, ctype):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            # /health stays open, as in server/custom_api.py: it is what the
            # engine polls to learn the server is up, before it sends a token.
            if self.path == "/health":
                self._send(200, b'{"status": "ok", "model": "kokoro-82m"}', "application/json")
                return
            if not self._authorised():
                return
            if self.path == "/v1/models":
                self._send(200, b'{"status": "ok", "model": "kokoro-82m"}', "application/json")
            else:
                self._send(404, b'{"error": "not found"}', "application/json")

        def do_POST(self):
            if not self._authorised():
                return
            if self.path != "/v1/audio/speech":
                self._send(404, b'{"error": "not found"}', "application/json")
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                text = (body.get("input") or "").strip()
                voice = body.get("voice") or "af_heart"
                speed = float(body.get("speed") or 1.0)
            except (ValueError, TypeError):
                self._send(400, b'{"error": "bad request"}', "application/json")
                return
            if not text:
                self._send(400, b'{"error": "no input"}', "application/json")
                return
            try:
                with _lock:
                    wav = synthesize(text, voice, speed, device)
            except Exception as e:     # a failed chunk is retried by the engine
                self._send(500, json.dumps({"error": str(e)[:300]}).encode(), "application/json")
                return
            self._send(200, wav, "audio/wav")

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--device", default=os.getenv("KOKORO_DEVICE", "cpu"),
                    help="cpu (default) or mps on Apple Silicon (~20%% faster)")
    ap.add_argument("--preload", default="a",
                    help="language code to load before serving (a = American English)")
    ap.add_argument("--parent-pid", type=int, default=None,
                    help="exit when this process is gone (the engine passes its own pid)")
    a = ap.parse_args()
    if a.parent_pid:
        # The engine stops this in its finally, but `kill -9` skips that. Not
        # billing, unlike a pod, yet a CPU-bound orphan is still a hot laptop.
        def watch():
            import time
            while True:
                time.sleep(5)
                try:
                    os.kill(a.parent_pid, 0)
                except OSError:
                    os._exit(0)
        threading.Thread(target=watch, daemon=True).start()
    # Before torch is imported: a few of Kokoro's ops have no MPS kernel.
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    # Loaded before the port opens, so /health answering means ready to narrate.
    pipeline(a.preload, a.device)
    server = ThreadingHTTPServer((a.host, a.port), make_handler(a.device, os.getenv("TTS_API_TOKEN", "").strip()))
    print(f"Kokoro narrating on {a.device}, http://{a.host}:{a.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
