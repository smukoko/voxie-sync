#!/usr/bin/env python3
"""The GPU benchmark ends a card that stops serving, and says why, offline.

On 2026-10-07 an RTX A6000 and an RTX 4090 served /health, then never
finished the workload: every chunk was retried three times at 300 s each,
the run was killed, and about $1 bought no result. tools/gpu_bench.render now
ends a card whose server stopped answering, or that spent its time budget,
and keeps every request's timing. Here a fake server does each on cue.

    python tools/test_gpu_bench.py
"""
import asyncio
import io
import os
import sys
import wave

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tools.gpu_bench as bench  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def one_second_wav():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0" * 8000)
    return buf.getvalue()


class FakeServer:
    """`answers` serves this many takes, then the server is gone (ConnectError)."""

    def __init__(self, answers, delay=0.0):
        self.answers, self.delay, self.calls = answers, delay, 0

    async def synthesize(self, client, url, text, timeout, direction=None):
        self.calls += 1
        n = self.calls          # before the await: the other callers move the counter
        await asyncio.sleep(self.delay)
        if n > self.answers:
            raise httpx.ConnectError("All connection attempts failed")
        return httpx.Response(200, content=one_second_wav())

    def is_valid_response(self, content):
        return True


async def no_pause(_):
    return None

real_sleep = asyncio.sleep
chunks = [(None, f"Chunk number {i} of the book.") for i in range(40)]

# The retry pause is 2 s per failure; the fakes need none of it.
bench.asyncio.sleep = lambda s: real_sleep(0)
try:
    server = FakeServer(answers=5)
    wall, audio, failures, takes, requests, stopped = asyncio.run(
        bench.render(server, "http://pod", chunks, callers=4))
    check("a server that stops answering ends the card, said as such",
          stopped and "stopped answering" in stopped, stopped)
    check("it ends after a handful of requests, not every chunk three times",
          server.calls < 5 + bench.DEAD_AFTER + 8, f"{server.calls} requests for {len(chunks)} chunks")
    s = bench.summarise(requests)
    check("every request is kept with its outcome", s["requests"] == len(requests) == server.calls
          and s["errors"].get("ConnectError", 0) >= bench.DEAD_AFTER, str(s))
    check("the first failure is dated from the start", s["first_error"]
          and s["first_error"].startswith("at ") and "ConnectError" in s["first_error"], s["first_error"])
    take = bench.wav_seconds(one_second_wav())
    check("the audio served before it stopped is counted", take > 0 and abs(audio - 5 * take) < 1e-6,
          f"{audio} s for 5 takes of {take} s")

    server = FakeServer(answers=10 ** 6)
    wall, audio, failures, takes, requests, stopped = asyncio.run(
        bench.render(server, "http://pod", chunks, callers=4))
    check("a healthy server renders the whole workload and is not stopped",
          stopped is None and abs(audio - len(chunks) * bench.wav_seconds(one_second_wav())) < 1e-6
          and failures == 0, f"{stopped}, {audio} s")
    s = bench.summarise(requests)
    check("a clean run has no errors and a median time", s["errors"] == {} and s["p50_s"] is not None, str(s))
finally:
    bench.asyncio.sleep = real_sleep

server = FakeServer(answers=10 ** 6, delay=0.05)
wall, audio, failures, takes, requests, stopped = asyncio.run(
    bench.render(server, "http://pod", chunks, callers=2, budget_s=0.2))
check("a card past its time budget ends unfinished, saying how much is left",
      stopped and "time budget" in stopped and audio < len(chunks), stopped)

print(f"\n{sum(results)} passed, {len(results) - sum(results)} failed")
sys.exit(0 if all(results) else 1)
