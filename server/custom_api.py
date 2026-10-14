"""CosyVoice 3 zero-shot cloning API, with registered speakers.

This is the FastAPI app the RunPod template starts. It lives here as well as in
the gist because it is part of this system: the /clone contract, the ~19s
behaviour, the sample rate and the speaker cache are all things the engine
depends on, and a gist is not somewhere you can diff or blame.

WHAT CHANGED FROM THE ORIGINAL

Every request used to carry the reference clip: upload it, write it to a temp
file, extract the speaker embedding, synthesise, delete. The embedding work is
identical on every chunk of a book -- the same clip, the same transcript, tens
or hundreds of times -- and CosyVoice exposes add_zero_shot_spk precisely so it
can be done once.

So /clone now takes EITHER:

    speaker_id=<name>            registered earlier, nothing uploaded
    audio_file + prompt_text     the original path, still works unchanged

The second form is kept so an older client keeps working and a one-off clip
needs no registration.

WHY REGISTRATION IS DISK-BACKED

add_zero_shot_spk stores the speaker in the model object's memory. That is fine
with one uvicorn worker and wrong with several, because each worker is a
separate process with its own model -- a voice registered through worker A
would be missing from worker B, and requests would fail depending on which one
answered.

So a registration writes the clip and its transcript to VOICES_DIR, and every
worker loads whatever is there at startup.

That is necessary and not sufficient, which cost a book to learn. A
registration POST is served by ONE worker; the others finished importing before
it arrived and never look again, so they answer /clone with 404 for a speaker
that plainly exists. Measured with four workers: 163 404s and 13 chunks
quarantined -- dropped from the book outright -- while /health reported the
speaker present, because the probe happened to hit the worker that registered
it. So /clone also loads lazily from disk on a miss, which is what actually
makes any worker count safe.

CONCURRENCY: PROCESSES, THEN BATCHES

Two different answers, and the second replaces the first.

Without vLLM the model runs eagerly at batch 1, which is latency-bound rather
than throughput-bound -- so more requests do not make the GPU busier, they only
queue. The escape was more PROCESSES: each uvicorn worker holds its own copy of
the model and those do run in parallel. Measured: a 4090 at API_WORKERS=4 tied
an L40S at API_WORKERS=8 (3.60x against 3.59x), which is what contention rather
than saturation looks like from outside, and why raising it further bought
nothing. Eight workers also does not fit a 24GB card -- two separate 4090s died
the same way, a day apart.

With vLLM the answer inverts. One engine batches many requests, so the right
shape is API_WORKERS=1 and GEN_CONCURRENCY high: N processes competing for one
GPU is exactly what batching replaces. Upstream measures CosyVoice3 at 9.2x for
batch 1 and 20x at batch 16 on one card.

    uvicorn custom_api:app --host 0.0.0.0 --port $PORT --workers ${API_WORKERS:-1}

So: LOAD_VLLM=0 -> raise API_WORKERS, keep GEN_CONCURRENCY at 1.
    LOAD_VLLM=1 -> keep API_WORKERS at 1, raise GEN_CONCURRENCY.
"""
import os
import sys

# Matcha-TTS submodule must be importable before anything from cosyvoice
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(ROOT_DIR, "third_party", "Matcha-TTS"))

import io
import json
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time

import torch
import torchaudio
from contextlib import contextmanager
from fastapi import FastAPI, File, Form, Response, UploadFile

from cosyvoice.cli.cosyvoice import AutoModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cosyvoice-api")

MODEL_DIR = os.environ.get("COSYVOICE_MODEL_DIR", "pretrained_models/Fun-CosyVoice3-0.5B")
# Where registered speakers persist, so every worker sees the same set.
VOICES_DIR = os.environ.get("COSYVOICE_VOICES_DIR", "/workspace/registered_voices")
# CosyVoice 3's LLM expects this instruction prefix in front of the prompt text
V3_PREFIX = "You are a helpful assistant.<|endofprompt|>"

# The narration director's vocal tags (core/director.TAGS). They are sounds,
# not words: MFA must never be asked to find "[breath]" in the audio, so the
# transcript kept for /align drops them. A test reads this pattern from here.
_TAG_RE = re.compile(r"\[(?:breath|quick_breath|sigh|laughter)\]\s*")

# A speaker id becomes a filename, so it must not be able to escape VOICES_DIR.
SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

# Where a run's generated chunks are kept so this pod can align its own output.
#
# Alignment used to happen on the Mac, and that is where a sixteen-hour book
# went to die: Montreal Forced Aligner publishes an x86 image only, so on Apple
# Silicon it runs under Rosetta in a 4GB Docker VM at 14.7x realtime -- 65
# minutes for one book, against 25 for generating it. Parallelising it there
# makes it WORSE, measured: --single_speaker --num_jobs 6 took 1,822s against
# 118s for the same 24 chunks, a 15x slowdown from six emulated processes
# thrashing.
#
# The pod is the obvious place instead. It is native x86 with a dozen-plus idle
# cores, it already has the audio it just generated, and with several pods the
# alignment parallelises for free across them.
RUNS_DIR = os.environ.get("COSYVOICE_RUNS_DIR", "/workspace/runs")
MFA_MODELS_DIR = os.environ.get("MFA_MODELS_DIR", "/workspace/mfa_models")
# The conda env the image installs MFA into. Absent on older images, which is
# what makes /align answer 404 there and the engine fall back to local MFA.
MFA_BIN = os.environ.get("MFA_BIN", "/opt/mfa/bin/mfa")

app = FastAPI(title="CosyVoice 3 zero-shot cloning")

# Shared-secret auth, on when TTS_API_TOKEN is set in the pod's environment.
#
# Off by default, because behind RunPod's HTTP proxy the server is reachable
# only through an unguessable *-8000.proxy.runpod.net URL. On the TCP transport
# there is no proxy: the server sits on a raw public ip:port with no TLS and no
# network ACL, so without this anyone who portscans the host gets a free GPU
# that is billed by the hour. The engine refuses to start on tcp unless the
# token is set, so the two halves cannot get out of step.
#
# /health stays open. It is what the engine probes to decide whether a pod has
# finished booting, it reveals nothing but a sample rate and the loaded speaker
# names, and keeping it unauthenticated means a boot diagnosis never turns into
# a question about credentials.
_API_TOKEN = os.getenv("TTS_API_TOKEN", "").strip()


# The pod's dead man's switch (server/idle_watchdog.py), started once the
# model has loaded, at the end of this file.
_idle = None
_OPEN_PATHS = ("/health", "/docs", "/openapi.json")


@app.middleware("http")
async def require_token(request, call_next):
    if _API_TOKEN and request.url.path not in _OPEN_PATHS:
        supplied = request.headers.get("authorization", "")
        if supplied != f"Bearer {_API_TOKEN}":
            return Response(content="unauthorized", status_code=401,
                            media_type="text/plain")
    if _idle is None or request.url.path in _OPEN_PATHS:
        return await call_next(request)
    # Only the client counts as activity: a refused request has returned above.
    _idle.begin()
    try:
        return await call_next(request)
    finally:
        _idle.end()

# vLLM batches the LLM, which is 90.6% of a chunk's wall clock (measured with
# POST /profile on a 4090). Off by default: the cu121 image has no vllm
# installed at all, and every book in the library was narrated without it.
LOAD_VLLM = os.environ.get("LOAD_VLLM", "0") == "1"

# How many requests may be inside inference_zero_shot at once.
#
# This is the whole point of the vLLM path and it is worth being explicit about
# why. Batching happens inside ONE engine when several requests reach it
# together; a mutex around generation means the engine never sees two at once,
# so the batching is structurally unreachable and vLLM buys only its compiled
# decode. The gate has to open for the win to exist.
#
# It defaults to 1, which is exactly today's behaviour -- one request in flight,
# the configuration whose audio has been accepted by ear. Raising it is a
# measurement (tools/gpu_bench.py), not a default.
GEN_CONCURRENCY = max(1, int(os.environ.get("GEN_CONCURRENCY", "1")))

# The two numbers that decide whether batching actually happens.
#
# Upstream's load_vllm hardcodes gpu_memory_utilization=0.2 and sets no
# max_num_seqs at all. On a 48GB L40S that is 9.6GB for the engine, its weights
# and its KV cache -- and the KV cache is what limits how many sequences can be
# in flight, which IS the batch size. Upstream's own figures for this model run
# 9.2x at batch 1 to 20x at batch 16, and 9.2x does not clear our gate, so the
# difference between those two numbers is the whole project.
#
# Left unset, both fall through to upstream's defaults and nothing changes.
VLLM_GPU_UTIL = os.environ.get("VLLM_GPU_UTIL")
VLLM_MAX_SEQS = os.environ.get("VLLM_MAX_SEQS")

if LOAD_VLLM and (VLLM_GPU_UTIL or VLLM_MAX_SEQS):
    # Patched on the vllm module rather than by reimplementing load_vllm.
    # `load_vllm` does `from vllm import EngineArgs` INSIDE the function, so it
    # picks this up at call time -- which keeps upstream as the author of how
    # the engine is built, and leaves nothing of ours to drift when they change
    # it. Reimplementing those eight lines would be the version that rots.
    import vllm as _vllm

    _RealEngineArgs = _vllm.EngineArgs

    def _TunedEngineArgs(**kwargs):
        if VLLM_GPU_UTIL:
            kwargs["gpu_memory_utilization"] = float(VLLM_GPU_UTIL)
        if VLLM_MAX_SEQS:
            kwargs["max_num_seqs"] = int(VLLM_MAX_SEQS)
        logger.info("vLLM EngineArgs overridden: %s",
                    {k: kwargs[k] for k in ("gpu_memory_utilization", "max_num_seqs")
                     if k in kwargs})
        return _RealEngineArgs(**kwargs)

    _vllm.EngineArgs = _TunedEngineArgs

if LOAD_VLLM:
    # vLLM will not find this architecture on its own. CosyVoice's own
    # export renames the checkpoint to CosyVoice2ForCausalLM (literally a
    # sed on config.json), and nothing in the library registers it -- upstream
    # does it in vllm_example.py, i.e. in the CALLER. Loading without this
    # fails on an unknown architecture after the engine has already started.
    #
    # Note the "2" while this serves CosyVoice3: one adapter covers both,
    # because the LLM backbone is Qwen2 either way.
    from vllm import ModelRegistry
    from cosyvoice.vllm.cosyvoice2 import CosyVoice2ForCausalLM

    ModelRegistry.register_model("CosyVoice2ForCausalLM", CosyVoice2ForCausalLM)
    logger.info("Registered CosyVoice2ForCausalLM with vLLM.")

logger.info("Loading CosyVoice 3.0 model from %s (vllm=%s, concurrency=%d) ...",
            MODEL_DIR, LOAD_VLLM, GEN_CONCURRENCY)
cosyvoice = AutoModel(model_dir=MODEL_DIR, load_vllm=LOAD_VLLM)

# The model is not thread-safe, so generation is gated -- but by a counted
# semaphore rather than a mutex, because "one at a time" and "N at a time" want
# to be the same code path with a different number.
#
# Registration is different in kind: add_zero_shot_spk mutates the model's own
# speaker table, so it must exclude every generation rather than merely queue
# behind one. _exclusive is the writer side of a read-write lock -- generation
# passes through it only to take a permit, so it is uncontended except while a
# speaker is being registered.
_gen_gate = threading.BoundedSemaphore(GEN_CONCURRENCY)
_exclusive = threading.Lock()


@contextmanager
def generation_slot():
    """One of GEN_CONCURRENCY permits to run inference."""
    with _exclusive:
        _gen_gate.acquire()
    try:
        yield
    finally:
        _gen_gate.release()


@contextmanager
def exclusive_slot():
    """Every permit: nothing may be generating while this runs."""
    with _exclusive:
        for _ in range(GEN_CONCURRENCY):
            _gen_gate.acquire()
        try:
            yield
        finally:
            for _ in range(GEN_CONCURRENCY):
                _gen_gate.release()


logger.info("Model loaded and ready.")

os.makedirs(VOICES_DIR, exist_ok=True)
_registered = set()


def _register(speaker_id, prompt_text, wav_path):
    """Teach the model one speaker. Caller holds no lock; this takes it."""
    with exclusive_slot():
        ok = cosyvoice.add_zero_shot_spk(V3_PREFIX + prompt_text, wav_path, speaker_id)
        if ok:
            cosyvoice.save_spkinfo()
    if ok:
        _registered.add(speaker_id)
    return bool(ok)


def _try_load_from_disk(speaker_id):
    """Pick up a speaker another worker registered after we started.

    This is the part the startup scan alone gets wrong. A registration POST is
    served by exactly one worker: that process adds the speaker to its own
    model and writes the clip to shared disk, but the other workers already
    finished importing and never look again. They then answer /clone with 404
    for a speaker that plainly exists, and which worker you reach is luck.

    Measured on a real drop of The Fall of the House of Usher with four
    workers: 163 HTTP 404s and 13 chunks quarantined -- permanently dropped
    from the book -- while /health reported the speaker present, because the
    health probe happened to land on the worker that had registered it.

    Loading lazily on a miss fixes it for any worker count, and is cheap: it
    only runs when a speaker is unknown, and only once per worker per speaker.
    """
    if not SAFE_ID.match(speaker_id or ""):
        return False
    wav = os.path.join(VOICES_DIR, speaker_id + ".wav")
    side = os.path.join(VOICES_DIR, speaker_id + ".json")
    if not (os.path.exists(wav) and os.path.exists(side)):
        return False
    try:
        prompt_text = json.load(open(side, encoding="utf-8"))["text"]
    except (ValueError, OSError, KeyError):
        return False
    if _register(speaker_id, prompt_text, wav):
        logger.info("Picked up speaker '%s' registered by another worker", speaker_id)
        return True
    return False


def _load_registered_voices():
    """Re-register everything on disk.

    Runs in every worker at import. Note this is necessary but NOT sufficient
    on its own -- see _try_load_from_disk for the registration that arrives
    after a worker has already started.
    """
    for entry in sorted(os.listdir(VOICES_DIR)):
        if not entry.endswith(".wav"):
            continue
        speaker_id = os.path.splitext(entry)[0]
        wav = os.path.join(VOICES_DIR, entry)
        side = os.path.join(VOICES_DIR, speaker_id + ".json")
        if not os.path.exists(side):
            continue
        try:
            prompt_text = json.load(open(side, encoding="utf-8"))["text"]
        except (ValueError, OSError, KeyError):
            logger.warning("Skipping %s: unreadable sidecar", speaker_id)
            continue
        if _register(speaker_id, prompt_text, wav):
            logger.info("Registered speaker '%s' from disk", speaker_id)
        else:
            logger.warning("Could not register speaker '%s'", speaker_id)


_load_registered_voices()


@app.get("/health")
def health():
    # "direction": /clone takes instruct_text and speed, and drops vocal tags
    # from the transcript it keeps. A client checks this before sending either,
    # because an older image would align "[breath]" as a word.
    return {"status": "ok", "sample_rate": cosyvoice.sample_rate,
            "speakers": sorted(_registered), "features": ["direction"]}


@app.get("/voices")
def list_voices():
    return {"speakers": sorted(_registered)}


@app.post("/voices")
def register_voice(
    speaker_id: str = Form(...),
    prompt_text: str = Form(...),
    audio_file: UploadFile = File(...),
):
    """Register a speaker once; /clone then needs only its id.

    Idempotent: re-registering the same id replaces it, which is what you want
    when a reference clip is improved.
    """
    if not SAFE_ID.match(speaker_id):
        return Response(content="speaker_id must match [A-Za-z0-9_.-]{1,64}",
                        status_code=400)
    wav = os.path.join(VOICES_DIR, speaker_id + ".wav")
    try:
        with open(wav, "wb") as out:
            shutil.copyfileobj(audio_file.file, out)
        json.dump({"text": prompt_text},
                  open(os.path.join(VOICES_DIR, speaker_id + ".json"), "w",
                       encoding="utf-8"), ensure_ascii=False)
        if not _register(speaker_id, prompt_text, wav):
            return Response(content="add_zero_shot_spk refused the clip",
                            status_code=400)
        logger.info("Registered speaker '%s'", speaker_id)
        return {"status": "ok", "speaker_id": speaker_id,
                "speakers": sorted(_registered)}
    except Exception:
        logger.exception("Registration error")
        return Response(content="Registration failed", status_code=500)


# Sync endpoint on purpose: FastAPI runs it in a threadpool, so the blocking
# GPU work does not stall the event loop.
@app.post("/clone")
def clone_voice(
    text: str = Form(...),
    prompt_text: str = Form(None),
    speaker_id: str = Form(None),
    audio_file: UploadFile = File(None),
    run_id: str = Form(None),
    chunk_name: str = Form(None),
    instruct_text: str = Form(None),
    speed: float = Form(1.0),
):
    tmp_path = None
    try:
        if not 0.5 <= speed <= 2.0:
            return Response(content="speed must be between 0.5 and 2.0", status_code=400)
        if instruct_text is not None:
            # The narration director. Instruction mode needs the clip itself:
            # with a registered speaker_id, CosyVoice's frontend_instruct2 reuses
            # the stored transcript as the prompt and the instruction is silently
            # dropped (cosyvoice/cli/frontend.py at the pinned commit).
            if audio_file is None or not instruct_text.endswith("<|endofprompt|>"):
                return Response(content="instruct_text needs audio_file and must end with <|endofprompt|>",
                                status_code=400)
            suffix = os.path.splitext(audio_file.filename or "")[1] or ".wav"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(audio_file.file.read())
                tmp_path = tmp.name
            generate = cosyvoice.inference_instruct2
            gen_args = dict(tts_text=text, instruct_text=instruct_text, prompt_wav=tmp_path,
                            stream=False, speed=speed)
        elif speaker_id:
            # Unknown here usually means "registered through a different
            # worker", not "never registered" -- so look on shared disk before
            # refusing. Still a 404 if it genuinely is not there: the client can
            # re-register and retry, where quietly substituting another voice
            # would ship a book narrated by the wrong person.
            if speaker_id not in _registered and not _try_load_from_disk(speaker_id):
                return Response(
                    content=f"Unknown speaker '{speaker_id}'. POST /voices first.",
                    status_code=404)
            generate = cosyvoice.inference_zero_shot
            gen_args = dict(tts_text=text, prompt_text="", prompt_wav="",
                            zero_shot_spk_id=speaker_id, stream=False, speed=speed)
        else:
            if audio_file is None or prompt_text is None:
                return Response(
                    content="Send speaker_id, or audio_file + prompt_text.",
                    status_code=400)
            suffix = os.path.splitext(audio_file.filename or "")[1] or ".wav"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(audio_file.file.read())
                tmp_path = tmp.name
            generate = cosyvoice.inference_zero_shot
            gen_args = dict(tts_text=text, prompt_text=V3_PREFIX + prompt_text,
                            prompt_wav=tmp_path, stream=False, speed=speed)

        # inference_zero_shot yields one chunk per text segment, even when
        # stream=False — collect them all instead of taking only the first.
        chunks = []
        with generation_slot():
            for out in generate(**gen_args):
                chunks.append(out["tts_speech"])

        if not chunks:
            return Response(content="Generation failed", status_code=500)

        audio_tensor = torch.cat(chunks, dim=1).cpu()

        buffer = io.BytesIO()
        torchaudio.save(
            buffer, audio_tensor, cosyvoice.sample_rate,
            format="wav", backend="soundfile",
        )
        wav_bytes = buffer.getvalue()

        # Keep a copy so this pod can align its own output later. Best-effort on
        # purpose: a chunk that cannot be written here is still a chunk the
        # caller gets back, and losing its timestamps costs sync precision
        # rather than narration. Failing the request instead would throw away
        # paid-for GPU work over a disk error.
        if run_id and chunk_name:
            try:
                _retain_chunk(run_id, chunk_name, wav_bytes, _TAG_RE.sub("", text))
            except Exception:
                logger.exception("Could not retain %s for alignment", chunk_name)

        return Response(content=wav_bytes, media_type="audio/wav")

    except Exception:
        logger.exception("Generation error")
        return Response(content="Generation failed", status_code=500)

    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# --- ALIGNMENT, ON THE POD THAT GENERATED THE AUDIO -------------------------
#
# Montreal Forced Aligner, run here rather than on the reader's machine. The
# argument is in the RUNS_DIR comment above; the short version is that MFA ships
# x86-only, so on an Apple Silicon Mac it runs emulated at 14.7x realtime and a
# sixteen-hour book spends 65 minutes aligning after it has finished generating.
# This pod is native, has idle cores while the GPU works, and already holds the
# audio.
#
# WHY THE CLIENT SENDS THE MODEL URLS. The dictionary and the acoustic model
# decide where every word boundary lands, so a book aligned against a different
# version gets a different sync map -- which is why core/languages.py pins both
# per language, to the same version. Duplicating that table here would let the
# two drift silently and move the timestamps of books already in the library.
# The client passes the URLs it resolved; this only fetches and caches them.

def _safe_dir(base, name):
    """base/name, with `name` proven to be a single harmless path segment."""
    if not SAFE_ID.match(name or ""):
        raise ValueError(f"unsafe name: {name!r}")
    path = os.path.join(base, name)
    os.makedirs(path, exist_ok=True)
    return path


def _retain_chunk(run_id, chunk_name, wav_bytes, text):
    """Store one generated chunk where /align can find it.

    The transcript goes beside the audio as a .lab file, which is MFA's own
    convention for a single-utterance corpus.
    """
    run_dir = _safe_dir(RUNS_DIR, run_id)
    if not SAFE_ID.match(chunk_name or ""):
        raise ValueError(f"unsafe chunk name: {chunk_name!r}")
    with open(os.path.join(run_dir, chunk_name + ".wav"), "wb") as f:
        f.write(wav_bytes)
    with open(os.path.join(run_dir, chunk_name + ".lab"), "w", encoding="utf-8") as f:
        f.write(text)


MFA_MODELS_URL = "https://github.com/MontrealCorpusTools/mfa-models/releases/download/"


def _fetch_model(url, dest):
    """Download a pinned MFA model once per pod. Returns the local path.

    Only from MFA's own releases: the URL arrives in a request, and a server
    that fetched any URL it was handed could be pointed at the pod's network.
    """
    if not str(url).startswith(MFA_MODELS_URL):
        raise ValueError(f"model URL must start with {MFA_MODELS_URL}")
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest
    import urllib.request
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    with urllib.request.urlopen(url, timeout=300) as r, open(tmp, "wb") as f:  # nosec B310: https, checked above
        shutil.copyfileobj(r, f)
    os.replace(tmp, dest)
    return dest


def _mfa_env():
    """Environment for the aligner subprocess, with its own bin/ on PATH.

    Invoking /opt/mfa/bin/mfa by absolute path is not enough. MFA shells out to
    OpenFST and Kaldi binaries -- fstcompile, fstarcsort, gmm-align-compiled --
    and finds them on PATH, which is the serving venv's, not the aligner's. The
    failure is immediate and reads as if nothing were installed at all:

        ThirdpartyError: Could not find 'fstcompile'. Please ensure that you
        have installed MFA's conda dependencies and are in the correct
        environment.

    Measured on a real pod: `mfa align` exited in 4 seconds having aligned
    nothing, while `mfa version` -- which never touches those binaries --
    answered perfectly happily.
    """
    root = os.path.dirname(os.path.dirname(MFA_BIN))      # /opt/mfa
    env = dict(os.environ, KMP_AFFINITY="disabled")
    env["PATH"] = os.path.join(root, "bin") + os.pathsep + env.get("PATH", "")
    # OpenFST's shared objects live beside the binaries. Most conda builds carry
    # an rpath, but not all, and a missing libfst is the same failure wearing a
    # different message.
    env["LD_LIBRARY_PATH"] = (os.path.join(root, "lib") + os.pathsep
                              + env.get("LD_LIBRARY_PATH", ""))
    return env


def _mfa_available():
    """Whether this pod can actually align, not merely whether mfa is present.

    fstcompile as well as mfa itself: an image with the CLI but without the
    OpenFST binaries answers `mfa version` and then fails every alignment, and
    the engine needs to know that BEFORE it decides to skip local alignment.
    """
    root = os.path.dirname(os.path.dirname(MFA_BIN))
    return (os.path.exists(MFA_BIN)
            and os.path.exists(os.path.join(root, "bin", "fstcompile")))


@app.get("/align/health")
def align_health():
    """Whether this pod can align, so the engine can decide before it needs to.

    Asked up front rather than discovered at the end of generation: falling back
    to local alignment is fine, but finding out only after the pods are gone
    means the audio is on the Mac and the choice has already been made.
    """
    return {"available": _mfa_available(), "mfa_bin": MFA_BIN}


@app.post("/align")
def align_run(
    run_id: str = Form(...),
    dict_url: str = Form(...),
    acoustic_url: str = Form(...),
    language: str = Form("en"),
    beam: int = Form(None),
    retry_beam: int = Form(None),
):
    """Align every chunk this pod generated for `run_id`. Returns word timings.

    Batch, not per chunk. `mfa align_one` exists and is the obvious fit, but
    nearly all of its cost is fixed setup: measured at 41 seconds for a single
    90-second chunk, against 118 seconds for twenty-four of them in one batch.
    Per-chunk alignment would be slower than the Mac it is replacing.
    """
    if not _mfa_available():
        # 404 rather than 500: this is "this server does not do that", which is
        # exactly what an older image is, and the engine reads it as a reason to
        # align locally rather than as a failure.
        return Response(content="This pod has no aligner; align locally.",
                        status_code=404)
    try:
        run_dir = _safe_dir(RUNS_DIR, run_id)
    except ValueError as e:
        return Response(content=str(e), status_code=400)

    chunks = sorted(f[:-4] for f in os.listdir(run_dir) if f.endswith(".wav"))
    if not chunks:
        return {"aligned": {}, "missing": [], "chunks": 0}

    out_dir = os.path.join(run_dir, "output")
    os.makedirs(out_dir, exist_ok=True)

    dict_path = _fetch_model(dict_url, os.path.join(
        MFA_MODELS_DIR, os.path.basename(dict_url)))
    acoustic_path = _fetch_model(acoustic_url, os.path.join(
        MFA_MODELS_DIR, os.path.basename(acoustic_url)))

    cmd = [MFA_BIN, "align", run_dir, dict_path, acoustic_path, out_dir,
           "--output_format", "json", "--clean",
           # Every chunk is the same narrator, so MFA collapses to a single job
           # unless told otherwise -- it says so in a warning nobody reads:
           # "due to only having 1 speakers, MFA will only use 1 jobs". On the
           # Mac forcing the issue made things worse (emulated processes
           # thrashing a 4GB VM); here the cores are real and idle.
           "--single_speaker",
           "-j", str(max(1, min(8, (os.cpu_count() or 4) - 2)))]
    if beam:
        cmd += ["--beam", str(beam), "--retry_beam", str(retry_beam or beam * 4)]

    proc = subprocess.run(cmd, capture_output=True, text=True, env=_mfa_env())

    aligned = {}
    for name in chunks:
        path = os.path.join(out_dir, name + ".json")
        if os.path.exists(path):
            try:
                aligned[name] = json.load(open(path, encoding="utf-8"))
            except ValueError:
                pass

    # MFA exits 0 even when it silently skips utterances, and it does so
    # routinely -- so what was submitted is compared against what came back
    # rather than trusting the return code. The caller decides what to do with
    # the misses; it can retry them with a wider beam, which is MFA's own
    # documented remedy.
    missing = [c for c in chunks if c not in aligned]
    if missing:
        logger.warning("MFA aligned %d of %d chunks for run %s (rc=%d)",
                       len(aligned), len(chunks), run_id, proc.returncode)
    return {"aligned": aligned, "missing": missing, "chunks": len(chunks),
            "returncode": proc.returncode,
            "stderr": proc.stderr[-2000:] if missing else ""}


@app.post("/align/cleanup")
def align_cleanup(run_id: str = Form(...)):
    """Forget a run's retained audio. Called once the client has its timings.

    A pod is terminated at the end of a book, so this exists for the case where
    one is reused across several -- `--use-pod`, or a future warm pool -- where
    a run's worth of WAVs would otherwise sit on the disk until it filled.
    """
    try:
        run_dir = _safe_dir(RUNS_DIR, run_id)
    except ValueError as e:
        return Response(content=str(e), status_code=400)
    shutil.rmtree(run_dir, ignore_errors=True)
    return {"status": "ok", "run_id": run_id}


# --- WHERE DOES THE TIME GO? ------------------------------------------------
#
# The question that decides the whole acceleration plan. Upstream's vLLM
# integration accelerates the LLM and ONLY the LLM -- flow-matching, token2wav
# and the vocoder stay in PyTorch/ONNX -- so Amdahl's law caps that path at
# whatever share of wall clock the LLM actually holds. At 80% the ceiling is
# 5x; at 40% it is 1.7x and the work is not worth doing.
#
# Nobody has measured that split for this model on this hardware, and it is
# one pod-minute to find out rather than a guess to build on.

_PROFILE_STAGES = [
    # (label, dotted path from the AutoModel, method name)
    ("llm", "model.llm", "inference"),
    ("flow", "model.flow", "inference"),
    ("vocoder", "model.hift", "inference"),
]


def _resolve(root, dotted):
    obj = root
    for part in dotted.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj


@app.post("/profile")
def profile(text: str = Form(...), speaker_id: str = Form(None)):
    """Time one synthesis, split by pipeline stage.

    Reports what it could NOT instrument as loudly as what it could. A split
    that silently omits a stage reads as "the LLM is 90% of the time" and sends
    weeks of work at the wrong component -- so `instrumented` names exactly
    which stages were wrapped, and `unaccounted` is the wall clock they failed
    to explain.
    """
    if speaker_id and speaker_id not in _registered:
        _try_load_from_disk(speaker_id)

    timings = {}
    originals = []
    for label, path, method in _PROFILE_STAGES:
        owner = _resolve(cosyvoice, path)
        fn = getattr(owner, method, None) if owner is not None else None
        if fn is None or not callable(fn):
            continue

        def make(label, fn):
            def wrapped(*a, **kw):
                t0 = time.perf_counter()
                try:
                    out = fn(*a, **kw)
                    # Generators do their work on iteration, not on call, so
                    # timing the call alone would report ~0 for a stage that
                    # dominates. Drain it here and hand back a list.
                    if hasattr(out, "__next__"):
                        out = list(out)
                    return out
                finally:
                    timings[label] = timings.get(label, 0.0) + (
                        time.perf_counter() - t0)
            return wrapped

        originals.append((owner, method, fn))
        setattr(owner, method, make(label, fn))

    try:
        t0 = time.perf_counter()
        with generation_slot():
            chunks = []
            for out in cosyvoice.inference_zero_shot(
                    tts_text=text, prompt_text="", prompt_wav="",
                    zero_shot_spk_id=speaker_id, stream=False):
                chunks.append(out["tts_speech"])
        wall = time.perf_counter() - t0
        audio_s = (sum(c.shape[-1] for c in chunks) / cosyvoice.sample_rate
                   if chunks else 0.0)
    finally:
        for owner, method, fn in originals:
            setattr(owner, method, fn)

    accounted = sum(timings.values())
    return {
        "chars": len(text),
        "wall_s": round(wall, 3),
        "audio_s": round(audio_s, 3),
        "realtime": round(audio_s / wall, 2) if wall else 0,
        "stages_s": {k: round(v, 3) for k, v in timings.items()},
        "stages_pct": {k: round(100 * v / wall, 1) for k, v in timings.items()}
                      if wall else {},
        "instrumented": sorted(timings),
        "not_instrumented": [lbl for lbl, _, _ in _PROFILE_STAGES
                             if lbl not in timings],
        "unaccounted_s": round(wall - accounted, 3),
        "unaccounted_pct": round(100 * (wall - accounted) / wall, 1) if wall else 0,
    }


# --- THE CLIENT IS GONE: STOP BILLING ------------------------------------------
# Last, so the clock starts when the model can serve, not while it loads.
from idle_watchdog import from_env as _idle_watchdog  # noqa: E402

_idle = _idle_watchdog()
if _idle.limit:
    print(f"⏹️  This pod terminates itself after {_idle.limit / 60:.0f} min without a request "
          "(POD_IDLE_MINUTES; 0 turns it off).", flush=True)
    threading.Thread(target=_idle.run_forever, daemon=True).start()
