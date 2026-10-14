"""Chunks the aligner refuses are narrated again while the pods still exist.

Twisted (2026-10-11): MFA refused 8 of 2,042 chunks at its default beam, and
in every one the narrator had skipped text (tools/narration_scan.py heard it,
and no skip anywhere else). The engine now retakes them on the pods before
releasing them. Offline: a fake worker and aligner around the real step, then
the real worker's cache rule.
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("RUNPOD_API_KEY", "test-only")

import librerian_engine_v3 as eng  # noqa: E402
from core.database import setup_database  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


class Pool:
    def __init__(self, n=1):
        self.n = n

    def urls(self):
        return [f"http://pod{i}" for i in range(self.n)]

    def __len__(self):
        return self.n


def world(names, aligned):
    work = tempfile.mkdtemp()
    out = os.path.join(work, "output")
    os.makedirs(out)
    db = os.path.join(work, "database.db")
    setup_database(db)
    plan = []
    with sqlite3.connect(db) as conn:
        for i, n in enumerate(names, 1):
            open(os.path.join(work, n + ".wav"), "wb").write(b"take 1 of " + n.encode())
            conn.execute("INSERT INTO chapters (book_id, chapter_index, audio_filename, duration) VALUES (1, ?, ?, 9.0)",
                         (i, n + ".mp3"))
            plan.append({"book_id": 1, "chapter_index": i, "filename": n + ".mp3", "text": "words", "run_id": "run"})
            if n in aligned:
                json.dump({"words": []}, open(os.path.join(out, n + ".json"), "w"))
    return work, out, db, plan


def run(names, aligned, fixes, same=(), retakes=2, pool=None):
    """`fixes`: chunks a retake makes alignable; `same`: chunks whose retake is identical."""
    work, out, db, plan = world(names, aligned)
    asked, runs = [], []

    async def worker(wid, queue, client, mfa_dir, db_path, url, provider, pod_urls=None, pool=None):
        while True:
            item = await queue.get()
            if item is None:
                return
            base = item["filename"][:-4]
            asked.append((base, item["retake"], item["run_id"]))
            if base not in same:
                open(os.path.join(mfa_dir, base + ".wav"), "wb").write(f"take {item['retake']}".encode())
            with sqlite3.connect(db_path) as conn:
                conn.execute("INSERT INTO chapters (book_id, chapter_index, audio_filename, duration) "
                             "VALUES (1, ?, ?, 9.0)", (item["chapter_index"], item["filename"]))

    async def align(pool, mfa_dir, run_id, language, key_to_name):
        runs.append(run_id)
        for base in fixes:
            json.dump({"words": []}, open(os.path.join(mfa_dir, "output", base + ".json"), "w"))

    left = asyncio.run(eng.retake_refused(pool if pool is not None else Pool(), plan, work, out, db, "run", "en", {}, None,
                                          retakes=retakes, worker=worker, align=align))
    with sqlite3.connect(db) as conn:
        rows = dict(conn.execute("SELECT audio_filename, COUNT(*) FROM chapters GROUP BY audio_filename").fetchall())
    return left, asked, runs, rows


left, asked, runs, rows = run(["a", "b", "c"], aligned={"a"}, fixes={"b", "c"})
check("only the refused chunks are narrated again", sorted(b for b, _, _ in asked) == ["b", "c"], str(asked))
check("as take 2, under a run of their own, so the pods align only them",
      all(t == 2 and r == "runr2" for _, t, r in asked) and runs == ["runr2"], f"({asked}, {runs})")
check("a retake that aligns is done", left == [], str(left))
check("each chunk keeps one chapter row: the replaced take's is removed", set(rows.values()) == {1}, str(rows))

left, asked, runs, rows = run(["a", "b"], aligned=set(), fixes=set(), retakes=2)
check("a chunk still refused gets every allowed take, then goes on as it is",
      [t for b, t, _ in asked if b == "a"] == [2, 3] and left == ["a", "b"], f"({asked}, {left})")

left, asked, runs, rows = run(["a", "b"], aligned=set(), fixes=set(), same={"b"}, retakes=3)
check("a retake identical to the take it replaces is not asked again",
      [t for b, t, _ in asked if b == "b"] == [2] and [t for b, t, _ in asked if b == "a"] == [2, 3, 4],
      str(asked))

left, asked, runs, rows = run(["a"], aligned=set(), fixes={"a"}, retakes=0)
check("CHUNK_RETAKES=0 retakes nothing", asked == [] and left == ["a"], f"({asked}, {left})")
left, asked, runs, rows = run(["a"], aligned=set(), fixes={"a"}, pool=Pool(0))
check("with no pod left, nothing is retaken (the local aligner takes over)", asked == [] and left == ["a"])

# --- the real worker: a retake is never served from the cache ----------------------
import httpx  # noqa: E402
from core import pod_pool, tts_worker  # noqa: E402


class Counting:
    REQUEST_TIMEOUT_SECONDS = 5
    asked = 0

    def cache_identity(self):
        return "test"

    async def synthesize(self, client, url, text, **kw):
        Counting.asked += 1
        raise httpx.ConnectError("refused")

    def is_valid_response(self, raw):
        return True

    def is_truncated(self, seconds):
        return False


async def one(chunk):
    work = tempfile.mkdtemp()
    db = os.path.join(work, "database.db")
    setup_database(db)
    q = asyncio.Queue()
    q.put_nowait(chunk)
    q.put_nowait(None)
    async with httpx.AsyncClient() as client:
        return await tts_worker.runpod_worker(1, q, client, work, db, "http://pod", Counting(),
                                              pod_urls=lambda: ["http://pod"], pool=pod_pool.PodPool())

chunk = {"book_id": 1, "chapter_index": 1, "filename": "001_A_One.mp3", "text": "Some words.", "nodes": []}
with mock.patch.object(tts_worker.chunk_cache, "get", return_value=True), \
        mock.patch.object(tts_worker, "prepare_chunk_audio"), mock.patch.object(tts_worker.asyncio, "sleep",
                                                                                side_effect=lambda s: asyncio.sleep(0)):
    Counting.asked = 0
    asyncio.run(one(dict(chunk)))
    cached = Counting.asked
    asyncio.run(one({**chunk, "retake": 2}))
    retaken = Counting.asked - cached
check("a cached chunk is served from the cache, but its retake asks the model",
      cached == 0 and retaken > 0, f"(cache: {cached} requests, retake: {retaken})")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
