import os
import re
import json
import sqlite3
import shutil
import subprocess
import asyncio
import traceback
import concurrent.futures
import time

import httpx
import wave
from mutagen.id3 import ID3, TXXX, TIT2, TPE1, TRCK, ID3NoHeaderError
from core.tts_providers import get_provider
from core.chunking import split_chunk
from core import chunk_cache
from core.director import Direction

# --- CONFIGURATION ---
RUNPOD_BASE_URL = os.getenv("RUNPOD_BASE_URL")

# Applied to every chunk before alignment. Uniform by design -- see the
# comment at the call site for why this is not loudnorm.
MFA_INPUT_GAIN_DB = 20



def clone_or_copy(src, dst):
    """Place src at dst, as an APFS clone where the filesystem can make one.

    Every chunk used to be copied from mfa_workspace into the audiobook folder
    before stitching, so a book briefly needed its whole narration on disk
    twice. On 2026-09-24 that was Redeeming Love's 5.3GB of WAVs with 1.0GB
    free, after the GPU had been paid for. A clone is instant and takes no
    space until one side is written, and nothing writes to these files -- the
    stitcher only reads them -- so the second copy costs nothing.

    `cp -c` is clonefile(2). Anywhere it cannot clone -- not macOS, not APFS,
    across volumes -- it fails, and this falls back to an ordinary copy, so the
    behaviour is never worse than before.
    """
    if os.path.lexists(dst):
        os.remove(dst)
    try:
        if subprocess.run(["cp", "-c", src, dst], capture_output=True).returncode == 0:
            return "clone"
    except OSError:
        pass
    if os.path.lexists(dst):
        os.remove(dst)
    shutil.copyfile(src, dst)
    return "copy"

def wav_duration_from_bytes(raw):
    """Seconds of audio in an in-memory WAV, read from its own header.

    Never from `len(raw) / (rate * 2)`. CosyVoice returns **32-bit float** PCM
    (pcm_f32le), so assuming 16 bits reports exactly double the real duration --
    and therefore double the throughput, which is a measurement that flatters
    whatever it is pointed at. This project has already been burned by that
    specific arithmetic once: a float32 WAV read as int16 produced "147s at
    4.04x" for 73.5s at 2.02x.

    Returns 0.0 for anything that is not a WAV this can parse, so a caller
    measuring throughput sees nothing rather than a plausible wrong number.
    """
    import struct
    if len(raw) < 44 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        return 0.0
    pos, rate, channels, bits, data_len = 12, None, None, None, None
    while pos + 8 <= len(raw):
        cid = raw[pos:pos + 4]
        size = struct.unpack("<I", raw[pos + 4:pos + 8])[0]
        body = pos + 8
        if cid == b"fmt " and body + 16 <= len(raw):
            _fmt, channels, rate, _bps, _align, bits = struct.unpack(
                "<HHIIHH", raw[body:body + 16])
        elif cid == b"data":
            # The declared size, except when it lies (a stream written with a
            # placeholder length), in which case what is actually here wins.
            data_len = min(size, len(raw) - body) if size else len(raw) - body
            break
        pos = body + size + (size & 1)
    if not (rate and channels and bits and data_len):
        return 0.0
    return data_len / float(rate * channels * (bits // 8))


def cache_text(text, direction):
    """What the chunk cache keys on besides the provider: the text, and its direction.

    A directed take is different audio from a plain one of the same words; a
    key without the direction would hand one back for the other.
    """
    if direction is None or direction.plain:
        return text
    return text + "\x00" + direction.key()


def pod_chunk_key(base_name):
    """A filename-safe, collision-free name for one chunk on the pod.

    A chunk's own name cannot be used. It is built from the chapter heading --
    "001_A_Chapitre 1", "004_B_THE FULL PROJECT GUTENBERG LICENSE" -- so it
    carries spaces, accents and arbitrary length, while the server refuses
    anything outside [A-Za-z0-9_.-]{1,64} because that name becomes a path. A
    rejected chunk would simply not be retained, and the only symptom would be
    missing timestamps for whichever chapters happened to have awkward titles.

    Hashed rather than sanitised: two different headings can sanitise to the
    same string, and a collision here silently gives one chunk another's
    alignment. The engine keeps the mapping back to the real name.
    """
    import hashlib
    return "c" + hashlib.sha1(base_name.encode("utf-8"), usedforsecurity=False).hexdigest()[:20]


def probe_duration(path):
    """Length of an audio file in seconds, measured from the file itself.

    The chunks are PCM WAV (the .pristine copies included), so the stdlib
    reads them exactly and without spawning a process per chunk; ffprobe is
    the fallback for anything it cannot parse.
    """
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True,
        )
        try:
            return float(out.stdout.strip())
        except ValueError:
            print(f"   ‼️  Could not measure duration of {os.path.basename(path)}")
            return 0.0

TAIL_SILENCE_KEEP = 0.5     # seconds of silence left after a chunk's last sound
SILENCE_DBFS = -40.0        # quieter than this, over 20 ms, is silence


def trim_trailing_silence(wav_path, keep=TAIL_SILENCE_KEEP):
    """Cut a chunk's silence after its last sound down to `keep` seconds, in place.

    CosyVoice sometimes ends a chunk in dead air: 6.4 s and then 6.9 s after
    the same line, in two takes of a scene in a cloned voice (2026-10-12),
    played as-is between two paragraphs. Only the end is cut: word timings
    are counted from a chunk's start (on the pod or by MFA), so nothing they
    say moves, and the chunk's duration is read from the file afterwards.

    The bytes are cut, never re-encoded: the audio stays lossless (CosyVoice
    returns float32) until the one encode per chapter. Returns the seconds
    removed (0.0 when there was nothing to remove or the file is not a PCM WAV
    this reads).
    """
    import struct
    import numpy as np
    with open(wav_path, "rb") as f:
        raw = f.read()
    if len(raw) < 44 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        return 0.0
    pos, fmt, data_at, data_len = 12, None, None, None
    while pos + 8 <= len(raw):
        cid, size = raw[pos:pos + 4], struct.unpack("<I", raw[pos + 4:pos + 8])[0]
        if cid == b"fmt " and pos + 24 <= len(raw):
            fmt = struct.unpack("<HHIIHH", raw[pos + 8:pos + 24])
        elif cid == b"data":
            data_at = pos + 8
            data_len = min(size, len(raw) - data_at) if size else len(raw) - data_at
            break
        pos += 8 + size + (size & 1)
    if not fmt or data_at is None:
        return 0.0
    tag, channels, rate, _, align, bits = fmt
    dtype = {(3, 32): "<f4", (1, 16): "<i2", (1, 32): "<i4"}.get((tag, bits))
    if dtype is None or not channels or not rate:
        return 0.0
    frames = data_len // align
    x = np.frombuffer(raw[data_at:data_at + frames * align], dtype=dtype).reshape(frames, channels)
    x = x.astype(np.float32).mean(axis=1)
    if dtype != "<f4":
        x /= float(2 ** (bits - 1))
    win = max(1, int(rate * 0.02))
    n = frames // win
    if n == 0:
        return 0.0
    rms = np.sqrt(np.mean(x[:n * win].reshape(n, win) ** 2, axis=1))
    loud = np.nonzero(20 * np.log10(rms + 1e-12) > SILENCE_DBFS)[0]
    if len(loud) == 0:
        return 0.0          # all silence: the bouncer's business, not this one's
    end = min(frames, (loud[-1] + 1) * win + int(keep * rate))
    if frames - end < int(0.1 * rate):
        return 0.0
    body = raw[data_at:data_at + end * align]
    header = bytearray(raw[:data_at])
    struct.pack_into("<I", header, 4, len(header) - 8 + len(body))
    struct.pack_into("<I", header, data_at - 4, len(body))
    with open(wav_path + ".trim.tmp", "wb") as f:
        f.write(bytes(header) + body)
    os.replace(wav_path + ".trim.tmp", wav_path)
    return (frames - end) / rate


def prepare_chunk_audio(wav_path, provider, worker_id, base_name):
    """Turn a model's raw output into the files the rest of the pipeline expects.

    Shared by the generate path and the cache-hit path deliberately. The cache
    stores what the model returned, and everything derived from it -- the
    .pristine copy, the MFA boost -- is reproduced here, so a hit and a miss
    leave the workspace in the same state. Two code paths doing this separately
    is precisely how a cached chunk would end up boosted twice, or not at all,
    with nothing downstream able to tell.

    Montreal Forced Aligner reads these WAVs directly and Voxtral returns roughly
    -34 to -46 LUFS. Alignment failed on 7 of 16 chunks of the first real book --
    silently, producing no timestamps and so no sync nodes. Give the aligner
    something audible.

    A FIXED gain, never loudnorm: every chunk is lifted by the same amount, so
    relative levels survive for the single chapter-level normalisation at
    assembly. Per-chunk normalisation here is exactly what makes the decoder
    noise pump between chunks.

    A provider whose output already sits at a listening level skips all of it.
    CosyVoice returns -18.8 to -19.8 LUFS, which the aligner hears perfectly
    well, so boosting would only lift its noise floor -- and with no boost there
    is nothing for the .pristine copy to protect the listener from.

    First, for every provider: dead air after a chunk's last word is cut to
    TAIL_SILENCE_KEEP (trim_trailing_silence). The cache keeps what the model
    returned, so a hit is trimmed here too.
    """
    cut = trim_trailing_silence(wav_path)
    if cut >= 1.0:
        print(f"   [W{worker_id}] ✂️  {base_name}: cut {cut:.1f}s of silence after its last word.")
    if not getattr(provider, "NEEDS_MFA_BOOST", True):
        return

    # The boost must not reach the listener, or every chunk arrives pre-gained
    # and limited before the chapter is normalised. The ".pristine" extension
    # keeps MFA from picking this file up as input.
    shutil.copyfile(wav_path, wav_path.replace(".wav", ".pristine"))

    # NOT ".boosted.wav": process_mfa_factory lists *.wav to find chunks, so a
    # leftover with that suffix was picked up as a chunk of its own and killed
    # the run on its missing sidecar.
    boosted = wav_path + ".boost.tmp"
    # -f wav is load-bearing. ffmpeg picks the container from the output
    # extension, and this temp is deliberately NOT .wav so the injector cannot
    # mistake a leftover for a chunk -- which left ffmpeg with '.tmp' and
    # nothing to infer:
    #   Error initializing the muxer for ...boost.tmp: Invalid argument
    # Every chunk then failed to boost, silently, and MFA was handed the
    # -34 to -46 LUFS audio the boost exists to rescue.
    gain = subprocess.run(
        ["ffmpeg", "-y", "-i", wav_path,
         "-af", f"volume={MFA_INPUT_GAIN_DB}dB,alimiter=limit=0.98",
         "-f", "wav", boosted],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if gain.returncode == 0 and os.path.exists(boosted):
        os.replace(boosted, wav_path)
    else:
        if os.path.exists(boosted):
            os.remove(boosted)
        print(f"   [W{worker_id}] ⚠️ Could not boost {base_name}; MFA may fail to align it.")


# --- PHASE 1: THE RUNPOD WORKER ---
def _retry_url(base_url, fallbacks, attempt, worker_id):
    """Where attempt N of a chunk should go.

    The first attempt goes to the worker's own pod. Every retry after that
    moves to a different one, because the failures worth retrying are mostly
    properties of a POD rather than of a chunk: a server whose workers have
    been OOM-killed answers ConnectError to everything, and a pod behind a bad
    route times out on everything. Asking it four more times is four more
    timeouts.

    This is what cost The Screwtape Letters two chunks. 042_B_IX and 043_C_IX
    were each killed twelve times at 95 seconds -- about nineteen minutes of
    paid GPU per chunk -- and then quarantined out of the book, all against the
    same server. The requeue path could already land a chunk elsewhere, but
    only by luck and only after four attempts had already been spent.

    Deliberately NOT speculative hedging. Racing a duplicate request on another
    pod would shorten the tail, but it pays for the same audio twice, and cost
    is the thing being optimised here -- so a second pod is asked only once the
    first has actually failed, which is free.
    """
    if attempt <= 1:
        return base_url
    others = [u for u in _resolve_pods(fallbacks) if u != base_url]
    if not others:
        return base_url
    return others[(worker_id + attempt) % len(others)]


def _resolve_pods(pod_urls):
    """The pool as it is NOW, not as it was when this worker started.

    Pods join the pool while the run is under way -- acquisition is concurrent
    and the first pod starts narrating at about a minute while the last is
    still pulling the image. A worker created early would hold a snapshot
    naming one pod and have nowhere to fail over to, which is exactly the
    moment when failing over is most useful.
    """
    if callable(pod_urls):
        try:
            return list(pod_urls())
        except Exception:
            return []
    return list(pod_urls or [])


async def runpod_worker(worker_id, audio_queue, client, mfa_dir, db_path, base_url,
                        provider=None, pod_urls=None, pool=None):
    """Hits RunPod at max speed and saves .wav, .txt, and node metadata.

    `base_url` is this worker's home pod; `pod_urls` is every pod in the run,
    used to retry somewhere else when the home pod is the problem. See
    _retry_url for why that matters.

    `pool` (core.pod_pool.PodPool) hears whether each request reached its
    pod. When this worker's own pod is lost, the worker puts its chunk back
    and returns "pod lost", and the engine terminates the pod and rents a
    replacement; a worker that ends any other way has crashed.
    """
    provider = provider or get_provider()
    # Kept as given -- a list or the pool's own accessor -- and resolved at
    # retry time, so pods that join after this worker started are usable.
    # The rotation is offset by worker id so the workers of a failing pod do
    # not all pile onto the same neighbour.
    fallbacks = pod_urls
    # Same PRAGMAs as core.database.get_connection: thirty workers share this
    # file, and the default journal mode serialises every one of them.
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.Error:
        pass
    cursor = conn.cursor()
    # Set before the loop: a worker whose first item is the poison pill (every
    # chunk went to others, or came from the cache) must reach the end without
    # it (found by layer 3, kill-9: such workers crashed and the run lost every
    # chapter of a fully cached book).
    retire = False

    while True:
        chapter = await audio_queue.get()
        if chapter is None:
            audio_queue.task_done()
            break

        # Everything from here to the matching task_done() is wrapped, because
        # anything that escapes this loop kills the worker while the queue still
        # counts its item as outstanding -- and audio_queue.join() then waits for
        # a worker that no longer exists, with the pod billing and no timeout
        # anywhere. The unguarded INSERT below was the live instance of that: a
        # "database is locked" under thirty concurrent writers is an ordinary
        # event, not an exceptional one, and it hung the whole run.
        chapter_id = None
        wav_path = txt_path = nodes_path = None
        retire = False
        base_name = chapter.get("filename", "?")
        try:
            book_id = chapter["book_id"]
            chapter_index = chapter["chapter_index"]
            filename = chapter["filename"]  # e.g., "001_Part_1.mp3"
            base_name = filename[:-4] if filename.endswith(".mp3") else filename

            wav_path = os.path.join(mfa_dir, f"{base_name}.wav")
            txt_path = os.path.join(mfa_dir, f"{base_name}.txt")
            nodes_path = os.path.join(mfa_dir, f"{base_name}_nodes.json")


            # 1. Reserve the Chapter ID in the Database.
            # Reserved per attempt, and released again if the chunk is requeued
            # or quarantined: the row used to be inserted here and never cleaned
            # up, so a requeued chunk left a duplicate behind and a quarantined
            # one left a phantom chapter with duration 0.0 and no audio, which
            # travelled all the way into the manifest and on to Audiobookshelf.
            cursor.execute('''
                INSERT INTO chapters (book_id, chapter_index, audio_filename, duration)
                VALUES (?, ?, ?, 0.0)
            ''', (book_id, chapter_index, filename))
            chapter_id = cursor.lastrowid
            conn.commit()

            # A chunk's audio is a pure function of its text, its narrator and
            # the model, so the same chunk never needs paying for twice. Checked
            # before the request rather than inside the retry loop: a hit is not
            # an attempt, and counting it as one would mean a cached chunk
            # consumed part of a budget it never needed.
            direction = (Direction.from_dict(chapter.get("direction"))
                         if getattr(provider, "SUPPORTS_DIRECTION", False) else None)
            ck = chunk_cache.key_for(provider, cache_text(chapter["text"], direction))
            # A retake (the aligner refused the take) must reach the model: the
            # cache would hand back the very take being replaced.
            if not chapter.get("retake") and chunk_cache.get(ck, wav_path):
                with open(txt_path, "w", encoding="utf-8") as f:
                    f.write(chapter["text"])
                with open(nodes_path, "w", encoding="utf-8") as f:
                    json.dump({"chapter_id": chapter_id,
                               "nodes": chapter.get("nodes", [])}, f)
                # Exactly what a freshly generated chunk gets. The cache holds
                # the model's raw output, so a hit has to reproduce the derived
                # files rather than assume them -- otherwise a cached chunk
                # reaches MFA unboosted while its neighbours are boosted, and
                # only that chunk fails to align.
                prepare_chunk_audio(wav_path, provider, worker_id, base_name)
                print(f"   [W{worker_id}] ⚡ Cache hit for {base_name} — no GPU needed.")
                # `continue`, not task_done() then continue: the finally below
                # already marks the item done, and continue runs it on the way
                # out. Calling it here too raises "task_done() called too many
                # times", which kills the worker and leaves audio_queue.join()
                # waiting on a consumer that no longer exists -- with the pod
                # billing, which is the failure mode this loop is wrapped to
                # prevent in the first place.
                continue

            # Announced only once a request is actually going to be made. It
            # used to print before the cache was consulted, so a run of hits
            # read as "Requesting... Cache hit" for every chunk -- which looks
            # exactly like a cache that is not working.
            print(f"   [W{worker_id}] 🚀 Requesting {provider.__class__.__name__} Audio for: {base_name}...")

            # 👇 THE BULLETPROOF ALIGNMENT PROTECTOR
            max_retries = 4
            for attempt in range(1, max_retries + 1):
                try:
                    # 1. The provider owns its own request shape: Voxtral posts
                    # JSON to an OpenAI-style endpoint, CosyVoice posts multipart
                    # with the reference clip. The timeout comes from the
                    # provider: CosyVoice caps it below RunPod's ~100s proxy
                    # ceiling so a doomed request fails fast instead of holding
                    # a worker, which is what turned eight busy workers into a
                    # run that made no progress at all.
                    # run_id/base_name let a pod that can align keep this
                    # chunk under a name the engine can match back afterwards.
                    # The name is the chunk's own filename stem, which is
                    # already what mfa_workspace/output is keyed by -- so a
                    # pod-aligned run and a locally aligned one produce
                    # identically named JSONs and nothing downstream can tell
                    # which path produced them.
                    if pool is not None and pool.is_lost(base_url):
                        retire = await _hand_back(chapter, audio_queue, conn, cursor, chapter_id,
                                                  worker_id, base_name, wav_path, txt_path, nodes_path)
                        break
                    target = _retry_url(base_url, fallbacks, attempt, worker_id)
                    if target != base_url:
                        print(f"   [W{worker_id}] ↪️  Retrying {base_name} on a "
                              f"different pod ({target}).")
                    response = await provider.synthesize(
                        client, target, chapter["text"],
                        timeout=getattr(provider, "REQUEST_TIMEOUT_SECONDS", 900),
                        run_id=chapter.get("run_id"),
                        chunk_name=pod_chunk_key(base_name),
                        **({"direction": direction} if direction else {}))
                    if pool is not None:
                        pool.note(target, reached=True)
                    response.raise_for_status()

                    # 2. Save the WAV file
                    with open(wav_path, "wb") as f:
                        f.write(response.content)

                    # 3. 🛡️ THE BOUNCER: Reject hallucinated empty files
                    if not provider.is_valid_response(response.content):
                        os.remove(wav_path)
                        raise Exception(f"{provider.__class__.__name__} returned an empty/silent audio file.")

                    # 3b. Truncation. The model stops at a fixed length and
                    # returns a perfectly well-formed file containing only part
                    # of the text, so nothing above this notices: the bouncer
                    # sees megabytes and waves it through, and the only symptom
                    # downstream is MFA failing to align a chunk whose transcript
                    # has more words than its audio -- which reads as an
                    # alignment problem rather than as missing narration.
                    #
                    # Reported, never discarded, and never retried. A retry
                    # returns the same truncation four times and then quarantines
                    # the chunk, which would throw away narration that was paid
                    # for and is mostly good -- the one thing this pipeline must
                    # never do. Partial audio loudly flagged beats partial audio
                    # silently shipped, and beats no audio at all.
                    truncated_seconds = probe_duration(wav_path)
                    if provider.is_truncated(truncated_seconds):
                        with open(wav_path + ".truncated", "w", encoding="utf-8") as f:
                            json.dump({"chunk": base_name,
                                       "seconds": truncated_seconds,
                                       "chars": len(chapter["text"])}, f)
                        print(f"   [W{worker_id}] ‼️  {base_name} hit the model's "
                              f"{truncated_seconds:.2f}s ceiling on {len(chapter['text'])} chars — "
                              "the tail of this chunk was never narrated.")

                    # 4. If it passes the bouncer, save the text and telemetry
                    with open(txt_path, "w", encoding="utf-8") as f:
                        f.write(chapter["text"])

                    with open(nodes_path, "w", encoding="utf-8") as f:
                        json.dump({"chapter_id": chapter_id, "nodes": chapter.get("nodes", [])}, f)

                    # Montreal Forced Aligner reads these WAVs directly, and the
                    # model returns audio at roughly -34 to -46 LUFS. Alignment
                    # failed on 7 of 16 chunks of the first real book -- silently,
                    # producing no timestamps and so no sync nodes, which is why
                    # that book's map came out four times coarser than the previous
                    # one. Give the aligner something audible to work with.
                    #
                    # A FIXED gain, not loudnorm: every chunk is lifted by the same
                    # amount, so their relative levels are untouched and the single
                    # chapter-level normalisation at assembly still has a coherent
                    # signal to work on. Per-chunk normalisation here is exactly
                    # what makes the decoder noise pump between chunks.
                    # Keep an untouched copy for the finished audiobook. The boost
                    # below exists only so the aligner has something audible; it
                    # must not reach the listener, or every chunk arrives pre-gained
                    # and limited before the chapter is normalised. The ".pristine"
                    # extension keeps MFA from picking this file up as input.
                    #
                    # A provider whose output already sits at a listening level
                    # skips all of this. CosyVoice returns -18.8 to -19.8 LUFS,
                    # which the aligner hears perfectly well, so boosting it
                    # would only lift its noise floor -- and without a boost
                    # there is nothing for the .pristine copy to protect the
                    # listener from. process_mfa_factory already falls back to
                    # the .wav when no .pristine exists, so skipping it here
                    # halves the chunk files on disk with no other change.
                    # Cached BEFORE any post-processing, so the entry holds what
                    # the model actually returned. That is the thing a chunk's
                    # identity describes -- text, narrator, model -- and every
                    # derived file is reproduced from it by prepare_chunk_audio
                    # below, on a hit exactly as on a miss.
                    #
                    # Caching the post-processed file instead was the first
                    # version and it was wrong in a way that would not have
                    # shown up for weeks: the boost branch writes .pristine
                    # BEFORE boosting, process_mfa_factory prefers .pristine for
                    # the finished audiobook, and a cache hit restoring only the
                    # boosted .wav would have put 20dB-too-loud narration into
                    # the book -- for cached chunks only.
                    chunk_cache.put(ck, wav_path,
                                    {"chunk": base_name,
                                     "chars": len(chapter["text"]),
                                     "seconds": truncated_seconds})

                    prepare_chunk_audio(wav_path, provider, worker_id, base_name)
                    break

                except Exception as exc:
                    if pool is not None and isinstance(exc, httpx.TransportError):
                        # No answer, a reset, a timeout: the pod, not the chunk.
                        pool.note(target, reached=False)
                        if pool.is_lost(base_url):
                            retire = await _hand_back(chapter, audio_queue, conn, cursor, chapter_id,
                                                      worker_id, base_name, wav_path, txt_path, nodes_path)
                            break
                    # If we get an error, use Exponential Backoff
                    if attempt < max_retries:
                        backoff = attempt * 2  # Waits 2s, then 4s, then 6s...
                        # Name the failure. "GPU Busy" was printed for every
                        # exception alike, and that cost real time: a run
                        # stalling on HTTP 524 (RunPod's proxy sits behind
                        # Cloudflare, which kills any response over ~100s) looks
                        # identical to one hitting a genuine server error, and
                        # the remedies are opposite -- fewer workers versus more
                        # retries.
                        why = type(exc).__name__
                        code = getattr(getattr(exc, "response", None), "status_code", None)
                        if code == 524:
                            why = "HTTP 524 - the proxy timed out (requests are too slow; try fewer workers)"
                        elif code:
                            why = f"HTTP {code}"
                        print(f"   [W{worker_id}] ⚠️ {why}. Retrying {base_name} "
                              f"({attempt}/{max_retries}) in {backoff} seconds...")
                        await asyncio.sleep(backoff)
                    else:
                        # Give the reserved row back before letting go of the chunk.
                        # Whether it is requeued or quarantined, this attempt is not
                        # going to produce audio, and a chapters row with no audio
                        # behind it reaches the manifest, the gateway and finally
                        # Audiobookshelf as a chapter that does not exist.
                        _release_chapter(conn, cursor, chapter_id, worker_id, base_name)
                        _discard_partials(wav_path, txt_path, nodes_path)

                        # 🚨 THE ROUND-ROBIN QUEUE JUMP
                        requeue_count = chapter.get("requeue_count", 0)

                        if requeue_count < 2:
                            # It failed 4 times. Push it to the back of the line!
                            print(f"   [W{worker_id}] 🔄 Traffic jam too thick! Pushing {base_name} to the back of the queue.")
                            chapter["requeue_count"] = requeue_count + 1
                            await audio_queue.put(chapter)
                            break # Let the worker exit this loop and grab a fresh task

                        # Twelve attempts at an unchanged request have failed.
                        # Asking a thirteenth time is what used to happen next,
                        # and it produced the same answer: 042_B_IX and
                        # 043_C_IX were each killed twelve times at the same
                        # 95-second wall and then dropped from the book. So
                        # change the request instead of repeating it -- half a
                        # chunk is half the generation time, which is the one
                        # variable actually under our control.
                        halves = split_chunk(chapter, language=chapter.get("language"))
                        if halves:
                            for n, half in enumerate(halves, start=1):
                                # Extend the label rather than the sequence
                                # number: the stitcher matches CHUNK_NAME and
                                # orders on the numeric prefix, so 042_B1_IX and
                                # 042_B2_IX stay between 041 and 043 and in the
                                # right order, while a 042a prefix would fail
                                # the pattern outright and vanish from the
                                # chapter -- which is exactly how the alphabet
                                # overflow silently dropped chunks at 27.
                                stem, label, rest = half["filename"].split("_", 2)
                                half["filename"] = f"{stem}_{label}{n}_{rest}"
                                await audio_queue.put(half)
                            print(f"   [W{worker_id}] ✂️  {base_name} failed twelve times; "
                                  f"splitting it into {len(halves)} shorter chunks and retrying "
                                  "those instead.")
                            break

                        else:
                            # It has been pushed to the back of the line twice and STILL failed.
                            #
                            # This chunk is now simply absent from the book, and
                            # for a long time nothing downstream said so: the
                            # engine reported success, the ghost packaged and
                            # uploaded, and Audiobookshelf rescanned, all while
                            # the narration had a hole in it. Measured on The
                            # Screwtape Letters (2026-09-15), where 042_B_IX and
                            # 043_C_IX both timed out twelve times and Letter IX
                            # shipped 2.4 minutes short with no warning anywhere
                            # outside one log line.
                            #
                            # A sidecar rather than a return value, matching the
                            # .truncated file above: the worker cannot reach the
                            # caller except through the queue, and a file on disk
                            # survives the worker dying, which is exactly when
                            # this most needs to be seen.
                            print(f"   [W{worker_id}] 🚨 POISON PILL DETECTED! {base_name} failed on a clear highway. Quarantining!")
                            try:
                                with open(os.path.join(mfa_dir, base_name + ".quarantined"),
                                          "w", encoding="utf-8") as f:
                                    json.dump({"chunk": base_name,
                                               "chars": len(chapter.get("text", "")),
                                               "attempts": max_retries * 3,
                                               "reason": why}, f)
                            except OSError:
                                pass  # never let the bookkeeping kill the run
                            break # Skip and move on

        except Exception:
            # The worker survives anything one chunk can throw. It used to die
            # here, and a dead worker never reaches task_done() below -- so
            # audio_queue.join() waited on it forever while the pod billed.
            print(f"   [W{worker_id}] 🚨 Chunk failed outside the retry loop; skipping it.")
            print(traceback.format_exc())
        finally:
            # 👇 THIS TELLS THE QUEUE WE ARE DONE WITH THIS CHAPTER
            audio_queue.task_done()
        if retire:
            break

    # 👇 THIS ONLY CLOSES WHEN THE QUEUE IS COMPLETELY EMPTY
    conn.close()
    if retire:
        print(f"   [W{worker_id}] 🪦 {base_url} is gone; this worker stops and its chunk "
              "goes to another pod.")
        return "pod lost"


async def _hand_back(chapter, audio_queue, conn, cursor, chapter_id, worker_id, base_name, *partials):
    """Give a chunk back to the queue because its pod is gone. Always True.

    The chunk's requeue count is not touched: the chunk did nothing wrong, and
    spending its retries on a dead machine is how a lost pod used to become
    quarantined narration.
    """
    _release_chapter(conn, cursor, chapter_id, worker_id, base_name)
    _discard_partials(*partials)
    await audio_queue.put(chapter)
    return True


def _release_chapter(conn, cursor, chapter_id, worker_id, base_name):
    """Delete a chapters row reserved for a chunk that produced no audio."""
    if chapter_id is None:
        return
    try:
        cursor.execute("DELETE FROM chapters WHERE chapter_id = ?", (chapter_id,))
        conn.commit()
    except Exception as e:
        print(f"   [W{worker_id}] ⚠️ Could not release the reserved row for {base_name}: {e}")


def _discard_partials(*paths):
    """Remove half-written sidecars so the injector never meets an orphan.

    process_mfa_factory drives off the .wav files present and then opens each
    one's _nodes.json unconditionally. A chunk that wrote its audio and then
    failed before its nodes file left exactly that orphan, and the injector
    died on it -- after the GPU had been paid for and the pod terminated.
    """
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

# --- PHASE 3: THE LIBRARIAN INJECTOR ---
def _norm(token):
    return re.sub(r"[^\w]", "", token.lower())


def map_timestamps(original_text, mfa_entries):
    """One {text,start,end} per whitespace word of `original_text`, from MFA.

    MFA's word tier does not tokenise the way str.split() does: it splits
    "e-mails", "life-change" and em-dash compounds into two entries. This used
    to pair the two lists one to one, so every word after the first such split
    took the next word's time -- on Redeeming Love 261 of 616 chunks drifted,
    4.5% of words by more than a second, worst 7.3s, paragraph starts included.

    Now each text word is broken into the pieces MFA would see and the two
    sequences are aligned: matched pieces carry their own times, a replaced
    stretch of equal length (an out-of-dictionary word MFA wrote as <unk>) is
    paired by position, and anything left over takes the time where the
    previous word ended. The output still has exactly one entry per text word,
    which is what callers slice by.
    """
    import difflib
    original_words = original_text.split()
    pieces, owner = [], []
    for i, word in enumerate(original_words):
        for piece in re.split(r"[^\w']+", word):
            if _norm(piece):
                pieces.append(_norm(piece))
                owner.append(i)
    labels = [_norm(str(e[2])) if len(e) > 2 else "" for e in mfa_entries]

    piece_time = {}
    matcher = difflib.SequenceMatcher(None, pieces, labels, autojunk=False)
    for tag, a0, a1, b0, b1 in matcher.get_opcodes():
        if tag == "equal" or (tag == "replace" and a1 - a0 == b1 - b0):
            for k in range(a1 - a0):
                piece_time[a0 + k] = mfa_entries[b0 + k]

    spans = {}
    for p, (i, entry) in enumerate(((owner[p], piece_time.get(p)) for p in range(len(pieces)))):
        if entry is None:
            continue
        first, last = spans.get(i, (entry, entry))
        spans[i] = (first, entry)

    word_timestamps = []
    for i, word in enumerate(original_words):
        if i in spans:
            first, last = spans[i]
            word_timestamps.append({"text": word, "start": first[0], "end": last[1]})
        else:
            t = word_timestamps[-1]["end"] if word_timestamps else 0.0
            word_timestamps.append({"text": word, "start": t, "end": t})
    return word_timestamps

def inject_chunk_map(cursor, chapter_id, nodes, full_text, mfa_entries, label=""):
    """Write one chunk's sync nodes and word timings, on the chunk's own clock.

    Shared by the engine (process_mfa_factory) and tools/rebuild_map.py, so a
    map rebuilt from alignments already on disk comes from the same code as
    one built during a run.
    """
    word_times = map_timestamps(full_text, mfa_entries)
    current_word_idx = 0
    for i, node in enumerate(nodes):
        word_count = len(node["text"].split())
        if word_count == 0:
            continue
        node_slice = word_times[current_word_idx: current_word_idx + word_count]
        start_sec = node_slice[0]["start"] if node_slice else 0.0
        end_sec = node_slice[-1]["end"] if node_slice else start_sec
        if i == 0:
            start_sec = 0.0   # the chunk's first node starts with the chunk

        cursor.execute('''
            INSERT INTO sync_nodes (chapter_id, xpath_string, start_time, end_time, text_content)
            VALUES (?, ?, ?, ?, ?)
        ''', (chapter_id, node["xpath"], start_sec, end_sec, node["text"]))

        # Offsets are into this node's own text, which is what the gateway's
        # prepare_kobo_xpath indexes. finditer rather than split() because
        # split() discards positions; the two agree on which substrings are words.
        node_id = cursor.lastrowid
        node_spans = [(m.start(), m.end()) for m in re.finditer(r"\S+", node["text"])]
        if len(node_spans) == len(node_slice):
            cursor.executemany('''
                INSERT INTO word_timings
                    (node_id, word_index, word, char_start, char_end, start_time, end_time)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', [(node_id, wi, w["text"], span[0], span[1], w["start"], w["end"])
                  for wi, (w, span) in enumerate(zip(node_slice, node_spans))])
        else:
            print(f"   ‼️  {label}: {len(node_spans)} words in node {i} but "
                  f"{len(node_slice)} timings; skipping word-level sync for it.")
        current_word_idx += word_count


def process_mfa_factory(mfa_dir, audio_dir, db_path, librarian_id, book_title, book_author):
    """Converts WAV to MP3, stamps ID3, and mathematically maps sync nodes."""
    print("\n📚 Phase 3: Librarian Injector Started...")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    output_folder = os.path.join(mfa_dir, "output")
    if not os.path.exists(output_folder):
        print("❌ Error: MFA output folder missing.")
        return

    # Iterate the chunks that were narrated, not the alignments MFA returned.
    # Driving this loop off output_folder meant a chunk MFA skipped never had
    # its audio copied into audio_dir at all, so it was dropped from the
    # finished chapter -- an alignment failure silently deleted narration. On
    # "A Doll's House" that cost 18.8 minutes of paid audio, and the warning
    # upstream told us the opposite ("the audio exists; only the mapping is
    # missing"). Audio placement is now unconditional; alignment only decides
    # whether the chunk also gets fine-grained sync nodes.
    chunk_names = sorted(f[:-4] for f in os.listdir(mfa_dir) if f.endswith(".wav"))

    # Drop chapter rows left behind by earlier attempts at this same book. A row
    # is inserted per chunk per attempt, and nothing ever removed them, so a
    # book that was narrated twice before it finished carried one live set plus
    # a set of phantoms per abandoned run -- rows with a NULL duration, no
    # audio, and no sidecar pointing at them. They travelled into the manifest
    # and on to the gateway, which rebuilds its chapters from exactly that list.
    # Measured on "The Yellow Wallpaper": 51 rows for a 17-chunk book.
    #
    # The live set is defined by the sidecars, not by recency: the _nodes.json
    # written beside each chunk carries the chapter_id its nodes belong to, so
    # deleting anything the sidecars still reference would orphan the map this
    # run is about to build. Placed here, next to the audio sweep below, for
    # the same reason -- by now the narration exists and is being rebuilt, so a
    # run that dies earlier leaves the previous book's rows intact.
    live_chapter_ids = set()
    for base_name in chunk_names:
        nodes_path = os.path.join(mfa_dir, f"{base_name}_nodes.json")
        if not os.path.exists(nodes_path):
            continue
        try:
            with open(nodes_path, 'r', encoding='utf-8') as f:
                live_chapter_ids.add(json.load(f)["chapter_id"])
        except (ValueError, KeyError):
            # A truncated sidecar is handled in the main loop below, which
            # reports it per chunk. It must not widen this delete.
            continue

    book_row = cursor.execute(
        "SELECT book_id FROM books WHERE lib_id = ?", (librarian_id,)).fetchone()
    if book_row and live_chapter_ids:
        placeholders = ",".join("?" * len(live_chapter_ids))
        stale = [r[0] for r in cursor.execute(
            # nosec B608: only "?" placeholders are interpolated.
            f"""SELECT chapter_id FROM chapters
                WHERE book_id = ? AND chapter_id NOT IN ({placeholders})""",  # nosec B608
            (book_row[0], *live_chapter_ids))]
        if stale:
            stale_placeholders = ",".join("?" * len(stale))
            cursor.execute(
                f"DELETE FROM sync_nodes WHERE chapter_id IN ({stale_placeholders})", stale)  # nosec B608
            cursor.execute(
                f"DELETE FROM chapters WHERE chapter_id IN ({stale_placeholders})", stale)  # nosec B608
            conn.commit()
            print(f"   🧹 Removed {len(stale)} phantom chapter row(s) from earlier attempts.")

    for base_name in chunk_names:
        mfa_json_path = os.path.join(output_folder, f"{base_name}.json")
        txt_path = os.path.join(mfa_dir, f"{base_name}.txt")
        wav_path = os.path.join(mfa_dir, f"{base_name}.wav")
        nodes_path = os.path.join(mfa_dir, f"{base_name}_nodes.json")
        chunk_wav_path = os.path.join(audio_dir, f"{base_name}.wav")

        # Stay lossless until the whole chapter is assembled. Encoding each
        # chunk to MP3 here and concatenating with -c copy meant every junction
        # carried MP3 encoder delay and padding -- roughly a frame, ~26ms, at
        # every one of hundreds of joins in a long book. That is cumulative
        # drift against a sync map built from exact per-chunk timings, and it
        # is also what made smaller chunks impossible: more joins, more drift.
        #
        # Loudness is handled once, on the finished chapter, in
        # stitch_audio_chapters. Normalising per chunk gave each one its own
        # gain -- and so its own noise floor, audibly rising and falling
        # between chunks.
        pristine_path = wav_path.replace(".wav", ".pristine")
        source_wav = pristine_path if os.path.exists(pristine_path) else wav_path
        clone_or_copy(source_wav, chunk_wav_path)

        # A .wav with no sidecar is not a chunk. This used to be an
        # unconditional open, so one orphan -- a worker that wrote its audio
        # and died before its nodes file, or an ffmpeg temp that happened to
        # end in .wav -- raised FileNotFoundError and took the whole run down
        # at the last step, after the GPU had been paid for.
        if not os.path.exists(nodes_path):
            print(f"   ⚠️  {base_name} has audio but no node sidecar — skipping it.")
            continue

        with open(nodes_path, 'r', encoding='utf-8') as f:
            node_data = json.load(f)
        chapter_id = node_data["chapter_id"]
        nodes = node_data["nodes"]

        # Measure the chunk from the audio, not from MFA's "end". The global
        # timeline is built by summing these durations, so taking the length
        # from an alignment that does not exist left the chunk at 0.0s and
        # shifted every position after it.
        audio_seconds = probe_duration(source_wav)

        if not os.path.exists(mfa_json_path):
            cursor.execute('UPDATE chapters SET duration = ? WHERE chapter_id = ?',
                           (audio_seconds, chapter_id))
            conn.commit()
            print(f"   ⚠️  No alignment for {base_name} — audio kept ({audio_seconds:.1f}s), no sync nodes.")
            continue

        with open(txt_path, 'r', encoding='utf-8') as f:
            full_text = f.read().strip()
        with open(mfa_json_path, 'r', encoding='utf-8') as f:
            mfa_data = json.load(f)

        try:
            mfa_entries = mfa_data["tiers"]["words"]["entries"]
        except KeyError:
            cursor.execute('UPDATE chapters SET duration = ? WHERE chapter_id = ?',
                           (audio_seconds, chapter_id))
            conn.commit()
            print(f"   ‼️  MFA produced no word tier for {base_name} — audio kept, no sync nodes.")
            continue

        inject_chunk_map(cursor, chapter_id, nodes, full_text, mfa_entries, base_name)
        chapter_duration = audio_seconds

        # Update Duration
        cursor.execute('UPDATE chapters SET duration = ? WHERE chapter_id = ?', (chapter_duration, chapter_id))
        conn.commit()
        print(f"   💉 Injected perfect Sync & MP3 metadata for: {base_name}")

    conn.close()

# --- PHASE 3.5: THE CHAPTER STITCHER ---
# A chunk's file name: its sequence number, a label, its chapter's title.
# Any number of digits: the engine writes "{n:03d}", which is four digits from
# chunk 1,000 on. The stitcher matched exactly three, and Twisted (2,045 chunks
# with the director, 2026-10-07) lost every chunk from 1,000: 6.6 of its 13.5
# hours, uploaded without a word of warning.
CHUNK_NAME = re.compile(r"^(\d+)_([^_]+)_(.+)\.(?:wav|mp3)$")


def chunk_order(filename):
    """Sort key: by sequence number, as a number. As text, "1000_A" sorts
    between "100_A" and "101_A"."""
    m = CHUNK_NAME.match(filename)
    return (int(m.group(1)), filename) if m else (float("inf"), filename)


def stitch_audio_chapters(audio_dir, book_title, book_author, librarian_id):
    """Join a chapter's chunks into one file: concatenate, normalise, encode once.

    Everything upstream stays lossless WAV so this is the only place audio is
    encoded. Two reasons.

    Drift: the previous version encoded each chunk to MP3 and concatenated with
    -c copy. MP3 frames carry encoder delay and padding, so every junction shed
    or gained roughly a frame (~26ms) against a sync map built from exact
    per-chunk timings, and a long book has hundreds of junctions. Concatenating
    PCM is sample-exact, which also makes chunk size a free parameter instead of
    a drift budget.

    Noise: loudness is corrected once, here, on the finished chapter. Doing it
    per chunk gave every chunk its own gain and therefore its own noise floor --
    with a model that is quiet and gets quieter as input grows, that is audible
    as the background rising and falling between chunks.
    """
    print("\n🪡 Phase 3.5: Stitching Sub-Chunks into Clean Chapters for Audiobookshelf...")
    wav_files = sorted((f for f in os.listdir(audio_dir) if CHUNK_NAME.match(f) and f.endswith(".wav")),
                       key=chunk_order)
    started_at = time.monotonic()
    # Measured before stitching, which deletes each chunk once it is in a chapter.
    stray = [f for f in os.listdir(audio_dir) if f.endswith(".wav") and not CHUNK_NAME.match(f)]
    narrated = sum(probe_duration(os.path.join(audio_dir, f)) for f in wav_files)

    # Group consecutive runs of one title, in file order. Consecutive, not
    # "everything sharing a title": two separated sections called "Notes"
    # would otherwise be merged into one chapter, played out of order, while
    # the sync map kept them apart. [^_]+ accepts the old non-alphabetic labels
    # past the 26th chunk as well as the spreadsheet-style ones.
    chapter_groups = []
    for filename in wav_files:
        match = CHUNK_NAME.match(filename)
        if match:
            base_title = match.group(3).strip()
            if chapter_groups and chapter_groups[-1][0] == base_title:
                chapter_groups[-1][1].append(filename)
            else:
                chapter_groups.append((base_title, [filename]))

    def stitch_one(chapter_index, base_title, files):
        """Concatenate, normalise and encode one chapter. Returns a report line."""
        clean_filename = f"Chapter_{chapter_index:02d}_{base_title}.mp3"
        final_path = os.path.join(audio_dir, clean_filename)

        # Named per chapter, not the shared "concat.txt" this used to write.
        # With chapters stitched one at a time a single scratch file was fine;
        # in parallel every chapter would overwrite the same list and each
        # ffmpeg would encode whichever chapter's chunks happened to be in it
        # at the moment it read the file -- silently, since the result is a
        # perfectly valid MP3 of the wrong audio.
        concat_path = os.path.join(audio_dir, f".concat_{chapter_index:03d}.txt")
        with open(concat_path, "w", encoding="utf-8") as f:
            for wav in files:
                escaped = wav.replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")

        try:
            # One pass: concatenate PCM, apply a single EBU R128 correction
            # across the whole chapter, encode once.
            result = subprocess.run([
                'ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', concat_path,
                '-af', 'loudnorm=I=-19:TP=-2:LRA=11',
                '-b:a', '192k', final_path
            ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

            if result.returncode != 0 or not os.path.exists(final_path):
                return (False, f"   ‼️  Failed to stitch {clean_filename}: "
                               f"{result.stderr.decode()[-300:]}")

            try:
                tags = ID3(final_path)
            except ID3NoHeaderError:
                tags = ID3()
            tags.add(TIT2(encoding=3, text=[base_title]))
            from mutagen.id3 import TALB
            tags.add(TALB(encoding=3, text=[book_title]))
            tags.add(TPE1(encoding=3, text=[book_author]))
            tags.add(TXXX(encoding=3, desc='ASIN', text=[librarian_id]))
            # Track order stated, not left to filename sorting in ABS.
            tags.add(TRCK(encoding=3, text=[f"{chapter_index}/{len(chapter_groups)}"]))
            tags.save(final_path)

            for wav in files:
                os.remove(os.path.join(audio_dir, wav))
            return (True, f"   🪡 Stitched {len(files)} chunks -> {clean_filename}")
        finally:
            if os.path.exists(concat_path):
                os.remove(concat_path)

    # Chapters in parallel, because a 16-hour book spends twenty minutes here
    # and the work is embarrassingly parallel. Measured on real chunks:
    # 48x realtime for the whole pass, of which `loudnorm` is 28 of 36 seconds
    # -- and that filter is single-threaded, so one chapter can never use more
    # than one core however many the machine has.
    #
    # Bounded by cores rather than by chapter count: nine ffmpeg processes on
    # eight cores is fine, ninety would thrash. Each one streams, so the memory
    # cost is per-process and small even on an 8GB machine.
    jobs = list(enumerate(chapter_groups, start=1))
    lanes = max(1, min(len(jobs), (os.cpu_count() or 4)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=lanes) as pool:
        futures = [pool.submit(stitch_one, i, title, files)
                   for i, (title, files) in jobs]
        # Reported in chapter order rather than completion order: the log is
        # read to find which chapter failed, and a shuffled list makes that
        # harder for no benefit.
        for f in futures:
            ok, line = f.result()
            print(line)

    if jobs:
        print(f"   🪡 {len(jobs)} chapter(s) stitched on {lanes} lane(s) in "
              f"{time.monotonic() - started_at:.0f}s.")

    # The chapters must hold every chunk. Checked here, where both sides are
    # known, because a short book is a valid MP3 set that nothing downstream
    # can tell from a whole one: Twisted lost 6.6 of 13.5 hours and the run
    # still said SUCCESS (2026-10-07).
    stitched = sum(probe_duration(os.path.join(audio_dir, f)) for f in os.listdir(audio_dir)
                   if f.startswith("Chapter_") and f.endswith(".mp3"))
    if stray or abs(stitched - narrated) > max(2.0, 0.001 * narrated):
        raise RuntimeError(
            f"The stitched chapters hold {stitched:.1f}s of the {narrated:.1f}s narrated"
            + (f"; {len(stray)} chunk file(s) were not recognised, e.g. {stray[0]}" if stray else "")
            + ". Nothing was uploaded; the narration is still in mfa_workspace/.")
    print(f"   ✅ The chapters hold every chunk: {stitched:.1f}s of {narrated:.1f}s narrated.")
