"""A narration worker: a lost pod hands its chunk back; a worker with no work ends cleanly.

Found by layer 3 (tools/chaos): a book on one pod was lost when that pod died
(every chunk asked twelve times, then quarantined), and the fix's first version
crashed every worker whose first item was the end of the queue, which lost
every chapter of a fully cached book. Offline: a fake provider, a real queue
and database.
"""
import asyncio
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["CHUNK_CACHE"] = "0"
os.environ["POD_LOST_AFTER"] = "2"
os.environ["POD_LOST_SECONDS"] = "0"     # the window is tested in test_pod_pool

import httpx  # noqa: E402
from core import pod_pool  # noqa: E402
from core.database import setup_database  # noqa: E402
from core.tts_worker import runpod_worker  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


class Dead:
    """A provider whose pod no longer answers at all."""
    REQUEST_TIMEOUT_SECONDS = 5
    asked = 0

    def cache_identity(self):
        return "test"

    async def synthesize(self, client, url, text, **kw):
        Dead.asked += 1
        raise httpx.ConnectError("connection refused")

    def is_valid_response(self, raw):
        return True

    def is_truncated(self, seconds):
        return False


def setup():
    work = tempfile.mkdtemp()
    db = os.path.join(work, "database.db")
    setup_database(db)
    return work, db


def chapter(n):
    return {"book_id": 1, "chapter_index": n, "filename": f"{n:03d}_A_One.mp3", "text": "Some words.",
            "nodes": []}


async def only_poison():
    work, db = setup()
    q = asyncio.Queue()
    q.put_nowait(None)
    async with httpx.AsyncClient() as client:
        return await runpod_worker(1, q, client, work, db, "http://pod", Dead(),
                                   pod_urls=lambda: ["http://pod"], pool=pod_pool.PodPool())


try:
    result = asyncio.run(only_poison())
    check("a worker given no work, only the end of the queue, ends cleanly", result is None, f"({result!r})")
except Exception as e:
    check("a worker given no work, only the end of the queue, ends cleanly", False, f"({type(e).__name__}: {e})")


async def lost_pod():
    work, db = setup()
    pool = pod_pool.PodPool()
    pool.add("pod1", "http://pod", owned=False)
    q = asyncio.Queue()
    q.put_nowait(chapter(1))
    real_sleep = asyncio.sleep

    async def no_wait(s):
        await real_sleep(0)
    asyncio.sleep = no_wait
    try:
        async with httpx.AsyncClient() as client:
            result = await runpod_worker(1, q, client, work, db, "http://pod", Dead(),
                                         pod_urls=pool.urls, pool=pool)
    finally:
        asyncio.sleep = real_sleep
    back = [q.get_nowait() for _ in range(q.qsize())]
    return result, back, pool, os.listdir(work)


result, back, pool, files = asyncio.run(lost_pod())
check("a worker whose pod stops answering stops, saying the pod is lost", result == "pod lost", f"({result!r})")
check("after POD_LOST_AFTER failures, not twelve", Dead.asked == 2, f"({Dead.asked} requests)")
check("its chunk goes back in the queue, untouched", len(back) == 1 and back[0]["filename"] == "001_A_One.mp3"
      and "requeue_count" not in back[0], str(back))
check("the pool knows the pod is lost", pool.is_lost("http://pod"))
check("and nothing was quarantined", not [f for f in files if f.endswith(".quarantined")], str(files))

async def already_lost():
    work, db = setup()
    pool = pod_pool.PodPool()
    pool.add("pod1", "http://gone", owned=False)
    for _ in range(pool.LOST_AFTER):
        pool.note("http://gone", reached=False)      # another worker found it gone
    q = asyncio.Queue()
    q.put_nowait(chapter(2))
    before = Dead.asked
    async with httpx.AsyncClient() as client:
        result = await runpod_worker(2, q, client, work, db, "http://gone", Dead(),
                                     pod_urls=pool.urls, pool=pool)
    return result, Dead.asked - before, q.qsize()

result, asked, left = asyncio.run(already_lost())
check("a worker whose pod another worker found lost sends nothing to it",
      result == "pod lost" and asked == 0 and left == 1, f"({result!r}, {asked} requests, {left} back)")

import io as _io, wave  # noqa: E402,E401


def one_second():
    buf = _io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(b"\0\0" * 24000)
    return buf.getvalue()


class Alive(Dead):
    NEEDS_MFA_BOOST = False

    async def synthesize(self, client, url, text, **kw):
        return httpx.Response(200, content=one_second(), request=httpx.Request("POST", url))


async def answered():
    work, db = setup()
    pool = pod_pool.PodPool()
    pool.add("pod1", "http://ok", owned=False)
    pool.failures["http://ok"] = 1          # one earlier blip
    q = asyncio.Queue()
    q.put_nowait(chapter(3))
    q.put_nowait(None)
    async with httpx.AsyncClient() as client:
        result = await runpod_worker(3, q, client, work, db, "http://ok", Alive(),
                                     pod_urls=pool.urls, pool=pool)
    return result, pool.failures["http://ok"], os.path.exists(os.path.join(work, "003_A_One.wav"))

result, count, saved = asyncio.run(answered())
check("an answer from the pod clears its failure count, and the chunk is saved",
      result is None and count == 0 and saved, f"({result!r}, failures {count}, saved {saved})")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
