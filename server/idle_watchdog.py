"""A pod that hears nothing from its client terminates itself.

On 2026-10-08 the Mac narrating Twisted ran out of battery at 08:21 and slept
until 12:05. Its three pods billed the whole time, $3.63 for nothing: a pod
cannot tell a paused client from a gone one, and the only thing that would
have ended them was the next start of the watcher. So the pod decides. After
POD_IDLE_MINUTES (15 by default; 0 turns it off) with no request from the
client, and none in flight, it terminates itself through RunPod's API with the
pod-scoped key RunPod puts in every pod (RUNPOD_POD_ID, RUNPOD_API_KEY).
Measured 2026-10-08: a pod that asked was gone within 17 s; an identical one
that did not ask stayed until it was terminated from outside.

What counts as the client: a request that passed the token check. /health
does not count (the engine polls it while a pod boots, and anyone may), and
neither does a request the token check refused, or a port scanner could keep
a pod alive for ever.

Several server processes (API_WORKERS > 1) share one activity file, so a
process with nothing to do never ends a pod its sibling is serving.

Standard library only: server/custom_api.py imports it, the tests import it
without the model stack.
"""
import json
import os
import tempfile
import threading
import time
import urllib.request

# Shared by the server processes of one pod; the container is single-tenant.
ACTIVITY_FILE = os.path.join(tempfile.gettempdir(), "voxie_last_request")


class IdleWatchdog:
    def __init__(self, minutes, terminate, activity_file=ACTIVITY_FILE, clock=time.time):
        self.limit = max(0.0, float(minutes)) * 60
        self.terminate, self.activity_file, self.clock = terminate, activity_file, clock
        self.inflight, self.done = 0, False
        self._lock = threading.Lock()
        self._touch()          # the server is up: the client has this long to arrive

    def _touch(self):
        try:
            with open(self.activity_file, "a"):
                pass
            now = self.clock()
            os.utime(self.activity_file, (now, now))
        except OSError:
            pass

    def begin(self):
        """A client request started."""
        with self._lock:
            self.inflight += 1
        self._touch()

    def end(self):
        """A client request finished, however it ended."""
        with self._lock:
            self.inflight = max(0, self.inflight - 1)
        self._touch()

    def idle_seconds(self):
        if self.inflight:
            return 0.0
        try:
            return max(0.0, self.clock() - os.path.getmtime(self.activity_file))
        except OSError:
            return 0.0      # no record: never a reason to end a pod

    def check(self):
        """Terminate the pod if the client has been gone too long. True once it is done."""
        if self.done or not self.limit or self.idle_seconds() < self.limit:
            return False
        print(f"⏹️  No request for {self.idle_seconds() / 60:.0f} min: the client is gone; "
              "terminating this pod so it stops billing.", flush=True)
        # Only once RunPod said yes: a failed call is tried again next check.
        self.done = bool(self.terminate())
        return self.done

    def run_forever(self, every=30):
        while not self.done:
            time.sleep(every)
            try:
                self.check()
            except Exception as e:      # noqa: BLE001 -- the watchdog must outlive one bad call
                print(f"⚠️  idle watchdog: {type(e).__name__}: {e}", flush=True)


def terminate_self(env=None, opener=None):
    """Ask RunPod to terminate this pod, with its own pod-scoped key. True if it agreed."""
    env = os.environ if env is None else env
    opener = opener or urllib.request.urlopen
    pod_id, key = env.get("RUNPOD_POD_ID", ""), env.get("RUNPOD_API_KEY", "")
    if not (pod_id and key):
        print("idle watchdog: not on RunPod (no RUNPOD_POD_ID / RUNPOD_API_KEY); nothing to terminate.",
              flush=True)
        return False
    query = json.dumps({"query": 'mutation { podTerminate(input: {podId: "%s"}) }' % pod_id}).encode()
    request = urllib.request.Request(
        "https://api.runpod.io/graphql?api_key=" + key, data=query,
        headers={"Content-Type": "application/json", "User-Agent": "voxie-idle-watchdog"})
    try:
        with opener(request, timeout=30) as r:      # nosec B310: fixed https URL
            body = json.loads(r.read() or b"{}")
    except Exception as e:      # noqa: BLE001
        print(f"idle watchdog: RunPod did not answer ({type(e).__name__}); trying again.", flush=True)
        return False
    if body.get("errors"):
        print(f"idle watchdog: RunPod refused: {str(body['errors'])[:200]}", flush=True)
        return False
    return True


def from_env(env=None):
    """The watchdog as the pod is configured (POD_IDLE_MINUTES, default 15)."""
    env = os.environ if env is None else env
    try:
        minutes = float(env.get("POD_IDLE_MINUTES", "15"))
    except ValueError:
        minutes = 15.0
    return IdleWatchdog(minutes, lambda: terminate_self(env))
