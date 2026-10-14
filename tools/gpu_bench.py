#!/usr/bin/env python3
"""What a GPU really costs per audiobook hour, measured on the real narrator.

Voxie's promise (user, 2026-10-08): a 16-hour novel narrated for under $1.
Twisted broke it at $1.70 for 13.5 h, on three secure pods, one of them an
H100. This rents one pod per card from the public template, renders the same
real workload on each (paragraphs of a book with the narration director's own
deliveries, packed exactly as the engine packs them), and reports per card:

  price ($/h, as RunPod billed it), boot (deploy to first answer), realtime
  speed, dollars per audio hour, and what a 16-hour book would cost on one
  pod and on two, boot included. A few takes per card go through Whisper, so
  a fast card that mumbles cannot win.

Every pod is registered at deploy time and terminated whatever happens.

    python tools/gpu_bench.py --epub ~/Kobo_Archive/book.epub \\
        --cards "NVIDIA GeForce RTX 4090:SECURE,NVIDIA RTX A5000:COMMUNITY" --chunks 160
    python tools/gpu_bench.py --report docs/measurements/gpu_bench.json   # the table again

Needs RUNPOD_API_KEY, TTS_API_TOKEN, a voice (COSY_VOICE) and, for the
director, an OpenRouter key; RUNPOD_TEMPLATE_ID or the public template.
"""
import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))     # before any core import (CLAUDE.md)
import httpx  # noqa: E402

from core import director, pod_pool  # noqa: E402
from core.chunking import chunk_text, pack_blocks  # noqa: E402
from core.narration import narrate_text  # noqa: E402
from core.parser import parse_xhtml  # noqa: E402
from core.structure import narratable_blocks  # noqa: E402
from core.tts_providers import get_provider  # noqa: E402

KEY = os.environ.get("RUNPOD_API_KEY", "")
TOKEN = os.environ.get("TTS_API_TOKEN", "")
BOOK_HOURS = 16.0
RESULTS = os.path.join(ROOT, "docs", "measurements", "gpu_bench.json")   # kept: core/cost_plan.SPEED cites it
# Boot logs carry uvicorn's access lines (client addresses): kept out of git.
BOOT_LOGS = os.path.join(ROOT, "reports", "gpu_bench")
# A server that failed this many requests in a row, all on connecting or
# reading, has stopped (an OOM-killed uvicorn answers nothing): the card ends
# rather than retrying every chunk three times at 300 s each. The A6000 and
# the 4090 on 2026-10-07 billed about $1 that way and left no result.
DEAD_AFTER = 8


def template_id():
    from libra_cli.main import RUNPOD_PUBLIC_TEMPLATE_ID
    return (os.environ.get("RUNPOD_TEMPLATE_ID") or "").split("#")[0].strip() or RUNPOD_PUBLIC_TEMPLATE_ID


# --- the workload ------------------------------------------------------------
def workload(epub, n, seed, directed=True, cache=None):
    """[(direction or None, text)]: the engine's own chunks for a slice of the book."""
    if cache and os.path.exists(cache):
        rows = json.load(open(cache))
        return [(director.Direction(**d) if d else None, t) for d, t in rows][:n]
    texts = []
    with zipfile.ZipFile(epub) as z:
        for name in sorted(z.namelist()):
            if not name.lower().endswith((".xhtml", ".html")):
                continue
            units = narratable_blocks(parse_xhtml(z.read(name)))
            raw = [t for _, t in units if t]
            for i, t in enumerate(raw):
                said = narrate_text("p", t, raw[i + 1] if i + 1 < len(raw) else None, "en")
                if said and len(said.split()) >= 4:
                    texts.append(said)
    start = random.Random(seed).randrange(0, max(1, len(texts) - 6 * n))
    texts = texts[start:start + 6 * n]
    if directed:
        from core.junk_provider import JevProvider
        async def direct():
            jev = JevProvider()
            try:
                return await director.direct_blocks(texts, "en", jev)
            finally:
                await jev.aclose()
        dirs = asyncio.run(direct())
    else:
        dirs = [director.PLAIN] * len(texts)
    chunks = []
    for d, run in director.group_by_direction(list(enumerate(texts)), dirs):
        for packed in pack_blocks(run, language="en"):
            chunks.append((None if d.plain else d, chunk_text(packed)))
    chunks = chunks[:n]
    if cache:
        json.dump([(d.as_dict() if d else None, t) for d, t in chunks], open(cache, "w"))
    return chunks


# --- one pod -------------------------------------------------------------------
async def gql(client, query):
    r = await client.post(f"https://api.runpod.io/graphql?api_key={KEY}", json={"query": query}, timeout=60)
    return r.json()


async def deploy(client, gpu, cloud):
    env = pod_pool.pod_env(api_workers=1, token=TOKEN)
    q = f'''mutation {{ podFindAndDeployOnDemand(input: {{ cloudType: {cloud}, gpuCount: 1,
        gpuTypeId: "{gpu}", supportPublicIp: true, {env} volumeInGb: 0, containerDiskInGb: 50,
        name: "voxie-gpu-bench", templateId: "{template_id()}" }}) {{ id costPerHr }} }}'''
    d = await gql(client, q)
    pod = (d.get("data") or {}).get("podFindAndDeployOnDemand")
    if not pod:
        raise RuntimeError((d.get("errors") or [{}])[0].get("message", "no pod"))
    pod_pool._register(pod["id"], f"gpu bench {gpu} {cloud}")
    return pod["id"], float(pod["costPerHr"])


async def tcp_url(client, pod_id, deadline, port="8000"):
    while time.monotonic() < deadline:
        try:
            r = (await client.get(f"https://rest.runpod.io/v1/pods/{pod_id}",
                                  headers={"Authorization": f"Bearer {KEY}"}, timeout=30)).json()
            ip, mapped = r.get("publicIp"), (r.get("portMappings") or {}).get(port)
            if ip and mapped:
                return f"http://{ip}:{mapped}"
        except (httpx.HTTPError, ValueError):
            pass
        await asyncio.sleep(10)
    raise RuntimeError(f"no public port {port} within the boot budget")


async def boot_log(client, pod_id, gpu):
    """The pod's own log (port 8001, docker/cosyvoice/start.sh), saved under reports/.

    Returns its last lines, or why it could not be read. RunPod keeps no
    container log: this is the only account of a pod that stopped serving.
    """
    try:
        url = await tcp_url(client, pod_id, time.monotonic() + 60, port="8001")
        text = (await client.get(f"{url}/boot.log", timeout=30)).text
    except Exception as e:      # noqa: BLE001 -- a missing log is reported, never fatal
        return f"(no boot log: {type(e).__name__}: {str(e)[:80]})"
    os.makedirs(BOOT_LOGS, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9]+", "-", gpu).strip("-")
    path = os.path.join(BOOT_LOGS, f"{name}-{time.strftime('%Y%m%dT%H%M%S')}.log")
    open(path, "w").write(text)
    print(f"   boot log of {gpu}: {path}", flush=True)
    return "\n".join(text.splitlines()[-25:])


async def ready(client, url, deadline):
    while time.monotonic() < deadline:
        try:
            r = await client.get(f"{url}/health", timeout=15)
            if r.status_code == 200 and "direction" in (r.json().get("features") or []):
                return
        except (httpx.HTTPError, ValueError):
            pass
        await asyncio.sleep(10)
    raise RuntimeError("the server never answered /health")


def wav_seconds(raw):
    # CosyVoice answers 32-bit float WAVs, which the wave module refuses
    # ("unknown format: 3"): the first benchmark lost every card to that.
    from core.tts_worker import wav_duration_from_bytes
    return wav_duration_from_bytes(raw)


async def render(provider, url, chunks, callers, budget_s=1800, timeout_s=300):
    """Render the workload. Returns (wall, audio seconds, failures, takes, requests, stopped).

    `requests` is every attempt as (started, seconds, outcome), relative to the
    start; `stopped` says why the card ended early (its time budget, or a
    server that stopped answering), else None.
    """
    queue = list(enumerate(chunks))
    audio, failures, takes, requests = 0.0, 0, {}, []
    stopped = None
    started = time.monotonic()
    deadline = started + budget_s

    def gone():
        tail = [o for _, _, o in requests[-DEAD_AFTER:]]
        return len(tail) == DEAD_AFTER and all(o.split(":")[0] in DEAD_ERRORS for o in tail)

    async def worker():
        nonlocal audio, failures, stopped
        async with httpx.AsyncClient() as c:
            while queue and not stopped:
                if time.monotonic() > deadline:
                    stopped = f"time budget of {budget_s:.0f} s spent, {len(queue)} chunk(s) not rendered"
                    break
                i, (d, text) = queue.pop(0)
                for attempt in range(3):
                    t = time.monotonic()
                    outcome = "ok"
                    try:
                        r = await provider.synthesize(c, url, text, timeout=timeout_s, direction=d)
                        if r.status_code == 200 and provider.is_valid_response(r.content):
                            audio += wav_seconds(r.content)
                            if len(takes) < 4 and i % 7 == 0:
                                takes[i] = r.content
                        else:
                            outcome = f"HTTP {r.status_code}"
                    except httpx.HTTPError as e:
                        outcome = f"{type(e).__name__}: {str(e)[:60]}"
                    except Exception as e:      # noqa: BLE001 -- one bad take must not end the card
                        outcome = f"{type(e).__name__}: {str(e)[:60]}"
                    requests.append((round(t - started, 1), round(time.monotonic() - t, 1), outcome))
                    if outcome == "ok":
                        break
                    print(f"   take {i}, attempt {attempt + 1}: {outcome}", flush=True)
                    if gone():
                        stopped = stopped or f"the server stopped answering ({DEAD_AFTER} requests in a row: {outcome})"
                        break
                    await asyncio.sleep(2)
                else:
                    failures += 1
    await asyncio.gather(*(worker() for _ in range(callers)))
    return time.monotonic() - started, audio, failures, takes, requests, stopped


# The failures that mean the server process is gone, not that one take was bad.
DEAD_ERRORS = {"ConnectError", "ConnectTimeout", "ReadError", "RemoteProtocolError", "ReadTimeout"}


def summarise(requests):
    """What the requests say: how many, how long, which failures and when the first came."""
    secs = sorted(s for _, s, o in requests if o == "ok")
    errors = {}
    for _, _, o in requests:
        if o != "ok":
            errors[o.split(":")[0]] = errors.get(o.split(":")[0], 0) + 1
    first = next(((t, o) for t, _, o in requests if o != "ok"), None)
    return {"requests": len(requests),
            "p50_s": secs[len(secs) // 2] if secs else None, "max_s": secs[-1] if secs else None,
            "errors": errors, "first_error": f"at {first[0]} s: {first[1]}" if first else None}


def intelligible(takes, chunks):
    """Mean share of each take's words Whisper hears, or None without Whisper."""
    try:
        import mlx_whisper
        from core.hf_cache import model_path
    except ImportError:
        return None
    import difflib
    import tempfile
    words = lambda s: re.findall(r"[\w']+", re.sub(r"\[[a-z_]+\]", "", s.lower()))  # noqa: E731
    shares = []
    for i, raw in takes.items():
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            f.write(raw)
            f.flush()
            heard = mlx_whisper.transcribe(f.name, path_or_hf_repo=model_path("mlx-community/whisper-large-v3-turbo"),
                                           language="en")["text"]
        want = words(chunks[i][1])
        m = difflib.SequenceMatcher(None, want, words(heard), autojunk=False)
        shares.append(sum(b.size for b in m.get_matching_blocks()) / max(1, len(want)))
    return round(sum(shares) / len(shares), 3) if shares else None


async def bench_card(gpu, cloud, chunks, callers, boot_budget, render_budget=1800):
    row = {"gpu": gpu, "cloud": cloud, "callers": callers, "chunks": len(chunks)}
    async with httpx.AsyncClient() as client:
        t0 = time.monotonic()
        try:
            pod_id, price = await deploy(client, gpu, cloud)
        except RuntimeError as e:
            row["error"] = f"deploy: {e}"
            return row
        row.update(pod=pod_id, price=price)
        try:
            deadline = t0 + boot_budget
            url = await tcp_url(client, pod_id, deadline)
            await ready(client, url, deadline)
            row["boot_s"] = round(time.monotonic() - t0, 1)
            provider = get_provider("cosyvoice", language="en")
            wall, audio, failures, takes, requests, stopped = await render(
                provider, url, chunks, callers, render_budget)
            row.update(summarise(requests))
            if stopped:
                # A card that did not finish has no speed: a partial one would
                # flatter a card that dies under load.
                row.update(error=stopped, wall_s=round(wall, 1), audio_s=round(audio, 1), failures=failures)
            else:
                row.update(wall_s=round(wall, 1), audio_s=round(audio, 1), failures=failures,
                           realtime=round(audio / wall, 2) if wall else 0)
                row["heard"] = intelligible(takes, chunks)
        except Exception as e:      # noqa: BLE001 -- a card's failure is a result, not a crash
            row["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        finally:
            if row.get("error") or row.get("failures"):
                row["boot_log_tail"] = await boot_log(client, pod_id, gpu)
            row["billed_s"] = round(time.monotonic() - t0, 1)
            row["terminated"] = await pod_pool.terminate(pod_id)
    return row


def project(row):
    """$ per audio hour of work, and a 16 h book on one and on two pods, boot included."""
    if not row.get("realtime"):
        return row
    hours_work = BOOK_HOURS / row["realtime"]
    boot_h = row["boot_s"] / 3600
    row["usd_per_audio_h"] = round(row["price"] / row["realtime"], 4)
    row["book_1pod_usd"] = round(row["price"] * (hours_work + boot_h), 3)
    row["book_1pod_min"] = round(60 * (hours_work + boot_h), 1)
    row["book_2pod_usd"] = round(row["price"] * (hours_work + 2 * boot_h), 3)
    row["book_2pod_min"] = round(60 * (hours_work / 2 + boot_h), 1)
    return row


def table(rows):
    print(f"\n{'card':34s} {'cloud':9s} {'$/h':>5s} {'boot':>6s} {'xRT':>6s} {'heard':>6s} "
          f"{'16h 1 pod':>15s} {'16h 2 pods':>15s}")
    for r in sorted(rows, key=lambda r: r.get("book_1pod_usd", 99)):
        if r.get("error"):
            print(f"{r['gpu'][-34:]:34s} {r['cloud']:9s} {r.get('price', 0):5.2f}  -- {r['error'][:60]}")
            continue
        print(f"{r['gpu'][-34:]:34s} {r['cloud']:9s} {r['price']:5.2f} {r['boot_s']/60:5.1f}m {r['realtime']:6.1f} "
              f"{str(r.get('heard')):>6s} ${r['book_1pod_usd']:.2f} {r['book_1pod_min']:5.0f}m "
              f"${r['book_2pod_usd']:.2f} {r['book_2pod_min']:5.0f}m")


async def main_async(args, chunks):
    cards = [c.rsplit(":", 1) for c in args.cards.split(",")]
    print(f"workload: {len(chunks)} chunks, {sum(len(t) for _, t in chunks):,} characters, "
          f"{sum(1 for d, _ in chunks if d)} directed", flush=True)
    async def one(gpu, cloud):
        row = project(await bench_card(gpu, cloud, chunks, args.callers, args.boot_budget,
                                       args.render_budget))
        # Saved as each card finishes: a run killed half-way keeps what it paid for.
        os.makedirs(os.path.dirname(RESULTS), exist_ok=True)
        old = json.load(open(RESULTS)) if os.path.exists(RESULTS) else []
        # Committed: no account details, and no boot log (client addresses).
        kept = {k: v for k, v in row.items() if k not in ("pod", "boot_log_tail")}
        if row.get("boot_log_tail"):
            print(f"\n--- the last lines of {gpu}'s boot log ---\n{row['boot_log_tail']}\n---", flush=True)
        json.dump(old + [{**kept, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "plain": args.plain}],
                  open(RESULTS, "w"), indent=1)
        table([row])
        return row
    try:
        rows = await asyncio.gather(*(one(g, c) for g, c in cards))
    finally:
        await pod_pool.sweep_orphans(verbose=False)
    table(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epub")
    ap.add_argument("--cards", default="NVIDIA GeForce RTX 4090:SECURE")
    ap.add_argument("--chunks", type=int, default=160)
    ap.add_argument("--callers", type=int, default=8)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--plain", action="store_true", help="no director: the engine's undirected chunks")
    ap.add_argument("--workload", help="cache the workload here (reuse it across runs)")
    ap.add_argument("--boot-budget", type=float, default=1500, help="seconds from deploy to ready")
    ap.add_argument("--render-budget", type=float, default=1800,
                    help="seconds a card may spend on the workload before it ends unfinished")
    ap.add_argument("--report", help="print the table from a results file and exit")
    args = ap.parse_args()
    if args.report:
        table([r for r in json.load(open(args.report)) if not r.get("plain")])
        return
    # Before the event loop: workload() runs its own (the director's calls),
    # and asyncio.run inside a running loop refuses. It passed only while the
    # workload came from a cache.
    # A missing voice used to fail on the first request, after a 4090 had
    # booted for nine minutes ($0.14, 2026-10-10): ask before renting.
    get_provider("cosyvoice", language="en")
    chunks = workload(args.epub, args.chunks, args.seed, not args.plain, cache=args.workload)
    asyncio.run(main_async(args, chunks))


if __name__ == "__main__":
    main()
