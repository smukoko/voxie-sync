import zipfile
import os
import sys
import re
import json
import asyncio
import argparse
import sqlite3
import time
import traceback
import httpx
import subprocess
from lxml import etree
import shutil

# Before any core import. core.tts_providers reads COSY_MAX_WORKERS and the
# template id when its classes are defined, and under launchd nothing else
# loads .env first: the unattended runs of 2026-09 used 4 workers per pod
# instead of the configured 8, and the chunk cache could not tell servers apart.
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from core.database import setup_database, register_book, apply_global_time_offsets
from core.fingerprint import koreader_partial_md5
from core.junk_provider import get_junk_provider, build as build_junk_provider
from core import block_filter, library
from core.structure import (boilerplate_phrases_in, structural_junk,
                            trim_boilerplate_blocks, say_chapter_numbers,
                            drop_print_artifacts, drop_picture_text,
                            chapter_starts, chapter_segments, untitled_document_title,
                            read_toc, drop_gutenberg_front_matter, drop_gutenberg_title_page, drop_printed_contents,
                            narratable_blocks,
                            drop_repeated_title, trim_credits, opening_credit,
                            is_dedication, publisher_furniture)
from core.tts_worker import (runpod_worker, process_mfa_factory,
                             stitch_audio_chapters, pod_chunk_key)
from core.tts_providers import get_provider, PROVIDERS
from core import director
from core.junk_provider import JevProvider, spend as junk_spend
from core.languages import (
    DEFAULT_LANGUAGE, NOT_ENGLISH, detect as detect_language, is_english,
    mfa_downloads, mfa_files,
)
from core.parser import (
    convert_to_kepub, canonicalise_epub, content_identity,
    get_zip_path, get_koreader_xpath_lxml,
    extract_epub_metadata, stamp_epub_watermark, parse_xhtml, sample_text
)
from core.chunking import pack_blocks, chunk_text
from core.narration import narrate_text, load_errata, apply_errata
from core import cost_plan, pod_pool, progress, narration_check
from core import chunk_cache
from core import drm, platform_support
# Set from --voice at startup; None means "use COSY_VOICE or the only
# voice whose language matches the book".
VOICE_CHOICE = None

RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")
RUNPOD_TEMPLATE_ID = os.getenv("RUNPOD_TEMPLATE_ID")
RUNPOD_URL_PATTERN = os.getenv("RUNPOD_URL_PATTERN")

# Sized for the prebuilt image, which carries the venv and the weights already.
#
# These were 50/50 for the old template, whose start command pip-installed vLLM
# and downloaded ~8GB of weights at boot -- asking for less ran that install out
# of disk, and the only symptom was a health check that never went green. None
# of that happens now: ghcr.io/smukoko/voxtral-tts ships the 18GB venv and the
# 7.6GB of weights baked in, so the container disk only has to hold the image.
#
# The volume is the part that mattered. A persistent volume is an extra
# placement constraint -- the host needs that much free persistent storage --
# and it buys nothing here, because nothing on the pod needs to survive it.
# Measured 2026-09-12, after seven tiers reported "no instances available" on
# two consecutive real drops: volumeInGb 50 placed zero times across every card
# and all three cloud types, while the identical request at volumeInGb 0 placed
# immediately, including on the L40S we actually want. Capacity fluctuates
# minute to minute, so treat any single probe as noise -- but the volume asked
# for storage the run never reads or writes, and paid for it in placements.
#
# 30 rather than 45, and that is a placement decision rather than a storage one.
# The "~26GB" this was sized against was the Voxtral image; the CosyVoice one is
# 6.85GB compressed across 23 layers and roughly 15GB unpacked, plus ~2GB of
# weights fetched at boot and a book's worth of retained chunks. 30 covers that
# with room and stops asking every host for 20GB it will never touch.
#
# It matters because free disk is a filter on which machines can take the pod at
# all, and it bites hardest exactly where the cheap capacity is: the community
# cloud is half the price of secure and its machines are smaller. This is the
# same lesson as the network volume, which placed zero times at 50GB and
# instantly at 0.
POD_CONTAINER_DISK_GB = int(os.getenv("POD_CONTAINER_DISK_GB", "30"))

# Which halves of the fleet to search, in order.
#
# The community cloud is roughly half the price -- a 4090 at $0.34 against
# $0.74, an L40S at $0.79 against $1.09 -- and cloudType ALL does NOT mean
# "cheapest available", it means "anywhere", so it had been quietly buying the
# expensive half on every pod of every book. Searching COMMUNITY first is
# therefore most of a book's bill.
#
# It is nevertheless NOT the default on the TCP transport, and that is measured
# rather than cautious. On 2026-09-21, five community pods across three cards
# and two container-disk sizes all rented, reported RUNNING, and never started
# a container: `publicIp: ''`, `portMappings: null`, `uptimeInSeconds: null`,
# right up to the 600s port-mapping budget. The three that served that day were
# all secure. TCP reaches the server at a raw public ip:port, and community
# machines largely cannot publish one -- so on tcp, searching community first
# buys nothing and costs ten minutes per pod before falling through.
#
# The proxy transport needs no public IP, so there the cheap half is tried
# first. Override with RUNPOD_CLOUD_ORDER (comma-separated) to force either.
# See SAC-72 for the experiment that would let tcp use community too.
# Read from the environment rather than RUNPOD_TRANSPORT, which is defined
# further down this file -- the two must agree, and the constant below is the
# one that moves.
#
# Measured again 2026-10-07 (tools/gpu_bench.py): a community RTX 3090 with
# supportPublicIp got no port in 25 minutes in one run, and a port but no
# answer on /health in 20 in the next. So on tcp it is SECURE, named: "ALL"
# means "anywhere" to RunPod, and is how Twisted got an H100 at $3.49/h. Each
# card is still priced first and kept under its ceiling (core/cost_plan.py).
_DEFAULT_CLOUD_ORDER = (
    "SECURE" if os.getenv("RUNPOD_TRANSPORT", "proxy").strip().lower() == "tcp"
    else "COMMUNITY,SECURE")
CLOUD_ORDER = [c.strip().upper()
               for c in os.getenv("RUNPOD_CLOUD_ORDER", _DEFAULT_CLOUD_ORDER).split(",")
               if c.strip()]
POD_VOLUME_GB = 0

# The ceiling on one boot attempt.
#
# 40 minutes suits the old install-on-boot template, where a cold boot is
# realistically 12-26 minutes. With the prebaked image there is no install at
# all and a healthy boot is model load only -- measured at 300s -- so this
# should come down to about 15 minutes once that image is live, which makes a
# bad machine cost a quarter of what it does now before the retry kicks in.
# Env-overridable so that is a one-line change rather than a code edit.
BOOT_TIMEOUT_SECONDS = int(os.getenv("BOOT_TIMEOUT_SECONDS", 40 * 60))
BOOT_POLL_SECONDS = int(os.getenv("BOOT_POLL_SECONDS", "10"))
BOOT_HEARTBEAT_SECONDS = 60

# How long 404s may persist before the boot is called a failure.
#
# This used to be a count of twelve, on the theory that "a healthy boot never
# returns a 404 at all". That theory was wrong, and proved wrong directly on
# 2026-09-11: a pod with vLLM healthy and answering on 127.0.0.1:8000, deployed
# without 8000/http among its ports, answers 404 through the proxy. A 404 means
# RunPod's proxy has no route for that port -- which during a boot can simply
# mean the port has not been registered yet, not that the pod is serving the
# wrong thing. The old rule could therefore abort a boot that was going to
# succeed, and twelve probes was barely three minutes of slack.
#
# So it is now a grace period rather than a count: 404s are only fatal if they
# are still all we are getting well after any reasonable registration delay.
# Generous on purpose -- the cost of waiting is minutes of GPU, the cost of
# being wrong is killing a working pod and paying for the whole boot again.
BOOT_404_GRACE_SECONDS = 10 * 60

# How many pods to try before giving up. A failed boot is far more often the
# machine than the template -- the same image that hangs on one host serves in
# five minutes on the next -- so the cheapest remedy is a different machine.
BOOT_ATTEMPTS = 3

# How long to keep sweeping the preference list when every card is out of stock.
# This is the failure *before* a bad boot: no pod is deployed at all, so unlike
# every other budget here, waiting costs nothing -- there is nothing billing.
# Measured 2026-09-12: three real drops aborted on the spot because all seven
# cards happened to be busy during one thirty-second sweep, while a probe
# deployed a 4090 minutes earlier and the L40S placed at four different disk
# sizes. Capacity churns on that timescale, so one sweep is a sample, not a
# verdict -- and an unattended drop (SAC-17) should outlast a busy patch rather
# than be decided by the minute it happened to land in.
CAPACITY_WAIT_SECONDS = int(os.getenv("CAPACITY_WAIT_SECONDS", str(45 * 60)))
CAPACITY_POLL_SECONDS = int(os.getenv("CAPACITY_POLL_SECONDS", "60"))

# Ceiling on the generation phase once the pod is serving. It exists because
# the alternative is an unbounded wait on a billing GPU.
#
# 90 minutes was derived from Voxtral's 28x realtime, where it was twenty times
# the worst observed book. CosyVoice serialises inference behind a lock and runs
# at 3.4-3.7x, so the same ceiling now sits *below* a real novel: Redeeming Love
# plans 616 chunks / 851,022 characters / ~16 hours of audio, which is ~4.7
# hours of generation -- the run would have aborted about a third of the way in
# and lost the lot, after the GPU had been paid for. 8 hours covers that book
# with headroom for a throughput dip to ~2x, and is still a bound rather than
# an open tab. Env-overridable because the number now tracks the provider's
# speed and the length of the longest book anyone drops, neither of which is a
# property of this code.
GENERATION_TIMEOUT_SECONDS = int(os.getenv("GENERATION_TIMEOUT_SECONDS", 8 * 60 * 60))

# How long one pod may take to align everything it generated. Generous because
# it is a single synchronous call covering a twelfth of a book, and because the
# alternative to waiting is the 65-minute local path -- but bounded, since the
# pods are still billing while this runs.
POD_ALIGN_TIMEOUT_SECONDS = int(os.getenv("POD_ALIGN_TIMEOUT_SECONDS", str(20 * 60)))

# Swarm size for a pod adopted with --use-pod. Conservative rather than derived:
# the pod's GPU is not known without another API round trip, and over-committing
# a small card is worse than under-using a large one.
ADOPTED_POD_WORKERS = 15

# --tts-url: a server you run yourself. Unset, the provider's per-server cap
# decides (start_workers_for takes the smaller; 8 for CosyVoice on one GPU).
# A smaller card may want fewer: TTS_URL_WORKERS=4.
OWN_SERVER_ID = "own-server"
OWN_SERVER_WORKERS = int(os.getenv("TTS_URL_WORKERS", "0")) or 1_000

# --- HARDWARE-AWARE SWARM CONFIGURATION ---
# Which card is rented, and in what order, is core/cost_plan.py's decision:
# measured cards only, at prices that keep a 16-hour book under the budget,
# cheapest per audio hour first. This list is only how each card is deployed.
# `name` must be RunPod's exact gpuTypeId.
#
# `api_workers` is per card and load-bearing. Each uvicorn worker holds its own
# copy of the model, so eight fit in 48GB and do NOT fit in 24GB: two separate
# 4090s at API_WORKERS=8, a day apart, returned RemoteProtocolError mid-response
# and then stopped answering entirely. Sending it with the deploy means the
# server count can never be left at a neighbouring card's value.
#
# `workers` is the client-side cap the GPU could sustain; the provider's own
# MAX_WORKERS (4, measured) is what actually applies.
#
# POD_API_WORKERS overrides the whole table, and the vLLM image needs it to.
# That per-card count exists because N processes hold N copies of the model --
# a model-per-process assumption that vLLM inverts: one engine batches many
# requests, and extra processes only take VRAM away from its KV cache. Without
# this override the engine would send API_WORKERS=8 with the deploy and
# silently overrule the template's 1, putting eight vLLM engines on one card,
# each asking for half its memory. Measured config for :4.0 is
# POD_API_WORKERS=1 with COSY_MAX_WORKERS=8.
POD_API_WORKERS = os.getenv("POD_API_WORKERS")
# Blackwell is not rented (core/cost_plan.SPEED has none) because it was
# measured failing on the cu121 images (:3.x) -- see below. The :4.x images are
# cu128 (torch 2.8, vLLM 0.11), which ships sm_120 kernels, so Blackwell should
# work there now; give it a speed only after tools/gpu_bench.py passes on one.
#
# The original finding, for the cu121 images: they pin torch 2.3.1+cu121,
# which ships kernels for sm_50 through sm_90 only. Blackwell is sm_120
# (RTX 50-series, RTX PRO 4500/6000 Blackwell, and every MIG slice of one) or
# sm_100 (B200, B300), and a cubin built for an older MAJOR compute version
# does not run there -- unlike Ada (sm_89), which runs the sm_86 kernels, which
# is why the 4090 and L40S are fine.
#
# Measured on a real pod, 2026-09-22, RTX PRO 4500 Blackwell at $0.72:
#
#     the image pulled, the weights were already in it, the model loaded in
#     27s, /health answered {"status":"ok"} -- and the first request that
#     touched a kernel died with
#         RuntimeError: CUDA error: no kernel image is available for execution
#     POST /voices failed after 79.6s, POST /clone after 3.0s.
#
# So a Blackwell card in this list is worse than no card at all: it rents,
# boots, passes every check the engine makes, and then fails every chunk while
# billing. Unlocking them needs an image on torch >=2.7/cu128, which is a
# different dependency resolution for the whole CosyVoice stack -- filed, not
# done. Until then the fleet's cheap Blackwell capacity is unreachable.
GPU_PREFERENCES = [
    {"name": "NVIDIA RTX A5000", "vram": "24GB", "workers": 15, "api_workers": 4},
    {"name": "NVIDIA GeForce RTX 3090", "vram": "24GB", "workers": 15, "api_workers": 4},
    {"name": "NVIDIA GeForce RTX 4090", "vram": "24GB", "workers": 15, "api_workers": 4},
    {"name": "NVIDIA RTX A4500", "vram": "20GB", "workers": 12, "api_workers": 4},
    {"name": "NVIDIA RTX 4000 Ada Generation", "vram": "20GB", "workers": 12, "api_workers": 4},
    {"name": "NVIDIA L4", "vram": "24GB", "workers": 15, "api_workers": 4},
    {"name": "NVIDIA RTX A6000", "vram": "48GB", "workers": 30, "api_workers": 8},
    {"name": "NVIDIA A40", "vram": "48GB", "workers": 30, "api_workers": 8},
]

class DeployRefusedError(Exception):
    """RunPod refused the deploy for a reason no amount of waiting fixes.

    Its own type so the capacity loop can tell it from a busy card: out of
    stock is worth waiting out for 45 minutes, this is not.
    """


class TemplateNotFoundError(DeployRefusedError):
    """RUNPOD_TEMPLATE_ID names no template RunPod knows (2026-10-07: a `#`
    with no space before it made the comment part of the id, and the engine
    reported "every card is busy" for 45 minutes)."""


class OutOfFundsError(DeployRefusedError):
    """The account cannot rent anything, as distinct from a card being busy.

    Its own type so the capacity loop can tell them apart: out of stock is
    worth waiting out, out of funds is not, and treating the second as the
    first means a book sits in the dropzone for 45 minutes reporting a
    shortage of GPUs that are sitting there available.
    """


async def deploy_burner_pod():
    """Hunts for a GPU and returns the Pod ID + Optimal Worker Count.

    One sweep of the preference list takes about half a minute, and RunPod's
    availability churns faster than that: on 2026-09-12 a 4090 deployed in a
    probe and was gone ninety seconds later, with an identical request. So a
    single sweep finding nothing says the fleet was busy for thirty seconds,
    not that the book cannot be narrated -- and aborting on it meant a drop
    made at a bad moment was simply lost.

    BOOT_ATTEMPTS already covers the other failure, a pod that deploys and
    never serves. This covers the one before it. Waiting here is free in the
    literal sense: no pod exists yet, so nothing is billing while it waits,
    which is what makes a long budget the right call rather than a costly one.
    """
    deadline = time.monotonic() + CAPACITY_WAIT_SECONDS
    sweep = 0
    while True:
        sweep += 1
        for cloud in CLOUD_ORDER:
            # DeployRefusedError (no funds, no such template) deliberately
            # propagates: waiting cannot fix it.
            pod = await _sweep_for_gpu(cloud)
            if pod is not None:
                return pod

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Exception(
                f"❌ No GPU became available in {CAPACITY_WAIT_SECONDS // 60} minutes "
                f"({sweep} sweeps of {len(GPU_PREFERENCES)} cards, community and secure). "
                "The book stays in the dropzone and is retried on the next drop "
                "or ghost restart; nothing was billed."
            )

        wait = min(CAPACITY_POLL_SECONDS, remaining)
        progress.update("waiting_gpu", say=False)
        print(f"\n😴 Every card is busy. Waiting {int(wait)}s and sweeping again "
              f"({int(remaining // 60)}m of budget left). Nothing is billing.")
        await asyncio.sleep(wait)


PRICE_QUERY = """{ gpuTypes { id securePrice communityPrice
  secure: lowestPrice(input: {gpuCount: 1, secureCloud: true}) { uninterruptablePrice stockStatus }
  community: lowestPrice(input: {gpuCount: 1, secureCloud: false}) { uninterruptablePrice stockStatus } } }"""


async def live_prices():
    """{(gpuTypeId, "SECURE"|"COMMUNITY"): $/h} as RunPod quotes them now, 0 when
    out of stock; {} when RunPod does not answer (the billed price is still
    checked after each deploy, so an outage costs the ranking, not the ceiling)."""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(pod_pool.graphql_url(RUNPOD_API_KEY),
                                  json={"query": PRICE_QUERY}, timeout=30.0)
        rows = (r.json().get("data") or {}).get("gpuTypes") or []
    except (httpx.HTTPError, ValueError):
        return {}
    prices = {}
    for g in rows:
        for cloud, key, listed in (("SECURE", "secure", g.get("securePrice")),
                                   ("COMMUNITY", "community", g.get("communityPrice"))):
            quote = g.get(key) or {}
            in_stock = quote.get("stockStatus") not in (None, "None")
            prices[(g["id"], cloud)] = (quote.get("uninterruptablePrice") or listed or 0) if in_stock else 0
    return prices


def _card(name):
    """The deploy settings for a card: GPU_PREFERENCES' entry, or the vLLM defaults."""
    return next((g for g in GPU_PREFERENCES if g["name"] == name),
                {"name": name, "vram": "?", "workers": 8, "api_workers": 1})


async def plan_cards(cloud="ALL"):
    """[(card, cloud, quoted $/h, ceiling $/h)] to try, cheapest per audio hour first.

    core/cost_plan.py decides: only measured cards, only at prices that keep a
    16-hour book within the budget. "ALL" prices both halves of the fleet and
    deploys each card on the half it was priced on: cloudType ALL itself means
    "anywhere", and on 2026-10-07 that bought Twisted's H100 at $3.49/h.
    """
    clouds = ("COMMUNITY", "SECURE") if cloud == "ALL" else (cloud,)
    prices = await live_prices()
    if prices:
        prices = {k: v for k, v in prices.items() if k[1] in clouds}
    else:
        # No quotes: every measured card at its own ceiling, fastest first.
        # The billed price is checked after the deploy all the same.
        prices = {(g, c): cost_plan.ceiling(s) for g, s in cost_plan.SPEED.items() for c in clouds}
    return [(_card(g), c, price, top) for g, c, price, top in cost_plan.candidates(prices)]


async def _sweep_for_gpu(cloud="ALL"):
    """One pass down GPU_PREFERENCES on one cloud. Returns (pod_id, workers) or None.

    `cloud` is a parameter rather than a constant because the two halves of the
    fleet differ in price by roughly 2x and RunPod will quietly sell you the
    expensive one. cloudType ALL used to be hardcoded here, correcting an
    earlier bug where COMMUNITY-only searched the empty half of the fleet
    (measured 2026-09-12: all seven preferred cards reported "no longer any
    instances available" against COMMUNITY while the 4090 was in stock on both).

    That fix overshot. Measured 2026-09-21 on a four-card sweep, every deploy
    with cloudType ALL: the RTX 4090 placed at $0.74/hr against a community
    price of $0.34, and the L40S at $1.09 against $0.79. ALL does not mean
    "cheapest available", it means "anywhere", and it had been paying a secure
    premium on every pod of every book since the change.

    So the caller sweeps COMMUNITY first and ALL second. Nothing is given up:
    an empty community cloud costs one extra ~30s pass and then searches
    exactly what it searched before.
    """
    url = pod_pool.graphql_url(RUNPOD_API_KEY)

    plan = await plan_cards(cloud)
    if not plan:
        spent = cost_plan.other_per_16h()
        print(f"💸 No card in stock on {cloud} can narrate a 16-hour book for "
              f"${cost_plan.budget_16h():.2f} right now (VOXIE_BUDGET_16H)"
              + (f", after ${spent:.2f} per 16 h on OpenRouter" if spent else "")
              + ". Nothing was rented.")
    for gpu, card_cloud, quoted, top in plan:
        gpu_name = gpu["name"]
        gpu_workers = gpu["workers"]
        # Sent with the deploy so the server-side worker count always matches
        # the card. Left to the template it is one number for every card, and
        # the template's 8 is fatal on anything with 24GB.
        api_workers = gpu.get("api_workers")
        if POD_API_WORKERS:
            api_workers = int(POD_API_WORKERS)

        print(f"🔍 Hunting for an available {gpu_name} ({gpu['vram']}) on {card_cloud} "
              f"at ${quoted:.2f}/h (ceiling ${top:.2f})...")
        extra = ""
        # The TCP transport reaches the server at a raw public ip:port, so a
        # host that cannot hand out a public IP is useless to it -- and RunPod
        # will happily rent you one anyway. The pod reports RUNNING, publishes
        # no portMappings and no publicIp at all, and the run then spends
        # PORT_MAPPING_TIMEOUT_SECONDS (600) waiting for a mapping that is never
        # coming, terminates it, and pays another machine to repeat the lesson.
        #
        # Measured 2026-09-21: of five pods deployed without this flag, three
        # never published an IP -- an RTX 3090, an L40S and an RTX 4090, all
        # RUNNING, all billing, all unreachable. The two that worked were simply
        # hosts that happened to support it. This is not a community-vs-secure
        # distinction, which is why it has to be asked for rather than inferred
        # from cloudType.
        #
        # Only on tcp: the proxy transport goes through
        # *-8000.proxy.runpod.net and needs no public IP, so requiring one there
        # would discard usable capacity for nothing.
        if RUNPOD_TRANSPORT == "tcp":
            extra += "supportPublicIp: true,\n                "
        env = pod_pool.pod_env(api_workers, TTS_API_TOKEN)
        if env:
            extra += env + "\n                "
        volume_gb = POD_VOLUME_GB

        payload = {
            "query": f'''
            mutation {{
              podFindAndDeployOnDemand(input: {{
                cloudType: {card_cloud},
                gpuCount: 1,
                gpuTypeId: "{gpu_name}",
                {extra}
                volumeInGb: {volume_gb},
                containerDiskInGb: {POD_CONTAINER_DISK_GB},
                name: "Librarian_Burner_Pod",
                templateId: "{RUNPOD_TEMPLATE_ID}"
              }}) {{
                id
                costPerHr
              }}
            }}
            '''
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, timeout=30.0)
            data = response.json()

            if "errors" in data:
                message = data["errors"][0].get("message", "")
                # "Your account balance is too low to rent a pod" arrives
                # through the same channel as "this card is out of stock", and
                # the two want opposite responses. Out of stock means try the
                # next card, and then wait and sweep again for up to
                # CAPACITY_WAIT_SECONDS -- 45 minutes of patiently reporting a
                # GPU shortage that does not exist, when the real answer is
                # four words long and no amount of waiting fixes it.
                if "template not found" in message.lower():
                    raise TemplateNotFoundError(
                        f"RunPod has no template '{RUNPOD_TEMPLATE_ID}' (RUNPOD_TEMPLATE_ID in .env). "
                        "Nothing was billed and nothing is running. Run `voxie init` to set it again.")
                if "balance" in message.lower():
                    raise OutOfFundsError(
                        "RunPod refused the deploy: " + message.strip() +
                        "\n   Nothing was billed and nothing is running. Top up "
                        "at https://console.runpod.io/user/billing and drop the "
                        "book again.")
                print(f"🛑 RUNPOD API ERROR for {gpu_name}: {message}")

            # If successful, return BOTH the Pod ID and the Worker Count!
            if "data" in data and data["data"] and data["data"].get("podFindAndDeployOnDemand"):
                new_pod_id = data["data"]["podFindAndDeployOnDemand"]["id"]
                billed = float(data["data"]["podFindAndDeployOnDemand"].get("costPerHr") or 0)
                if billed > top + 0.005:
                    # The quote was a moment ago; the bill is now. A pod over
                    # its ceiling breaks the promise, so it goes at once (a
                    # few seconds billed) and the next card is tried.
                    pod_pool._register(new_pod_id, note=f"over ceiling {gpu_name}")
                    print(f"💸 {gpu_name} came at ${billed:.2f}/h, over its ${top:.2f} ceiling; returning it.")
                    await release_pod(new_pod_id)
                    continue
                print(f"🔥 Burner Pod Deployed! Hardware: {gpu_name} | "
                      f"{api_workers or 'template default'} server workers | "
                      f"Swarm Capacity: {gpu_workers} Workers")
                # RunPod has no log API — the container log exists only in the console, and
                # only while the pod lives. If the boot misbehaves, this link is the only
                # place to watch it happen, so print it before the wait rather than after.
                print(f"📜 Live boot log: https://console.runpod.io/pods/{new_pod_id}")
                return new_pod_id, gpu_workers

        print(f"⚠️ No {gpu_name} available right now. Trying next tier...")

    # Out of stock is not fatal here -- the caller waits and sweeps again.
    return None

async def release_pod(pod_id):
    """Terminate a pod, and forget it only once RunPod has accepted.

    The boot-failure paths used to call terminate_burner_pod, which never reads
    RunPod's answer, and then print that billing was "safely locked at $0.00"
    either way -- and they never took the pod out of the registry, so a pod
    that was really gone stayed there until the next sweep (seen on 2026-09-24:
    one terminated 4090 sat in ~/.librasync_active_pods.json after its run had
    finished). pod_pool.terminate already has the rule this needs: unregister
    after RunPod says yes, and not before.
    """
    print(f"🧨 Terminating Burner Pod {pod_id} and deleting storage...")
    if await pod_pool.terminate(pod_id):
        print("🧨 Burner Pod destroyed. Cloud billing is safely locked at $0.00.")
        return True
    print(f"⚠️  RunPod did not confirm terminating {pod_id}. It stays in the registry, "
          "so the next sweep retries it; check https://console.runpod.io/pods")
    return False


async def terminate_burner_pod(pod_id):
    """Nukes the pod from orbit so you pay exactly $0.00 in storage fees."""
    url = pod_pool.graphql_url(RUNPOD_API_KEY)
    payload = {
        "query": f'mutation {{ podTerminate(input: {{ podId: "{pod_id}" }}) }}'
    }

    async with httpx.AsyncClient() as client:
        print(f"🧨 Terminating Burner Pod {pod_id} and deleting storage...")
        await client.post(url, json=payload, timeout=30.0)

async def wait_for_ai_brain(health_path="/v1/models", base_url=None):
    """Polls the TTS server until it can serve, or gives up.

    health_path comes from the provider: vLLM answers /v1/models, the CosyVoice
    API answers /health, and probing the wrong one looks exactly like a pod that
    never came up.

    Returns (True, None) once healthy, or (False, last_state) on timeout. The caller owns
    the failure path, so the pod can be described before it gets terminated.

    last_state distinguishes the two failure modes that look identical from the outside:
    'refused' means nothing ever bound port 8000 (the server died or never launched),
    while a timeout or an HTTP status means something is listening but not ready yet.
    """
    minutes = BOOT_TIMEOUT_SECONDS // 60
    # base_url is passed explicitly now that pods are acquired concurrently:
    # several boots are in flight at once, so a module-level "the" pod address
    # would be whichever task wrote to it last and every waiter would probe the
    # same machine. It still falls back to the global, which is what the tools
    # and tools/test_boot_logic.py set.
    health_url = f"{base_url or RUNPOD_BASE_URL}{health_path}"
    # Name the endpoint rather than the model: "Voxtral is awake" printed by a
    # CosyVoice run sends whoever is debugging a bad boot to the wrong logs.
    print(f"⏳ Waiting up to {minutes} min for the pod at {health_url} to come up...")

    attempts = BOOT_TIMEOUT_SECONDS // BOOT_POLL_SECONDS
    heartbeat_every = max(1, BOOT_HEARTBEAT_SECONDS // BOOT_POLL_SECONDS)
    last_state = "no attempt made"
    started = time.monotonic()
    first_404_at = None

    async with httpx.AsyncClient() as client:
        for attempt in range(attempts):
            try:
                response = await client.get(health_url, timeout=5.0)
                if response.status_code == 200:
                    elapsed = int(time.monotonic() - started)
                    print(f"🟢 TTS server is awake and ready! (booted in {elapsed // 60}m {elapsed % 60}s)")
                    return True, None

                # A healthy boot never returns 404 even once. Measured on two
                # runs of the same template on 2026-08-31: the one that worked
                # answered 502 four times and then 200, while the one that
                # failed answered 502 for five minutes, produced its first 404
                # at 6m14s, and then alternated 404 and 502 for another
                # thirty-seven minutes without ever recovering.
                #
                # The two codes mean different things -- 502 is RunPod's proxy
                # finding nothing bound on :8000, 404 is something bound there
                # that is not vLLM's OpenAI API -- so alternating between them
                # is a process binding the port, failing, and being restarted.
                # Counting cumulatively rather than consecutively matters for
                # exactly that reason: the 502s in between would reset a
                # consecutive counter forever.
                #
                # Giving up here turns a 40-minute L40S bill for nothing into
                # about seven minutes, and reports the failure while someone
                # might still be watching.
                if response.status_code == 404:
                    if first_404_at is None:
                        first_404_at = time.monotonic()
                    stuck = time.monotonic() - first_404_at
                    if stuck >= BOOT_404_GRACE_SECONDS:
                        elapsed = int(time.monotonic() - started)
                        print(f"\n   🛑 Giving up after {elapsed // 60}m{elapsed % 60:02d}s: "
                              f"still 404 after {int(stuck) // 60} minutes of them.")
                        print("      A 404 means the proxy has no route for :8000. Early in a boot "
                              "that is just the port not being registered yet, which is why this "
                              "waits rather than failing on the first one -- but this long means "
                              "the port is never going to appear.")
                        return False, (f"HTTP 404 for {int(stuck) // 60}m — the proxy never "
                                       f"registered a route to :8000")
                else:
                    # Any other answer resets the clock. This deliberately means
                    # the alternating 404/502 failure seen on 2026-08-31 never
                    # trips this rule -- it falls through to the overall timeout
                    # and then to a fresh machine, which is the safe direction to
                    # be wrong in. Not resetting would catch that case, but it
                    # would also kill a slow install-on-boot run that happened to
                    # see one registration-lag 404 in its first minutes, and
                    # killing a working pod costs the whole boot again.
                    first_404_at = None

                last_state = f"HTTP {response.status_code} — listening, not ready"
            except httpx.ConnectError:
                last_state = "connection refused — nothing listening on :8000 yet"
            except httpx.TimeoutException:
                last_state = "timed out — listening but not answering"
            except httpx.RequestError as e:
                last_state = f"{type(e).__name__}: {e}"

            # A 40-minute silent wait is indistinguishable from a hang. Say something.
            if (attempt + 1) % heartbeat_every == 0:
                elapsed = int(time.monotonic() - started)
                print(f"   ⏱️  {elapsed // 60}m{elapsed % 60:02d}s elapsed — {last_state}")

            await asyncio.sleep(BOOT_POLL_SECONDS)

    return False, last_state


async def describe_pod(pod_id):
    """Print whatever RunPod will tell us about a pod, for post-mortem on a failed boot.

    RunPod exposes no container-log endpoint, so pod state plus the health check's last
    error is as close to a diagnosis as the API allows. Never raises: this runs on the
    path to termination, and nothing here is worth leaving a pod billing over.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                pod_pool.rest_url(f"/pods/{pod_id}"),
                headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
                timeout=30.0,
            )
        if response.status_code != 200:
            print(f"   (could not read pod state: HTTP {response.status_code})")
            return

        pod = response.json()
        print(f"   status:      {pod.get('desiredStatus')}")
        print(f"   last event:  {pod.get('lastStatusChange')}")
        print(f"   started at:  {pod.get('lastStartedAt')}")
        print(f"   disk:        {pod.get('containerDiskInGb')}GB container / {pod.get('volumeInGb')}GB volume")
        print(f"   ports:       {pod.get('portMappings') or 'none mapped'}")
        print(f"   cost:        ${pod.get('costPerHr')}/hr")
    except Exception as e:
        print(f"   (could not read pod state: {e})")

# How the engine reaches the pod's server.
#
#   proxy  https://<pod>-8000.proxy.runpod.net  -- RunPod's HTTP proxy. Simple,
#          needs no template change, and sits behind Cloudflare, which kills any
#          response over ~100 seconds with an HTTP 524. That ceiling is not a
#          throughput limit but a per-request one, and it is what quarantined
#          042_B_IX and 043_C_IX out of The Screwtape Letters on 2026-09-15:
#          both were merely slow, not broken, and twelve attempts each were
#          killed mid-generation.
#
#   tcp    http://<ip>:<mapped port>  -- RunPod's TCP port mapping, straight to
#          the container with no Cloudflare in between and no response ceiling.
#          Requires the template to expose 8000 as TCP rather than HTTP, which
#          is a change in the RunPod console that this repo cannot make.
#
# Two things change with it and both are deliberate. The ip:port is assigned at
# runtime, so it cannot be a pattern and has to be read back from the API after
# the pod is running. And there is no TLS and no network ACL -- the server is
# then reachable by anyone who portscans that host, on a GPU billed by the hour,
# which is why TTS_API_TOKEN exists and why tcp refuses to start without one.
RUNPOD_TRANSPORT = os.getenv("RUNPOD_TRANSPORT", "proxy").strip().lower()

# Shared secret for the TTS server. Sent as a bearer token on every request.
TTS_API_TOKEN = os.getenv("TTS_API_TOKEN", "").strip()

# How long to wait for RunPod to publish the TCP mapping.
#
# 600, not 180. The port is assigned when the container starts, and on a host
# that has never pulled this image that means waiting out 6.78GB first --
# measured 2026-09-19 on the five-pod Tactique run, where a warm host served in
# ~2 min and a cold one took ~4m50s. 180s killed a perfectly good pod on its
# fourth deploy purely because the host was cold, and BOOT_ATTEMPTS then paid
# for another machine to learn the same thing.
#
# Cheap to be generous here: an unmapped pod is caught by BOOT_TIMEOUT_SECONDS
# anyway, so this bound only decides how quickly a slow pull is mistaken for a
# broken one.
PORT_MAPPING_TIMEOUT_SECONDS = int(os.getenv("PORT_MAPPING_TIMEOUT_SECONDS", "600"))

# Which container port the TTS server listens on. 8000 for this project's own
# CosyVoice image, but the seam exists so a third-party server can be tried
# without editing the engine: LightTTS's published image serves 8080, and a
# hardcoded 8000 means resolve_pod_base_url waits out the full 600s budget for
# a mapping that was never going to appear, then discards a perfectly healthy
# pod.
POD_CONTAINER_PORT = int(os.getenv("POD_CONTAINER_PORT", "8000"))


async def resolve_pod_base_url(pod_id):
    """Where this pod's server can be reached, for the configured transport.

    On the proxy transport this is a pure string format and cannot fail. On tcp
    it polls the REST API until the mapping for container port 8000 appears,
    because RunPod assigns the public port when the container starts and there
    is nothing to connect to before then.
    """
    if RUNPOD_TRANSPORT != "tcp":
        return RUNPOD_URL_PATTERN.format(pod_id=pod_id)

    deadline = time.time() + PORT_MAPPING_TIMEOUT_SECONDS
    while time.time() < deadline:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    pod_pool.rest_url(f"/pods/{pod_id}"),
                    headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
                    timeout=30.0)
            if r.status_code == 200:
                pod = r.json()
                mappings = pod.get("portMappings") or {}
                public_ip = pod.get("publicIp") or pod.get("ip")
                port = mappings.get(str(POD_CONTAINER_PORT)) or \
                    mappings.get(POD_CONTAINER_PORT)
                if public_ip and port:
                    url = f"http://{public_ip}:{port}"
                    print(f"   🔌 TCP mapping resolved: {url} (no proxy, no 100s ceiling)")
                    return url
        except Exception as e:
            print(f"   (waiting on the TCP mapping: {str(e)[:60]})")
        await asyncio.sleep(5)

    # Deliberately does NOT assert the template is misconfigured. It said that
    # for a long time, and on 2026-09-19 it said it about the fourth pod of a
    # run whose first three had mapped fine on the same template -- which would
    # send anyone reading it into the console hunting a problem that is not
    # there. The honest reading is "no mapping yet", and the two real causes
    # look identical from here.
    raise Exception(
        f"Pod {pod_id} published no TCP mapping for port 8000 within "
        f"{PORT_MAPPING_TIMEOUT_SECONDS}s. Usually a cold host still pulling the "
        f"image; if EVERY pod does this, check the template exposes 8000/tcp "
        f"rather than 8000/http.")


def cpu_route():
    """True when narration runs on this machine's CPU (Kokoro, server/cpu_tts.py)."""
    return os.getenv("TTS_PROVIDER", "kokoro").strip().lower() == "kokoro"


def start_cpu_server(provider):
    """Start server/cpu_tts.py on a free 127.0.0.1 port. Returns (process, url).

    Stopped by the engine's finally, by atexit when the engine fails before
    that try is entered, and by the server itself if the engine is killed.
    """
    import atexit
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    log = open(os.path.join("mfa_workspace", "cpu_tts.log"), "w")
    proc = subprocess.Popen(
        [sys.executable, "-u", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "server", "cpu_tts.py"),
         "--port", str(port), "--preload", provider.voice[0],
         "--parent-pid", str(os.getpid())],
        stdout=log, stderr=subprocess.STDOUT)
    atexit.register(stop_cpu_server, proc)
    print(f"\n💻 Narrating on this computer (Kokoro, voice {provider.voice}); "
          f"no GPU, no account. Log: mfa_workspace/cpu_tts.log")
    return proc, f"http://127.0.0.1:{port}"


def stop_cpu_server(proc):
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


# Exit code for "no usable narrator voice". mac_ghost turns it into advice
# and a notification instead of "the Engine crashed".
NO_VOICE_EXIT = 4
# A book that is not English: published as an ebook only (core/languages.py),
# and the watcher says why. mac_ghost.py repeats the number.
NOT_ENGLISH_EXIT = 5


def preflight_voice(book_language):
    """Stop before any GPU when the cloning provider has no voice to clone.

    Runs after the ebook is published, so the book is readable either way.
    COSY_REF_WAV bypasses voices/ entirely, so it bypasses this check too.
    """
    if os.getenv("TTS_PROVIDER", "kokoro").strip().lower() != "cosyvoice" \
            or os.getenv("COSY_REF_WAV"):
        return
    from core.voices import missing_voice
    problem = missing_voice(VOICE_CHOICE, book_language)
    if problem:
        print(f"\n🎙️  {problem}")
        print("   The ebook is published; drop the book again once a voice exists. "
              "Nothing was billed.")
        raise SystemExit(NO_VOICE_EXIT)


def _uses_our_server():
    """True when the TTS server is server/custom_api.py, which enforces TTS_API_TOKEN."""
    return os.getenv("TTS_PROVIDER", "kokoro").strip().lower() == "cosyvoice"


async def assert_server_requires_token(base_url):
    """Fail if the server is not enforcing TTS_API_TOKEN. Only on the TCP path.

    Observed live on 2026-09-15: .env had the token, the RunPod template had the
    token and exposed 8000/tcp, server/custom_api.py had the middleware, and the
    pod still answered `GET /voices` with HTTP 200 and no credentials -- because
    the template's start command is a *gist*, and the gist had not been
    re-pasted. So a whole book was about to be narrated through an open
    CosyVoice server on a raw public ip:port with an L40S billing behind it.

    Nothing on this Mac can see what a pod actually boots. The only honest test
    is to ask the running server.

    Fatal rather than a warning, and the pod is terminated. The engine already
    refuses to *start* on tcp without a token; narrating 153 chunks through a
    server that ignores it would be the same failure, just later and after the
    exposure had already happened. The cost of being wrong here is one boot.
    """
    # Only our CosyVoice server carries the token middleware; Voxtral's is
    # stock vLLM. On both transports: a proxy URL is only as secret as the logs
    # and screenshots that print it.
    if not _uses_our_server() or not TTS_API_TOKEN:
        return True, ""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{base_url}/voices", timeout=30.0)
    except Exception as e:
        # Unreachable is wait_for_ai_brain's problem, not this check's.
        print(f"   (could not verify auth: {str(e)[:60]})")
        return True, ""
    if r.status_code == 401:
        # And it must be OUR token it enforces. A template that still carries
        # someone's TTS_API_TOKEN would refuse every chunk with 401, after
        # the pod had been paid to boot.
        try:
            async with httpx.AsyncClient() as client:
                mine = await client.get(f"{base_url}/voices", timeout=30.0,
                                        headers={"Authorization": f"Bearer {TTS_API_TOKEN}"})
        except Exception as e:
            print(f"   (could not verify the token: {str(e)[:60]})")
            return True, ""
        if mine.status_code == 401:
            return False, (
                "the server refuses this .env's TTS_API_TOKEN: it is enforcing a different\n"
                "   one. The RunPod template probably sets its own TTS_API_TOKEN; remove it\n"
                "   there (the engine sends yours with every pod).")
        print("   🔐 Server rejects unauthenticated requests and accepts your token.")
        return True, ""
    return False, (
        f"the server answered /voices with HTTP {r.status_code} and no token.\n"
        "   It is running a server without the auth middleware: a RunPod template\n"
        "   with an old image or a pasted start command. On the TCP transport that\n"
        "   leaves an open CosyVoice server on a public ip:port with a GPU billing.\n"
        "   Fix: the template runs ghcr.io/smukoko/cosyvoice-tts:4.1 (or later) with\n"
        "   no start command; `voxie init` sets Voxie's public template.")


async def acquire_serving_pod(health_path="/v1/models"):
    """Deploy a pod and wait for it to serve, trying another machine if it does not.

    A boot that never serves is far more often the machine than the template.
    Measured 2026-09-11: the same image that hung for over an hour on one host
    installed in twelve minutes and served on the next, with nothing changed.
    So the cheapest remedy for a bad boot is simply a different machine, and
    that is worth doing automatically -- an unattended run (SAC-17) should not
    be decided by which host RunPod happened to pick.

    Each failed attempt is terminated here, so the caller only ever receives a
    pod that is already answering, and a failure leaves nothing billing.
    """
    last_state = "no attempt made"
    for attempt in range(1, BOOT_ATTEMPTS + 1):
        if attempt > 1:
            print(f"\n🔁 Boot attempt {attempt} of {BOOT_ATTEMPTS} — deploying on a different machine...")
        pod_id, swarm_limit = await deploy_burner_pod()
        # Recorded the instant RunPod hands back an id, NOT once it serves. The
        # pod starts billing at deploy and takes two to three minutes to come
        # up; registering after that left a window per pod in which a kill -9
        # orphaned a machine the sweep could not find, because it had never been
        # written down. Observed live on 2026-09-19: a pod billing with an empty
        # registry. Idempotent, so the later pool.add() is harmless.
        pod_pool._register(pod_id, note="booting")
        try:
            base_url = await resolve_pod_base_url(pod_id)
            print(f"📡 AI Gateway established at: {base_url}")
            awake, last_state = await wait_for_ai_brain(health_path, base_url)
        except asyncio.CancelledError:
            # The run finished before this pod did. Now that acquisition is
            # concurrent that is a real case rather than a theoretical one: the
            # queue can drain on the pods that came up while a straggler is
            # still pulling the image, and the caller then cancels the rest.
            #
            # A cancelled task runs none of its own cleanup, so without this the
            # pod bills on until something sweeps the registry -- which is the
            # next ghost restart, and may be hours. Shielded because the
            # termination itself is an await, and an unshielded one inside a
            # cancelled task is cancelled before it reaches RunPod.
            print(f"\n⏹️  Pod {pod_id} is no longer needed; terminating it.")
            await asyncio.shield(release_pod(pod_id))
            raise
        except Exception as e:
            # A pod that never publishes its mapping is billing for nothing, and
            # the next attempt needs a clean slate rather than an orphan.
            print(f"\n❌ {e}")
            await release_pod(pod_id)
            last_state = str(e)
            continue

        if awake:
            ok, why = await assert_server_requires_token(base_url)
            if not ok:
                print(f"\n🔓 REFUSING TO NARRATE: {why}")
                await release_pod(pod_id)
                raise DeployRefusedError("The pod's server does not enforce your TTS_API_TOKEN; see above. "
                                         "Nothing was narrated on it, and it was terminated.")
            return pod_id, swarm_limit, base_url

        print(f"\n❌ Pod {pod_id} never started serving on :8000.")
        print(f"   last health check: {last_state}")
        await describe_pod(pod_id)
        print("   The container log is only in the console, and only until the pod is")
        print(f"   terminated: https://console.runpod.io/pods/{pod_id}")
        await release_pod(pod_id)

    raise Exception(f"No pod served within {BOOT_ATTEMPTS} attempts. Last state: {last_state}")



def retired_workers(consumers, done):
    """The workers in `done` that returned "pod lost": their pod is gone, they did not crash."""
    return [c for c in consumers if c in done and not c.cancelled()
            and c.exception() is None and c.result() == "pod lost"]


async def replace_lost_pods(pool, wanted, replacements, acquisition, rent_one):
    """Terminate every lost pod; rent one replacement if the plan and the cap allow.

    `rent_one()` makes the coroutine that rents it. Returns (acquisition,
    replacements): the task now renting, if any, and how many were rented.
    """
    for url in list(pool.lost):
        print(f"\n🪦 The pod at {url} stopped answering; terminating it so it stops billing.")
        await pool.drop(url)
    if (len(pool) < wanted and replacements < MAX_POD_REPLACEMENTS
            and (acquisition is None or acquisition.done())):
        replacements += 1
        print(f"   🔁 Renting a replacement ({replacements} of {MAX_POD_REPLACEMENTS} "
              "allowed for one book); its chunks wait in the queue.")
        acquisition = asyncio.create_task(rent_one())
    return acquisition, replacements


def nothing_left(consumers, acquisition):
    """Why the run must stop, when no worker is left and none is coming; else None."""
    if consumers or not (acquisition is None or acquisition.done()):
        return None
    why = acquisition.exception() if acquisition is not None and not acquisition.cancelled() else None
    # A refusal (no funds, no such template) keeps its own words.
    return (f"No pod served: {why}" if why else
            "Every pod was lost and no replacement is coming; the chunks already narrated "
            "are kept for the next run.")


async def wait_for_generation(join_task, consumers, pool, acquisition, wanted_pods,
                              rent_one, timeout_s=None):
    """Wait until every chunk is narrated, or say why the run must stop.

    Not a bare join(). A worker that dies never calls task_done(), so the
    queue counts its chunk as outstanding forever and a join waits with the
    pod billing and nothing to stop it. The workers only ever exit on a
    poison pill, and those are not sent until after this returns -- so any
    consumer finishing *here* has died, and that is the signal to give up
    rather than wait. Out of main() so the offline tests can drive every
    branch with fake workers (tools/test_lost_pod_replacement.py).

    The watch list is rebuilt each pass because the swarm grows: a pod that
    serves at minute nine adds workers (to `consumers`, the caller's own
    list) that the first asyncio.wait could not have known about.

    Returns (failure, acquisition): failure is None once the queue drained;
    acquisition is the task renting now (a replacement may have started one).
    """
    timeout_s = GENERATION_TIMEOUT_SECONDS if timeout_s is None else timeout_s
    deadline = time.monotonic() + timeout_s
    too_long = (f"Generation exceeded {int(timeout_s // 60)} minutes "
                "with chunks still outstanding.")
    replacements = 0
    while True:
        watching = [join_task, *consumers]
        if acquisition is not None and not acquisition.done():
            watching.append(acquisition)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return too_long, acquisition
        done, _ = await asyncio.wait(watching, timeout=remaining,
                                     return_when=asyncio.FIRST_COMPLETED)
        if join_task in done:
            return None, acquisition
        # A worker whose pod stopped answering hands its chunk back and
        # returns "pod lost": not a crash. Found by layer 3 (tools/chaos,
        # dies-mid-book): with one pod, a lost pod lost the whole book.
        for c in retired_workers(consumers, done):
            consumers.remove(c)
        if pool.lost:
            # No `continue`: when every worker of the lost pod retired in this
            # one pass and no replacement may be rented, nothing is left to
            # drain the queue, and only nothing_left below stops the run
            # rather than the 8-hour limit.
            acquisition, replacements = await replace_lost_pods(
                pool, wanted_pods, replacements, acquisition, rent_one)
        failure = nothing_left(consumers, acquisition)
        if failure:
            return failure, acquisition
        dead = [c for c in consumers if c in done]
        if dead:
            return (f"{len(dead)} worker(s) died before the queue drained; aborting "
                    "rather than waiting on a pod that will never finish."), acquisition
        if acquisition is not None and acquisition in done:
            # Acquisition finishing is normal and not a reason to stop -- the
            # pods it got are still narrating. One that got nothing at all with
            # no worker left was already stopped by nothing_left above.
            continue
        if not done:
            return too_long, acquisition


# Pods rented to replace lost ones, at most, for one book. Each costs a boot;
# a host that keeps failing is not worth a third.
MAX_POD_REPLACEMENTS = int(os.getenv("MAX_POD_REPLACEMENTS", "2"))

# How many pods one book may use, at most; the dollar rule (core/cost_plan.py,
# planned_pod_count) cuts it to what the book's budget pays for. The default was
# 1 on the belief that more pods multiply the bill. They do not: the same audio
# costs the same GPU-hours however many pods share it, and each extra pod adds
# only its boot (about 13 minutes of its price). On 2026-10-22 a 6.9-hour book
# on one A5000 took 48 minutes to narrate and 75 in all, for $0.29 of a budget
# that allowed two pods; the user's first complaint was the hour.
POD_COUNT = int(os.getenv("POD_COUNT", "4"))


# What this run has spent besides the GPU; the cartographer fills it in.
RUN_SPEND = {"openrouter": 0.0}


PROGRESS_SECONDS = 30


async def report_narration_progress(narration_plan, mfa_dir, audio_hours):
    """Tell the progress file how much of the text has audio, every half minute,
    with the time left (narrating, then the finishing steps). Counted in
    characters: the longest chunks go first (see the queue below), so a count of
    chunks would promise the end too soon. Cancelled when narration ends."""
    total = sum(len(c.get("text", "")) for c in narration_plan) or 1
    started = time.monotonic()
    while True:
        await asyncio.sleep(PROGRESS_SECONDS)
        try:
            done = sum(len(c.get("text", "")) for c in narration_plan
                       if os.path.exists(os.path.join(
                           mfa_dir, (c["filename"][:-4] if c["filename"].endswith(".mp3") else c["filename"]) + ".wav")))
            left = progress.narration_eta(done, total, time.monotonic() - started)
            progress.update("narrating", say=False, done=done, total=total,
                            eta_minutes=None if left is None else round(left + progress.finishing_minutes(audio_hours)))
        except Exception:
            pass


async def planned_pod_count(narration_plan):
    """POD_COUNT, cut to what the budget allows for this book on the best card
    in stock now, with the estimate said before anything bills.

    The director's and the filters' OpenRouter spend is already known here
    and comes out of the same budget: the promise is what a book costs."""
    hours = cost_plan.audio_hours(sum(len(c.get("text", "")) for c in narration_plan))
    cost_plan.set_other_spend(RUN_SPEND["openrouter"], hours)
    plan = await plan_cards(CLOUD_ORDER[0] if len(CLOUD_ORDER) == 1 else "ALL")
    if not plan:
        return POD_COUNT
    card, _, price, _ = plan[0]
    speed = cost_plan.SPEED[card["name"]]
    count = cost_plan.pods_for(hours, price, speed, POD_COUNT)
    cost = cost_plan.book_cost(price, speed, hours, count)
    other = RUN_SPEND["openrouter"]
    allowed = cost_plan.budget_16h() * max(hours, 1.0) / cost_plan.BOOK_HOURS + cost_plan.allowance_for(hours)
    print(f"\n💰 About {hours:.1f} h of audio. On {count} {card['name']} pod(s) at ${price:.2f}/h: "
          f"about ${cost:.2f} and {60 * (hours / speed / count + cost_plan.BOOT_HOURS):.0f} min"
          + (f", plus ${other:.2f} already spent on OpenRouter (the director, the filters)" if other else "")
          + f". About ${cost + other:.2f} in all; the budget is ${allowed:.2f} for this length "
          f"(${cost_plan.budget_16h():.2f} per 16 h).")
    if count < POD_COUNT:
        print(f"   POD_COUNT is {POD_COUNT}; {count} keep(s) the budget, so {count} it is.")
    progress.update("starting_gpu", audio_hours=round(hours, 2), eta_minutes=round(
        60 * (hours / speed / count + cost_plan.BOOT_HOURS) + progress.finishing_minutes(hours)))
    return count


async def acquire_pool(health_path="/v1/models", count=None, pool=None, on_ready=None):
    """Deploy up to `count` serving pods, concurrently. Returns (pool, swarm).

    Elastic rather than fixed. Capacity churns faster than one sweep of the
    preference list takes -- a 4090 deployed in a probe and was gone ninety
    seconds later on the identical request -- so insisting on exactly five pods
    means sometimes getting none. A book runs on whatever answered, and says so.

    CONCURRENT, and this is a correction rather than a tuning choice. Acquiring
    one at a time made the acquisition itself the slowest part of the book:
    measured on the five-pod Tactique run, 27 minutes to get five pods against
    13.7 to use them. Worse, the cost is quadratic in the wrong direction --
    a pod bills from the moment it is deployed, so pod 1 paid for the entire
    boot of pods 2 through 5 while doing nothing. Twelve pods that way would
    spend more on waiting than on narrating.

    The original worry -- five half-placed pods to unwind against a thin market
    -- is handled rather than avoided: every pod is registered at deploy time,
    each acquisition terminates its own failures, and whatever did not serve
    simply is not in the pool. There is nothing to unwind that sweep_orphans
    does not already cover.

    `on_ready(pod_id, base_url, swarm)` is called the instant each pod answers,
    which is what lets the caller start narrating on the first one while the
    rest are still pulling the image.
    """
    count = count or POD_COUNT
    pool = pool if pool is not None else pod_pool.PodPool()
    ready = 0
    refusals = []

    async def one(n):
        nonlocal ready
        try:
            pod_id, swarm, base_url = await acquire_serving_pod(health_path)
        except DeployRefusedError as e:
            # No funds, no such template, a server that ignores the token:
            # waiting fixes none of them, and the full message says what does.
            refusals.append(e)
            print(f"   ⚠️  Pod {n} of {count} refused: {e}")
            return None
        except Exception as e:
            # Whatever is already serving is still a usable book. Raising here
            # would throw away pods that are working and billing.
            print(f"   ⚠️  Pod {n} of {count} did not come up ({str(e)[:60]}).")
            return None
        pool.add(pod_id, base_url)
        ready += 1
        print(f"   ➕ Pod {n} of {count} serving at {base_url} "
              f"({ready} ready so far)")
        if on_ready:
            # Deliberately awaited rather than fired and forgotten: the caller
            # spawns workers here, and a failure to do so must surface as this
            # pod failing rather than as a pod that serves and is never used.
            await on_ready(pod_id, base_url, swarm)
        return swarm

    swarms = await asyncio.gather(*[one(n) for n in range(1, count + 1)])
    swarms = [s for s in swarms if s]

    if not pool:
        if refusals:
            # Found by layer 3 (tools/chaos, out-of-funds): "Top up at ..." was
            # cut to 60 characters and the run ended "No pod served".
            raise refusals[0]
        raise Exception("No pod served; nothing to generate on.")
    if len(pool) < count:
        print(f"\n⚠️  Running on {len(pool)} pod(s) rather than {count}. "
              "The book still completes, more slowly.")
    # The pool can be mixed -- the preference list falls through cards -- so the
    # per-pod worker count is the smallest any of them declared. Sizing every
    # pod by the largest would queue requests on the weakest.
    return pool, min(swarms)

# Pinned by digest: MFA 3.3.10, the version the pods run (docker/cosyvoice) and
# the one every book in the library was aligned with. The aligner decides where
# each word boundary lands, and Docker Hub's `latest` moved to 3.4.2 in 2026-09,
# so a fresh pull on another machine would have re-timed every new book.
MFA_IMAGE = ("mmcauliffe/montreal-forced-aligner@sha256:"
             "67b33020220fef0010974fe920230e463192fc8bd8f29176bc3ac3c7d35f8538")


def run_mfa_align(mfa_dir, corpus_rel, output_rel, beam=None, retry_beam=None,
                  language=DEFAULT_LANGUAGE):
    """Run one MFA pass. mfa_dir is mounted at /data so the dict/model resolve.

    corpus_rel and output_rel are paths relative to mfa_dir, which lets a retry
    pass work on an isolated subset without MFA reusing the first pass's
    cached corpus state.
    """
    dict_name, acoustic_name = mfa_files()
    # --rm: every result leaves through the /data mount, so the container is
    # only scratch. Without it each pass left one behind inside Docker's disk
    # image -- six were found at 240-870MB each, 2.2GB, on a Mac where disk is
    # what stops a book after its GPU has been paid for. Docker Desktop does not
    # hand that space back to macOS promptly, so the leak is best not started.
    cmd = [
        "docker", "run", "--rm", "--platform", "linux/amd64",
        "-e", "KMP_AFFINITY=disabled", "-v", f"{mfa_dir}:/data",
        # On Linux, as this user: a root-owned output/ could not be cleared
        # before the next book (core/platform_support.py).
        *platform_support.docker_user_args(),
        MFA_IMAGE,
        "mfa", "align", "/data" if corpus_rel == "." else f"/data/{corpus_rel}",
        f"/data/{dict_name}", f"/data/{acoustic_name}",
        f"/data/{output_rel}", "--output_format", "json", "--clean",
    ]
    if beam:
        cmd += ["--beam", str(beam), "--retry_beam", str(retry_beam or beam * 4)]
    return subprocess.run(cmd).returncode


# --- ALIGNMENT ON THE PODS -------------------------------------------------
#
# Montreal Forced Aligner runs on the machines that generated the audio, not on
# the reader's Mac. The Mac is where a long book went to die: MFA ships x86-only,
# so on Apple Silicon it runs under Rosetta in Docker's 4GB VM at 14.7x realtime
# -- 65 minutes for Redeeming Love's sixteen hours, against about 25 to generate
# them. Parallelising it there is worse, not better: --single_speaker --num_jobs 6
# measured 1,822s against 118s for the same 24 chunks.
#
# On the pods it is close to free. They are native x86 with a dozen-plus cores
# that sit idle while the GPU works, they already hold the audio they just
# produced, and with twelve pods the work divides twelve ways.
#
# The fallback is the whole design. A pod built from an older image answers
# /align with 404, and the run simply aligns locally as it always did -- slower,
# never broken. That is checked BEFORE generation starts, because discovering it
# afterwards would mean the pods are already gone.


def pod_auth_headers():
    """The pod's token, for every request but /health.

    The server refuses every other path without it (server/custom_api.py,
    _OPEN_PATHS). pods_can_align once asked /align/health bare: every pod
    answered 401, read as "cannot align", and every book was aligned on the
    Mac (2026-10-10: "0 of N pod(s) can align").
    """
    token = os.getenv("TTS_API_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


async def pods_can_align(pool):
    """True only if every pod in the pool can align its own output.

    Every pod, not any: a mixed pool would leave the chunks from the older pods
    to be aligned on the Mac anyway, and a book is only as fast as the part that
    still has to happen there. Asked once generation has finished, while the
    pods still exist; chunks are retained on every pod regardless, so the
    answer can wait until the pool is complete.
    """
    if not len(pool):
        return False
    async with httpx.AsyncClient() as client:
        async def ask(url):
            try:
                r = await client.get(f"{url}/align/health", headers=pod_auth_headers(), timeout=20.0)
                return r.status_code == 200 and bool(r.json().get("available"))
            except Exception:
                return False
        answers = await asyncio.gather(*[ask(u) for u in pool.urls()])
    if all(answers):
        print(f"   🧠 All {len(answers)} pod(s) can align their own output; "
              "alignment will not run on this Mac.")
        return True
    print(f"   ℹ️  {sum(answers)} of {len(answers)} pod(s) can align; "
          "falling back to local alignment (slower, unchanged).")
    return False


# Takes a chunk the aligner refuses may get, at most. MFA refusing a chunk at
# its default beam is the narrator skipping text: Twisted (2026-10-11) had 8
# such chunks, and the Whisper scan (tools/narration_scan.py) heard a skip in
# every one of them and in no other chunk.
CHUNK_RETAKES = int(os.getenv("CHUNK_RETAKES", "2"))


def refused_chunks(narration_plan, mfa_dir, mfa_output_dir):
    """The plan's chunks with audio and no alignment, in plan order."""
    out = []
    for chunk in narration_plan:
        base = chunk["filename"][:-4] if chunk["filename"].endswith(".mp3") else chunk["filename"]
        if (os.path.exists(os.path.join(mfa_dir, base + ".wav"))
                and not os.path.exists(os.path.join(mfa_output_dir, base + ".json"))):
            out.append(chunk)
    return out


def _file_digest(path):
    import hashlib
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


async def retake_refused(pool, narration_plan, mfa_dir, mfa_output_dir, db_path, run_id,
                         language, key_to_name, provider, retakes=None, worker=None, align=None):
    """Narrate again, while the pods still exist, the chunks their aligner refused.

    A refused chunk is one whose audio does not say its text: the narrator
    skipped words, or babbled. Each retake goes through the narration worker
    itself (same text, same delivery, no cache) under its own run id, so the
    pods then align only the retakes; a chunk that aligns is done. A retake
    that comes back byte for byte the same is not asked again. Whatever is
    still refused keeps its last take and goes on to the local aligner's wider
    beam, as before. Returns the chunk names still refused.
    """
    retakes = CHUNK_RETAKES if retakes is None else retakes
    worker = worker or runpod_worker
    align = align or align_on_pods
    pending = refused_chunks(narration_plan, mfa_dir, mfa_output_dir)
    if not pending or not len(pool) or retakes <= 0:
        return [c["filename"][:-4] for c in pending]
    refused_first = list(pending)
    for take in range(2, retakes + 2):
        print(f"\n🔁 {len(pending)} chunk(s) the aligner refused: narrating them again (take {take}).")
        before = {}
        with sqlite3.connect(db_path, timeout=30.0) as conn:
            for c in pending:
                base = c["filename"][:-4]
                before[base] = _file_digest(os.path.join(mfa_dir, base + ".wav"))
                # The take being replaced: its row goes, the worker reserves a new one.
                conn.execute("DELETE FROM chapters WHERE book_id = ? AND audio_filename = ?",
                             (c["book_id"], c["filename"]))
        take_run = f"{run_id}r{take}"[-64:]
        queue = asyncio.Queue()
        for c in pending:
            queue.put_nowait({**c, "run_id": take_run, "retake": take})
        urls = pool.urls()
        n = max(1, min(len(pending), 8 * len(urls)))
        for _ in range(n):
            queue.put_nowait(None)
        # As the narration's own client: each request carries the provider's timeout.
        async with httpx.AsyncClient(limits=httpx.Limits(max_connections=64,
                                                         max_keepalive_connections=64)) as client:
            await asyncio.gather(*(worker(f"R{i + 1}", queue, client, mfa_dir, db_path, urls[i % len(urls)],
                                          provider, pod_urls=pool.urls, pool=pool) for i in range(n)),
                                 return_exceptions=True)
        try:
            await align(pool, mfa_dir, take_run, language, key_to_name)
        except Exception as e:      # noqa: BLE001 -- never fatal: the local aligner remains
            print(f"   ⚠️  The pods could not align the retakes ({str(e)[:80]}).")
        still = refused_chunks(pending, mfa_dir, mfa_output_dir)
        same = [c for c in still if _file_digest(os.path.join(mfa_dir, c["filename"][:-4] + ".wav"))
                == before[c["filename"][:-4]]]
        if same:
            print(f"   ⚠️  {len(same)} retake(s) came back identical to the take they replace: "
                  "not asked again.")
        pending = [c for c in still if c not in same]
        print(f"   ✅ {len(refused_first) - len(refused_chunks(refused_first, mfa_dir, mfa_output_dir))} "
              f"of {len(refused_first)} now align.")
        if not pending:
            break
    left = [c["filename"][:-4] for c in refused_chunks(narration_plan, mfa_dir, mfa_output_dir)]
    if left:
        print(f"   ℹ️  Still refused, kept as they are and aligned locally on the wider beam: "
              f"{', '.join(left[:6])}{' ...' if len(left) > 6 else ''}")
    return left


async def align_on_pods(pool, mfa_dir, run_id, language, key_to_name):
    """Collect word timings from every pod. Returns the chunk names written.

    Each pod aligns only what it generated, which is why this asks all of them
    and merges: workers are bound to a pod for the life of the run, so the
    book's chunks are spread across the pool with no single pod holding them all.

    The model URLs travel with the request rather than living on the pod.
    core/languages.py pins the dictionary and the acoustic model per language,
    to the same version, because those decide where every word boundary lands --
    a second copy of that table on the pod could drift from this one and move
    the timestamps of books already in the library.
    """
    out_dir = os.path.join(mfa_dir, "output")
    os.makedirs(out_dir, exist_ok=True)

    urls = mfa_downloads()
    dict_url = next(u for n, u in urls.items() if n.endswith(".dict"))
    acoustic_url = next(u for n, u in urls.items() if n.endswith(".zip"))

    headers = pod_auth_headers()
    written = set()

    async def one(base_url):
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{base_url}/align",
                data={"run_id": run_id, "dict_url": dict_url,
                      "acoustic_url": acoustic_url, "language": language},
                headers=headers, timeout=POD_ALIGN_TIMEOUT_SECONDS)
            r.raise_for_status()
            return base_url, r.json()

    results = await asyncio.gather(*[one(u) for u in pool.urls()],
                                   return_exceptions=True)

    for res in results:
        if isinstance(res, Exception):
            # Not fatal: whatever this pod held is simply not aligned yet, and
            # align_with_retry picks up anything missing from output/ locally.
            print(f"   ⚠️  A pod could not align its chunks ({str(res)[:80]}); "
                  "those will be aligned locally.")
            continue
        base_url, payload = res
        for key, data in (payload.get("aligned") or {}).items():
            name = key_to_name.get(key)
            if not name:
                # A key we did not send. Ignored rather than trusted: writing it
                # would create an output file matching no chunk, and
                # process_mfa_factory would then read a sidecar that is not there.
                continue
            with open(os.path.join(out_dir, name + ".json"), "w",
                      encoding="utf-8") as f:
                json.dump(data, f)
            written.add(name)
        missing = payload.get("missing") or []
        if missing:
            print(f"   ⚠️  {base_url} skipped {len(missing)} chunk(s); "
                  "they fall through to the local aligner.")

    print(f"   🧠 {len(written)} chunk(s) aligned on the pods.")
    return written


def _align_subset(mfa_dir, mfa_output_dir, names, tag, beam=None,
                  retry_beam=None, language=DEFAULT_LANGUAGE):
    """Align just `names`, in their own corpus directory. Returns recovered names.

    Its own directory because MFA caches corpus state per corpus root and
    --clean only resets the one it is pointed at; running a subset against the
    full directory re-reads every chunk. Returns None if MFA itself crashed,
    which the caller distinguishes from "ran and skipped things".
    """
    work = os.path.join(mfa_dir, tag)
    out = os.path.join(mfa_dir, tag + "_output")
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    try:
        for name in names:
            for ext in (".wav", ".txt"):
                srcf = os.path.join(mfa_dir, name + ext)
                if os.path.exists(srcf):
                    shutil.copyfile(srcf, os.path.join(work, name + ext))
        rc = run_mfa_align(mfa_dir, tag, tag + "_output", beam=beam,
                           retry_beam=retry_beam, language=language)
        recovered = []
        if os.path.isdir(out):
            for f in os.listdir(out):
                if f.endswith(".json"):
                    shutil.copyfile(os.path.join(out, f),
                                    os.path.join(mfa_output_dir, f))
                    recovered.append(f[:-5])
        return None if (rc != 0 and not recovered) else recovered
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)


def mfa_unready(mfa_dir):
    """Why MFA cannot run here at all (Docker down, a model file missing), or None.

    Asked when a pass ends in an error with nothing aligned, to tell "MFA could
    not run" (fatal: no chunk will align) from "MFA ran and aligned none of
    these" (the remainder after the pods is exactly the chunks that are hard
    to align, and MFA exits 1 when it aligns none of them).
    """
    missing = [f for f in mfa_files() if not os.path.exists(os.path.join(mfa_dir, f))]
    if missing:
        return f"the model file(s) {', '.join(missing)} are missing from {mfa_dir}"
    try:
        if subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode != 0:
            return "Docker is not running"
    except (OSError, subprocess.TimeoutExpired):
        return "Docker is not installed or does not answer"
    return None


def align_with_retry(mfa_dir, mfa_output_dir, language=DEFAULT_LANGUAGE):
    """Align every chunk, retrying the ones MFA skips with a wider beam.

    MFA exits 0 even when it silently skips individual utterances, and it does
    so routinely -- 8 of 75 chunks on "A Doll's House", 7 of 16 on the book
    before it. Widening the beam is the documented remedy, so the failures get
    a second pass on their own rather than being written off. Returns the
    chunks still unaligned afterwards.
    """
    submitted = {f[:-4] for f in os.listdir(mfa_dir) if f.endswith(".wav")}
    # Anything already in output/ was aligned by the pod that generated it.
    # Aligning it again here would be the 65-minute path this exists to avoid,
    # and would overwrite timings with identical ones for no reason.
    preexisting = {f[:-5] for f in os.listdir(mfa_output_dir) if f.endswith(".json")}
    todo = submitted - preexisting

    if not todo:
        print(f"   ✅ All {len(submitted)} chunks were aligned on the pods; "
              "nothing to do locally.")
        return []

    if preexisting:
        # A partial result: some pods could align and some could not, or a pod
        # died with chunks outstanding. Only the remainder runs here, on the
        # same subset machinery the wider-beam retry already uses.
        print(f"   🧩 {len(preexisting)} chunk(s) aligned on the pods; "
              f"aligning the remaining {len(todo)} locally.")
        if _align_subset(mfa_dir, mfa_output_dir, sorted(todo), "local",
                         language=language) is None:
            # Twisted, 2026-10-11: the pods aligned 2,034 of 2,042 chunks, the
            # 8 left were the ones MFA cannot align (text the narrator
            # skipped), MFA exits 1 when it aligns none, and this used to
            # throw away the whole 13.8-hour book. A chunk's audio is placed
            # whether or not it aligned: on to the wider beam, as for any skip.
            why = mfa_unready(mfa_dir)
            if why:
                raise Exception(f"❌ FATAL: Montreal Forced Aligner cannot run: {why}.")
            print(f"   ⚠️  MFA aligned none of the {len(todo)} chunk(s) left after the pods.")
    elif run_mfa_align(mfa_dir, ".", "output", language=language) != 0:
        raise Exception("❌ FATAL: Montreal Forced Aligner crashed! Is Docker Desktop running? Are the dictionary files in mfa_workspace?")

    aligned = {f[:-5] for f in os.listdir(mfa_output_dir) if f.endswith(".json")}
    unaligned = sorted(submitted - aligned)
    if not unaligned:
        print(f"   ✅ MFA aligned all {len(aligned)} chunks.")
        return []

    print(f"\n   ⚠️  MFA skipped {len(unaligned)} of {len(submitted)} chunks. Retrying them with a wider beam...")
    retry_dir = os.path.join(mfa_dir, "retry")
    retry_out = os.path.join(mfa_dir, "retry_output")
    shutil.rmtree(retry_dir, ignore_errors=True)
    shutil.rmtree(retry_out, ignore_errors=True)
    os.makedirs(retry_dir, exist_ok=True)
    try:
        for name in unaligned:
            for ext in (".wav", ".txt"):
                srcf = os.path.join(mfa_dir, name + ext)
                if os.path.exists(srcf):
                    shutil.copyfile(srcf, os.path.join(retry_dir, name + ext))

        run_mfa_align(mfa_dir, "retry", "retry_output", beam=100, retry_beam=400,
                      language=language)

        recovered = []
        if os.path.isdir(retry_out):
            for f in os.listdir(retry_out):
                if f.endswith(".json"):
                    shutil.copyfile(os.path.join(retry_out, f),
                                    os.path.join(mfa_output_dir, f))
                    recovered.append(f[:-5])
        still = sorted(set(unaligned) - set(recovered))
        if recovered:
            print(f"   ✅ Recovered {len(recovered)} chunk(s) on the wider beam.")
        if still:
            # Say which kind of failure this is. "Only the map is missing" is
            # true of an ordinary alignment failure and false of a truncated
            # chunk, where MFA fails precisely *because* the audio does not
            # contain all the words -- and reporting the reassuring version of
            # a truncation is how 2,125 characters of Sleepy Hollow reached a
            # finished audiobook looking like a sync problem.
            truncated = {name for name in still
                         if os.path.exists(os.path.join(mfa_dir, name + ".wav.truncated"))}
            print(f"   ‼️  {len(still)} chunk(s) still unaligned after retry:")
            for name in still:
                print(f"      - {name}" + ("   [TRUNCATED by the model]" if name in truncated else ""))
            if truncated:
                print(f"   ‼️  {len(truncated)} of them hit the model's output ceiling, so part of")
                print("   ‼️  their text was never narrated. That is missing audio, not just a")
                print("   ‼️  missing map -- shorten these chunks and regenerate the book.")
            if set(still) - truncated:
                print(f"   ‼️  The other {len(set(still) - truncated)} are narrated and kept in the book;")
                print("   ‼️  only the fine-grained sync map is missing inside those stretches.")
            print()
        return still
    finally:
        shutil.rmtree(retry_dir, ignore_errors=True)
        shutil.rmtree(retry_out, ignore_errors=True)


def chunk_label(index):
    """Spreadsheet-style label for a chunk within its chapter: A..Z, AA, AB...

    This used to be chr(ord(prev) + 1), which walks straight out of the
    alphabet at the 27th chunk and yields '[', '\\' and ']'. Those are glob
    and regex metacharacters, and the stitcher's filename pattern rejected
    them outright -- so any chapter longer than 26 chunks silently lost
    everything past the 26th from the finished audio.
    """
    label = ""
    index += 1
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        label = chr(65 + remainder) + label
    return label


# --- 1. THE PRODUCER (CARTOGRAPHER) ---
async def cartographer_producer(epub_path, audio_queue, book_id, language=None):
    """Plan the narration. `language` picks the sentence splitter's rules.

    It is the language the EPUB *declares*, which can be None -- the detection
    fallback needs this function's own output and so cannot run before it. An
    unknown language splits by English rules, which moves a split point and
    never loses a word (see core/chunking).
    """
    print(f"\n🗺️  Cartographer: Exploring {os.path.basename(epub_path)}...")
    with zipfile.ZipFile(epub_path, 'r') as z:
        # Each file's name, and its entries with a #fragment: where its chapters
        # start when one file holds several (core.structure.chapter_starts).
        toc_lookup, toc_anchors = read_toc(z)
        toc_labels = set(toc_lookup.values()) | {lbl for entries in toc_anchors.values() for _, lbl, _ in entries}

        container = etree.fromstring(z.read('META-INF/container.xml'))
        opf_path = container.xpath('//n:rootfile/@full-path', namespaces={'n': 'urn:oasis:names:tc:opendocument:xmlns:container'})[0]
        opf_tree = etree.fromstring(z.read(opf_path))
        ns_opf = {'opf': 'http://www.idpf.org/2007/opf'}
        manifest = {item.get('id'): item.get('href') for item in opf_tree.xpath('//opf:item', namespaces=ns_opf)}
        spine_hrefs = [manifest[itemref.get('idref')] for itemref in opf_tree.xpath('//opf:itemref', namespaces=ns_opf) if itemref.get('idref') in manifest]

        total_files = len(spine_hrefs)
        opf_dir = os.path.dirname(opf_path)
        doc_fragment_counter = 0
        audio_chapter_counter = 0
        kept_documents = 0
        previous_chapter = None   # (spine index, title of its last chapter)

        structural_verdict = structural_junk(z, opf_tree, opf_path)
        # The book's own title, so a running title can be said once rather
        # than at the top of every chapter file (32 times in Screwtape).
        _dc = {'dc': 'http://purl.org/dc/elements/1.1/'}
        book_title = (opf_tree.xpath('//dc:title/text()', namespaces=_dc) or [""])[0]
        book_author = (opf_tree.xpath('//dc:creator/text()', namespaces=_dc) or [""])[0]
        repeat_state = {}
        errata = load_errata(os.path.dirname(epub_path))
        errata_used = set()
        if errata:
            print(f"   📝 {len(errata)} errata correction(s) for this book")
        if structural_verdict:
            print(f"   🏷️  The book declares {len(structural_verdict)} non-narrative section(s).")
        narration_plan = []
        unjudged = []   # documents kept because the junk filter could not judge them

        # Built once per book rather than per document, so a shadow run can
        # tally agreement across the whole book and a client is opened once.
        junk_provider = get_junk_provider()
        print(f"   🧠 Junk filter: {getattr(junk_provider, 'display', junk_provider.name)}")

        # Blocks inside a KEEP document, asked of JEV one by one. Off unless
        # JUNK_BLOCK_FILTER says otherwise; see core/block_filter.py for the
        # run rule that keeps a lone mid-chapter verdict from deleting text.
        # The narration director (core/director.py): JEV decides each
        # paragraph's delivery while the plan is built, before anything bills.
        # Only for a provider that follows instructions; the free route
        # (Kokoro) is never directed and needs no key.
        direct_mode = director.mode(PROVIDERS.get(os.getenv("TTS_PROVIDER", "kokoro").strip().lower()))
        director_jev = JevProvider() if direct_mode == "on" else None
        book_directions = []
        if director_jev:
            print(f"   🎭 Narration director: JEV, {len(director.DELIVERIES)} deliveries")

        block_mode = block_filter.mode()
        block_provider = build_junk_provider("jev") if block_mode != "off" else None
        if block_provider:
            print(f"   🧽 Block filter: {block_mode} (JEV, threshold {block_filter.BLOCK_THRESHOLD}, "
                  f"runs of {block_filter.MIN_RUN}+ or at a document's edge)")

        for index, href in enumerate(spine_hrefs):
            doc_fragment_counter += 1
            full_href = os.path.join(opf_dir, href).replace('\\', '/') if opf_dir else href

            try:
                html_bytes = z.read(full_href)
                tree = parse_xhtml(html_bytes)
                # Leaf blocks and the loose text between them (StrayRun), in
                # reading order: text in no leaf block used to be dropped.
                block_texts = narratable_blocks(tree)

                # Publisher boilerplate can sit *inside* a document alongside the
                # real work -- in pg1080.epub the essay and the entire Project
                # Gutenberg licence share one spine file, and the licence was 46%
                # of the finished audiobook. No document-level verdict can
                # separate those, so cut on the markers before anything else
                # looks at the text.
                block_texts, boiler_words, boiler_reason = trim_boilerplate_blocks(block_texts)
                if boiler_reason and "header" in boiler_reason:
                    block_texts, _front = drop_gutenberg_front_matter(
                        tree, block_texts, toc_anchors.get(full_href, []))
                    if _front:
                        print(f"   📰 {href}: dropped {len(_front)} block(s) of the source edition's "
                              "front matter (before the first TOC entry)")
                    block_texts, _imprint = drop_gutenberg_title_page(
                        tree, block_texts, toc_anchors.get(full_href, []), book_title, book_author)
                    if _imprint:
                        print(f"   📰 {href}: dropped {len(_imprint)} line(s) of the source edition's "
                              "title page (the title and author stay)")

                # The other half of the same problem, and a bigger one on a
                # commercial EPUB than Gutenberg's licence ever was. A book
                # converted from a print PDF carries the typesetter's furniture
                # as ordinary blocks -- page numbers, the production footer, and
                # the running head spelled one letter per block -- and Redeeming
                # Love is 49.1% of those. They are cut before chunking so the
                # sync map never contains a node for a page number.
                block_texts, artifact_counts = drop_print_artifacts(block_texts)
                if any(artifact_counts.values()):
                    print(f"   🧹 {href}: dropped "
                          + ", ".join(f"{v} {k}" for k, v in artifact_counts.items() if v))
                # Picture captions and the list of illustrations: printed for
                # the eye, and read mid-story they repeat the sentence before.
                block_texts, picture_counts = drop_picture_text(block_texts)
                if any(picture_counts.values()):
                    print(f"   🖼️  {href}: dropped "
                          + ", ".join(f"{v} {k}" for k, v in picture_counts.items() if v))
                # The book's own printed table of contents, read aloud it is a
                # list of titles (a Maupassant collection ends on one).
                block_texts, _contents = drop_printed_contents(block_texts, toc_labels)
                if _contents:
                    print(f"   📑 {href}: dropped the printed table of contents ({len(_contents)} block(s))")

                # A block that is nothing but a chapter number is spelled out by
                # the model -- "XXV" is read as three letters, measured at 0.78s
                # against 0.35s for the eight-letter WORMWOOD. Said properly
                # here, before the text is chunked, so the sync node carries the
                # words the listener actually hears.
                _rewritten = say_chapter_numbers([t for _, t in block_texts],
                                                 language=language or DEFAULT_LANGUAGE)
                block_texts = [(b, new_t) for (b, _), new_t in zip(block_texts, _rewritten)]
                block_texts, _title_repeats, _says_title = drop_repeated_title(
                    block_texts, book_title, repeat_state)
                block_texts, _credits = trim_credits(block_texts)
                if _title_repeats:
                    print(f"   🔁 {href}: the title again ({len(_title_repeats)}x) -- said once already")
                if _credits:
                    print(f"   🧾 {href}: dropped {len(_credits)} block(s) of e-text credits")
                if boiler_reason:
                    print(f"   ✂️  {href}: removed {boiler_words} words of boilerplate ({boiler_reason})")

                leaf_blocks = [block for block, _ in block_texts]
                raw_text_lines = [text for _, text in block_texts]

                full_chapter_text = "\n\n".join(raw_text_lines)
                wc = len(full_chapter_text.split())

                # The floor skips covers and bare title pages; a dedication is
                # the author's and is kept without asking a model.
                dedication = is_dedication(raw_text_lines)
                if wc < 20 and not dedication: continue

                rho = index / max(1, (total_files - 1))

                # The book may already have declared this section non-narrative.
                # Publishers write it down in the OPF guide or the navigation
                # landmarks; believing them beats guessing from an embedding.
                declared = structural_verdict.get(full_href)
                # Recognised from its own words, before any classifier: a model
                # that is missing or down must not read a sign-up page aloud.
                furniture = "" if (declared or dedication) else publisher_furniture(raw_text_lines, rho)
                if declared:
                    verdict = "JUNK"
                    print(f"   🏷️  {href}: JUNK, declared by the book ({declared})")
                elif dedication:
                    verdict = "KEEP"
                    print(f"   💐 {href}: the dedication, kept")
                elif furniture:
                    verdict = "JUNK"
                    print(f"   🏷️  {href}: JUNK, {furniture}")
                else:
                    try:
                        decision = await junk_provider.classify(
                            full_chapter_text, wc, rho, href)
                        verdict = decision.verdict
                    except Exception as e:
                        # An outage costs the verdict, never the book. On
                        # 2026-10-07 the local classifier could not load its
                        # model (Hugging Face's client was closed) and every
                        # chapter was skipped as an error: "Nothing to narrate".
                        verdict = "KEEP"
                        unjudged.append(href)
                        print(f"   ‼️  {href}: the junk filter failed ({type(e).__name__}: {str(e)[:120]}); "
                              "kept without a verdict.")
                narration_plan.append((href, verdict, wc,
                                       boilerplate_phrases_in(full_chapter_text) if verdict == "KEEP" else []))

                if verdict == "KEEP" and _says_title:
                    # Only now is the title really said -- a document skipped for
                    # length or judged junk must not use up the one time.
                    repeat_state["title_said"] = True

                if verdict == "KEEP" and block_provider and not dedication:
                    _exempt = {i for i, (_, t) in enumerate(block_texts)
                               if opening_credit(t, book_title, book_author)}
                    block_texts, block_report = await block_filter.filter_blocks(
                        block_provider, block_texts, rho, href, apply=(block_mode == "on"),
                        exempt=_exempt)
                    for line in block_filter.describe(block_report, block_mode == "on"):
                        print(line)
                    if block_mode == "on" and block_report.dropped:
                        # Keep the plan honest about what will actually be read.
                        h, v, w, flags = narration_plan[-1]
                        narration_plan[-1] = (h, v, w - block_report.dropped_words, flags)

                if verdict == "KEEP":
                    kept_documents += 1
                    # A document the table of contents does not name is called
                    # after its own first short heading, else by its place
                    # among the narrated sections. It used to be named after
                    # the running chunk count, so Redeeming Love's epigraph
                    # (its 3rd section) was the chapter "Part_4".
                    fallback = next((t for b, t in block_texts
                                     if b.tag in ("h1", "h2", "h3", "h4") and 0 < len(t) <= 60),
                                    f"Part {kept_documents}")
                    chapter_title = toc_lookup.get(full_href, fallback)
                    # A file's only navigation entry can point at boilerplate we
                    # have just removed: pg1080's single navPoint for the essay
                    # targets #pg-footer-heading, so every chapter of A Modest
                    # Proposal was published as "THE FULL PROJECT GUTENBERG
                    # LICENSE". If the label names boilerplate it is not a
                    # chapter name, whatever the navigation says.
                    if boilerplate_phrases_in(chapter_title):
                        chapter_title = f"Part {kept_documents}"
                    # A document the table of contents names nothing in joins
                    # the chapter before it (core/structure, the user's rule).
                    chapter_title = untitled_document_title(
                        chapter_title,
                        has_entry=full_href in toc_lookup or bool(toc_anchors.get(full_href)),
                        book_has_toc=bool(toc_lookup or toc_anchors),
                        previous_title=previous_chapter[1] if previous_chapter else None)

                    # --- THE CHUNKING ENGINE ---
                    # Size is a correctness bound, not a preference: Voxtral
                    # stops at 163.84s without saying so, and the old rule
                    # ("accumulate whole paragraphs until 350 words") checked
                    # only after adding, so it had no ceiling at all -- one long
                    # paragraph was one oversized chunk, and its tail was never
                    # narrated. core/chunking.py keeps paragraphs whole wherever
                    # it can and splits at sentences only where it must; see its
                    # docstring for why each rule is there.
                    prepared = []
                    # Iterate the (block, text) pairs, NOT leaf_blocks. This
                    # loop used to re-extract each block's text from the DOM,
                    # which silently discarded every rewrite made above it --
                    # say_chapter_numbers turned The Screwtape Letters' bare
                    # "IX" into "Nine." and the narration got "IX"
                    # back anyway, spelled out as two letters, 28 times.
                    for n_block, (block, rewritten) in enumerate(block_texts):
                        # core.narration owns the final text -- caps, typography,
                        # forced punctuation -- so tools/narration_review.py
                        # reads exactly what is sent here. It sees the next
                        # block because a page-split sentence must not get a
                        # full stop in the middle.
                        following = block_texts[n_block + 1][1] if n_block + 1 < len(block_texts) else None
                        clean_text = narrate_text(block.tag, rewritten, following,
                                                  language or DEFAULT_LANGUAGE)
                        if clean_text and errata:
                            clean_text = apply_errata(clean_text, errata, errata_used)
                        if clean_text is None:
                            continue

                        prepared.append((block, clean_text))

                    # One file may hold several chapters: each is chunked on its
                    # own, so no audio file straddles two, and named after its
                    # TOC entry (the stitcher, ABS and the device's map all
                    # group chunks by this title).
                    starts = chapter_starts(tree, prepared, toc_anchors.get(full_href, []))
                    # Text above this file's first entry ends the chapter the
                    # previous file was in, when that file was narrated.
                    continues = previous_chapter[1] if previous_chapter and previous_chapter[0] == index - 1 else None
                    segments = chapter_segments(prepared, starts, chapter_title, continues=continues)
                    if segments:
                        previous_chapter = (index, segments[-1][0])
                    if len(segments) > 1:
                        print(f"   📑 {href}: {len(segments)} chapters in one file")
                    # A request carries one delivery, so a chunk never spans two:
                    # paragraphs are packed within runs of the same direction.
                    chunks = []
                    for seg_title, seg_blocks in segments:
                        safe = re.sub(r'[\\/*?:"<>|]', "", seg_title)
                        if director_jev:
                            directions = await director.direct_blocks(
                                [t for _, t in seg_blocks], language or DEFAULT_LANGUAGE, director_jev,
                                book=f"{book_title} by {book_author}", chapter=seg_title)
                            book_directions.extend(directions)
                            runs = director.group_by_direction(seg_blocks, directions)
                        else:
                            runs = [(None, seg_blocks)]
                        for direction, run in runs:
                            for packed in pack_blocks(run, language=language or DEFAULT_LANGUAGE):
                                chunks.append((safe, packed, direction))
                    for sub_chunk_index, (safe_title, packed, direction) in enumerate(chunks):
                        # Offsets are recomputed per chunk because a paragraph
                        # that had to be split contributes a different slice of
                        # itself to each one. They are carried on the node even
                        # though sync_nodes has nowhere to put them yet -- that
                        # column is the other half of SAC-43, and the engine
                        # computing them and throwing them away is what made
                        # sentence-level sync look like a bigger job than it is.
                        node_map = []
                        cursor_idx = 0
                        for block, piece in packed:
                            end_idx = cursor_idx + len(piece)
                            node_map.append({
                                "xpath": get_koreader_xpath_lxml(doc_fragment_counter, block),
                                "text": piece,
                                "start_idx": cursor_idx,
                                "end_idx": end_idx,
                                "char_count": len(piece),
                            })
                            cursor_idx = end_idx + 1

                        audio_chapter_counter += 1
                        filename = f"{audio_chapter_counter:03d}_{chunk_label(sub_chunk_index)}_{safe_title}.mp3"

                        await audio_queue.put({
                            "book_id": book_id,
                            "chapter_index": audio_chapter_counter,
                            "filename": filename,
                            "text": chunk_text(packed),
                            "direction": direction.as_dict() if direction and not direction.plain else None,
                            "nodes": node_map,
                            # Carried so a chunk that has to be split later
                            # cuts on this language's sentence rules rather
                            # than falling back to English inside the worker,
                            # which has no other way to know.
                            "language": language or DEFAULT_LANGUAGE,
                        })
                        print(f"   🌊 Streamed Sub-Chunk to Audio Queue: {filename}")
            except Exception as e:
                # With its stack: the message alone ("the client has been
                # closed") could not say which client had lost a whole book.
                print(f"   ❌ Error processing {href}: {e}")
                traceback.print_exc(file=sys.stdout)
                continue

    # Say what will be narrated before a paid GPU turns any of it into audio.
    # The first real run put 46% of an audiobook's runtime into the Project
    # Gutenberg licence and nothing said so until the finished file was played.
    kept = sum(wc for _, verdict, wc, _ in narration_plan if verdict == "KEEP")
    dropped = sum(wc for _, verdict, wc, _ in narration_plan if verdict != "KEEP")
    print("\n   📋 Narration plan:")
    for href, verdict, wc, flags in narration_plan:
        mark = "✅" if verdict == "KEEP" else "🚫"
        note = f"   ⚠️ contains: {', '.join(flags)}" if flags else ""
        print(f"      {mark} {wc:>6} words  {href}{note}")
    total = kept + dropped
    share = (kept / total * 100) if total else 0
    print(f"   📋 Narrating {kept:,} of {total:,} words ({share:.0f}%); skipping {dropped:,}.")
    if unjudged:
        print(f"   ‼️  {len(unjudged)} section(s) kept WITHOUT a junk verdict (the filter failed): "
              f"{', '.join(unjudged[:5])}{' ...' if len(unjudged) > 5 else ''}. "
              "A licence or an index among them would be read aloud.")

    # A shadow run says here whether the two filters would have made the same
    # book, while the plan it refers to is still on the screen.
    if errata:
        stale = [w for w, _ in errata if w not in errata_used]
        print(f"   📝 errata: {len(errata) - len(stale)} of {len(errata)} applied"
              + (f"; {len(stale)} matched nothing (stale?): {stale[:3]}" if stale else ""))
    if hasattr(junk_provider, "summary"):
        print(f"   👥 {junk_provider.summary()}")
    # Part of what this book costs, counted against the budget before any GPU
    # is rented (core/cost_plan.py).
    RUN_SPEND["openrouter"] = sum(junk_spend(p) for p in (junk_provider, block_provider, director_jev))
    await junk_provider.aclose()
    if block_provider:
        print(f"   🧽 block filter ({block_mode}): {block_provider.summary()}")
        await block_provider.aclose()
    if director_jev:
        print(f"   🎭 director: {director.summary(book_directions)} [{director_jev.summary()}]")
        await director_jev.aclose()

    flagged = [(href, flags) for href, verdict, _, flags in narration_plan if verdict == "KEEP" and flags]
    if flagged:
        print("\n   ⚠️  BOILERPLATE SURVIVED THE FILTER — this will be read aloud:")
        for href, flags in flagged:
            print(f"      {href}: {', '.join(flags)}")
        print("   ⚠️  Check the book before spending GPU time, or expect it in the audio.\n")


# Pinned rather than resolved to "latest": the acoustic model decides where word
# boundaries land, so silently upgrading it between runs would move every
# timestamp in books already in the library.



def ensure_mfa_models(mfa_dir, language=DEFAULT_LANGUAGE):
    """Download the MFA dictionary and acoustic model if they are not present.

    These used to be a manual prerequisite that nothing fetched and nothing
    documented, sitting in mfa_workspace/ alongside genuinely disposable scratch
    files -- so clearing that directory quietly removed them, and the failure
    only appeared on the next run. They are ~90MB and freely downloadable, so
    fetching them is strictly better than failing.
    """
    os.makedirs(mfa_dir, exist_ok=True)
    for name, url in mfa_downloads().items():
        path = os.path.join(mfa_dir, name)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            continue
        print(f"   ⬇️  Fetching MFA model {name} (one-off, ~90MB total)...")
        tmp = path + ".part"
        with httpx.stream("GET", url, follow_redirects=True, timeout=300.0) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        os.replace(tmp, path)
        print(f"   ✅ {name} ready.")


def check_prerequisites(mfa_dir, language=None, own_server=False):
    """Fail fast with a clear message if a manual prerequisite is missing,
    before we spend money spinning up a RunPod GPU pod.

    language=None checks everything except the acoustic models: which pair to
    download cannot be decided until the book has been opened, so that check
    runs later -- still before anything bills.
    """
    missing = []

    # Your own GPU (--tts-url) needs no RunPod account at all.
    if not own_server and (not RUNPOD_API_KEY or not RUNPOD_TEMPLATE_ID or not RUNPOD_URL_PATTERN):
        missing.append("RUNPOD_API_KEY / RUNPOD_TEMPLATE_ID / RUNPOD_URL_PATTERN not set in .env "
                       "(or run on your own GPU with --tts-url)")

    # A template id is a plain RunPod id. On 2026-10-07 the .env line was
    # `RUNPOD_TEMPLATE_ID=y1s4i5a5qa#x1y2z3w4v5`: a `#` starts a comment only
    # after a space, so the comment became part of the id, every deploy said
    # "Template not found", and the run waited 45 minutes for capacity.
    if not own_server and RUNPOD_TEMPLATE_ID and not re.fullmatch(r"[A-Za-z0-9_-]+", RUNPOD_TEMPLATE_ID):
        missing.append(f"RUNPOD_TEMPLATE_ID is '{RUNPOD_TEMPLATE_ID}', which is not a RunPod template id. "
                       "A comment after it needs a space before the '#'.")

    if not own_server and RUNPOD_TRANSPORT not in ("proxy", "tcp"):
        missing.append(f"RUNPOD_TRANSPORT is '{RUNPOD_TRANSPORT}'; it must be 'proxy' or 'tcp'")

    # Refused before anything bills, not warned about afterwards. The TCP
    # transport puts the TTS server on a raw public ip:port with no TLS and no
    # network ACL in front of it, so without a shared secret the GPU being
    # billed by the hour is usable by anyone who portscans the host. This is the
    # same hole /upload had before UPLOAD_TOKEN, and it was found there by
    # reading the code rather than by noticing the bill.
    # RunPod's transport only matters when a pod is rented; the CPU route's
    # server listens on 127.0.0.1 alone.
    if ((RUNPOD_TRANSPORT == "tcp" and not own_server) or _uses_our_server()) and not TTS_API_TOKEN:
        missing.append(
            "TTS_API_TOKEN is not set in .env. Without it the GPU server answers "
            "anyone who reaches it -- a raw TCP port, or a proxy URL printed in a "
            "log -- and bills you for their narration. Set the same value in the "
            "RunPod template's environment (openssl rand -hex 32).")

    if shutil.which("docker") is None:
        missing.append("Docker not found on PATH (needed for Montreal Forced Aligner)")
    else:
        if not platform_support.ensure_docker():
            missing.append("Docker is installed but not running (open Docker Desktop and wait until it "
                           "says it is running; the first time, it asks for an administrator's password)")

    if language is not None:
        try:
            ensure_mfa_models(mfa_dir, language)
        except Exception as e:
            missing.append(f"MFA models for '{language}' missing in {mfa_dir} and could not be downloaded: {e}")

    if missing:
        print("\n❌ Cannot start: missing prerequisites:")
        for m in missing:
            print(f"   - {m}")
        raise SystemExit(1)


EBOOK_READY_MARKER = os.path.join("library", ".ebook_ready.json")


def publish_ebook_early(ebook_dir, lib_id, book_id, db_path, book_title):
    """Write the ebook-only manifest and tell the watcher it can ship it now.

    The kepub, the lib_id watermark and the database row are all final before a
    single GPU second is spent -- everything a reader needs in order to start
    reading exists roughly two minutes into a run that will take two hours. It
    was simply never sent until the audio was finished, so a book was unreadable
    for the whole time it was being narrated, and a run that died at hour four
    of five delivered nothing at all.

    audio_state is 'pending' rather than 'none': the difference matters to
    anything deciding whether to wait. 'none' is an ebook-only book at rest,
    which is a legitimate resting state; 'pending' says narration is being made
    right now.

    Nothing here is allowed to fail the run. This is an optimisation on the way
    to the same destination -- if the early publish does not happen the book
    still arrives complete at the end, which is exactly what happened before
    this existed.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=20.0)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("UPDATE books SET audio_state = 'pending' WHERE book_id = ?", (book_id,))
        conn.commit()
        c.execute("SELECT * FROM books WHERE book_id = ?", (book_id,))
        book_dict = dict(c.fetchone())
        book_dict["koreader_md5"] = koreader_partial_md5(book_dict["epub_path"])
        conn.close()

        # No chapters and no sync nodes: the gateway reads an empty chapter list
        # as "ebook only", and _clear_superseded_audio only empties folders the
        # pack actually carries audio for -- so this can never disturb an
        # existing narration of the same book.
        manifest = {"book": book_dict, "chapters": [], "sync_nodes": [],
                    "word_timings": []}
        with open(os.path.join(ebook_dir, "manifest.json"), "w") as mf:
            json.dump(manifest, mf)

        with open(EBOOK_READY_MARKER, "w") as f:
            json.dump({"ebook_dir": ebook_dir, "lib_id": lib_id,
                       "title": book_title}, f)
        print(f"\n📖 EBOOK READY: {book_title} can be read now, while the audio is made.")
    except Exception as e:
        print(f"   ⚠️  Could not publish the ebook early ({e}); "
              "it will still ship with the finished book.")


BUILD_MARKER = os.path.join("library", ".last_build.json")


def write_build_marker(ebook_dir, audio_dir, lib_id):
    """Record which folders this run produced, for the watcher to read.

    The Sync Pack is assembled by mac_ghost, which has no other way to learn
    what the engine just built: it inferred it from directory mtimes, so any
    unrelated touch under library/ebooks sent the wrong book to the gateway.
    Written last, so its presence also means the run got to the end.
    """
    try:
        os.makedirs(os.path.dirname(BUILD_MARKER), exist_ok=True)
        with open(BUILD_MARKER, "w") as f:
            json.dump({"lib_id": lib_id, "ebook_dir": ebook_dir,
                       "audio_dir": audio_dir}, f)
    except OSError as e:
        print(f"   ⚠️  Could not write {BUILD_MARKER} ({e}); "
              "the watcher will fall back to guessing by mtime.")


# --- 2. THE OVERSEER (MAIN) ---
def publish_ebook_only(epub_path):
    """Publish the book to the catalog without generating an audiobook.

    Reading and listening are separate decisions, and wanting the book on the
    device now does not mean wanting to pay for narration now. This path touches
    no GPU: it registers the identity, publishes the EPUB, and writes a manifest
    with no chapters, which the gateway records as audio_state='none'.

    Generating audio later is just a normal run over the same file -- identity is
    derived from content, so it lands on the same book, and the position the
    reader has already reached maps straight onto the new audiobook.
    """
    book_title, book_author, book_language = extract_epub_metadata(epub_path)
    print(f"\n📖 BOOK DETECTED: '{book_title}' by {book_author}"
          f"{f' [{book_language}]' if book_language else ''}")
    drm.warn_if_protected(epub_path)

    librarian_id = content_identity(epub_path)
    print(f"🧬 BOOK IDENTITY (from content): {librarian_id}")

    kepub_path = convert_to_kepub(epub_path)
    canonicalise_epub(kepub_path)

    # One folder per book, so Audiobookshelf shows it once (core/library.py).
    ebook_dir = library.book_dir(book_title)
    os.makedirs(ebook_dir, exist_ok=True)

    db_path = os.path.join("library", "database.db")
    setup_database(db_path)
    book_id = register_book(db_path, librarian_id)
    stamp_epub_watermark(kepub_path, librarian_id)

    final_kepub_dest = os.path.join(ebook_dir, f"{book_title}.kepub.epub")
    shutil.move(kepub_path, final_kepub_dest)

    conn = sqlite3.connect(db_path, timeout=20.0)
    conn.execute("UPDATE books SET epub_path = ?, title = ?, author = ? WHERE lib_id = ?",
                 (final_kepub_dest, book_title, book_author, librarian_id))
    conn.commit()
    conn.close()

    manifest = {
        "book": {"book_id": book_id, "lib_id": librarian_id, "abs_id": None,
                 "kobo_hash": None, "epub_path": final_kepub_dest,
                 "title": book_title, "author": book_author,
                 "koreader_md5": koreader_partial_md5(final_kepub_dest)},
        "chapters": [],
        "sync_nodes": [],
    }
    with open(os.path.join(ebook_dir, "manifest.json"), "w") as mf:
        json.dump(manifest, mf)

    write_build_marker(ebook_dir, None, librarian_id)

    print(f"\n📚 Published to the catalog without audio: {book_title}")
    print("   Drop the same file again without --no-audio to narrate it later;")
    print("   it will land on this same book and keep your place.")
    return 0


async def main(epub_path, use_pod=None, language_override=None, tts_url=None):
    # Bound before anything that can raise. The kill switch in the finally
    # reads it, and an unbound name there would raise NameError *over* the real
    # exception -- hiding the actual failure behind a bookkeeping one, on the
    # exact path that exists to stop pods billing.
    pool = None
    active_pod_id = None
    owns_pod = False
    cpu_server = None

    mfa_dir = os.path.abspath("mfa_workspace")
    check_prerequisites(mfa_dir, own_server=bool(tts_url) or cpu_route())

    # --- BOOK PREPARATION AND THE NARRATION PLAN ---
    # All of this runs before a pod exists. It used to run after the deploy,
    # so the GPU was already billing while the EPUB was parsed and the plan
    # printed -- and a book that turned out to be unnarratable could not be
    # abandoned for free.
    book_title, book_author, book_language = extract_epub_metadata(epub_path)
    if language_override:
        # A declared language can simply be wrong: an English book that says
        # it is French would otherwise never be narrated.
        if book_language != language_override:
            print(f"🌍 Language overridden: EPUB declares '{book_language}', "
                  f"using '{language_override}'.")
        book_language = language_override
    print(f"\n📖 BOOK DETECTED: '{book_title}' by {book_author}")
    progress.update("reading", book=book_title)

    # English only (user, 2026-10-07): decided before anything is converted or
    # billed. Another language goes the ebook-only way, as from
    # Kobo_Dropzone_NoAudio, and the watcher tells the reader why.
    if book_language:
        print(f"🌍 Language: {book_language} "
              f"({'overridden on the command line' if language_override else 'declared by the EPUB'}).")
    else:
        book_language = detect_language(sample_text(epub_path))
        print(f"🌍 No dc:language in the EPUB; detected '{book_language}' from the text.")
    if not is_english(book_language):
        print("\n🌍 " + NOT_ENGLISH.format(title=book_title, language=book_language))
        publish_ebook_only(epub_path)
        raise SystemExit(NOT_ENGLISH_EXIT)
    book_language = DEFAULT_LANGUAGE
    drm.warn_if_protected(epub_path)

    # Identity comes from the file the user dropped, before any conversion:
    # the same book therefore resolves to the same id forever, on any
    # machine, which is what makes re-uploading idempotent instead of
    # minting a new book each time and orphaning the device's progress.
    librarian_id = content_identity(epub_path)
    print(f"🧬 BOOK IDENTITY (from content): {librarian_id}")

    kepub_path = convert_to_kepub(epub_path)
    # kepubify writes members in a nondeterministic order, and KOReader
    # identifies a book by hashing the file -- so without this the same
    # book would get a new hash on every regeneration and lose its place.
    canonicalise_epub(kepub_path)

    # The ebook and its audio share one folder, so Audiobookshelf makes one
    # item of them instead of two (core/library.py).
    audio_dir = ebook_dir = library.book_dir(book_title)

    # Setup MFA Workspace & Safe Cleanup (mfa_dir computed earlier in check_prerequisites)
    mfa_output_dir = os.path.join(mfa_dir, "output")
    os.makedirs(mfa_output_dir, exist_ok=True)

    print("\n🧹 Sweeping MFA Workspace (Preserving Acoustic Models)...")
    for f in os.listdir(mfa_dir):
        if f.endswith((".wav", ".txt", ".pristine", "_nodes.json")):
            os.remove(os.path.join(mfa_dir, f))
    for f in os.listdir(mfa_output_dir):
        os.remove(os.path.join(mfa_output_dir, f))
    for d in ("retry", "retry_output"):
        shutil.rmtree(os.path.join(mfa_dir, d), ignore_errors=True)

    os.makedirs(audio_dir, exist_ok=True)

    db_path = os.path.join("library", "database.db")
    setup_database(db_path)
    book_id = register_book(db_path, librarian_id)
    stamp_epub_watermark(kepub_path, librarian_id)

    final_kepub_dest = os.path.join(ebook_dir, f"{book_title}.kepub.epub")
    shutil.move(kepub_path, final_kepub_dest)

    conn = sqlite3.connect(db_path, timeout=20.0)
    conn.execute("UPDATE books SET epub_path = ?, title = ?, author = ? WHERE lib_id = ?",
                 (final_kepub_dest, book_title, book_author, librarian_id))
    conn.commit()
    conn.close()

    # The book is readable from here. Everything a reader needs is on disk and
    # the GPU has not been touched yet, so say so now rather than in two hours.
    publish_ebook_early(ebook_dir, librarian_id, book_id, db_path, book_title)
    progress.update("ebook_ready")
    preflight_voice(book_language)

    # Build the whole narration plan with nothing billing. cartographer_producer
    # writes into an unbounded queue, so it runs to completion with no consumers.
    plan_queue = asyncio.Queue()
    await cartographer_producer(final_kepub_dest, plan_queue, book_id,
                               language=book_language)
    narration_plan = []
    while not plan_queue.empty():
        narration_plan.append(plan_queue.get_nowait())

    # Names this run's retained chunks on the pods. Derived from the book's
    # identity and the wall clock rather than random: the identity makes it
    # obvious in a pod's filesystem which book it belongs to, and the timestamp
    # keeps two runs of the SAME book from merging their chunks -- which would
    # otherwise hand a re-run the previous run's audio to align.
    run_id = f"{librarian_id.replace('-', '')}_{int(time.time())}"[-64:]
    key_to_name = {}
    for chunk in narration_plan:
        chunk["run_id"] = run_id
        base = chunk["filename"]
        base = base[:-4] if base.endswith(".mp3") else base
        key_to_name[pod_chunk_key(base)] = base

    if not narration_plan:
        print("\n❌ Nothing to narrate: the cartographer produced no chunks.")
        print("   No pod deployed, nothing billed.")
        # Non-zero, because this is a failure. Returning 0 told mac_ghost the
        # run had succeeded, and it went on to build and upload a Sync Pack
        # with no manifest.json in it -- which the gateway extracts happily,
        # creating no rows and no book. A silent half-arrival is worse than a
        # loud failure.
        raise SystemExit(3)

    total_chars = sum(len(c.get("text", "")) for c in narration_plan)
    longest = max(len(c.get("text", "")) for c in narration_plan)
    print(f"\n📋 NARRATION PLAN: {len(narration_plan)} chunks, {total_chars:,} characters "
          f"(longest {longest:,}).")

    # own_server again: this second check (the acoustic models) used to omit
    # it, so --tts-url with no RunPod account passed the first check and died here.
    check_prerequisites(mfa_dir, book_language, own_server=bool(tts_url) or cpu_route())

    # --- FROM HERE ON, THE GPU IS BILLING ---
    global RUNPOD_BASE_URL
    # Built before the pod is acquired: the provider decides which health
    # endpoint to probe and how many workers are worth running.
    tts_provider = get_provider(language=book_language, voice=VOICE_CHOICE)
    print(f"🗣️  TTS provider: {tts_provider.__class__.__name__} "
          f"(voice {tts_provider.voice})")

    if cpu_route() and not tts_url:
        # The free route: this machine's CPU. Joins the own-server path below,
        # so the swarm, the retries and local alignment are all unchanged.
        cpu_server, tts_url = start_cpu_server(tts_provider)

    if tts_url:
        # Your own GPU: any machine running server/custom_api.py (docker/cosyvoice).
        # Nothing is deployed, so nothing is terminated either: the pool holds
        # it as adopted, the same promise --use-pod makes.
        owns_pod = False
        RUNPOD_BASE_URL = tts_url.rstrip("/")
        pool = pod_pool.PodPool()
        pool.add(OWN_SERVER_ID, RUNPOD_BASE_URL, owned=False)
        print(f"\n🖥️  Using your own TTS server at {RUNPOD_BASE_URL}; no RunPod pod is deployed.")
        awake, last_state = await wait_for_ai_brain(tts_provider.HEALTH_PATH, RUNPOD_BASE_URL)
        if not awake:
            raise Exception(f"The TTS server at {RUNPOD_BASE_URL} is not serving: {last_state}")
        # The same rule as a pod: a GPU reachable without the token narrates for
        # whoever finds it, and a home server is one port-forward from public.
        ok, _ = await assert_server_requires_token(RUNPOD_BASE_URL)
        if not ok:
            raise Exception(f"The TTS server at {RUNPOD_BASE_URL} answers without TTS_API_TOKEN; "
                            "start it with the same TTS_API_TOKEN as this .env.")
    elif use_pod:
        # Adopting a pod somebody else started. Deliberately does NOT terminate
        # it afterwards: a pod this run did not create is not this run's to
        # destroy, and the whole point of --use-pod is to reuse one across
        # several attempts. No retry either -- retrying means deploying a new
        # machine, which is exactly what --use-pod exists to avoid.
        active_pod_id = use_pod
        owns_pod = False
        RUNPOD_BASE_URL = await resolve_pod_base_url(use_pod)
        # A pool of one, so the worker-binding and cleanup below have a single
        # shape to deal with rather than two. `owned=False` is what keeps the
        # promise that --use-pod never terminates somebody else's machine.
        pool = pod_pool.PodPool()
        pool.add(use_pod, RUNPOD_BASE_URL, owned=False)
        print(f"\n♻️  Using existing pod {use_pod} ({ADOPTED_POD_WORKERS} workers); "
              "it will NOT be terminated by this run.")
        print(f"📡 AI Gateway established at: {RUNPOD_BASE_URL}")
        awake, last_state = await wait_for_ai_brain(tts_provider.HEALTH_PATH,
                                                    RUNPOD_BASE_URL)
        if not awake:
            raise Exception(f"Adopted pod {use_pod} is not serving: {last_state}")
    else:
        # Created here rather than inside acquire_pool so the finally block can
        # terminate whatever was deployed even if acquisition itself fails
        # halfway through twelve concurrent boots.
        pool = pod_pool.PodPool()
        owns_pod = True

    try:
        audio_queue = asyncio.Queue()
        # Longest chunk first, not document order. Generation time is roughly
        # linear in characters, so this is the classic longest-processing-time
        # scheduling rule: whatever is left at the end is short, and the run
        # ends when the LAST worker finishes rather than when the unluckiest
        # one picks up a 2,000-character chunk with everything else done.
        #
        # It matters because idle pods still bill. Modelled on this book's real
        # chunk lengths: about 1.4 minutes off the tail, which is 0.3 pod-hours
        # across twelve pods. Small, but it is free and it never costs anything.
        #
        # Order is safe to change: every chunk already carries its own
        # chapter_index and filename, workers write their own files, and
        # process_mfa_factory reads them back by name afterwards. Nothing
        # downstream depends on the order they were generated in.
        for chunk in sorted(narration_plan,
                            key=lambda c: len(c.get("text", "")), reverse=True):
            audio_queue.put_nowait(chunk)

        # --- PHASE 1: GENERATIVE SWARM (RunPod) ---
        # MAX_WORKERS is per pod, not per run. It is the point where one
        # server stops converting extra callers into throughput -- measured at
        # 4 for CosyVoice with API_WORKERS=8 -- so N pods want N times as many.
        # Capping the whole run at MAX_WORKERS would leave four pods idle while
        # the fifth queued.
        print(f"\n🌩️ PHASE 1: Narrating {len(narration_plan)} chunks.")

        # One connection per worker, with headroom. 50 was right for five pods
        # and is a silent throttle at twelve: workers past the limit block on
        # the pool instead of on the GPU, which looks exactly like a slow model.
        custom_limits = httpx.Limits(max_connections=256, max_keepalive_connections=256)

        # Hears each chunk as it lands (core/narration_check.py): the aligner
        # places a mangled word without a murmur, Whisper does not. VOXIE_LISTEN=off
        # turns it off; with no Whisper installed it is off by itself.
        slips = narration_check.SlipListener(narration_plan, mfa_dir)
        if os.getenv("VOXIE_LISTEN", "auto").strip().lower() == "off" or cpu_route():
            # The free route (Kokoro on this CPU) has no pods to retake on.
            slips.listen = None
        if not slips.enabled and not cpu_route() and os.getenv("VOXIE_LISTEN", "auto").strip().lower() != "off":
            print("   👂 No Whisper here (mlx-whisper, or faster-whisper): chunks are not listened to.")

        async with httpx.AsyncClient(limits=custom_limits) as client:
            # Workers are bound to one pod for the life of the run. Not per
            # request: a provider registers its speaker with one server, so a
            # worker that moved would hit a server that has never heard of its
            # narrator -- which is the 163-HTTP-404 incident, rediscovered.
            consumers = []
            narrating = {"ticker": None, "listener": None}

            async def start_workers_for(pod_id, base_url, gpu_workers):
                """Put this pod to work the moment it serves.

                Called by acquire_pool as each pod comes up, which is the whole
                point: narration starts on pod 1 while pod 12 is still pulling
                the image, instead of every pod billing through the slowest
                boot in the batch.
                """
                n = min(tts_provider.MAX_WORKERS, gpu_workers) \
                    if tts_provider.MAX_WORKERS else gpu_workers
                for _ in range(n):
                    wid = len(consumers) + 1
                    consumers.append(asyncio.create_task(runpod_worker(
                        wid, audio_queue, client, mfa_dir, db_path,
                        base_url, tts_provider, pod_urls=pool.urls, pool=pool)))
                print(f"   🌩️  +{n} worker(s) on {pod_id} "
                      f"({len(consumers)} narrating now).")
                if narrating["ticker"] is None:
                    # The first pod is serving: from here the pace is measured.
                    chars = sum(len(c.get("text", "")) for c in narration_plan)
                    hours = cost_plan.audio_hours(chars)
                    progress.update("narrating", done=0, total=chars, audio_hours=round(hours, 2))
                    narrating["ticker"] = asyncio.create_task(
                        report_narration_progress(narration_plan, mfa_dir, hours))
                    if slips.enabled:
                        narrating["listener"] = asyncio.create_task(slips.watch())

            wanted_pods = 0     # rented pods; a replacement is rented only for those
            if tts_url:
                await start_workers_for(OWN_SERVER_ID, pool.urls()[0], OWN_SERVER_WORKERS)
                acquisition = None
            elif use_pod:
                await start_workers_for(use_pod, pool.urls()[0], ADOPTED_POD_WORKERS)
                acquisition = None
            else:
                if platform_support.on_battery():
                    # 2026-10-08: a laptop at 1% slept for four hours mid-book.
                    print("\n🔋 This computer is on battery. If it sleeps or the battery runs out "
                          "mid-book, narration stops; the pods end themselves after "
                          f"{os.getenv('POD_IDLE_MINUTES') or 15} min without work (POD_IDLE_MINUTES), "
                          "and the next run picks up what is already narrated. Plug in for a long book.")
                wanted_pods = await planned_pod_count(narration_plan)
                acquisition = asyncio.create_task(acquire_pool(
                    tts_provider.HEALTH_PATH, wanted_pods, pool, start_workers_for))

            # Wait for EVERY chunk (including requeued traffic jams) to finish,
            # or stop with the reason (wait_for_generation says why not a join).
            join_task = asyncio.create_task(audio_queue.join())
            failure, acquisition = await wait_for_generation(
                join_task, consumers, pool, acquisition, wanted_pods,
                lambda: acquire_pool(tts_provider.HEALTH_PATH, 1, pool, start_workers_for))

            if failure:
                join_task.cancel()
                if acquisition is not None:
                    acquisition.cancel()
                for c in consumers:
                    c.cancel()
                raise Exception(failure)

            # A pod still booting has nothing left to narrate. Settle the
            # acquisition before handing out poison pills, or a pod that serves
            # a second later adds workers that never receive one and the run
            # hangs at the finish line with everything already generated.
            if acquisition is not None and not acquisition.done():
                print("   ⏹️  Queue is empty; letting pod acquisition settle.")
                acquisition.cancel()
                try:
                    await acquisition
                except (asyncio.CancelledError, Exception):
                    pass

            # Whether the pods can align what they just made. Asked here rather
            # than before generation because the pool is only complete now --
            # pods join it as they boot -- and it costs one HTTP call per pod.
            #
            # Chunks were retained on every pod regardless (the run_id travels
            # with each request), which is deliberate: retention is a few files
            # on a machine that is about to be destroyed, while NOT retaining
            # them would make this decision unrecoverable.
            pod_alignment = await pods_can_align(pool)

            # NOW that the belt is 100% empty, hand out the poison pills
            for _ in range(len(consumers)):
                await audio_queue.put(None)

            for c in consumers:
                await c
            if narrating["ticker"] is not None:
                narrating["ticker"].cancel()
            if narrating["listener"] is not None:
                narrating["listener"].cancel()

        active_pod_id = pool.pods[0][0] if pool.pods else None

        # Chunks the swarm gave up on. Each one is narration that simply is not
        # in the book, and until 2026-09-15 nothing downstream said so: the
        # engine reported success, the ghost packaged and uploaded, and
        # Audiobookshelf rescanned, while The Screwtape Letters shipped Letter IX
        # 2.4 minutes short. The only trace was one POISON PILL line five
        # hundred lines up a log nobody reads on a run that appeared to work.
        #
        # Reported, never fatal, and deliberately so: the same reasoning as the
        # truncation guard in core/tts_worker.py. A book missing two of a
        # hundred and fifty chunks is worth having, and aborting here would
        # throw away every chunk that did generate along with the GPU time that
        # paid for them. What must not happen is shipping it silently.
        quarantined = sorted(
            f[:-len(".quarantined")] for f in os.listdir(mfa_dir)
            if f.endswith(".quarantined"))
        if quarantined:
            missing_chars = 0
            for name in quarantined:
                try:
                    with open(os.path.join(mfa_dir, name + ".quarantined")) as f:
                        missing_chars += json.load(f).get("chars", 0)
                except (OSError, ValueError):
                    pass
            print("\n" + "=" * 60)
            print(f"‼️  INCOMPLETE: {len(quarantined)} of {len(narration_plan)} chunk(s) were "
                  "never narrated.")
            for name in quarantined:
                print(f"      • {name}")
            print(f"   About {missing_chars:,} characters of this book have no audio.")
            print("   The book below is real but has holes in it. Re-run it once the "
                  "cause is fixed;")
            print("   tools/repair_book.py cannot help -- these chunks have no audio "
                  "on disk to rebuild from.")
            print("=" * 60)

        # --- ALIGNMENT, WHILE THE PODS STILL EXIST ---
        # Before the pods are released, not after, because the audio they are
        # aligning is on them. This is the one step that has to happen with the
        # GPUs still billing, and it is worth it: a couple of minutes of pod time
        # against 65 minutes of emulated MFA on this Mac.
        progress.update("aligning")
        if pod_alignment:
            print("\n🧠 PHASE 2a: Collecting word timings from the pods...")
            try:
                await align_on_pods(pool, mfa_dir, run_id, book_language, key_to_name)
            except Exception as e:
                # Never fatal. Everything these pods would have produced is
                # recomputable locally from audio that is already on this Mac,
                # so the cost of this failing is time, not narration.
                print(f"   ⚠️  Pod alignment failed ({str(e)[:100]}); "
                      "falling back to the local aligner.")
            else:
                # A chunk that does not say its text is refused as if the aligner
                # had refused it: its word timings go, and it is narrated again.
                if slips.enabled:
                    suspects = await slips.finish()
                    for base in suspects:
                        try:
                            os.remove(os.path.join(mfa_output_dir, base + ".json"))
                        except OSError:
                            pass
                await retake_refused(pool, narration_plan, mfa_dir, mfa_output_dir, db_path, run_id,
                                     book_language, key_to_name, tts_provider)
                if slips.enabled and slips.suspects:
                    # The retakes, heard again: said, not retaken a third time.
                    again = set(slips.suspects)
                    slips.heard -= again
                    slips.suspects.clear()
                    await slips.finish()
                    print(f"   👂 After the retakes {len(slips.suspects)} of {len(again)} still do not "
                          "say their text.")

        # 👇 KILL THE PODS EARLY TO SAVE MONEY
        # Every one of them, before stitching -- which is minutes of local CPU
        # work that no GPU is needed for. With five pods that is five times the
        # saving and five times the cost of forgetting.
        print(f"\n🧨 Releasing {len(pool)} pod(s)...")
        await pool.close()
        active_pod_id = None  # Disarm the final kill switch so it doesn't try twice

        # --- PHASE 2: ALIGNMENT ENGINE (Docker) ---
        # Whatever the pods already aligned is in mfa_output_dir, and
        # align_with_retry skips it -- so on a fully pod-aligned run this
        # prints one line and does nothing, and on a partial one it handles
        # only the remainder.
        print("\n🧠 PHASE 2: Alignment...")
        align_with_retry(mfa_dir, mfa_output_dir, language=book_language)

        # --- PHASE 3: THE LIBRARIAN INJECTION ---
        # Clear this book's previous audio before writing the new run's. The
        # folder is keyed by title while a chapter's filename comes from its own
        # heading, so a regenerated book whose headings changed leaves the old
        # MP3s beside the new ones -- and they go into the Sync Pack, which is
        # how they reach the server. The gateway clears its own copy too, but
        # that alone cannot help: it re-extracts whatever the pack contains.
        #
        # Deliberately here rather than beside the mfa_workspace sweep at the
        # top of the run: by this point the narration exists and is about to be
        # replaced, so a run that dies earlier leaves the previous audiobook
        # intact instead of destroying it on the way to failing.
        #
        # Audio only: the kepub and its manifest live in this folder too.
        if library.clear_audio(audio_dir):
            print("   🧹 Cleared this book's previous audio before rebuilding it.")

        process_mfa_factory(mfa_dir, audio_dir, db_path, librarian_id, book_title, book_author)

        # --- PHASE 3.5: THE AUDIO STITCHER ---
        progress.update("stitching", eta_minutes=None)
        stitch_audio_chapters(audio_dir, book_title, book_author, librarian_id)

        # --- FINALIZATION ---
        total_seconds = apply_global_time_offsets(db_path, book_id)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        # State is stated, not left to a default. The only place audio_state was
        # ever written to 'ready' was the one-time migration that added the
        # column, so every book generated after it kept the 'none' default no
        # matter how much audio it had -- the row said "ebook only" while nine
        # chapters and a finished MP3 sat next to it. The manifest is built from
        # this row a few lines down, so it has to be true before that read.
        c.execute("UPDATE books SET audio_state = 'ready' WHERE book_id = ?", (book_id,))
        conn.commit()

        c.execute("SELECT * FROM books WHERE book_id = ?", (book_id,))
        book_dict = dict(c.fetchone())
        # The device's own id for this file, so a gateway pairs it on arrival
        # even when it is sent the map without the book.
        book_dict["koreader_md5"] = koreader_partial_md5(final_kepub_dest)
        c.execute("SELECT * FROM chapters WHERE book_id = ?", (book_id,))
        chapters = [dict(row) for row in c.fetchall()]
        c.execute("SELECT * FROM sync_nodes WHERE chapter_id IN (SELECT chapter_id FROM chapters WHERE book_id = ?)", (book_id,))
        sync_nodes = [dict(row) for row in c.fetchall()]

        # Word timings travel with the nodes they belong to. They reference
        # node_id, and the gateway re-mints every node_id on insert, so the
        # rows carry the *source* node_id and the gateway remaps them through
        # the same dictionary it already builds for chapters.
        c.execute("""SELECT * FROM word_timings WHERE node_id IN (
                       SELECT node_id FROM sync_nodes WHERE chapter_id IN (
                         SELECT chapter_id FROM chapters WHERE book_id = ?))""",
                  (book_id,))
        word_timings = [dict(row) for row in c.fetchall()]

        manifest = {"book": book_dict, "chapters": chapters,
                    "sync_nodes": sync_nodes, "word_timings": word_timings}
        manifest_path = os.path.join(ebook_dir, "manifest.json")
        with open(manifest_path, 'w') as mf:
            json.dump(manifest, mf)
        conn.close()

        # Name the folder this run actually built. mac_ghost used to guess by
        # taking the most recently modified directory under library/ebooks,
        # which is right only when nothing else has touched the library since
        # -- and raises ValueError outright when the directory is empty.
        write_build_marker(ebook_dir, audio_dir, librarian_id)

        print(f"📦 Telemetry Manifest exported to: {manifest_path}")

        # After the book is finished, never during it. Evicting mid-run could
        # drop a chunk this very book is about to ask for, turning a free hit
        # into a GPU bill while the pods are already running.
        chunk_cache.enforce_budget()

        # Handed to the watcher, which sends the pack and says when it is done.
        progress.update("uploading", eta_minutes=None, **(
            {"audio_hours": round(total_seconds / 3600, 2)} if total_seconds else {}))
        print("\n🎉 SUCCESS! Factory processing complete.")

    finally:
            # 🚨 THE IRONCLAD KILL SWITCH 🚨
            # Runs no matter what happens above -- but NOT on SIGKILL, which is
            # the case that actually cost money: `kill -9` skips finally
            # entirely, and with a pool that is several pods left billing rather
            # than one. That is why core.pod_pool writes every pod to a registry
            # at deploy time and mac_ghost sweeps it at startup. This block is
            # the tidy path; the registry is the one that survives being killed.
            if pool is not None:
                await pool.close()
            stop_cpu_server(cpu_server)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("book_path", nargs="?")
    parser.add_argument("--voice", default=None,
                        help="narrator from voices/ (cloning providers only); "
                             "overrides COSY_VOICE. Use --list-voices to see them.")
    parser.add_argument("--list-voices", action="store_true",
                        help="show the narrators in voices/ and exit")
    parser.add_argument("--no-audio", action="store_true",
                        help="publish to the catalog only; generate no audiobook (no GPU cost)")
    parser.add_argument("--language", default=None, choices=["en"],
                        help="narrate a book whose EPUB declares another language but "
                             "is English. Voxie narrates English only; any other book "
                             "is published as an ebook without audio.")
    parser.add_argument("--tts-url", metavar="URL", default=os.getenv("TTS_URL") or None,
                        help="narrate on your own GPU: the address of a running "
                             "server/custom_api.py (e.g. http://localhost:8000). "
                             "No RunPod account is needed. Also TTS_URL in .env.")
    parser.add_argument("--use-pod", metavar="POD_ID", default=None,
                        help="run against an already-running RunPod pod instead of "
                             "deploying one. The pod is left running afterwards.")
    args = parser.parse_args()
    if args.list_voices:
        from core.voices import describe
        print(describe())
        sys.exit(0)
    if not args.book_path:
        parser.error("book_path is required (or use --list-voices)")
    # Module-level so main() can reach it without threading a parameter through
    # every call between here and the provider.
    globals()['VOICE_CHOICE'] = args.voice
    if args.no_audio:
        sys.exit(publish_ebook_only(args.book_path))
    if args.tts_url and args.use_pod:
        parser.error("--tts-url and --use-pod both name the server; pass one")
    # Held here as well as in the watcher: the engine is also run by itself,
    # and a machine that idles out mid-book leaves rented pods with no client.
    with platform_support.keep_awake():
        asyncio.run(main(args.book_path, use_pod=args.use_pod,
                         language_override=args.language, tts_url=args.tts_url))
