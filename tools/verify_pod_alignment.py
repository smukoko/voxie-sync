#!/usr/bin/env python3
"""Prove a pod can align its own audio -- and that it lands in the same place.

Two separate questions, and only the second one is interesting.

*Does it work* is easy: deploy a pod on the 3.1 image, generate a few real
chunks with a run_id, POST /align, see timings come back.

*Does it agree with the Mac* is the one that matters. Alignment decides where
every word boundary goes, which is why core/languages.py pins the dictionary
and the acoustic model per language to the same version -- a book aligned
against a different setup gets a different sync map. Moving the aligner from
this Mac to a pod is exactly the kind of change that could shift boundaries
quietly: same MFA version (3.3.10, pinned in the image to match), same models
(fetched from the same pinned URLs), but a different machine and a different
job count. So this runs the SAME chunks through the local aligner and compares
word for word.

A few milliseconds of disagreement is fine -- the gateway aims at a word, and
SAC-43's measured win was 173.0s down to 3.8s. Tens of milliseconds would be
worth knowing about. Whole seconds would mean the pod is aligning against
something else.

    python tools/verify_pod_alignment.py                  # deploy, verify, terminate
    python tools/verify_pod_alignment.py --use-pod <id>   # against a pod already up
    python tools/verify_pod_alignment.py --no-compare     # skip the local MFA pass
"""
import argparse
import asyncio
import json
import os
import shutil
import sys
import time

import httpx
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()

import librerian_engine_v3 as eng                      # noqa: E402
from core import pod_pool                              # noqa: E402
from core.languages import mfa_downloads               # noqa: E402
from core.tts_providers import get_provider            # noqa: E402
from core.tts_worker import pod_chunk_key, probe_duration  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"   {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def words_of(alignment):
    """Flatten MFA's JSON to [(word, start, end)], whichever tier shape it used."""
    tiers = alignment.get("tiers") or {}
    for key in ("words", "word"):
        if key in tiers:
            out = []
            for entry in tiers[key].get("entries") or []:
                # MFA json entries are [start, end, label]
                if len(entry) >= 3 and entry[2]:
                    out.append((entry[2], float(entry[0]), float(entry[1])))
            return out
    return []


async def generate(client, base_url, provider, texts, run_id, workdir):
    """Narrate a few chunks, retaining them on the pod and writing them here too.

    Written locally as well because the local comparison pass needs the very
    same audio -- comparing the pod's alignment of one take against the Mac's
    alignment of a different take would measure the model's variance, not the
    aligner's.
    """
    names = []
    for i, text in enumerate(texts, start=1):
        base = f"{i:03d}_A_Verify"
        r = await provider.synthesize(client, base_url, text, timeout=600.0,
                                      run_id=run_id, chunk_name=pod_chunk_key(base))
        r.raise_for_status()
        with open(os.path.join(workdir, base + ".wav"), "wb") as f:
            f.write(r.content)
        with open(os.path.join(workdir, base + ".txt"), "w", encoding="utf-8") as f:
            f.write(text)
        secs = probe_duration(os.path.join(workdir, base + ".wav"))
        print(f"   🎤 {base}: {len(text):,} chars -> {secs:.1f}s")
        names.append(base)
    return names


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", default=os.getenv("RUNPOD_TEMPLATE_31", "y3yg7fmrwg"),
                    help="Template carrying the aligner (default: the 3.1 one).")
    ap.add_argument("--use-pod", default=None, help="Verify against a running pod.")
    ap.add_argument("--chunks", type=int, default=3)
    ap.add_argument("--passages", default="tools/_verify_passages.json")
    ap.add_argument("--language", default="en")
    ap.add_argument("--no-compare", action="store_true",
                    help="Skip the local MFA pass (faster, proves much less).")
    ap.add_argument("--keep", action="store_true", help="Do not terminate the pod.")
    args = ap.parse_args()

    if not os.path.exists(args.passages):
        raise SystemExit(f"No passages at {args.passages} — write a JSON list of "
                         "chunk texts (the engine's narration plan is the source).")
    texts = json.load(open(args.passages, encoding="utf-8"))
    texts.sort(key=len)
    mid = len(texts) // 2
    texts = texts[mid:mid + args.chunks]

    workdir = os.path.abspath("mfa_verify")
    shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(os.path.join(workdir, "output"), exist_ok=True)

    provider = get_provider(language=args.language)
    run_id = f"verify{int(time.time())}"
    print(f"🔎 Verifying pod alignment: template {args.template}, "
          f"{len(texts)} chunk(s), voice {provider.voice}")

    pod_id = args.use_pod
    owned = False
    try:
        if not pod_id:
            real_tpl = eng.RUNPOD_TEMPLATE_ID
            eng.RUNPOD_TEMPLATE_ID = args.template
            try:
                pod_id, _, base_url = await eng.acquire_serving_pod(provider.HEALTH_PATH)
            finally:
                eng.RUNPOD_TEMPLATE_ID = real_tpl
            owned = True
        else:
            base_url = await eng.resolve_pod_base_url(pod_id)
        print(f"   📡 {base_url}")

        token = os.getenv("TTS_API_TOKEN", "").strip()
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        async with httpx.AsyncClient() as client:
            # 1. Does this image carry an aligner at all?
            h = await client.get(f"{base_url}/align/health", headers=headers, timeout=30.0)
            available = h.status_code == 200 and h.json().get("available")
            check("the pod reports an aligner", available,
                  f"HTTP {h.status_code} {h.text[:80]}")
            if not available:
                return 1

            # 2. Narrate, retaining each chunk on the pod under the run id.
            names = await generate(client, base_url, provider, texts, run_id, workdir)

            # 3. Align them there.
            urls = mfa_downloads(args.language)
            t0 = time.monotonic()
            r = await client.post(
                f"{base_url}/align",
                data={"run_id": run_id, "language": args.language,
                      "dict_url": next(u for n, u in urls.items() if n.endswith(".dict")),
                      "acoustic_url": next(u for n, u in urls.items() if n.endswith(".zip"))},
                headers=headers, timeout=eng.POD_ALIGN_TIMEOUT_SECONDS)
            took = time.monotonic() - t0
            r.raise_for_status()
            payload = r.json()

        audio_s = sum(probe_duration(os.path.join(workdir, n + ".wav"))
                      for n in names)
        aligned = payload.get("aligned") or {}
        check("every chunk came back aligned",
              len(aligned) == len(names) and not payload.get("missing"),
              f"{len(aligned)}/{len(names)}, missing={payload.get('missing')}")
        check("alignment on the pod is faster than realtime",
              took > 0 and audio_s / took > 1.0,
              f"{audio_s:.0f}s of audio in {took:.0f}s = {audio_s / max(took, 1e-9):.1f}x "
              f"(this Mac manages 14.7x, emulated)")

        pod_words = {}
        for key, data in aligned.items():
            name = next((n for n in names if pod_chunk_key(n) == key), None)
            if name:
                pod_words[name] = words_of(data)
        # `all()` over an empty mapping is True, so these have to require that
        # something came back first. Without that, a pod that aligned NOTHING
        # passed both of them -- three green ticks under a red one, which is
        # worse than no check at all.
        check("the timings contain actual words",
              bool(pod_words) and all(len(w) > 5 for w in pod_words.values()),
              ", ".join(f"{n}:{len(w)}" for n, w in pod_words.items()) or "nothing returned")
        check("word times are ordered and inside the audio",
              bool(pod_words) and all(
                  all(a <= b for _, a, b in ws) and
                  all(ws[i][1] <= ws[i + 1][1] for i in range(len(ws) - 1))
                  for ws in pod_words.values()),
              "" if pod_words else "nothing returned")

        # 4. The real question: does the Mac put the words in the same places?
        if not args.no_compare:
            print("\n   🧮 Aligning the same audio locally to compare...")
            eng.ensure_mfa_models(workdir, args.language)
            t0 = time.monotonic()
            rc = eng.run_mfa_align(workdir, ".", "output", language=args.language)
            print(f"      local pass: {time.monotonic() - t0:.0f}s (rc={rc})")

            worst = 0.0
            compared = 0
            for name, pw in pod_words.items():
                path = os.path.join(workdir, "output", name + ".json")
                if not os.path.exists(path):
                    continue
                lw = words_of(json.load(open(path, encoding="utf-8")))
                if len(lw) != len(pw):
                    check(f"{name}: same number of words locally", False,
                          f"pod {len(pw)} vs local {len(lw)}")
                    continue
                compared += 1
                for (pword, ps, _), (lword, ls, _) in zip(pw, lw):
                    if pword == lword:
                        worst = max(worst, abs(ps - ls))
            local_jsons = [f for f in os.listdir(os.path.join(workdir, "output"))
                           if f.endswith(".json")]
            if compared:
                check("word boundaries agree with the local aligner",
                      worst < 0.050,
                      f"worst disagreement {worst * 1000:.0f}ms across {compared} chunk(s) "
                      "— tens of ms would be worth investigating, seconds would mean "
                      "the pod aligned against different models")
            else:
                # Name the side that actually produced nothing. Reporting "is
                # Docker running?" when the local pass wrote three perfectly
                # good JSONs sends whoever reads it after a problem that is not
                # there -- and the real fault, on the pod, goes unnamed.
                why = ("the pod returned no timings to compare against"
                       if local_jsons else
                       "the LOCAL pass produced no JSON — is Docker Desktop running?")
                check("there was something to compare", False,
                      f"{why} (local produced {len(local_jsons)})")
    finally:
        if pod_id and owned and not args.keep:
            if await pod_pool.terminate(pod_id):
                print(f"\n   🧨 Terminated {pod_id}.")
            else:
                print(f"\n   ‼️  {pod_id} may still be billing — check "
                      f"{pod_pool.REGISTRY}")
        elif pod_id and args.keep:
            print(f"\n   ♻️  Left {pod_id} running (--keep).")

    print("\n" + "=" * 60)
    passed = sum(1 for r in results if r)
    print(f"{len(results)} checks: {passed} passed, {len(results) - passed} FAILED")
    print(f"   audio + timings kept in {workdir}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
