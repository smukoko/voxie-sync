"""The orphan sweep never loses a billing pod and never kills a live run's.

A mocked RunPod API and a temporary registry; nothing reaches RunPod. Each case
is a defect found on 2026-09-26: GraphQL errors arrive inside HTTP 200, an API
outage looked like "no pods", and a restarted ghost killed a running engine's.
"""
import asyncio, os, subprocess, sys, tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["RUNPOD_API_KEY"] = "fake"
from core import pod_pool  # noqa: E402

pod_pool.REGISTRY = os.path.join(tempfile.mkdtemp(), "pods.json")
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  ({detail})" if detail else ""))


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        return self._body


def api(listed=None, terminate_body=None, fail=False):
    calls = []

    class C:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, **k):
            calls.append(json["query"])
            if fail:
                raise RuntimeError("network down")
            if "myself" in json["query"]:
                return Resp(200, {"data": {"myself": {"pods": [{"id": p} for p in listed or []]}}})
            return Resp(200, terminate_body or {"data": {"podTerminate": None}})
    return (lambda *a, **k: C()), calls


async def main():
    client, _ = api(terminate_body={"errors": [{"message": "Something went wrong"}], "data": None})
    pod_pool._register("pod-A")
    with mock.patch.object(pod_pool.httpx, "AsyncClient", client):
        ok = await pod_pool.terminate("pod-A")
    check("a GraphQL error inside HTTP 200 is not a termination", ok is False and "pod-A" in pod_pool._read())

    client, _ = api(fail=True)
    pod_pool._write({"pod-B": {"since": 0, "pid": 1, "note": "pool"}})
    with mock.patch.object(pod_pool.httpx, "AsyncClient", client):
        killed = await pod_pool.sweep_orphans(verbose=False)
    check("an API outage at startup keeps every registered pod", killed == [] and "pod-B" in pod_pool._read())

    engine = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        pod_pool._write({"pod-C": {"since": 0, "pid": engine.pid, "note": "pool"},
                         "pod-D": {"since": 0, "pid": 999999, "note": "pool"}})
        client, calls = api(listed=["pod-C", "pod-D"])
        with mock.patch.object(pod_pool.httpx, "AsyncClient", client):
            killed = await pod_pool.sweep_orphans(verbose=False)
        check("a pod whose engine is still running is left alone", "pod-C" not in killed and "pod-C" in pod_pool._read())
        check("a pod whose process is gone is terminated", killed == ["pod-D"] and "pod-D" not in pod_pool._read())
    finally:
        engine.kill()

    client, _ = api(listed=[])
    pod_pool._write({"pod-E": {"since": 0, "pid": 999999, "note": "pool"}})
    with mock.patch.object(pod_pool.httpx, "AsyncClient", client):
        await pod_pool.sweep_orphans(verbose=False)
    check("a pod RunPod confirms is gone is forgotten", "pod-E" not in pod_pool._read())

asyncio.run(main())
print("=" * 60)
print(f"{len(results)} checks: {sum(results)} passed, {len(results) - sum(results)} FAILED")
sys.exit(0 if all(results) else 1)
