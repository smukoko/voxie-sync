"""A pod whose client is gone terminates itself (server/idle_watchdog.py).

2026-10-08: the Mac narrating Twisted ran out of battery and slept for four
hours; its three pods billed the whole time ($3.63). Offline: a fake clock,
a fake RunPod answer, and the server's source for the wiring.
"""
import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, ROOT)
from idle_watchdog import IdleWatchdog, from_env, terminate_self  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def watchdog(minutes=15, answers=(True,), shared=None):
    clock, calls = Clock(), []
    seq = list(answers)

    def terminate():
        calls.append(clock.t)
        return seq.pop(0) if len(seq) > 1 else seq[0]
    path = shared or os.path.join(tempfile.mkdtemp(), "last_request")
    return IdleWatchdog(minutes, terminate, activity_file=path, clock=clock), clock, calls


# --- the clock ---------------------------------------------------------------
w, clock, calls = watchdog()
clock.t += 14 * 60
check("a pod idle for 14 of its 15 minutes keeps running", not w.check() and not calls)
clock.t += 61
check("at 15 minutes with no request it terminates itself", w.check() and len(calls) == 1)
clock.t += 600
check("and asks only once", not w.check() and len(calls) == 1)

w, clock, calls = watchdog()
clock.t += 10 * 60
w.begin()
clock.t += 30 * 60          # one very long request
check("a request in flight is never idle, however long it takes", not w.check() and not calls)
w.end()
clock.t += 14 * 60
check("the clock restarts when the request ends", not w.check() and not calls)

w, clock, calls = watchdog(answers=(False, True))
clock.t += 16 * 60
check("RunPod refusing the first time does not end the watch", not w.check() and len(calls) == 1)
clock.t += 30
check("the next check asks again", w.check() and len(calls) == 2)

w, clock, calls = watchdog(minutes=0)
clock.t += 24 * 3600
check("POD_IDLE_MINUTES=0 turns it off", not w.check() and not calls)

# --- several server processes, one pod -----------------------------------------
shared = os.path.join(tempfile.mkdtemp(), "last_request")
a, clock_a, calls_a = watchdog(shared=shared)
b, clock_b, calls_b = watchdog(shared=shared)
clock_a.t = clock_b.t = clock_a.t + 10 * 60
b.begin()
b.end()                       # process B served a chunk; A had nothing to do
clock_a.t = clock_b.t = clock_a.t + 10 * 60
check("a process with nothing to do never ends a pod its sibling is serving",
      not a.check() and not calls_a, f"(A idle {a.idle_seconds() / 60:.0f} min by the shared record)")

# --- a broken record is never a reason to end a pod ------------------------------
nowhere = os.path.join(tempfile.mkdtemp(), "no", "such", "dir", "last_request")
lost, lost_clock, lost_calls = watchdog(shared=nowhere)
lost_clock.t += 24 * 3600
check("an activity record that cannot be written or read never ends a pod",
      lost.idle_seconds() == 0.0 and not lost.check() and not lost_calls)

# --- the watch itself outlives one bad check ------------------------------------
import idle_watchdog  # noqa: E402
w, clock, calls = watchdog(minutes=1)
said = []
real_sleep, real_print = idle_watchdog.time.sleep, print
broke = {"once": True}
real_check = w.check


def flaky_check():
    if broke.pop("once", False):
        raise RuntimeError("RunPod's DNS hiccupped")
    clock.t += 120
    return real_check()


w.check = flaky_check
idle_watchdog.time.sleep = lambda s: None
try:
    import builtins
    builtins.print = lambda *a, **k: said.append(" ".join(map(str, a)))
    w.run_forever(every=0)
finally:
    builtins.print = real_print
    idle_watchdog.time.sleep = real_sleep
check("the watch survives a check that raises, and still ends the pod after",
      w.done and len(calls) == 1 and any("DNS hiccupped" in line for line in said), str(said[:2]))

# --- asking RunPod -------------------------------------------------------------------
sent = []


def opener(answer):
    def open_(request, timeout=None):
        sent.append((request.full_url, json.loads(request.data)))
        if isinstance(answer, Exception):
            raise answer
        body = io.BytesIO(json.dumps(answer).encode())
        body.__enter__ = lambda *a: body
        body.__exit__ = lambda *a: False
        return body
    return open_


env = {"RUNPOD_POD_ID": "abc123", "RUNPOD_API_KEY": "podkey"}
ok = terminate_self(env, opener({"data": {"podTerminate": None}}))
url, body = sent[-1]
check("it terminates its own pod, with its own key", ok and url.startswith("https://api.runpod.io/graphql")
      and "api_key=podkey" in url and 'podId: "abc123"' in body["query"] and "podTerminate" in body["query"])
check("a refusal from RunPod is not taken for a yes",
      terminate_self(env, opener({"errors": [{"message": "Unauthorized"}]})) is False)
check("no answer from RunPod is not a yes either", terminate_self(env, opener(OSError("down"))) is False)
before = len(sent)
check("off RunPod (no pod id or key) nothing is called",
      terminate_self({}, opener({})) is False and len(sent) == before)
check("the default is 15 minutes", from_env({}).limit == 15 * 60)
check("POD_IDLE_MINUTES sets it", from_env({"POD_IDLE_MINUTES": "5"}).limit == 5 * 60)
check("a value that is not a number keeps the default", from_env({"POD_IDLE_MINUTES": "soon"}).limit == 15 * 60)

# --- the server wires it as intended ---------------------------------------------
api = open(os.path.join(ROOT, "server", "custom_api.py")).read()
mw = api[api.index("async def require_token"):api.index("# vLLM batches the LLM")]
check("only requests that passed the token check count as the client",
      mw.index("status_code=401") < mw.index("_idle.begin()"))
check("/health does not count (the engine polls it while a pod boots)",
      "request.url.path in _OPEN_PATHS" in mw and '"/health"' in api[api.index("_OPEN_PATHS ="):][:80])
check("a request that fails still ends its activity (finally)", "finally:\n        _idle.end()" in mw)
check("the clock starts once the model has loaded",
      api.index("cosyvoice = AutoModel(") < api.index("_idle = _idle_watchdog()"))
docker = open(os.path.join(ROOT, "docker", "cosyvoice", "Dockerfile")).read()
check("the image carries the module beside the server",
      "COPY server/idle_watchdog.py /opt/cosyvoice/idle_watchdog.py" in docker)

# --- the engine sends the user's setting -------------------------------------------
from core import pod_pool  # noqa: E402
os.environ["POD_IDLE_MINUTES"] = "20"
check("POD_IDLE_MINUTES in .env travels with the deploy", 'key: "POD_IDLE_MINUTES", value: "20.0"' in pod_pool.pod_env(1, "tok"))
os.environ["POD_IDLE_MINUTES"] = "twenty"
try:
    pod_pool.pod_env(1, "tok")
    check("a setting that is not a number is refused before anything bills", False)
except ValueError:
    check("a setting that is not a number is refused before anything bills", True)
del os.environ["POD_IDLE_MINUTES"]
check("unset, nothing is sent and the image's default applies", "POD_IDLE_MINUTES" not in pod_pool.pod_env(1, "tok"))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
