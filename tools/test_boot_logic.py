#!/usr/bin/env python3
"""Exercise wait_for_ai_brain against replayed boot sequences.

This exists because the rule it tests has already been wrong once. It used to
give up after twelve 404s, on the theory that "a healthy boot never returns a
404 at all" -- and that was disproved directly on 2026-09-11 by a pod with vLLM
healthy and answering on 127.0.0.1:8000, which still returned 404 through
RunPod's proxy because it had been deployed without 8000/http among its ports.
A 404 means the proxy has no route yet, not that the pod is serving the wrong
thing, so the old rule could kill a boot that was going to succeed.

Replayed rather than live: a real boot costs a GPU and twenty minutes, which is
why the original rule went untested long enough to be believed.

    python tools/test_boot_logic.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RUNPOD_API_KEY", "test-only")

import httpx  # noqa: E402
import librerian_engine_v3 as eng  # noqa: E402

eng.RUNPOD_BASE_URL = "http://replayed.invalid"
eng.BOOT_POLL_SECONDS = 10
eng.BOOT_HEARTBEAT_SECONDS = 10_000          # silence the per-minute heartbeat
eng.BOOT_TIMEOUT_SECONDS = 40 * 60
eng.BOOT_404_GRACE_SECONDS = 10 * 60

results = []


class _Response:
    def __init__(self, code):
        self.status_code = code


def run(sequence, tail):
    """Serve `sequence` of status codes, then `tail` forever. Returns (result, probes)."""
    calls = {"n": 0}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, timeout=None):
            i = calls["n"]
            calls["n"] += 1
            item = sequence[i] if i < len(sequence) else tail
            if item == "refused":
                raise httpx.ConnectError("refused")
            return _Response(item)

    # A virtual clock. The grace period and the budget are both measured in
    # wall time (correctly -- that is what a real boot spends), so a test that
    # skips the sleeps has to advance time itself or neither rule can ever fire.
    clock = {"t": 0.0}

    real_client = httpx.AsyncClient
    real_sleep = asyncio.sleep
    real_monotonic = eng.time.monotonic

    async def _no_sleep(seconds):
        clock["t"] += seconds
        return None

    httpx.AsyncClient = lambda *a, **k: _Client()
    asyncio.sleep = _no_sleep
    eng.time.monotonic = lambda: clock["t"]
    try:
        return asyncio.run(eng.wait_for_ai_brain()), calls["n"]
    finally:
        httpx.AsyncClient = real_client
        asyncio.sleep = real_sleep
        eng.time.monotonic = real_monotonic


def check(name, got, want):
    ok = got == want
    results.append(ok)
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else f"   got={got} want={want}"))


print("wait_for_ai_brain — replayed boot sequences\n")

# The boot that works: nothing bound, then serving.
(ok, state), _ = run([502] * 24, 200)
check("502s then 200 succeeds", (ok, state), (True, None))

# The softening. A burst of 404s while the proxy registers the route, then a
# real 200. The old count-of-12 rule aborted this; it must now succeed.
(ok, state), _ = run([502] * 6 + [404] * 30 + [502] * 3, 200)
check("404 burst then 200 succeeds (the old rule killed this)", (ok, state), (True, None))

# Nothing but 404 forever: the route never appears, so give up -- but on the
# grace period, well inside the overall budget.
(ok, state), n = run([], 404)
check("sustained 404 fails", ok, False)
check("...on the grace, not the full budget",
      n * eng.BOOT_POLL_SECONDS < eng.BOOT_TIMEOUT_SECONDS, True)
print(f"          gave up at {n * eng.BOOT_POLL_SECONDS // 60}m "
      f"(grace {eng.BOOT_404_GRACE_SECONDS // 60}m, budget {eng.BOOT_TIMEOUT_SECONDS // 60}m)")

# The 2026-08-31 signature. Any non-404 resets the grace, so this deliberately
# falls through to the overall budget and then to a fresh machine, rather than
# risking a slow-but-healthy boot that saw one registration-lag 404.
(ok, state), _ = run([502] * 6 + [404, 502] * 5000, 404)
check("alternating 404/502 fails via the budget, not the 404 rule", ok, False)

# Nothing ever binds. A different diagnosis -- a slow image pull looks like this
# early on -- so it must run the full budget and say so.
(ok, state), _ = run([], "refused")
check("never-bound pod runs the full budget", ok, False)
check("...and reports the refusal, not a 404", "refused" in (state or ""), True)


# --- deploy_burner_pod: waiting out a busy fleet -------------------------
#
# The failure before a bad boot. Three real drops on 2026-09-12 aborted
# because all seven cards were busy during one thirty-second sweep, while a
# probe had deployed a 4090 minutes earlier -- capacity churns faster than a
# sweep takes. Nothing is billing during the wait, since no pod exists yet.
print("\ndeploy_burner_pod — sweeping a busy fleet\n")

eng.CAPACITY_POLL_SECONDS = 60
eng.CAPACITY_WAIT_SECONDS = 45 * 60


class _Clock:
    """Real `time`, except monotonic runs on whatever the fake sleep advanced.

    Without this the budget is measured against the wall clock while the sleeps
    are instant, so a test for "gives up after 45 minutes" simply never gives up.
    """

    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def __getattr__(self, name):
        import time as _real
        return getattr(_real, name)


def run_deploy(sweeps_until_stock, stock_on="COMMUNITY"):
    """Return (result_or_error, sweeps, slept, clouds). Stock after N sweeps.

    A "sweep" here is one pass over the card list on ONE cloud. deploy_burner_pod
    tries COMMUNITY then ALL within each cycle, so a cycle costs two sweeps --
    `stock_on` says which cloud finally has the card, which is how the ordering
    is asserted rather than assumed.
    """
    state = {"sweeps": 0, "slept": 0.0, "clouds": []}
    clock = _Clock()

    async def fake_sweep(cloud="ALL"):
        state["sweeps"] += 1
        state["clouds"].append(cloud)
        if state["sweeps"] >= sweeps_until_stock and cloud == stock_on:
            return ("pod-abc", 30)
        return None

    async def fake_sleep(s):
        state["slept"] += s
        clock.t += s

    real_sweep, real_sleep, real_time = eng._sweep_for_gpu, asyncio.sleep, eng.time
    eng._sweep_for_gpu = fake_sweep
    eng.time = clock
    asyncio.sleep = fake_sleep
    try:
        try:
            out = asyncio.run(eng.deploy_burner_pod())
        except Exception as exc:
            out = exc
    finally:
        eng._sweep_for_gpu, asyncio.sleep, eng.time = real_sweep, real_sleep, real_time
    return out, state["sweeps"], state["slept"], state["clouds"]

# Stock on the first sweep: no waiting at all, which is the common case and
# must not have been slowed down by any of this.
# Pinned rather than inherited: CLOUD_ORDER now depends on RUNPOD_TRANSPORT, so
# a suite that read it from the environment would assert different things on
# different machines.
eng.CLOUD_ORDER = ["COMMUNITY", "ALL"]
out, sweeps, slept, clouds = run_deploy(1)
check("stock on the first sweep returns immediately", (out, sweeps, slept), (("pod-abc", 30), 1, 0.0))

# Which halves of the fleet get searched, and in what order, is transport-
# dependent -- and that is measured rather than cautious. The community cloud is
# half the price, but on 2026-09-21 five community pods across three cards all
# rented, reported RUNNING and never started a container, while every pod that
# served was secure. TCP needs a raw public ip:port and community machines
# largely cannot publish one, so searching there first costs ten minutes per pod
# and buys nothing.
real_order = eng.CLOUD_ORDER
try:
    eng.CLOUD_ORDER = ["ALL"]
    out, sweeps, slept, clouds = run_deploy(1, stock_on="ALL")
    check("on tcp the whole fleet is searched directly", clouds, ["ALL"])

    # On the proxy transport there is no public-IP requirement, so the cheap
    # half is tried first and secure is the fallback. This is the property the
    # cost work exists for, and the fallback is what the older fix protected.
    eng.CLOUD_ORDER = ["COMMUNITY", "ALL"]
    out, sweeps, slept, clouds = run_deploy(1, stock_on="COMMUNITY")
    check("where community works it is tried before secure", clouds[0], "COMMUNITY")

    out, sweeps, slept, clouds = run_deploy(2, stock_on="ALL")
    check("an empty community cloud falls through to secure", out, ("pod-abc", 30))
    check("...having tried community first", clouds, ["COMMUNITY", "ALL"])
finally:
    eng.CLOUD_ORDER = real_order

# Busy for a few minutes, then a card frees up. This is the case that used to
# abort the run outright. Nine sweeps = four full cycles of two, then community
# on the fifth -- so the cycle length is pinned rather than inherited from
# whatever transport the environment happens to be set to.
eng.CLOUD_ORDER = ["COMMUNITY", "ALL"]
out, sweeps, slept, clouds = run_deploy(9)
check("a busy fleet is waited out, not fatal", out, ("pod-abc", 30))
check("...and it kept sweeping until stock appeared", sweeps, 9)
check("...having waited between sweeps", slept, 4 * 60)

# Busy for longer than the budget: give up, but only after really waiting,
# and say that nothing was billed and the book is retried.
eng.CLOUD_ORDER = ["COMMUNITY", "ALL"]
out, sweeps, slept, clouds = run_deploy(10_000)
check("a fleet busy past the budget gives up", isinstance(out, Exception), True)
check("...only after spending the budget", slept >= eng.CAPACITY_WAIT_SECONDS, True)
check("...and says nothing was billed", "billed" in str(out), True)
check("...and that the book is retried", "dropzone" in str(out), True)

# Out of funds must stop the run, not be waited out. RunPod delivers "your
# account balance is too low" through the same channel as "this card is out of
# stock", and the capacity loop's whole job is to wait stock out -- so without
# a distinction a book sits for 45 minutes reporting a GPU shortage while the
# GPUs are sitting there available. Observed for real on 2026-09-22.
def _sweep_with_api_error(message):
    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"errors": [{"message": message}], "data": None}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, timeout=None):
            return _Resp()

    real_client = eng.httpx.AsyncClient
    eng.httpx.AsyncClient = _Client
    try:
        return asyncio.run(eng._sweep_for_gpu("ALL"))
    except Exception as exc:
        return exc
    finally:
        eng.httpx.AsyncClient = real_client


out = _sweep_with_api_error(
    "Your account balance is too low to rent a pod. Please add funds to your account.")
check("an empty balance raises instead of sweeping on",
      type(out).__name__, "OutOfFundsError")
check("...and says nothing was billed", "Nothing was billed" in str(out), True)
check("...and says where to fix it", "console.runpod.io" in str(out), True)

out = _sweep_with_api_error("There are no longer any instances available.")
check("an out-of-stock card is NOT treated as out of funds", out, None)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
