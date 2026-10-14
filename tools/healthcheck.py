#!/usr/bin/env python3
"""Daily health check for the whole Voxie pipeline.

Most of what has broken this project was not a code change. It was the world
moving under working code: Groq retired the model the junk filter depended on
and every call quietly answered KEEP; macOS wrote a FAT32 filesystem the Kobo
deleted files from; an Audiobookshelf API key was revoked while its token still
looked valid. None of that shows up in a unit test, and all of it is invisible
until a book comes out wrong -- after the GPU has been paid for.

So these checks exercise the real dependencies, and each one asserts a *result*
rather than that a call returned. Run it daily:

    python tools/healthcheck.py            # everything
    python tools/healthcheck.py --quick    # skip the slow/Docker checks

Exit code is 0 only if nothing failed.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

PASS, FAIL, SKIP, WARN = "PASS", "FAIL", "SKIP", "WARN"
results = []


def record(name, status, detail=""):
    results.append((name, status, detail))
    icon = {PASS: "✅", FAIL: "❌", SKIP: "⏭️ ", WARN: "⚠️ "}[status]
    print(f"{icon} {name}" + (f" — {detail}" if detail else ""))
    return status


def check(name):
    """Run a check, turning any exception into a failure rather than a crash."""
    def wrap(fn):
        try:
            status, detail = fn()
        except Exception as e:
            status, detail = FAIL, f"{type(e).__name__}: {e}"
        return record(name, status, detail)
    return wrap


def read_env():
    """Read .env exactly the way the engine does, not approximately.

    This used to split on "=" and strip -- which keeps a trailing comment as
    part of the value, while python-dotenv (what the engine actually uses)
    removes it. So `RUNPOD_TEMPLATE_ID=abc #old` read as the id "abc #old"
    here and as "abc" in every program that matters, and this file exists to
    check the world the engine sees. It cost one false alarm; the same gap
    could as easily have hidden a real one, since a checker with its own
    parser can agree with itself while disagreeing with production.
    """
    path = os.path.join(REPO_ROOT, ".env")
    if not os.path.exists(path):
        return {}
    from dotenv import dotenv_values
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def read_config():
    path = os.path.expanduser("~/.librasync.json")
    return json.load(open(path)) if os.path.exists(path) else {}


# ----------------------------------------------------------------- config ---
def check_config(env, cfg):
    @check("Config: ~/.librasync.json has what the Mac side needs")
    def _():
        required = {"GATEWAY_IP", "ABS_API_TOKEN", "GROQ_API_KEY", "UPLOAD_TOKEN"}
        missing = required - set(cfg)
        if missing:
            return FAIL, f"missing {', '.join(sorted(missing))} — run 'voxie init'"
        return PASS, f"gateway {cfg['GATEWAY_IP']}"

    @check("Config: .env has what the engine needs")
    def _():
        required = {"GROQ_API_KEY", "GROQ_MODEL", "RUNPOD_API_KEY",
                    "RUNPOD_TEMPLATE_ID", "RUNPOD_URL_PATTERN"}
        missing = required - set(env)
        if missing:
            return FAIL, f"missing {', '.join(sorted(missing))}"
        if "{pod_id}" not in env["RUNPOD_URL_PATTERN"]:
            return FAIL, "RUNPOD_URL_PATTERN has no {pod_id} placeholder"
        return PASS, f"TTS provider {env.get('TTS_PROVIDER', 'kokoro')}"


# ------------------------------------------------------------------ groq ---
def check_groq(env):
    @check("Groq: the configured model still exists AND classifies correctly")
    def _():
        from groq import Groq
        model = env.get("GROQ_MODEL", "")
        client = Groq(api_key=env["GROQ_API_KEY"])

        live = {m.id for m in client.models.list().data}
        if model not in live:
            return FAIL, (f"GROQ_MODEL={model!r} is not served any more. "
                          f"Available: {', '.join(sorted(live))[:180]}")

        # Not just "did it answer" -- a retired model used to return 404 and the
        # engine defaulted to KEEP, so the filter looked healthy while doing
        # nothing. Assert it gets an obvious case right.
        def verdict(text):
            r = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content":
                           'Reply ONLY {"llm_classification":"KEEP"} or '
                           '{"llm_classification":"JUNK"}. KEEP = prose a narrator '
                           'should read aloud. JUNK = copyright pages, licences, '
                           f'indexes, tables of contents.\n\nText:\n{text}'}],
                temperature=0.0, max_tokens=512, reasoning_effort="low",
            )
            body = (r.choices[0].message.content or "")
            m = re.search(r'"llm_classification"\s*:\s*"([^"]+)"', body)
            return m.group(1).upper() if m else None

        junk = verdict("Copyright 1998 by the publisher. All rights reserved. No part "
                       "of this publication may be reproduced. ISBN 0-000-00000-0.")
        keep = verdict("It is a melancholy object to those, who walk through this great "
                       "town, when they see the streets crowded with beggars.")
        if junk != "JUNK" or keep != "KEEP":
            return FAIL, f"model answers are wrong (copyright→{junk}, prose→{keep})"
        return PASS, f"{model} classifies both cases correctly"


# ------------------------------------------------------------------ jev ---
def check_jev(env):
    @check("JEV: the key answers AND classifies correctly")
    def _():
        import asyncio
        from core.junk_provider import JevProvider
        for k, v in env.items():
            if k.startswith(("JEV_", "OPENROUTER_", "OPEN_ROUTER_", "TYPESAFE_")):
                os.environ.setdefault(k, v)

        # Same rule as the Groq check: assert a result, not a reply. A key
        # over its monthly limit answers 403 to everything (2026-09-25), and
        # the engine then falls back -- so this is the only place it shows.
        async def run():
            jev = JevProvider()
            try:
                junk = await jev.classify(
                    "Copyright 1998 by the publisher. All rights reserved. No part of this "
                    "publication may be reproduced. ISBN 0-000-00000-0.", 20, 0.02, "healthcheck")
                keep = await jev.classify(
                    "It is a melancholy object to those, who walk through this great town, "
                    "when they see the streets crowded with beggars.", 22, 0.5, "healthcheck")
                return junk, keep
            finally:
                await jev.aclose()

        junk, keep = asyncio.run(run())
        if junk.verdict != "JUNK" or keep.verdict != "KEEP":
            return FAIL, f"answers are wrong (copyright→{junk}, prose→{keep})"
        return PASS, f"copyright→{junk}, prose→{keep}"


# --------------------------------------------------------------- runpod ---
def check_runpod(env):
    @check("RunPod: credentials valid and the template still exists")
    def _():
        import httpx
        key = env["RUNPOD_API_KEY"]
        tmpl = env["RUNPOD_TEMPLATE_ID"]
        # Read-only. Never deploy from a health check -- that bills a GPU.
        q = {"query": "query { myself { id podTemplates { id name } } }"}
        r = httpx.post(f"https://api.runpod.io/graphql?api_key={key}", json=q, timeout=30)
        if r.status_code != 200:
            return FAIL, f"API returned HTTP {r.status_code}"
        data = r.json()
        if "errors" in data:
            return FAIL, str(data["errors"])[:160]
        me = (data.get("data") or {}).get("myself") or {}
        templates = {t["id"]: t.get("name") for t in (me.get("podTemplates") or [])}
        if tmpl not in templates:
            return FAIL, (f"RUNPOD_TEMPLATE_ID={tmpl} not found on this account. "
                          "The pod template is load-bearing and lives outside this repo.")
        return PASS, f"template {tmpl} ({templates[tmpl]}) present"


# ------------------------------------------------------------------ mfa ---
def check_mfa(quick):
    @check("MFA: the pinned English models are cached or still downloadable")
    def _():
        # MFA versions its models on its own cadence, with no working "latest",
        # so a retagged release upstream silently removes the pair the aligner
        # depends on. Cached models are reported; anything not cached is
        # confirmed still downloadable. English only (core/languages.py).
        import httpx
        from core.languages import MFA_MODEL, mfa_downloads

        mfa_dir = os.path.join(REPO_ROOT, "mfa_workspace")
        cached, fetchable, broken = [], [], []
        for name, url in mfa_downloads().items():
            if os.path.exists(os.path.join(mfa_dir, name)):
                cached.append(name)
                continue
            try:
                if httpx.head(url, follow_redirects=True, timeout=20.0).status_code == 200:
                    fetchable.append(name)
                else:
                    broken.append(name)
            except Exception as e:
                broken.append(f"{name} ({type(e).__name__})")
        if broken:
            return FAIL, ("these models are neither cached nor downloadable, so no book "
                          f"can be aligned: {', '.join(broken)}")
        return PASS, f"{MFA_MODEL[0]} {MFA_MODEL[2]} — {len(cached)} cached, {len(fetchable)} downloadable"

    @check("Docker: running, with the MFA image pulled")
    def _():
        if quick:
            return SKIP, "--quick"
        if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
            return FAIL, "Docker is not running — the engine cannot align"
        out = subprocess.run(["docker", "images", "--format", "{{.Repository}}"],
                             capture_output=True, text=True).stdout
        if "montreal-forced-aligner" not in out:
            return WARN, "MFA image not pulled; the next run will download 5.6GB"
        return PASS, "daemon up, MFA image present"


# -------------------------------------------------------------- gateway ---
def check_gateway(cfg, env, quick):
    from core.gateway_address import abs_url, gateway_url
    base = gateway_url(cfg)
    abs_base = abs_url(cfg)
    if not base:
        return

    @check("Gateway: healthy")
    def _():
        import httpx
        r = httpx.get(f"{base}/health", timeout=15)
        body = r.json()
        if r.status_code != 200 or body.get("status") != "ok":
            return FAIL, f"HTTP {r.status_code} {body}"
        return PASS, "database reachable"

    @check("Gateway: upload is authenticated")
    def _():
        import httpx
        files = {"file": ("x.zip", b"x", "application/zip")}
        no_token = httpx.post(f"{base}/upload", files=files, timeout=20)
        if no_token.status_code != 401:
            return FAIL, (f"an unauthenticated upload returned {no_token.status_code}, "
                          "not 401 — port 3000 is open to the internet")
        good = httpx.post(f"{base}/upload", files=files,
                          headers={"X-Libra-Token": cfg["UPLOAD_TOKEN"]}, timeout=20)
        if good.status_code == 401:
            return FAIL, "the Mac's UPLOAD_TOKEN is rejected — it and the server's .env disagree"
        return PASS, "rejects without a token, accepts with one"

    @check("Gateway: the library and sync need a login, and yours works")
    def _():
        import base64, hashlib
        import httpx
        for path in ("/opds", "/download/LBR-000000000000", "/syncs/progress/0"):
            code = httpx.get(base + path, timeout=15).status_code
            if code != 401:
                return FAIL, f"{path} answered {code} without a login -- it must be 401"
        user, pw = cfg.get("ACCOUNT_USERNAME"), cfg.get("ACCOUNT_PASSWORD")
        if not (user and pw):
            return WARN, "no ACCOUNT_USERNAME/ACCOUNT_PASSWORD in ~/.librasync.json to test with"
        key = hashlib.md5(pw.encode()).hexdigest()
        if httpx.get(base + "/users/auth", headers={"x-auth-user": user, "x-auth-key": key}, timeout=15).status_code != 200:
            return FAIL, f"the gateway rejects '{user}' -- the device would stop syncing"
        auth = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
        feed = httpx.get(base + "/opds", headers={"Authorization": auth}, timeout=15)
        if feed.status_code != 200:
            return FAIL, f"the catalogue rejects '{user}' ({feed.status_code})"
        return PASS, f"anonymous 401 everywhere; '{user}' logs in and sees {feed.text.count('<entry>')} book(s)"

    @check("Audiobookshelf: token valid and a library points at /library")
    def _():
        import httpx
        r = httpx.get(f"{abs_base}/api/libraries",
                      headers={"Authorization": f"Bearer {cfg['ABS_API_TOKEN']}"}, timeout=20)
        if r.status_code in (401, 403):
            return FAIL, ("the API key is rejected. A key deleted in the ABS UI leaves a "
                          "token that still looks valid — regenerate with tools/bootstrap_abs.py")
        if r.status_code != 200:
            return FAIL, f"HTTP {r.status_code}"
        libs = r.json().get("libraries", [])
        paths = [f["fullPath"] for l in libs for f in l.get("folders", [])]
        if "/library" not in paths:
            return FAIL, f"no library folder at /library (found {paths}) — books have nowhere to land"
        return PASS, f"{len(libs)} library, folder /library present"

    @check("Audiobookshelf: the engine's own copy of the key still works")
    def _():
        # The check above reads ~/.librasync.json, which is what the CLI and the
        # watcher use. The engine and a locally-run gateway read .env instead,
        # and the two drift: on 2026-08-23 .env still held a revoked key that
        # returned 401 while every check here passed. A key is only as good as
        # the file the code that needs it actually reads.
        import httpx
        env_token = env.get("ABS_API_TOKEN")
        if not env_token:
            return FAIL, "ABS_API_TOKEN missing from .env — the engine cannot reach ABS"
        if env_token == cfg.get("ABS_API_TOKEN"):
            return PASS, "same key as ~/.librasync.json, already verified above"
        r = httpx.get(f"{abs_base}/api/libraries",
                      headers={"Authorization": f"Bearer {env_token}"}, timeout=20)
        if r.status_code in (401, 403):
            return FAIL, (".env holds a different ABS key and it is REJECTED. Copy the working "
                          "key from ~/.librasync.json into .env, or reissue with tools/bootstrap_abs.py")
        if r.status_code != 200:
            return FAIL, f"HTTP {r.status_code}"
        return WARN, ".env holds a different key to ~/.librasync.json; both work, but they will drift"


# --------------------------------------------------------------- kobo ---
def check_kobo_release():
    @check("KOReader: the release still has an asset our installer matches")
    def _():
        import httpx
        r = httpx.get("https://api.github.com/repos/koreader/koreader/releases/latest",
                      timeout=30, follow_redirects=True)
        assets = [a["name"] for a in r.json().get("assets", [])]
        matched = [a for a in assets if re.match(r"^koreader-kobo-v.*\.zip$", a)]
        if len(matched) != 1:
            return FAIL, (f"expected exactly 1 matching asset, found {len(matched)}: {matched}. "
                          f"Assets are {assets}")
        return PASS, matched[0]


# ------------------------------------------------------- pipeline logic ---
def check_junk_filter(quick):
    @check("Junk filter: a known book still yields only its real text")
    def _():
        if quick:
            return SKIP, "--quick"
        fixture = os.path.expanduser("~/Kobo_Archive/pg1080.epub")
        if not os.path.exists(fixture):
            return SKIP, "fixture pg1080.epub not present"
        import asyncio, contextlib, io as _io
        from librerian_engine_v3 import cartographer_producer

        async def run():
            q = asyncio.Queue()
            await cartographer_producer(fixture, q, book_id=1)
            words = 0
            while not q.empty():
                words += len((await q.get())["text"].split())
            return words

        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            words = asyncio.run(run())
        # The essay is ~3,386 words. Before the filter was fixed this was 6,295,
        # 46% of it the Project Gutenberg licence.
        if not (3200 <= words <= 3600):
            return FAIL, (f"{words} words would be narrated; expected ~3,390. "
                          "Boilerplate is leaking back in, or real text is being dropped.")
        return PASS, f"{words} words — the essay, without the licence"


def check_stitcher(quick):
    @check("Audio: chapter stitching introduces no drift")
    def _():
        if quick:
            return SKIP, "--quick"
        from core.tts_worker import stitch_audio_chapters
        work = tempfile.mkdtemp()
        for i in range(1, 5):
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=300:duration=5",
                            "-ar", "24000", "-ac", "1",
                            os.path.join(work, f"{i:03d}_A_Drift Check.wav")],
                           capture_output=True)
        import contextlib, io as _io
        with contextlib.redirect_stdout(_io.StringIO()):
            stitch_audio_chapters(work, "Test", "Test", "LBR-TEST")
        out = os.path.join(work, "Chapter_01_Drift Check.mp3")
        if not os.path.exists(out):
            return FAIL, "stitching produced no file"
        dur = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", out], capture_output=True, text=True).stdout.strip())
        # 4 x 5.000s. Concatenating MP3s with -c copy used to give 20.15s here.
        if abs(dur - 20.0) > 0.02:
            return FAIL, f"{dur:.3f}s for 20.000s of input — junction drift is back"
        return PASS, f"{dur:.3f}s for 20.000s of input"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true",
                    help="skip Docker and the pipeline checks")
    args = ap.parse_args()

    print("Voxie health check\n" + "=" * 60)
    env, cfg = read_env(), read_config()

    check_config(env, cfg)
    if env.get("GROQ_API_KEY"):
        check_groq(env)
    if env.get("JUNK_PROVIDER", "ai_brain") in ("jev", "shadow"):
        check_jev(env)
    if env.get("RUNPOD_API_KEY"):
        check_runpod(env)
    check_mfa(args.quick)
    check_gateway(cfg, env, args.quick)
    check_kobo_release()
    check_junk_filter(args.quick)
    check_stitcher(args.quick)

    print("=" * 60)
    failed = [r for r in results if r[1] == FAIL]
    warned = [r for r in results if r[1] == WARN]
    print(f"{len(results)} checks: {len(results) - len(failed) - len(warned)} passed, "
          f"{len(warned)} warned, {len(failed)} FAILED")
    for name, _, detail in failed:
        print(f"   ❌ {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
