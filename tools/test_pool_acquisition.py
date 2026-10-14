#!/usr/bin/env python3
"""Does the pool acquire concurrently, and start narrating on the first pod?

Both properties are about money rather than correctness, which is why they need
a test: a run that acquires sequentially still produces a perfect audiobook, it
just pays pod 1 to watch pods 2..12 boot. Measured on the five-pod Tactique run,
that was 27 minutes of acquisition against 13.7 of use -- and it scales the
wrong way, so twelve pods would spend more waiting than narrating.

Replayed, never live. A real twelve-pod acquisition costs a GPU and ten minutes,
which is precisely why the sequential version went unexamined for so long.

    python tools/test_pool_acquisition.py
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RUNPOD_API_KEY", "test-only")

import librerian_engine_v3 as eng      # noqa: E402
from core import pod_pool              # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"   {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


# Boots are replayed at 1/1000 speed: a 240-second boot takes 240ms here. Real
# asyncio sleeps rather than a virtual clock, because concurrency is the whole
# property under test -- a shared virtual "now" advanced by every task's sleep
# reports the SUM of four concurrent boots, which is precisely the bug this
# suite exists to detect, and it would report it against correct code.
TIME_SCALE = 1000.0


class FakeClock:
    """Wall clock in replayed seconds: real time, scaled up by TIME_SCALE."""

    def __init__(self):
        self.t0 = time.monotonic()

    def monotonic(self):
        return (time.monotonic() - self.t0) * TIME_SCALE

    async def sleep(self, seconds):
        await asyncio.sleep(seconds / TIME_SCALE)


def install_fake_pods(boot_times, fail=()):
    """Replace acquire_serving_pod with one that 'boots' on a schedule.

    boot_times is seconds-per-pod in deploy order; `fail` names indices that
    never serve.
    """
    clock = FakeClock()
    order = {"n": 0}
    events = []

    async def fake_acquire(health_path="/health"):
        i = order["n"]
        order["n"] += 1
        await clock.sleep(boot_times[i % len(boot_times)])
        if i in fail:
            raise Exception(f"pod {i} never served")
        pod_id = f"pod{i}"
        events.append(("ready", pod_id, clock.monotonic()))
        return pod_id, 30, f"http://pod{i}.invalid"

    eng.acquire_serving_pod = fake_acquire
    return clock, events


async def scenario_concurrent():
    """Four pods with staggered boots finish in the slowest boot, not their sum."""
    clock, events = install_fake_pods([60, 120, 180, 240])
    pool = pod_pool.PodPool()
    pool.add = lambda pid, url, owned=True: pool.pods.append((pid, url))  # no registry writes

    t_end_before = clock.monotonic()
    await eng.acquire_pool("/health", 4, pool)
    span = clock.monotonic() - t_end_before

    # Generous headroom on the scaled clock -- this is asserting "nothing like
    # 600" rather than timing the event loop.
    check("four pods are acquired concurrently", span < 400,
          f"finished in {span:.0f}s (sequential would be 600s)")
    check("all four end up in the pool", len(pool) == 4, f"got {len(pool)}")


async def scenario_first_pod_starts_early():
    """on_ready fires per pod, at its own boot time -- not once at the end."""
    clock, events = install_fake_pods([60, 120, 180, 240])
    pool = pod_pool.PodPool()
    pool.add = lambda pid, url, owned=True: pool.pods.append((pid, url))
    started = []

    async def on_ready(pod_id, base_url, workers):
        started.append((pod_id, clock.monotonic()))

    await eng.acquire_pool("/health", 4, pool, on_ready)

    check("every pod is put to work", len(started) == 4, f"{len(started)} callbacks")
    if started:
        check("the first pod starts before the last one has booted",
              started[0][1] < started[-1][1],
              f"first at {started[0][1]:.0f}s, last at {started[-1][1]:.0f}s")
        check("the first pod starts at its own boot time, not the slowest",
              started[0][1] < 120, f"started at {started[0][1]:.0f}s, slowest boot was 240s")


async def scenario_elastic():
    """A thin market yields a slower book, never no book."""
    clock, _ = install_fake_pods([60, 60, 60, 60], fail={1, 3})
    pool = pod_pool.PodPool()
    pool.add = lambda pid, url, owned=True: pool.pods.append((pid, url))

    pool_out, swarm = await eng.acquire_pool("/health", 4, pool)
    check("pods that never serve do not fail the run", len(pool_out) == 2,
          f"{len(pool_out)} of 4 served")
    check("and the run still reports a worker count", swarm == 30, f"swarm={swarm}")


async def scenario_none_served():
    """If nothing serves there is no book, and it must say so rather than hang."""
    clock, _ = install_fake_pods([60], fail={0, 1})
    pool = pod_pool.PodPool()
    pool.add = lambda pid, url, owned=True: pool.pods.append((pid, url))
    try:
        await eng.acquire_pool("/health", 2, pool)
        check("no pod serving raises rather than returning an empty pool", False)
    except Exception as e:
        check("no pod serving raises rather than returning an empty pool",
              "No pod served" in str(e), str(e)[:60])


async def scenario_mixed_cards_take_the_smallest():
    """A mixed pool is sized by its weakest pod, not its strongest."""
    clock = FakeClock()
    order = {"n": 0}
    workers = [30, 12, 20]

    async def fake_acquire(health_path="/health"):
        i = order["n"]
        order["n"] += 1
        await clock.sleep(10)
        return f"pod{i}", workers[i], f"http://pod{i}.invalid"

    eng.acquire_serving_pod = fake_acquire
    pool = pod_pool.PodPool()
    pool.add = lambda pid, url, owned=True: pool.pods.append((pid, url))
    _, swarm = await eng.acquire_pool("/health", 3, pool)
    check("a mixed pool is sized by its weakest card", swarm == 12,
          f"swarm={swarm} (cards offered {workers})")


async def main():
    real_acquire = eng.acquire_serving_pod
    try:
        print("\n🎛️  Concurrent acquisition")
        await scenario_concurrent()
        print("\n🎛️  Narration starts on the first pod")
        await scenario_first_pod_starts_early()
        print("\n🎛️  Elasticity")
        await scenario_elastic()
        await scenario_none_served()
        print("\n🎛️  Mixed fleets")
        await scenario_mixed_cards_take_the_smallest()
    finally:
        eng.acquire_serving_pod = real_acquire

    print("\n" + "=" * 60)
    passed = sum(1 for r in results if r)
    print(f"{len(results)} checks: {passed} passed, {len(results) - passed} FAILED")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
