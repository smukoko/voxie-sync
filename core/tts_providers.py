"""
TTS provider abstraction.

The engine talks to whatever GPU pod RunPod hands it, but the request
shape (endpoint, payload, voice options) is specific to whichever TTS
model is running on that pod. To swap models without touching
tts_worker.py or the engine, add a new TTSProvider subclass below,
register it in PROVIDERS, and set TTS_PROVIDER in .env.
"""
import os

from core.languages import DEFAULT_LANGUAGE, KOKORO_VOICE
from core.voices import choose as choose_voice


class TTSProvider:
    """Base interface every TTS provider must implement."""

    # Longest audio this provider will ever return for one request, in seconds,
    # or None when it has no known ceiling. A model that stops at a fixed length
    # does not say so -- it returns a well-formed file containing only part of
    # the text -- so the ceiling has to be declared here to be detectable at all.
    MAX_AUDIO_SECONDS = None

    # Where the engine asks "are you serving yet?". Every provider brings its own
    # server, so this cannot be assumed to be vLLM's /v1/models.
    HEALTH_PATH = "/v1/models"

    # Whether the aligner needs the chunk lifted before it can see it. True for a
    # model that returns near-inaudible audio; False for one that already lands
    # at a sane level, where the boost would only add noise and a second copy of
    # every chunk on disk.
    NEEDS_MFA_BOOST = True

    # How long to wait on one synthesis request. Providers behind a proxy that
    # kills long responses must set this below that ceiling.
    REQUEST_TIMEOUT_SECONDS = 900

    # Upper bound on concurrent workers, or None to let the GPU decide. A server
    # that serialises inference behind a lock gains nothing from a swarm, and
    # thirty workers against it just means thirty queued requests.
    MAX_WORKERS = None
    # Whether core.director may direct this provider (an instruction, a speed,
    # a vocal tag per chunk). Only a model that follows instructions: Kokoro
    # has no such input.
    SUPPORTS_DIRECTION = False

    def build_request(self, text: str) -> tuple[str, dict]:
        """Return (endpoint_path, json_payload) for a synthesis request."""
        raise NotImplementedError

    def cache_identity(self) -> str:
        """Everything about this provider that changes the audio it returns.

        core.chunk_cache keys a chunk on this plus the text, so a value that
        omits a parameter means the cache serves the old audio after that
        parameter changes -- silently, and for every book afterwards. That is
        the only way the cache can be wrong, which is why this is each
        provider's own responsibility rather than something inferred from
        attributes.

        Not implemented here on purpose: a default that returned "" would be
        the wrong answer for every provider while looking like it worked.
        """
        raise NotImplementedError

    # Sent on every request when set. The proxy transport leaves the server
    # reachable only through an obscure RunPod URL; the TCP transport puts it on
    # a raw public ip:port with nothing in front of it, so the token is what
    # stops a portscan from finding a free GPU. Empty by default, which is the
    # proxy transport's existing behaviour -- the engine refuses to start on tcp
    # without one.
    @property
    def auth_headers(self) -> dict:
        token = os.getenv("TTS_API_TOKEN", "").strip()
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def synthesize(self, client, base_url: str, text: str, timeout: float,
                         run_id: str = None, chunk_name: str = None):
        """Perform one synthesis request and return the httpx response.

        The default is a JSON POST, which is what an OpenAI-shaped endpoint
        wants. A provider whose API takes multipart (a reference clip, say)
        overrides this rather than contorting build_request into carrying files.

        run_id/chunk_name name this chunk so a server that can align its own
        output knows what to file it under. Accepted and ignored here: a
        provider whose server cannot align simply never uses them, and the
        worker does not have to know which is which.
        """
        endpoint, payload = self.build_request(text)
        return await client.post(f"{base_url}{endpoint}", json=payload,
                                 headers=self.auth_headers, timeout=timeout)

    def is_valid_response(self, raw_bytes: bytes) -> bool:
        """Return False to reject a hallucinated/empty response and trigger a retry."""
        raise NotImplementedError

    def is_truncated(self, seconds: float) -> bool:
        """True when audio of this length is the provider's ceiling, not an ending.

        Deliberately a hair under the ceiling rather than an equality test: the
        duration is measured back from a written file, so it need not land on
        the exact figure. A chunk that genuinely ends within 50ms of the ceiling
        would be a false positive, and the cost of one is a printed warning.
        """
        if self.MAX_AUDIO_SECONDS is None:
            return False
        return seconds >= self.MAX_AUDIO_SECONDS - 0.05


class CosyVoiceProvider(TTSProvider):
    """Fun-CosyVoice3-0.5B behind the custom /clone API (server/custom_api.py).

    Zero-shot cloning: every chunk is conditioned on the same reference clip and
    its exact transcript, which is what keeps one narrator sounding like one
    narrator. Voxtral cannot do this at all -- its open checkpoint ships without
    the encoder weights, so `voice` is only a preset label and each chunk
    re-rolls its delivery.

    Measured against a live pod on 2026-09-13, all of it different from Voxtral:

      no truncation ceiling   1,553 chars -> 104.8s at a steady 14.8 chars/sec,
                              where Voxtral silently stopped at 163.84s
      -18.8 to -19.8 LUFS     already at listening level, so no MFA boost
      0.5 dB spread           across takes of identical text (Voxtral: 9.0 dB
                              across one chapter, which alone reads as a
                              different speaker)
      2.26x realtime, serial  the server runs uvicorn with a single worker and a
                              lock around inference, so four concurrent requests
                              measured 1.10x -- a swarm buys nothing
    """

    HEALTH_PATH = "/health"
    SUPPORTS_DIRECTION = True
    MAX_AUDIO_SECONDS = None       # measured: none, unlike Voxtral's 163.84s
    NEEDS_MFA_BOOST = False        # -19 LUFS is already audible to the aligner

    # Two, not thirty, and measured rather than assumed. Concurrency scaling
    # against a fresh pod on 2026-09-14, ~990-char chunks, throughput as a
    # multiple of realtime:
    #
    #   workers   1     2     3     4     6     8
    #   thruput  2.72  3.11  3.14  3.16  3.19  2.99
    #   per-req  23.4s 30.6s 41.6s 49.9s 70.8s 84.4s
    #
    # Throughput plateaus immediately and then degrades, while per-request
    # latency grows linearly -- the signature of requests queueing behind a
    # lock, which is exactly what the server does (uvicorn --workers 1 with a
    # threading.Lock around inference). Going 2 -> 6 buys 2.6%; 8 is worse
    # than 2.
    #
    # So the ~15% the second worker earns is overlapping upload and decode with
    # inference, and there is nothing left to overlap after that. More client
    # workers cannot fix a server-side bottleneck; running several model
    # instances behind the API is the only lever that would.
    # Two, and this is now measured three ways rather than reasoned about.
    #
    # Throughput, same pipeline, real books:
    #     2 workers  3.71x realtime   (Masque of the Red Death)
    #     4 workers  1.95x realtime   (Usher, 36 chunks)
    #     8 workers  did not finish   (163 HTTP 524s, run stalled at 8 of 36)
    #
    # More workers are not merely useless here, they are actively slower. The
    # server serialises inference behind a lock, so added concurrency buys no
    # throughput and instead inflates per-request latency until requests start
    # dying against RunPod's proxy, which sits behind Cloudflare and kills any
    # response over ~100s. At 4 workers 24 requests each burned ~95s of GPU and
    # were thrown away; at 8 nothing completed at all.
    #
    # The intuition that more workers should help is a good one and it is wrong
    # for this architecture. Raising it again is only worth doing if the server
    # stops serialising -- several model instances behind the API, not more
    # callers in front of it.
    #
    # 2026-09-18: the server stopped serialising. With API_WORKERS=8 on the pod,
    # the same 12 chunks through the same harness measured
    #
    #     clients        1       2       4       8
    #     API_WORKERS=1  2.43x   2.46x   2.46x    --
    #     API_WORKERS=8  3.33x   3.78x   3.79x   4.00x
    #
    # so 4 callers is +54% over the old pairing. Note the API_WORKERS=1 row is
    # flat across concurrency -- that is the lock, measured, and it is why 2 was
    # right until now.
    #
    # Four rather than eight: 8 clients measured 4.00x against 3.79x, a 5% gain
    # for p50 latency of 215s against 101s. A fatter tail is not worth 5% when a
    # slow chunk already has to clear a timeout.
    #
    # Env-overridable because the right number tracks the server's API_WORKERS,
    # which lives in the RunPod template rather than in this repo.
    MAX_WORKERS = int(os.getenv("COSY_MAX_WORKERS", "4"))

    # Below the proxy's ~100s ceiling on purpose. A request that will be killed
    # at 100s should fail fast and free its worker, not hold it for the 900s
    # the old value allowed -- which is how eight stuck workers produced a run
    # that made no progress at all.
    #
    # That ceiling is Cloudflare's, not the model's, so it only applies to the
    # proxy transport. On RUNPOD_TRANSPORT=tcp there is no proxy and nothing
    # kills a long response, and ducking under a limit that no longer exists
    # would keep throwing away chunks that were merely slow -- which is exactly
    # what cost The Screwtape Letters 042_B_IX and 043_C_IX, both of them
    # generating fine and killed twelve times each at 95 seconds.
    PROXY_REQUEST_TIMEOUT_SECONDS = 95
    TCP_REQUEST_TIMEOUT_SECONDS = 600

    @property
    def REQUEST_TIMEOUT_SECONDS(self):
        if os.getenv("RUNPOD_TRANSPORT", "proxy").strip().lower() == "tcp":
            return self.TCP_REQUEST_TIMEOUT_SECONDS
        return self.PROXY_REQUEST_TIMEOUT_SECONDS

    def __init__(self, language=None, voice_name=None):
        self.language = language
        # A named voice from voices/ is the normal path. COSY_REF_WAV/_TEXT stay
        # as a direct override so a one-off clip needs no registry entry.
        override_wav = os.getenv("COSY_REF_WAV")
        override_text = (os.getenv("COSY_REF_TEXT") or "").strip()
        if override_wav and override_text:
            self.ref_wav, self.ref_text = override_wav, override_text
            self.voice = f"clone:{os.path.basename(override_wav)}"
        else:
            chosen = choose_voice(voice_name, book_language=language)
            self.ref_wav, self.ref_text = chosen.wav, chosen.text
            self.voice = f"{chosen.name} ({chosen.label})"

        if not os.path.exists(self.ref_wav):
            raise FileNotFoundError(
                f"Reference clip not found: {self.ref_wav}. Zero-shot cloning needs "
                "one (3-30s, ideally 8-15s, 16kHz mono, clean).")
        if not self.ref_text:
            raise ValueError(
                f"No transcript for {self.ref_wav}. It must be the *exact* words "
                "spoken in the clip -- a wrong transcript degrades the clone "
                "quietly rather than failing.")

        # Registration state. The id is derived from the clip's content, so a
        # changed clip registers as a new speaker instead of silently reusing
        # the old embedding under the same name.
        import hashlib
        digest = hashlib.sha256(open(self.ref_wav, "rb").read()).hexdigest()[:12]
        base = os.path.splitext(os.path.basename(self.ref_wav))[0]
        self.speaker_id = f"{base}_{digest}"[-64:]
        # Keyed by server, because one provider serves a pool of pods and each
        # of them holds its own speaker table.
        self._registered_on = set()
        self._registration_tried = set()
        # base_url -> whether that server takes direction (its /health says so).
        self._directs = {}
        self._warned_no_direction = False

    def build_request(self, text: str) -> tuple[str, dict]:
        # Kept for interface completeness; /clone is multipart, so synthesize
        # below is the path actually taken.
        return "/clone", {"text": text, "prompt_text": self.ref_text}

    # Which server produced the audio, when that is not the default one.
    #
    # BATCH_SERVER_PLAN F4: the cache keys on provider identity plus text, and a
    # vLLM engine is a different sampler from eager PyTorch -- same model, same
    # clip, audio that need not be identical. Without this, half a re-run would
    # come from the cache in the old voice and half from the new server, which
    # is two narrators inside one book: exactly the failure the cache docstring
    # calls the only way it can be wrong.
    #
    # Keyed on the template id because that is what selects the image, and the
    # image is what decides the server. It is deliberately over-sensitive: a
    # template change for an unrelated reason costs a re-generation, while a
    # missed one costs a book nobody can hear the seam in until they listen.
    SERVER_TAG = os.environ.get("RUNPOD_TEMPLATE_ID", "")

    def cache_identity(self) -> str:
        # speaker_id already ends in a hash of the reference clip's bytes, so a
        # re-cut or re-recorded clip changes the identity and correctly misses
        # the cache. The transcript is included because it conditions the clone
        # independently of the audio -- fixing a typo in it changes the output
        # while the clip, and therefore the speaker_id, stays the same.
        return (f"cosyvoice3|srv={self.SERVER_TAG}|spk={self.speaker_id}"
                f"|prompt={self.ref_text.strip()}")

    async def _ensure_registered(self, client, base_url: str):
        """Register the clip once PER SERVER, if that server supports it.

        Per server, not per provider. One provider instance is shared by every
        worker in the run and the workers are spread across the pool, so a
        single `self._registered` flag meant pod 1 registered, set it, and every
        other pod was then sent a speaker_id it had never heard of. Each of
        those chunks paid a 404 round trip, and the recovery path cleared the
        flag for the whole run -- so the pods knocked each other's registration
        state down in turn. It self-healed, which is why it never showed up as a
        failure; it just spent a request per chunk doing it, and the cost grows
        with the size of the pool.

        Older servers have no /voices; they get the clip on every request, which
        is what the code did before and still works. Registration failing is
        never fatal -- it costs the per-request upload and embedding extraction
        back, not the book.
        """
        if base_url in self._registered_on or base_url in self._registration_tried:
            return
        # Marked before the await, so the other workers on this same pod do not
        # all fire their own registration while the first is in flight.
        self._registration_tried.add(base_url)
        try:
            with open(self.ref_wav, "rb") as f:
                files = {"audio_file": (os.path.basename(self.ref_wav), f.read(), "audio/wav")}
            r = await client.post(
                f"{base_url}/voices",
                data={"speaker_id": self.speaker_id, "prompt_text": self.ref_text},
                files=files, headers=self.auth_headers, timeout=120.0)
            if r.status_code == 200:
                self._registered_on.add(base_url)
                print(f"   🎙️  Registered '{self.speaker_id}' on {base_url}; "
                      "the clip is no longer sent with every chunk.")
            elif r.status_code == 404:
                print("   ℹ️  Server has no /voices endpoint; sending the clip per "
                      "request (see server/custom_api.py for the patch).")
        except Exception as e:
            # type(e) as well as the message: httpx raises several of these with
            # an empty str(), and "Could not register the speaker ()" says
            # nothing at all about which of them happened.
            print(f"   ⚠️ Could not register the speaker on {base_url} "
                  f"({type(e).__name__}: {str(e)[:60]}); falling back to "
                  "per-request upload.")

    async def _server_directs(self, client, base_url: str) -> bool:
        """True when this server advertises "direction" on /health. Asked once.

        Not assumed: an image older than the director would take "[breath]" as
        a word in the transcript it keeps for pod-side alignment.
        """
        if base_url not in self._directs:
            try:
                r = await client.get(f"{base_url}/health", timeout=30.0)
                self._directs[base_url] = (r.status_code == 200 and
                                           "direction" in (r.json().get("features") or []))
            except Exception:
                self._directs[base_url] = False
            if not self._directs[base_url] and not self._warned_no_direction:
                self._warned_no_direction = True
                print(f"   ℹ️  {base_url} predates the narration director; its chunks are "
                      "narrated plain (build the image from this repository to direct them).")
        return self._directs[base_url]

    async def synthesize(self, client, base_url: str, text: str, timeout: float,
                         run_id: str = None, chunk_name: str = None, direction=None):
        await self._ensure_registered(client, base_url)
        # Sent when the caller supplies them. A server built before pod-side
        # alignment ignores unknown form fields, so this stays compatible with
        # an older image rather than needing a version check.
        tag = {}
        if run_id and chunk_name:
            tag = {"run_id": run_id, "chunk_name": chunk_name}
        if direction is not None and not direction.plain and await self._server_directs(client, base_url):
            # The tag is a sound the model makes before the paragraph; the
            # server keeps it out of the alignment transcript.
            text = direction.tag + text
            tag["speed"] = str(direction.speed)
            if direction.instruct_text:
                # Instruction mode needs the clip uploaded: with a registered
                # speaker the instruction is silently replaced by the stored
                # transcript (see server/custom_api.py, /clone).
                with open(self.ref_wav, "rb") as f:
                    files = {"audio_file": (os.path.basename(self.ref_wav), f.read(), "audio/wav")}
                return await client.post(
                    f"{base_url}/clone", files=files, headers=self.auth_headers, timeout=timeout,
                    data={"text": text, "instruct_text": direction.instruct_text, **tag})
        if base_url in self._registered_on:
            # Nothing uploaded: the server already holds this speaker's
            # embedding, so each chunk is text plus a name.
            r = await client.post(
                f"{base_url}/clone",
                data={"text": text, "speaker_id": self.speaker_id, **tag},
                headers=self.auth_headers, timeout=timeout)
            if r.status_code != 404:
                return r
            # A 404 is this server saying it does not know this speaker. With
            # several server workers that can happen to any of them, so
            # re-register THIS pod and fall through to the upload path for this
            # chunk rather than burning a retry. Only this pod: clearing the
            # state for the whole pool would make one server's miss cost every
            # other server its registration too.
            self._registered_on.discard(base_url)
            self._registration_tried.discard(base_url)
            await self._ensure_registered(client, base_url)

        with open(self.ref_wav, "rb") as f:
            files = {"audio_file": (os.path.basename(self.ref_wav), f.read(), "audio/wav")}
        data = {"text": text, "prompt_text": self.ref_text, **tag}
        return await client.post(f"{base_url}/clone", data=data, files=files,
                                 headers=self.auth_headers, timeout=timeout)

    def is_valid_response(self, raw_bytes: bytes) -> bool:
        # A real chunk is hundreds of KB of 24kHz mono; anything this small is an
        # error page or a silent stub.
        return len(raw_bytes) >= 15000 and raw_bytes[:4] == b"RIFF"


class KokoroProvider(TTSProvider):
    """Kokoro-82M on your own CPU (server/cpu_tts.py): the free narration route.

    Preset voices only; it cannot clone. The engine starts the server itself
    on 127.0.0.1 unless --tts-url points somewhere else.
    """

    MODEL_NAME = "hexgrad/Kokoro-82M"
    HEALTH_PATH = "/health"
    # -25.7 LUFS measured: quieter than CosyVoice but well inside what MFA
    # hears (it failed at -34 to -46). The chapter loudnorm does the rest.
    NEEDS_MFA_BOOST = False
    # The server synthesises one chunk at a time and torch already uses every
    # core, so a second caller only queues.
    MAX_WORKERS = 1
    # A 2,400-character chunk is ~40 s at 3.7x realtime; generous for slow machines.
    REQUEST_TIMEOUT_SECONDS = 1800

    def __init__(self, language=None):
        self.language = language or DEFAULT_LANGUAGE
        self.voice = os.getenv("KOKORO_VOICE") or KOKORO_VOICE
        self.speed = float(os.getenv("KOKORO_SPEED", "1.0"))

    def build_request(self, text):
        return "/v1/audio/speech", {"input": text, "voice": self.voice, "speed": self.speed}

    def cache_identity(self):
        # The words taught to Kokoro change its audio (core/kokoro_words).
        from core import kokoro_words
        return f"{self.MODEL_NAME}|voice={self.voice}|speed={self.speed}|words={kokoro_words.fingerprint()}"

    def is_valid_response(self, raw_bytes):
        # A RIFF header and more than a tenth of a second of 24 kHz PCM16.
        return raw_bytes[:4] == b"RIFF" and len(raw_bytes) > 44 + 4800


PROVIDERS = {
    "cosyvoice": CosyVoiceProvider,
    "kokoro": KokoroProvider,
}


def get_provider(name: str = None, language: str = None, voice: str = None) -> TTSProvider:
    """Instantiate the configured TTS provider (default: TTS_PROVIDER, else kokoro, the free route).

    `voice` names an entry in voices/ and is only meaningful to providers that clone.
    """
    name = (name or os.getenv("TTS_PROVIDER", "kokoro")).lower()
    if name not in PROVIDERS:
        raise ValueError(f"Unknown TTS_PROVIDER '{name}'. Available: {', '.join(PROVIDERS)}")
    cls = PROVIDERS[name]
    if "voice_name" in cls.__init__.__code__.co_varnames:
        return cls(language=language, voice_name=voice)
    return cls(language=language)
