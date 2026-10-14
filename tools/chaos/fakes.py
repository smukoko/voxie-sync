"""A fake RunPod and fake narration pods that fail on cue (layer 3, docs/TESTING.md).

The real engine talks to these exactly as it talks to RunPod and to a pod's
CosyVoice server, over HTTP on 127.0.0.1 (pod_pool.graphql_url/rest_url read
RUNPOD_GRAPHQL_URL and RUNPOD_REST_URL). Nothing in the engine is patched.

FakeRunPod answers what the engine asks of RunPod: a deploy (or one of the
errors RunPod really gives, inside an HTTP 200), a termination, the account's
pods, today's prices, and the REST record whose port appears only once the
pod has "booted". It keeps the books: every pod's life and what it would
have billed.

FakePod is one pod's narration server: /health, /voices, /clone, with the
token checked as server/custom_api.py checks it. It speaks the text with
espeak-ng, as 32-bit float WAV like CosyVoice, so the run aligns real
speech. Told to, it never becomes healthy, dies after N chunks, answers 503
every Nth chunk, or ignores the token.
"""
import re
import socket
import subprocess
import threading
import time
import uuid

from flask import Flask, Response, jsonify, request
from werkzeug.serving import make_server


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """A Flask app on 127.0.0.1, in a thread, that can be stopped (a dead pod)."""

    def __init__(self, app):
        self.port = free_port()
        self.httpd = make_server("127.0.0.1", self.port, app, threaded=True)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.stopped = False

    def stop(self):
        if not self.stopped:
            self.stopped = True
            self.httpd.shutdown()
            self.httpd.server_close()


def speech(text):
    """`text` spoken by espeak-ng, as a 24 kHz float32 WAV (CosyVoice's format)."""
    said = subprocess.run(["espeak-ng", "--stdout", text], capture_output=True, check=True).stdout
    return subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-ar", "24000", "-ac", "1",
                           "-c:a", "pcm_f32le", "-f", "wav", "pipe:1"],
                          input=said, capture_output=True, check=True).stdout


class FakePod:
    """One pod's narration server.

    never_healthy  /health answers 503 for ever (a boot that never finishes)
    dies_after     after this many chunks the server goes away mid-book
    fail_every     every Nth chunk answers 503 (a server under strain)
    open_door      no token check at all (a pod someone else configured)
    delay          seconds per chunk (a run killed mid-book needs some in flight)
    """

    def __init__(self, token, never_healthy=False, dies_after=None, fail_every=None, open_door=False,
                 delay=0.0):
        self.token, self.speakers, self.chunks, self.answered = token, set(), 0, 0
        self.delay = delay
        self.died_at = None
        self.never_healthy, self.dies_after, self.fail_every = never_healthy, dies_after, fail_every
        app = Flask(f"pod-{uuid.uuid4().hex[:6]}")

        @app.before_request
        def guard():
            if not open_door and request.path != "/health" and \
                    request.headers.get("Authorization", "") != f"Bearer {self.token}":
                return Response("unauthorized", 401)

        @app.get("/health")
        def health():
            if self.never_healthy:
                return Response("loading", 503)
            return jsonify(status="ok", sample_rate=24000, speakers=sorted(self.speakers),
                           features=["direction"])

        @app.get("/voices")
        def voices():
            return jsonify(speakers=sorted(self.speakers))

        @app.post("/voices")
        def register():
            self.speakers.add(request.form["speaker_id"])
            return jsonify(ok=True)

        @app.post("/clone")
        def clone():
            self.chunks += 1
            if self.dies_after is not None and self.chunks > self.dies_after:
                # Gone mid-book: this request fails and nothing answers after it.
                if self.died_at is None:
                    self.died_at = time.time()
                    threading.Thread(target=self.server.stop, daemon=True).start()
                return Response("pod lost", 502)
            if self.fail_every and self.chunks % self.fail_every == 0:
                return Response("busy", 503)
            if "speaker_id" in request.form and request.form["speaker_id"] not in self.speakers:
                return Response("unknown speaker", 404)
            time.sleep(self.delay)      # a real chunk takes a while; kill-9 needs that
            self.answered += 1
            return Response(speech(request.form["text"]), mimetype="audio/wav")

        self.server = Server(app)

    @property
    def port(self):
        return self.server.port


class FakeRunPod:
    """The parts of RunPod the engine uses, scripted.

    pods         behaviour for each pod deployed, in order (FakePod kwargs plus
                 boot_seconds and price); the last one repeats
    deploy_errors  messages for the first deploy attempts, returned the way
                 RunPod returns them: HTTP 200 with an `errors` list
    prices       {(gpuTypeId, "SECURE"|"COMMUNITY"): $/h} for the price query
    """

    KEY = "fake-runpod-key"

    def __init__(self, pods=None, deploy_errors=None, prices=None):
        self.behaviours = list(pods or [{}])
        self.deploy_errors = list(deploy_errors or [])
        self.prices = prices or {("NVIDIA RTX A5000", "SECURE"): 0.27}
        self.pods, self.deploy_attempts, self.log = {}, 0, []
        app = Flask("fake-runpod")

        @app.post("/graphql")
        def graphql():
            if request.args.get("api_key") != self.KEY:
                return jsonify(errors=[{"message": "Unauthorized"}])
            return jsonify(self._graphql(request.get_json()["query"]))

        @app.get("/v1/pods/<pod_id>")
        def pod_record(pod_id):
            if request.headers.get("Authorization") != f"Bearer {self.KEY}":
                return Response("unauthorized", 401)
            pod = self.pods.get(pod_id)
            if pod is None or pod["terminated"] is not None:
                return Response("not found", 404)
            booted = time.time() >= pod["boot_at"]
            return jsonify(id=pod_id, desiredStatus="RUNNING", costPerHr=pod["price"],
                           publicIp="127.0.0.1" if booted else "",
                           portMappings={"8000": pod["server"].port} if booted else None)

        self.server = Server(app)

    # --- addresses for the engine ------------------------------------------
    def env(self):
        base = f"http://127.0.0.1:{self.server.port}"
        return {"RUNPOD_API_KEY": self.KEY, "RUNPOD_GRAPHQL_URL": f"{base}/graphql",
                "RUNPOD_REST_URL": f"{base}/v1"}

    # --- GraphQL -------------------------------------------------------------
    def _graphql(self, q):
        if "podFindAndDeployOnDemand" in q:
            return self._deploy(q)
        if "podTerminate" in q:
            pod_id = re.search(r'podId:\s*"([^"]+)"', q).group(1)
            self.terminate(pod_id)
            return {"data": {"podTerminate": None}}
        if "myself" in q:
            return {"data": {"myself": {"pods": [{"id": i} for i, p in self.pods.items()
                                                 if p["terminated"] is None]}}}
        if "gpuTypes" in q:
            names = {g for g, _ in self.prices}
            rows = []
            for g in sorted(names):
                sec, com = self.prices.get((g, "SECURE")), self.prices.get((g, "COMMUNITY"))
                rows.append({"id": g, "securePrice": sec or 0, "communityPrice": com or 0,
                             "secure": {"uninterruptablePrice": sec, "stockStatus": "High" if sec else None},
                             "community": {"uninterruptablePrice": com, "stockStatus": "High" if com else None}})
            return {"data": {"gpuTypes": rows}}
        return {"errors": [{"message": f"fake RunPod does not know: {q[:60]}"}]}

    def _deploy(self, q):
        self.deploy_attempts += 1
        if self.deploy_errors:
            return {"errors": [{"message": self.deploy_errors.pop(0)}]}
        gpu = re.search(r'gpuTypeId:\s*"([^"]+)"', q).group(1)
        cloud = re.search(r"cloudType:\s*(\w+)", q).group(1)
        token = re.search(r'key:\s*"TTS_API_TOKEN",\s*value:\s*"([^"]*)"', q)
        b = dict(self.behaviours.pop(0) if len(self.behaviours) > 1 else self.behaviours[0])
        boot, price = b.pop("boot_seconds", 2.0), b.pop("price", self.prices.get((gpu, cloud), 0.27))
        pod_id = f"fake{uuid.uuid4().hex[:10]}"
        # The deploy's env wins over the template's (2026-10-04, measured).
        self.pods[pod_id] = {"gpu": gpu, "cloud": cloud, "price": price, "created": time.time(),
                             "boot_at": time.time() + boot, "terminated": None,
                             "server": FakePod(token.group(1) if token else "", **b)}
        self.log.append(("deploy", pod_id, gpu, cloud, price))
        return {"data": {"podFindAndDeployOnDemand": {"id": pod_id, "costPerHr": price}}}

    def terminate(self, pod_id):
        pod = self.pods.get(pod_id)
        if pod and pod["terminated"] is None:
            pod["terminated"] = time.time()
            pod["server"].server.stop()
            self.log.append(("terminate", pod_id))

    # --- the books -------------------------------------------------------------
    def live(self):
        return [i for i, p in self.pods.items() if p["terminated"] is None]

    def billed(self):
        """Dollars the pods would have cost: every second from deploy to termination."""
        now = time.time()
        return sum(((p["terminated"] or now) - p["created"]) * p["price"] / 3600 for p in self.pods.values())

    def stop(self):
        for pod_id in list(self.pods):
            self.terminate(pod_id)
        self.server.stop()
