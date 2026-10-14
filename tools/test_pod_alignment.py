#!/usr/bin/env python3
"""Alignment on the pods, and the fallback that makes it safe to rely on.

The speed argument is simple: MFA ships x86-only, so on this Mac it runs under
Rosetta at 14.7x realtime -- 65 minutes for a sixteen-hour book against ~25 to
generate it -- while the pods that made the audio are native, idle-CPU'd and
already holding it. Parallelising locally is worse, measured: --single_speaker
--num_jobs 6 took 1,822s against 118s for the same 24 chunks.

What needs testing is not the speed but every way it can quietly not happen:

  - a pod built from an older image has no /align at all
  - one pod in the pool can align and another cannot
  - a pod errors, or returns timings for chunks nobody asked about
  - the local aligner must then do the remainder, and ONLY the remainder

Every one of those has the same failure signature if it is wrong -- a book with
some chapters finely synced and others not -- which no downstream check looks
for. So they are asserted here.

    python tools/test_pod_alignment.py
"""
import ast
import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("RUNPOD_API_KEY", "test-only")

import librerian_engine_v3 as eng          # noqa: E402
from core import pod_pool                  # noqa: E402
from core.tts_worker import pod_chunk_key  # noqa: E402

results = []

# The server's own guard on a name that becomes a path (server/custom_api.py).
SERVER_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def check(name, ok, detail=""):
    results.append(ok)
    print(f"   {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


def make_pool(urls):
    pool = pod_pool.PodPool()
    for i, u in enumerate(urls):
        pool.pods.append((f"pod{i}", u))      # bypass the registry
    return pool


class FakeClient:
    """Stands in for httpx.AsyncClient. `handler(method, url, data)` answers."""

    def __init__(self, handler):
        self.handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, timeout=None, headers=None):
        return pod_door(url, headers) or self.handler("GET", url, None)

    async def post(self, url, data=None, headers=None, timeout=None):
        return pod_door(url, headers) or self.handler("POST", url, data)


# The real server refuses every path but these without the token. Read from the
# server itself: a fake that took any request let a probe without the token
# pass here while every real pod answered it 401 ("0 of N pod(s) can align").
_SERVER = ast.parse(open(os.path.join(ROOT, "server", "custom_api.py")).read())
OPEN_PATHS = next(ast.literal_eval(n.value) for n in _SERVER.body if isinstance(n, ast.Assign)
                  and any(getattr(t, "id", None) == "_OPEN_PATHS" for t in n.targets))
POD_TOKEN = "pod-token-for-tests"
os.environ["TTS_API_TOKEN"] = POD_TOKEN


def pod_door(url, headers):
    """What server/custom_api.require_token answers, or None to let the request in."""
    if urlparse(url).path in OPEN_PATHS or (headers or {}).get("Authorization") == f"Bearer {POD_TOKEN}":
        return None
    return FakeResponse(401)


class FakeResponse:
    def __init__(self, code, payload=None):
        self.status_code = code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def with_client(handler):
    """Swap httpx.AsyncClient inside the engine for the duration of a call."""
    class _Factory:
        def __call__(self, *a, **kw):
            return FakeClient(handler)
    return _Factory()


# --- the chunk key ---------------------------------------------------------
print("\n🎛️  Naming chunks for a server that will make them paths")
real_names = [
    "001_A_Part_1",
    "004_B_THE FULL PROJECT GUTENBERG LICENSE",
    "012_C_Chapitre 3 — Été",
    "099_AA_" + "x" * 300,
]
keys = [pod_chunk_key(n) for n in real_names]
check("every chunk name maps to a key the server will accept",
      all(SERVER_SAFE_ID.match(k) for k in keys),
      "real names carry spaces, accents and arbitrary length")
check("distinct chunks get distinct keys", len(set(keys)) == len(keys))
check("the key is stable across calls",
      keys == [pod_chunk_key(n) for n in real_names],
      "a key that moved between request and collection would orphan the timings")


# --- capability probe ------------------------------------------------------
print("\n🎛️  Deciding whether the pods can align")


def probe(answers):
    def handler(method, url, data):
        for host, available in answers.items():
            if url.startswith(host):
                if available is None:
                    raise RuntimeError("connection refused")
                if available == 404:
                    return FakeResponse(404)
                return FakeResponse(200, {"available": available})
        raise AssertionError(url)
    real = eng.httpx.AsyncClient
    eng.httpx.AsyncClient = with_client(handler)
    try:
        return asyncio.run(eng.pods_can_align(make_pool(list(answers))))
    finally:
        eng.httpx.AsyncClient = real


check("a pool where every pod can align says yes",
      probe({"http://a": True, "http://b": True}) is True)
check("one pod that cannot align disables it for the whole run",
      probe({"http://a": True, "http://b": False}) is False,
      "a mixed pool leaves the older pod's chunks on the Mac anyway")
check("an older image (404 on /align/health) falls back",
      probe({"http://a": 404}) is False)
check("a pod that cannot be reached falls back rather than raising",
      probe({"http://a": True, "http://b": None}) is False)
check("an empty pool cannot align", asyncio.run(eng.pods_can_align(make_pool([]))) is False)
check("the fake pod refuses what the real one refuses: /align/health without the token",
      pod_door("http://a/align/health", {}) is not None and pod_door("http://a/health", {}) is None,
      f"open paths {OPEN_PATHS}")


# --- collecting the timings ------------------------------------------------
print("\n🎛️  Collecting word timings from the pods")

work = tempfile.mkdtemp(prefix="podalign_")
try:
    out_dir = os.path.join(work, "output")
    os.makedirs(out_dir)
    names = ["001_A_Part 1", "002_B_Part 1", "003_A_Part 2"]
    key_to_name = {pod_chunk_key(n): n for n in names}

    def handler(method, url, data):
        if url.startswith("http://a"):
            k = pod_chunk_key(names[0])
            return FakeResponse(200, {"aligned": {k: {"words": [1]}}, "missing": []})
        if url.startswith("http://b"):
            k = pod_chunk_key(names[1])
            return FakeResponse(200, {
                "aligned": {k: {"words": [2]}, "cUNKNOWNKEY": {"words": [99]}},
                "missing": ["cSOMETHINGELSE"]})
        raise RuntimeError("pod c exploded")

    real = eng.httpx.AsyncClient
    eng.httpx.AsyncClient = with_client(handler)
    try:
        written = asyncio.run(eng.align_on_pods(
            make_pool(["http://a", "http://b", "http://c"]),
            work, "run1", "en", key_to_name))
    finally:
        eng.httpx.AsyncClient = real

    check("timings are written under the chunk's real name, not the pod key",
          sorted(written) == sorted(names[:2]), str(sorted(written)))
    check("the files on disk match what process_mfa_factory will look for",
          sorted(f[:-5] for f in os.listdir(out_dir)) == sorted(names[:2]),
          str(sorted(os.listdir(out_dir))))
    check("a key nobody asked about is ignored, not written",
          not any("UNKNOWN" in f for f in os.listdir(out_dir)),
          "writing it would leave an output file matching no chunk")
    check("one pod failing does not lose the others' work", len(written) == 2)
    if written:
        got = json.load(open(os.path.join(out_dir, names[0] + ".json")))
        check("the timings are stored verbatim", got == {"words": [1]}, str(got))
finally:
    shutil.rmtree(work, ignore_errors=True)


# --- the local aligner does only the remainder -----------------------------
print("\n🎛️  What the local aligner is left to do")

work = tempfile.mkdtemp(prefix="podalign2_")
try:
    out_dir = os.path.join(work, "output")
    os.makedirs(out_dir)
    for n in ("a", "b", "c"):
        open(os.path.join(work, n + ".wav"), "wb").write(b"RIFF")
        open(os.path.join(work, n + ".txt"), "w").write("hello")

    called = {"n": 0}

    def never(*a, **kw):
        called["n"] += 1
        return 0

    real_align = eng.run_mfa_align
    eng.run_mfa_align = never
    try:
        # Every chunk already aligned on a pod.
        for n in ("a", "b", "c"):
            json.dump({"words": []}, open(os.path.join(out_dir, n + ".json"), "w"))
        left = eng.align_with_retry(work, out_dir)
        check("a fully pod-aligned book never starts Docker at all",
              called["n"] == 0 and left == [],
              f"MFA invoked {called['n']} times")
    finally:
        eng.run_mfa_align = real_align
finally:
    shutil.rmtree(work, ignore_errors=True)

# --- the remainder MFA cannot align is not a crash -------------------------------
# Twisted, 2026-10-11: the pods aligned 2,034 of 2,042 chunks; MFA aligned none
# of the 8 left (text the narrator skipped) and exited 1, and the engine threw
# the whole book away as "MFA crashed".
print("\n🎛️  A remainder MFA cannot align")

work = tempfile.mkdtemp(prefix="podalign3_")
try:
    out_dir = os.path.join(work, "output")
    os.makedirs(out_dir)
    for n in ("a", "b", "c"):
        open(os.path.join(work, n + ".wav"), "wb").write(b"RIFF")
        open(os.path.join(work, n + ".txt"), "w").write("hello")
    for n in ("a", "b"):
        json.dump({"words": []}, open(os.path.join(work, "output", n + ".json"), "w"))
    passes = []

    def aligns_none(mfa_dir, corpus_rel, output_rel, beam=None, retry_beam=None, language=None):
        passes.append(beam)
        return 1            # NoAlignmentsError: ran, aligned nothing

    real_align, real_unready = eng.run_mfa_align, getattr(eng, "mfa_unready", None)
    eng.run_mfa_align, eng.mfa_unready = aligns_none, lambda d: None
    try:
        try:
            left, raised = eng.align_with_retry(work, out_dir), None
        except Exception as e:
            left, raised = None, str(e)
        check("MFA aligning none of the chunks the pods left does not end the book",
              raised is None and left == ["c"], f"({raised or left})")
        check("they get the wider beam first, as any skipped chunk does", passes == [None, 100], str(passes))
        eng.mfa_unready = lambda d: "Docker is not running"
        try:
            eng.align_with_retry(work, out_dir)
            said = None
        except Exception as e:
            said = str(e)
        check("but MFA that cannot run at all still stops it, and says why",
              said and "Docker is not running" in said, str(said))
    finally:
        eng.run_mfa_align, eng.mfa_unready = real_align, real_unready
finally:
    shutil.rmtree(work, ignore_errors=True)

# --- what "MFA cannot run" means ------------------------------------------------------
work = tempfile.mkdtemp(prefix="podalign4_")
try:
    from core.languages import mfa_files
    real_run = eng.subprocess.run

    def docker(rc=0, raises=None):
        def run(cmd, **kw):
            if raises:
                raise raises
            return mock_result(rc)
        return run

    class mock_result:
        def __init__(self, rc):
            self.returncode = rc

    said = eng.mfa_unready(work)
    check("a missing model file is a reason MFA cannot run", said and "missing" in said, str(said))
    for f in mfa_files():
        open(os.path.join(work, f), "w").write("model")
    try:
        eng.subprocess.run = docker(rc=1)
        down = eng.mfa_unready(work)
        eng.subprocess.run = docker(raises=FileNotFoundError("docker"))
        absent = eng.mfa_unready(work)
        eng.subprocess.run = docker(rc=0)
        ready = eng.mfa_unready(work)
    finally:
        eng.subprocess.run = real_run
    check("Docker not running, or not there, is a reason too",
          down == "Docker is not running" and absent and "not installed" in absent, f"({down}, {absent})")
    check("with both models and Docker answering, MFA can run", ready is None, str(ready))
finally:
    shutil.rmtree(work, ignore_errors=True)

print("\n" + "=" * 60)
passed = sum(1 for r in results if r)
print(f"{len(results)} checks: {passed} passed, {len(results) - passed} FAILED")
sys.exit(0 if passed == len(results) else 1)
