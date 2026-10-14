#!/usr/bin/env python3
"""A pod that fails to boot is terminated, and forgotten only if RunPod agrees.

Drives acquire_serving_pod through each failure path with fakes -- no RunPod,
no network -- against a temporary registry. Found on 2026-09-24: a 4090 that
never published its TCP mapping was terminated, but stayed in
~/.librasync_active_pods.json after the run finished, and the log had claimed
"billing is safely locked at $0.00" without ever reading RunPod's answer.

    python tools/test_pod_release.py
"""
import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import librerian_engine_v3 as eng  # noqa: E402
from core import pod_pool  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  ({detail})" if detail else ""))


tmp = tempfile.mkdtemp()
pod_pool.REGISTRY = os.path.join(tmp, "pods.json")


def registry():
    try:
        return json.load(open(pod_pool.REGISTRY))
    except (OSError, ValueError):
        return {}


def scenario(deploys, resolve, awake, terminate_ok=True):
    """Run acquire_serving_pod with scripted fakes; return (result, printed, terminated)."""
    if os.path.exists(pod_pool.REGISTRY):
        os.remove(pod_pool.REGISTRY)
    ids = iter(deploys)
    terminated = []

    async def deploy():
        return next(ids), 8

    async def resolve_url(pod_id):
        r = resolve(pod_id)
        if isinstance(r, Exception):
            raise r
        return r

    async def wait(health_path, base_url):
        return awake(base_url)

    async def token_ok(base_url):
        return True, ""

    async def describe(pod_id):
        return None

    async def terminate(pod_id):
        terminated.append(pod_id)
        if terminate_ok:
            pod_pool._unregister(pod_id)
        return terminate_ok

    eng.deploy_burner_pod, eng.resolve_pod_base_url, eng.wait_for_ai_brain = deploy, resolve_url, wait
    eng.assert_server_requires_token, eng.describe_pod = token_ok, describe
    pod_pool.terminate = terminate

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            result = asyncio.run(eng.acquire_serving_pod("/health"))
        except Exception as exc:
            result = exc
    return result, out.getvalue(), terminated


# --- a pod with no TCP mapping, then a good one ---------------------------------
res, log, gone = scenario(
    deploys=["bad-pod", "good-pod"],
    resolve=lambda p: Exception("no TCP mapping within 600s") if p == "bad-pod" else "http://1.2.3.4:5",
    awake=lambda url: (True, None))
check("the run carries on with the next pod", isinstance(res, tuple) and res[0] == "good-pod", str(res)[:60])
check("the failed pod was terminated", gone == ["bad-pod"])
check("...and forgotten, once RunPod accepted", "bad-pod" not in registry(), str(registry()))
check("the serving pod stays registered until released", "good-pod" in registry())

# --- RunPod refuses the termination -----------------------------------------------
res, log, gone = scenario(
    deploys=["stuck-pod", "good-pod"],
    resolve=lambda p: Exception("no mapping") if p == "stuck-pod" else "http://1.2.3.4:5",
    awake=lambda url: (True, None), terminate_ok=False)
check("an unconfirmed termination keeps the pod in the registry", "stuck-pod" in registry(),
      "so the next sweep retries it")
check("...and never claims billing is locked at $0.00", "$0.00" not in log.split("good-pod")[0])
check("...and says so", "did not confirm terminating stuck-pod" in log)

# --- pods that never serve --------------------------------------------------------
res, log, gone = scenario(
    deploys=[f"dead-{i}" for i in range(eng.BOOT_ATTEMPTS)],
    resolve=lambda p: "http://1.2.3.4:5",
    awake=lambda url: (False, "refused"))
check("after BOOT_ATTEMPTS the run fails loudly", isinstance(res, Exception) and "No pod served" in str(res))
check("every dead pod was terminated", len(gone) == eng.BOOT_ATTEMPTS, f"{len(gone)} terminated")
check("...and none is left in the registry", registry() == {}, str(registry()))

print("\n" + "=" * 60)
passed = sum(1 for r in results if r)
print(f"{len(results)} checks: {passed} passed, {len(results) - passed} FAILED")
sys.exit(0 if passed == len(results) else 1)
