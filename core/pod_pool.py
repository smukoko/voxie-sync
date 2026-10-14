"""Several pods for one book, and a registry that survives being killed.

Generation is 72-87% of a book's wall clock and one L40S saturates at about
3.8x realtime, so the only way past that on this model is more cards. Five pods
take The Screwtape Letters' generation from 41 minutes to about 8.

The throughput is the easy part. The dangerous part is that this multiplies the
one failure this project already has on record:

    kill -9 on the engine orphans its pod, and the pod keeps billing ... Four
    pods in one day were deployed and never terminated by the engine because the
    engine was killed by hand; one ran 26 minutes unnoticed at $1.09/hr.

A SIGKILL runs no cleanup -- not `finally`, not `atexit`, not a signal handler.
So cleanup cannot live only in the process that owns the pods. Every pod is
written to a registry file the instant it is deployed and removed only once
RunPod confirms it is gone, which means a killed run leaves a readable list of
exactly what is still costing money. `sweep_orphans()` acts on that list, and
mac_ghost calls it at startup: the next drop cleans up after the last kill,
without anybody having to remember.

The pool is also elastic rather than fixed. Capacity churns faster than one
sweep of the preference list takes, so insisting on exactly five pods means
sometimes getting none. Whatever serves is what the book runs on, and the run
says how many it got rather than failing.
"""
import json
import re
import os
import time
from urllib.parse import urlparse

import httpx

REGISTRY = os.path.expanduser("~/.librasync_active_pods.json")


def _api_key():
    return os.getenv("RUNPOD_API_KEY")


def _endpoint(name, default):
    """A RunPod address, from the environment when set.

    Set only by tools/chaos (layer 3), which runs the real engine against a
    fake RunPod that fails on cue. The API key travels with every request, so
    anything but https is refused unless it is this machine.
    """
    url = (os.getenv(name) or default).rstrip("/")
    parts = urlparse(url)
    if parts.scheme != "https" and parts.hostname not in ("127.0.0.1", "localhost"):
        raise ValueError(f"{name}={url!r}: RunPod's key is sent there, so it must be https "
                         "(or 127.0.0.1 for a test).")
    return url


def graphql_url(key=None):
    """RunPod's GraphQL API, with the key (RUNPOD_GRAPHQL_URL for a test)."""
    return f"{_endpoint('RUNPOD_GRAPHQL_URL', 'https://api.runpod.io/graphql')}?api_key={key or _api_key()}"


def rest_url(path):
    """RunPod's REST API, e.g. rest_url(f"/pods/{pod_id}") (RUNPOD_REST_URL for a test)."""
    return _endpoint("RUNPOD_REST_URL", "https://rest.runpod.io/v1") + path


def _register(pod_id, note=""):
    """Record a pod before it can possibly be lost. Never raises.

    Written at deploy time rather than after the pod serves, because a pod that
    is deploying is already billing -- and a crash between those two points is
    exactly when the registry earns its keep.
    """
    try:
        pods = _read()
        pods[pod_id] = {"since": time.time(), "pid": os.getpid(), "note": note}
        _write(pods)
    except OSError as e:
        print(f"   ⚠️  Could not record pod {pod_id} ({e}); "
              "terminate it by hand if this run is killed.")


def _unregister(pod_id):
    try:
        pods = _read()
        pods.pop(pod_id, None)
        _write(pods)
    except OSError:
        pass


def _read():
    try:
        with open(REGISTRY) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write(pods):
    tmp = REGISTRY + ".tmp"
    with open(tmp, "w") as f:
        json.dump(pods, f, indent=1)
    os.replace(tmp, REGISTRY)


def _graphql_ok(r):
    """True only for a GraphQL answer with no errors.

    GraphQL reports failure inside an HTTP 200 -- an unknown pod, a revoked
    key, a transient server error all arrive as 200 with an `errors` array. A
    200 alone therefore proves nothing, and treating it as proof forgot pods
    that were still billing.
    """
    if r.status_code != 200:
        return False
    try:
        body = r.json()
    except ValueError:
        return False
    return not body.get("errors")


async def terminate(pod_id):
    """Terminate one pod and forget it. True only if RunPod confirmed it."""
    key = _api_key()
    if not key:
        return False
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                graphql_url(key),
                json={"query": 'mutation { podTerminate(input: {podId: "%s"}) }' % pod_id},
                timeout=30.0)
        ok = _graphql_ok(r)
        if not ok:
            print(f"   ⚠️  RunPod did not confirm terminating {pod_id}: {r.text[:120]}")
    except Exception as e:
        print(f"   ⚠️  Could not terminate {pod_id}: {str(e)[:70]}")
        return False
    if ok:
        _unregister(pod_id)
    return ok


async def live_pods():
    """Pod ids RunPod currently reports as ours, or None when it cannot say.

    None, not an empty set: "the API is down" and "you have no pods" must not
    look alike, because the sweep forgets every registered pod that is absent
    from this answer -- an outage at startup used to wipe the whole registry
    without terminating anything.
    """
    key = _api_key()
    if not key:
        return None
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(graphql_url(key),
                             json={"query": "query { myself { pods { id } } }"},
                             timeout=30.0)
        if not _graphql_ok(r):
            return None
        return {p["id"] for p in r.json()["data"]["myself"]["pods"]}
    except Exception:
        return None


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True


async def sweep_orphans(verbose=True):
    """Terminate registered pods that no run is using. Returns the ids killed.

    Called by mac_ghost at startup, so the next drop pays for the last kill.
    A pod whose registering process is still alive belongs to a live run and
    is left alone: launchd restarts a crashed ghost while its engine may still
    be narrating. A pod RunPod no longer lists is simply forgotten.
    """
    registered = _read()
    if not registered:
        return []
    alive = await live_pods()
    if alive is None:
        if verbose:
            print(f"   ⚠️  RunPod did not answer; keeping all {len(registered)} registered pod(s) "
                  "for the next sweep.")
        return []
    killed, forgotten = [], []
    for pod_id, info in list(registered.items()):
        if pod_id not in alive:
            _unregister(pod_id)
            forgotten.append(pod_id)
            continue
        if info.get("pid") and info.get("pid") != os.getpid() and _pid_alive(info["pid"]):
            if verbose:
                print(f"   ⏭️  Pod {pod_id} belongs to a running process ({info['pid']}); leaving it.")
            continue
        age = (time.time() - info.get("since", 0)) / 60
        if verbose:
            print(f"   🧟 Orphaned pod {pod_id} has been billing for {age:.0f} min — terminating.")
        if await terminate(pod_id):
            killed.append(pod_id)
    if verbose and forgotten:
        print(f"   🧹 Forgot {len(forgotten)} pod(s) already gone.")
    return killed


class PodPool:
    """The pods one run owns, and the guarantee that they all get terminated.

    `close()` is idempotent and safe to call from a success path, an exception
    handler and a finally block alike -- which is deliberate, because the
    alternative is reasoning about which of those ran.
    """

    # Connection-level failures in a row before a pod is taken for lost: no
    # answer, a reset, a timeout. An HTTP error is a server that is alive.
    LOST_AFTER = int(os.getenv("POD_LOST_AFTER", "6"))
    # ...and this long without a single answer. A healthy pod drops the odd
    # connection (Twisted, 2026-10-08: four RemoteProtocolErrors in 35 minutes,
    # each fine on the retry), and eight workers can stack six of those in
    # seconds; a dead pod never answers again.
    LOST_SECONDS = float(os.getenv("POD_LOST_SECONDS", "90"))

    def __init__(self):
        self.pods = []        # [(pod_id, base_url)]
        self.adopted = set()  # ids this run did not create and must not destroy
        self.failures = {}    # base_url -> connection failures in a row
        self.answered = {}    # base_url -> when it last answered (or joined)
        self.lost = set()     # base_urls given up on, not yet dropped

    def add(self, pod_id, base_url, owned=True):
        self.pods.append((pod_id, base_url))
        self.failures[base_url] = 0
        self.answered[base_url] = time.monotonic()
        if owned:
            _register(pod_id, note="pool")
        else:
            self.adopted.add(pod_id)

    def urls(self):
        """The pods still worth asking: a lost one is skipped by every retry."""
        return [u for _, u in self.pods if u not in self.lost]

    def note(self, base_url, reached):
        """Record whether a request reached this pod. True when it is newly lost.

        Found by layer 3 (tools/chaos, dies-mid-book): with one pod, a pod that
        died mid-book was asked twelve times per chunk, every chunk was
        quarantined, and the book was lost while a replacement would have
        saved it.
        """
        now = time.monotonic()
        if reached:
            self.failures[base_url] = 0
            self.answered[base_url] = now
            return False
        self.failures[base_url] = self.failures.get(base_url, 0) + 1
        silent = now - self.answered.setdefault(base_url, now)
        if (self.failures[base_url] >= self.LOST_AFTER and silent >= self.LOST_SECONDS
                and base_url not in self.lost):
            self.lost.add(base_url)
            return True
        return False

    def is_lost(self, base_url):
        return base_url in self.lost

    async def drop(self, base_url):
        """Terminate a lost pod and take it out of the pool. True if it is gone."""
        for pod_id, url in list(self.pods):
            if url != base_url:
                continue
            if pod_id not in self.adopted and not await terminate(pod_id):
                return False        # stays in the pool, so close() tries again
            self.pods.remove((pod_id, url))
            self.lost.discard(url)
            return True
        return True

    def url_for_worker(self, i):
        """Round-robin, so N workers spread evenly over M pods.

        Workers are bound to a pod for the life of the run rather than per
        request: a provider registers its speaker with one server, and moving a
        worker between pods mid-run would hit a server that has never seen it.
        """
        return self.pods[i % len(self.pods)][1]

    def __len__(self):
        return len(self.pods)

    async def close(self):
        """Terminate every pod this run created. Idempotent."""
        for pod_id, _ in list(self.pods):
            if pod_id in self.adopted:
                print(f"   ♻️  Leaving pod {pod_id} running (not ours to terminate).")
                continue
            if await terminate(pod_id):
                print(f"   🧨 Terminated {pod_id}.")
            else:
                # Left in the registry on purpose, so the next sweep tries again
                # rather than this being the last anyone hears of it.
                print(f"   ‼️  {pod_id} may still be billing — it stays in "
                      f"{REGISTRY} for the next sweep.")
        self.pods = [p for p in self.pods if p[0] in self.adopted]


# A value spliced into the GraphQL text, so only characters that cannot end a
# string or start a new field. `openssl rand -hex 32`, and `voxie init`'s
# secrets.token_hex(32), are well inside it.
_POD_ENV_VALUE = re.compile(r"[A-Za-z0-9._~-]{1,512}")


def pod_env(api_workers=None, token=None):
    """The `env:` field of a deploy: what the pod gets on top of its template.

    The worker count, and this user's own TTS_API_TOKEN (`token`). A public template
    cannot hold anyone's token, so each engine hands its own to the pods it
    starts, and the pod's server then accepts this user and no one else
    (LAUNCH_PLAN 4.6). Checked live on 2026-10-04, one 4090 from the private
    template, which holds a different TTS_API_TOKEN: the pod answered 401 with
    no token and 200 with the one sent here. RunPod merges this env into the
    template's, and a value sent here wins for the same key.
    """
    pairs = []
    if api_workers:
        pairs.append(("API_WORKERS", str(api_workers)))
    token = (token or "").strip()
    if token:
        if not _POD_ENV_VALUE.fullmatch(token):
            raise ValueError("TTS_API_TOKEN may hold only letters, digits and . _ ~ - "
                             "(make one with: openssl rand -hex 32)")
        pairs.append(("TTS_API_TOKEN", token))
    # The pod's dead man's switch (server/idle_watchdog.py): minutes without a
    # request before it terminates itself. Sent only when set; the image's
    # default is 15.
    idle = (os.getenv("POD_IDLE_MINUTES") or "").strip()
    if idle:
        try:
            pairs.append(("POD_IDLE_MINUTES", str(float(idle))))
        except ValueError:
            raise ValueError(f"POD_IDLE_MINUTES is {idle!r}; it must be a number of minutes (0 = off)")
    if not pairs:
        return ""
    return "env: [" + ", ".join(f'{{ key: "{k}", value: "{v}" }}' for k, v in pairs) + "],"
