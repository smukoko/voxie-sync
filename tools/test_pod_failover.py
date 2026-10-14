#!/usr/bin/env python3
"""When a chunk fails, does the retry go somewhere else?

The failures worth retrying are mostly properties of a POD, not of a chunk. A
server whose uvicorn workers have been OOM-killed answers ConnectError to
everything -- measured on an RTX 4090 at API_WORKERS=8, where the whole process
went away. A pod behind a bad route times out on everything. Asking the same
machine four more times produces four more of the same answer, slowly, while
billing.

That is what cost The Screwtape Letters two chunks: 042_B_IX and 043_C_IX were
each killed twelve times at 95 seconds -- roughly nineteen minutes of paid GPU
per chunk -- and then dropped from the book. The requeue path could already
land a chunk on another pod, but only by luck and only after four attempts had
been spent on the sick one.

Note what is deliberately NOT here: speculative hedging, where a duplicate is
raced on a second pod before the first has failed. That shortens the tail and
pays for the same audio twice, which is the wrong trade when the goal is a
cheaper book. A second pod is asked only after the first has actually failed.

    python tools/test_pod_failover.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.tts_worker import _retry_url, _resolve_pods   # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"   {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


POOL = ["http://a", "http://b", "http://c", "http://d"]

print("\n🎛️  Where each attempt goes")
check("the first attempt uses the worker's own pod",
      _retry_url("http://a", POOL, 1, 1) == "http://a")
check("every retry leaves the pod that just failed",
      all(_retry_url("http://a", POOL, n, 1) != "http://a" for n in (2, 3, 4)),
      str([_retry_url("http://a", POOL, n, 1) for n in (2, 3, 4)]))
check("consecutive retries do not sit on one neighbour",
      len({_retry_url("http://a", POOL, n, 1) for n in (2, 3, 4)}) > 1,
      "a chunk that fails twice should see two different machines")

print("\n🎛️  Degenerate pools")
check("a one-pod run still returns that pod rather than nothing",
      _retry_url("http://a", ["http://a"], 3, 1) == "http://a",
      "POD_COUNT=1 is the default; failover must not break it")
check("an empty pool falls back to the home pod",
      _retry_url("http://a", [], 2, 1) == "http://a")
check("a pool of only the home pod falls back to it",
      _retry_url("http://a", ["http://a", "http://a"], 2, 1) == "http://a")

print("\n🎛️  A pool that grows mid-run")
# Acquisition is concurrent: the first pod narrates at ~60s while the last is
# still pulling the image. A worker created then must not be stuck with a
# one-entry snapshot for the rest of the book.
live = ["http://a"]
check("a worker started with one pod has nowhere to go yet",
      _retry_url("http://a", lambda: live, 2, 1) == "http://a")
live.extend(["http://b", "http://c"])
check("...and uses the pods that joined later",
      _retry_url("http://a", lambda: live, 2, 1) != "http://a",
      "resolved at retry time, not at worker creation")
check("a pool accessor that raises does not kill the retry",
      _resolve_pods(lambda: (_ for _ in ()).throw(RuntimeError("boom"))) == [],
      "a failure to list pods must cost a fallback, not the chunk")

print("\n🎛️  Spreading the load off a failing pod")
# If pod A dies, all of its workers retry at once. They must not all choose the
# same neighbour and flatten it.
targets = [_retry_url("http://a", POOL, 2, w) for w in range(1, 9)]
check("the workers of a dead pod spread across the survivors",
      len(set(targets)) > 1, f"{sorted(set(targets))}")

print("\n" + "=" * 60)
passed = sum(1 for r in results if r)
print(f"{len(results)} checks: {passed} passed, {len(results) - passed} FAILED")
sys.exit(0 if passed == len(results) else 1)
