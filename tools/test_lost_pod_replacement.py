"""What the engine does when a pod is lost mid-book, decision by decision.

Found by layer 3 (tools/chaos, only-pod-dies): with one pod, a pod that died
mid-book lost the whole book. The wait loop now retires the workers of a lost
pod, terminates the pod, rents a replacement within the plan and a cap, and
stops at once, with the right words, when nothing is left. The chaos
scenarios run all of it end to end; this runs each decision offline.
"""
import asyncio
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("RUNPOD_API_KEY", "test-only")

import librerian_engine_v3 as eng  # noqa: E402
from core import pod_pool  # noqa: E402

pod_pool.REGISTRY = os.path.join(tempfile.mkdtemp(), "pods.json")   # never the real one
passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


# --- which workers retired, and which crashed -------------------------------------
async def workers():
    async def lost():
        return "pod lost"

    async def finished():
        return None

    async def crashed():
        raise RuntimeError("worker crashed")

    async def forever():
        await asyncio.sleep(3600)
    tasks = [asyncio.create_task(c()) for c in (lost, finished, crashed, forever)]
    tasks[3].cancel()
    await asyncio.wait(tasks)
    return tasks, eng.retired_workers(tasks, set(tasks))

tasks, retired = asyncio.run(workers())
check("a worker that returned 'pod lost' retired; one that crashed or was cancelled did not",
      retired == [tasks[0]], f"({len(retired)} retired)")

# --- terminating lost pods, renting replacements --------------------------------------
terminated, rented = [], []


async def fake_terminate(pod_id):
    terminated.append(pod_id)
    return True

pod_pool.terminate = fake_terminate


def lost_pool(*urls):
    pool = pod_pool.PodPool()
    pool.LOST_SECONDS = 0
    for i, url in enumerate(urls):
        pool.add(f"pod{i}", url)
    for _ in range(pool.LOST_AFTER):
        pool.note(urls[0], reached=False)
    return pool


async def rent():
    rented.append(1)
    return "pool", 8


async def replace(pool, wanted, replacements, acquisition=None):
    acquisition, replacements = await eng.replace_lost_pods(pool, wanted, replacements, acquisition, rent)
    if acquisition is not None:
        await acquisition
    return acquisition, replacements

pool = lost_pool("http://a", "http://b")
acq, n = asyncio.run(replace(pool, 2, 0))
check("a lost pod is terminated and leaves the pool", terminated == ["pod0"] and pool.urls() == ["http://b"])
check("one replacement is rented when the plan wanted more pods", acq is not None and n == 1 and rented == [1])

rented.clear()
acq, n = asyncio.run(replace(lost_pool("http://c", "http://d"), 2, eng.MAX_POD_REPLACEMENTS))
check("none past the cap: a host that keeps failing is not worth another boot", acq is None and not rented)
acq, n = asyncio.run(replace(lost_pool("http://e", "http://f"), 1, 0))
check("none when the pods left already meet the plan", acq is None and not rented)


async def still_booting():
    pool = lost_pool("http://g")
    booting = asyncio.create_task(asyncio.sleep(3600))
    acq, n = await eng.replace_lost_pods(pool, 2, 0, booting, rent)
    booting.cancel()
    return acq is booting and n == 0

check("none while the first pods are still being rented", asyncio.run(still_booting()) and not rented)

# --- stopping when nothing is left ----------------------------------------------------
async def outcomes():
    async def refused():
        raise eng.OutOfFundsError("RunPod refused the deploy: balance too low. Top up at ...")

    async def ok():
        return "pool", 8
    failed_acq, fine_acq = asyncio.create_task(refused()), asyncio.create_task(ok())
    cancelled = asyncio.create_task(asyncio.sleep(3600))
    cancelled.cancel()
    running = asyncio.create_task(asyncio.sleep(3600))
    await asyncio.wait([failed_acq, fine_acq, cancelled])
    out = {
        "workers left": eng.nothing_left(["worker"], None),
        "still renting": eng.nothing_left([], running),
        "refused": eng.nothing_left([], failed_acq),
        "lost, none coming": eng.nothing_left([], None),
        "cancelled": eng.nothing_left([], cancelled),
    }
    running.cancel()
    return out

out = asyncio.run(outcomes())
check("workers left: the run goes on", out["workers left"] is None)
check("a pod still being rented: the run waits for it", out["still renting"] is None)
check("every pod refused: the refusal's own words, 'top up' and all",
      out["refused"].startswith("No pod served: RunPod refused") and "Top up" in out["refused"], out["refused"])
check("every pod lost and none coming: it says so, and that the narrated chunks are kept",
      "Every pod was lost" in out["lost, none coming"] and "kept" in out["lost, none coming"])
check("a cancelled acquisition is not mistaken for a refusal", "Every pod was lost" in out["cancelled"])

# --- the wait loop itself, with fake workers ------------------------------------------------
# The real engine's loop (wait_for_generation), driven end to end on a real
# asyncio.Queue: each worker below is what a runpod_worker does in that case.
async def drains(q):
    await q.get()
    q.task_done()


async def loses_its_pod(q, pool, url):
    chunk = await q.get()
    for _ in range(pool.LOST_AFTER):
        pool.note(url, reached=False)
    q.put_nowait(chunk)          # handed back for another pod, as runpod_worker does
    q.task_done()
    return "pod lost"


async def crashes(q):
    await q.get()
    raise RuntimeError("worker crashed")


async def waits_forever(q):
    await asyncio.sleep(3600)


async def run_loop(make_workers, wanted=1, rent_one=None, timeout_s=None, acquisition=None, pool=None):
    terminated.clear()
    q = asyncio.Queue()
    q.put_nowait({"text": "one chunk"})
    pool = pool or pod_pool.PodPool()
    pool.LOST_SECONDS = 0
    pool.add("pod0", "http://p0")
    consumers = []
    consumers.extend(asyncio.create_task(w) for w in make_workers(q, pool, consumers))
    join_task = asyncio.create_task(q.join())
    failure, acq = await eng.wait_for_generation(
        join_task, consumers, pool, acquisition, wanted,
        (lambda: rent_one(q, pool, consumers)) if rent_one else (lambda: rent()), timeout_s)
    for t in [join_task, *consumers] + ([acq] if acq else []):
        t.cancel()
    return failure, list(terminated), q.qsize()

failure, gone, left = asyncio.run(run_loop(lambda q, p, c: [drains(q)]))
check("the queue drains: no failure", failure is None and left == 0, repr(failure))

failure, gone, left = asyncio.run(run_loop(lambda q, p, c: [crashes(q), waits_forever(q)]))
check("a worker that crashes stops the run, said as such, while another still waits",
      failure and "1 worker(s) died" in failure, repr(failure))


async def replacement(q, pool, consumers):
    """A new pod serves and its worker joins the swarm, as start_workers_for does."""
    pool.add("pod1", "http://p1")
    consumers.append(asyncio.create_task(drains(q)))
    return pool, 8

failure, gone, left = asyncio.run(run_loop(
    lambda q, p, c: [loses_its_pod(q, p, "http://p0")], wanted=1, rent_one=replacement))
check("the only pod is lost: it is terminated, a replacement is rented, and its new worker "
      "narrates the handed-back chunk", failure is None and gone == ["pod0"] and left == 0,
      f"({failure!r}, {gone}, {left} left)")

# The old loop said the same words, but only once the whole limit (8 hours) had
# run out with nothing left to narrate: a 5-second limit here, and a clock.
started = time.monotonic()
failure, gone, left = asyncio.run(run_loop(lambda q, p, c: [loses_its_pod(q, p, "http://p0")], wanted=0,
                                           timeout_s=5))
waited = time.monotonic() - started
check("the only pod is lost and none may be rented (your own server): the run stops at once, "
      "saying the narrated chunks are kept",
      failure and "Every pod was lost" in failure and gone == ["pod0"] and waited < 2,
      f"({failure!r}, {waited:.1f} s)")


async def refuses(q, pool, consumers):
    raise eng.OutOfFundsError("RunPod refused the deploy: balance too low. Top up at ...")

failure, gone, left = asyncio.run(run_loop(
    lambda q, p, c: [loses_its_pod(q, p, "http://p0")], wanted=1, rent_one=refuses))
check("the replacement is refused: the refusal's own words end the run",
      failure and failure.startswith("No pod served: RunPod refused"), repr(failure))


async def boots_late(q, pool, consumers):
    """The first pod serves after the loop started waiting: its worker must still be watched."""
    await asyncio.sleep(0.05)
    consumers.append(asyncio.create_task(drains(q)))
    return pool, 8


async def late_pod():
    q = asyncio.Queue()
    q.put_nowait({"text": "one chunk"})
    consumers = []
    acq = asyncio.create_task(boots_late(q, pod_pool.PodPool(), consumers))
    join_task = asyncio.create_task(q.join())
    out = await eng.wait_for_generation(join_task, consumers, pod_pool.PodPool(), acq, 1, rent, 5)
    return out[0], q.qsize()

failure, left = asyncio.run(late_pod())
check("a pod that serves after the wait began: its workers are watched and drain the queue",
      failure is None and left == 0, repr(failure))

failure, gone, left = asyncio.run(run_loop(lambda q, p, c: [waits_forever(q)], timeout_s=0.1))
check("a run past its time limit stops, in minutes", failure and failure.startswith("Generation exceeded 0 minutes")
      and left == 1, repr(failure))
failure, gone, left = asyncio.run(run_loop(lambda q, p, c: [waits_forever(q)], timeout_s=0))
check("a limit already spent stops before waiting", failure and failure.startswith("Generation exceeded"), repr(failure))

# --- the pool, refusing as a whole ---------------------------------------------------------
async def all_refused():
    real = eng.acquire_serving_pod

    async def refuse(health_path):
        raise eng.OutOfFundsError("RunPod refused the deploy: balance too low. Top up at ...")
    eng.acquire_serving_pod = refuse
    try:
        await eng.acquire_pool("/health", 2, pod_pool.PodPool())
    except eng.DeployRefusedError as e:
        return str(e)
    finally:
        eng.acquire_serving_pod = real

said = asyncio.run(all_refused())
check("when every pod is refused, the pool raises the refusal itself", said and "Top up" in said, str(said))

# --- a pod that ignores the token ends the run, it is not retried -------------------
async def open_door():
    saved = (eng.deploy_burner_pod, eng.resolve_pod_base_url, eng.wait_for_ai_brain,
             eng.assert_server_requires_token, eng.release_pod)
    released = []

    async def deploy():
        return "podX", 8

    async def resolve(pod_id):
        return "http://podX"

    async def healthy(path, base):
        return True, None

    async def ignores_token(base):
        return False, "it answers without a token"

    async def release(pod_id):
        released.append(pod_id)
        return True
    (eng.deploy_burner_pod, eng.resolve_pod_base_url, eng.wait_for_ai_brain,
     eng.assert_server_requires_token, eng.release_pod) = deploy, resolve, healthy, ignores_token, release
    try:
        await eng.acquire_serving_pod("/health")
        return None, released
    except eng.DeployRefusedError as e:
        return str(e), released
    finally:
        (eng.deploy_burner_pod, eng.resolve_pod_base_url, eng.wait_for_ai_brain,
         eng.assert_server_requires_token, eng.release_pod) = saved

said, released = asyncio.run(open_door())
check("a pod that ignores the token is terminated and refused for good, not retried",
      said and "TTS_API_TOKEN" in said and released == ["podX"], f"({said!r}, {released})")

# --- the old terminate path asks RunPod at the right address ----------------------------
asked = []


class _Client:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, timeout=None):
        asked.append((url, json["query"]))

real_client = eng.httpx.AsyncClient
eng.httpx.AsyncClient = _Client
try:
    asyncio.run(eng.terminate_burner_pod("podY"))
finally:
    eng.httpx.AsyncClient = real_client
check("terminate_burner_pod asks RunPod's GraphQL to end that pod",
      asked and asked[0][0].startswith("https://api.runpod.io/graphql") and 'podId: "podY"' in asked[0][1])

# --- RunPod's addresses -------------------------------------------------------------------
for name in ("RUNPOD_GRAPHQL_URL", "RUNPOD_REST_URL"):
    os.environ.pop(name, None)
check("by default the key goes to RunPod over https",
      pod_pool.graphql_url("k").startswith("https://api.runpod.io/graphql?api_key=k")
      and pod_pool.rest_url("/pods/x") == "https://rest.runpod.io/v1/pods/x")
os.environ["RUNPOD_REST_URL"] = "http://127.0.0.1:9999/v1"
check("a test may point it at this machine", pod_pool.rest_url("/pods/x") == "http://127.0.0.1:9999/v1/pods/x")
os.environ["RUNPOD_GRAPHQL_URL"] = "http://evil.example/graphql"
try:
    pod_pool.graphql_url("k")
    check("the key is never sent in clear text to another host", False)
except ValueError:
    check("the key is never sent in clear text to another host", True)
for name in ("RUNPOD_GRAPHQL_URL", "RUNPOD_REST_URL"):
    os.environ.pop(name, None)
check("dropping a pod the pool does not hold is a no-op", asyncio.run(pod_pool.PodPool().drop("http://none")))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
