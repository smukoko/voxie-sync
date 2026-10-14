"""The pod pool, tested on the thing that actually costs money.

Throughput is not what these check. Five pods generating is arithmetic; five
pods still running after the engine was killed is $5.45 an hour, and this
project has the receipt: four pods in one day deployed and never terminated,
one billing 26 minutes unnoticed.

So the assertions are about the registry and cleanup:

  * a pod is recorded at DEPLOY time, not once it serves -- a pod that is
    booting is already billing
  * a pod is forgotten only after RunPod confirms termination, so a failed
    terminate leaves it in the registry for the next sweep rather than losing it
  * close() is idempotent, because it is called from the success path, the
    exception handler and the finally block
  * an adopted pod (--use-pod) is never terminated
  * the registry survives the process, which is the only reason any of this
    helps after a SIGKILL

    python tools/test_pod_pool.py
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import pod_pool

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"   {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


async def main():
    work = tempfile.mkdtemp()
    pod_pool.REGISTRY = os.path.join(work, "pods.json")
    os.environ["RUNPOD_API_KEY"] = "test-key-not-used"

    terminated = []
    fail_on = set()

    async def fake_terminate(pod_id):
        if pod_id in fail_on:
            return False
        terminated.append(pod_id)
        pod_pool._unregister(pod_id)
        return True

    real_terminate = pod_pool.terminate
    pod_pool.terminate = fake_terminate

    print("pod pool\n" + "=" * 60)

    # --- registry ---------------------------------------------------------
    pool = pod_pool.PodPool()
    pool.add("podA", "http://a:1")
    pool.add("podB", "http://b:2")
    registry = json.load(open(pod_pool.REGISTRY))
    check("pods are recorded the moment they are added",
          set(registry) == {"podA", "podB"},
          "recorded at deploy time, because a booting pod already bills")
    check("the registry is on disk, not in memory",
          os.path.exists(pod_pool.REGISTRY),
          "the only thing that survives kill -9")

    # --- worker binding ---------------------------------------------------
    urls = [pool.url_for_worker(i) for i in range(6)]
    check("workers spread round-robin over the pool",
          urls == ["http://a:1", "http://b:2"] * 3, str(urls[:4]))
    check("a worker keeps one pod for the whole run",
          pool.url_for_worker(0) == pool.url_for_worker(0),
          "a provider registers its speaker with one server")

    # --- cleanup ----------------------------------------------------------
    await pool.close()
    check("close terminates every pod", sorted(terminated) == ["podA", "podB"])
    check("and the registry is emptied", json.load(open(pod_pool.REGISTRY)) == {})

    before = len(terminated)
    await pool.close()
    check("close is idempotent", len(terminated) == before,
          "it is called from the success path, the handler and the finally")

    # --- a terminate that fails must NOT be forgotten ---------------------
    terminated.clear()
    fail_on.add("podC")
    pool2 = pod_pool.PodPool()
    pool2.add("podC", "http://c:3")
    await pool2.close()
    check("a pod that failed to terminate stays in the registry",
          "podC" in json.load(open(pod_pool.REGISTRY)),
          "so the next sweep retries instead of losing it")
    fail_on.clear()

    # --- adopted pods are never destroyed ---------------------------------
    terminated.clear()
    pool3 = pod_pool.PodPool()
    pool3.add("someone-elses", "http://x:9", owned=False)
    check("an adopted pod is not registered",
          "someone-elses" not in json.load(open(pod_pool.REGISTRY)))
    await pool3.close()
    check("and is never terminated", terminated == [],
          "--use-pod must not destroy a machine it did not create")

    # --- the SIGKILL scenario --------------------------------------------
    terminated.clear()
    pod_pool._write({})
    killed_run = pod_pool.PodPool()
    killed_run.add("orphan1", "http://o1")
    killed_run.add("orphan2", "http://o2")
    del killed_run          # as a SIGKILL does: no close(), no finally

    async def fake_live():
        return {"orphan1", "orphan2"}
    pod_pool.live_pods = fake_live

    swept = await pod_pool.sweep_orphans(verbose=False)
    check("a killed run's pods are recoverable from the registry",
          sorted(swept) == ["orphan1", "orphan2"],
          "this is the whole reason the registry exists")
    check("and the registry is clean afterwards",
          json.load(open(pod_pool.REGISTRY)) == {})

    # --- a pod already gone is forgotten, not reported --------------------
    pod_pool._write({"ghost": {"since": 0, "pid": 1}})
    async def none_live():
        return set()
    pod_pool.live_pods = none_live
    swept = await pod_pool.sweep_orphans(verbose=False)
    check("a pod RunPod no longer lists is forgotten, not 'terminated'",
          swept == [] and json.load(open(pod_pool.REGISTRY)) == {})

    # --- a pod that stops answering ------------------------------------------
    # Found by layer 3 (tools/chaos, dies-mid-book): a lost pod was asked
    # twelve times per chunk and, with one pod, the whole book was lost.
    terminated.clear()
    pool = pod_pool.PodPool()
    pool.add("live", "http://a")
    pool.add("blip", "http://blip")
    for _ in range(pool.LOST_AFTER * 2):
        pool.note("http://blip", reached=False)
    check("a burst of failures right after an answer is a blip, not a lost pod",
          not pool.is_lost("http://blip"), f"(within {pool.LOST_SECONDS:.0f}s of its last answer)")
    pool.pods.remove(("blip", "http://blip"))
    pool.LOST_SECONDS = 0          # the rest is about the count; the window is above
    pool.add("dying", "http://b")
    for _ in range(pool.LOST_AFTER - 1):
        pool.note("http://b", reached=False)
    check("a pod is not given up on before LOST_AFTER failures in a row",
          not pool.is_lost("http://b") and pool.urls() == ["http://a", "http://b"])
    pool.note("http://b", reached=True)
    for _ in range(pool.LOST_AFTER - 1):
        pool.note("http://b", reached=False)
    check("one answer in between starts the count again", not pool.is_lost("http://b"))
    newly = pool.note("http://b", reached=False)
    check("LOST_AFTER failures in a row: lost, and said once", newly and pool.is_lost("http://b")
          and not pool.note("http://b", reached=False))
    check("a lost pod is no longer offered to any retry", pool.urls() == ["http://a"])
    gone = await pool.drop("http://b")
    check("dropping it terminates it and takes it out of the pool",
          gone and terminated == ["dying"] and len(pool) == 1 and not pool.lost)
    fail_on.add("live")
    for _ in range(pool.LOST_AFTER):
        pool.note("http://a", reached=False)
    kept = await pool.drop("http://a")
    check("a pod RunPod will not terminate stays in the pool, for close() to try again",
          not kept and len(pool) == 1 and pool.is_lost("http://a"))
    fail_on.clear()
    adopted = pod_pool.PodPool()
    adopted.LOST_SECONDS = 0
    adopted.add("theirs", "http://c", owned=False)
    for _ in range(adopted.LOST_AFTER):
        adopted.note("http://c", reached=False)
    terminated.clear()
    check("an adopted pod is dropped from the run but never terminated",
          await adopted.drop("http://c") and terminated == [] and len(adopted) == 0)

    pod_pool.terminate = real_terminate
    print("=" * 60)
    failed = [r for r in results if not r[1]]
    print(f"{len(results)} checks: {len(results) - len(failed)} passed, {len(failed)} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
