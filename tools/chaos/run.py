#!/usr/bin/env python3
"""Layer 3 (docs/TESTING.md): the GPU route when things go wrong, with the real engine.

The engine runs unchanged, as a subprocess in a scratch copy of the code with
its own home (pod registry, chunk cache), against tools/chaos/fakes.py: a fake
RunPod and fake narration pods on 127.0.0.1 that fail on cue. The pods speak
with espeak-ng and the engine aligns with the real MFA, so every scenario ends
in a real manifest. What each one proves, as PASS/FAIL lines:

  healthy          two pods narrate the book; every pod is terminated after
  never-boots      a pod that never serves is terminated and replaced
  dies-mid-book    a pod lost mid-book: its chunks finish on the other one
  only-pod-dies    the only pod lost mid-book: terminated, replaced, the book made
  flaky-server     503 on every other chunk: retried, nothing quarantined
  runpod-errors    RunPod's errors inside HTTP 200 are waited out, not fatal
  over-ceiling     a pod billed over its ceiling is returned within seconds
  out-of-funds     a spent account stops the run at once; nothing is rented
  open-door        a pod that ignores the token is refused before any chunk
  kill-9           the engine killed mid-book: the watcher's sweep ends every
                   pod, and the next run resumes from the chunks already paid
  nothing-affordable  no card under the budget: nothing is rented, and it says so

    python tools/chaos/run.py                 # all, ~15 minutes; needs Docker, espeak-ng
    python tools/chaos/run.py kill-9 healthy  # some

No key, no network service: RunPod and the pods are fakes on this machine.
"""
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "tools", "integration"))
from fakes import FakeRunPod, speech  # noqa: E402
from free_route import make_epub  # noqa: E402

logging.getLogger("werkzeug").setLevel(logging.ERROR)
TITLE = "The Long River"
# MFA's English models (~90 MB), downloaded once for every scenario rather than
# once each: a fresh copy of the code has an empty mfa_workspace.
MFA_CACHE = os.environ.get("VOXIE_CHAOS_MFA_CACHE") or os.path.expanduser("~/.cache/voxie-chaos-mfa")
results = []

# Eight short chapters: enough chunks for two pods to share, small enough to
# narrate with espeak and align with MFA in about a minute.
CHAPTERS = [(f"Chapter {n}", [
    f"In the {word} week the river rose again, and the village on its bank watched the water climb "
    f"the stone steps one by one, as it had done every spring that anyone could remember.",
    f"Marta counted the steps each morning and wrote the number in a small book with a {word} cover, "
    f"because her grandmother had done the same, and the book was older than the bridge.",
]) for n, word in enumerate(["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth"], 1)]
CLIP_TEXT = "Every spring the river rose and the village watched the water climb the old stone steps."


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)
    return ok


class Run:
    """A scratch copy of the code with its own home, and the engine run in it."""

    def __init__(self, work, fake, **env):
        self.dir, self.home = os.path.join(work, "code"), os.path.join(work, "home")
        if not os.path.isdir(self.dir):
            shutil.copytree(ROOT, self.dir, ignore=shutil.ignore_patterns(
                ".git", "library", "mfa_workspace", "voices", ".env", "training_lab", "reports",
                "__pycache__", "*.wav", "*.mp3", "incoming", "archive", "config", "metadata",
                "node_modules", ".venv"))
            os.makedirs(self.home, exist_ok=True)
            open(os.path.join(self.dir, ".env"), "w").close()
            with open(os.path.join(work, "clip.wav"), "wb") as f:
                f.write(speech(CLIP_TEXT))
            self.epub = os.path.join(work, "the-long-river.epub")
            make_epub(self.epub, TITLE, CHAPTERS)
            # mac_ghost reads this before it will sweep (configure()).
            json.dump({"GATEWAY_IP": "127.0.0.1", "UPLOAD_TOKEN": "unused"},
                      open(os.path.join(self.home, ".librasync.json"), "w"))
        self.epub = os.path.join(work, "the-long-river.epub")
        hf = os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface")
        self.env = {**os.environ, "HOME": self.home, "HF_HOME": hf, "PYTHONUNBUFFERED": "1",
                    **fake.env(),
                    "TTS_PROVIDER": "cosyvoice", "RUNPOD_TRANSPORT": "tcp",
                    "RUNPOD_TEMPLATE_ID": "faketemplate",
                    "RUNPOD_URL_PATTERN": "https://{pod_id}-8000.proxy.runpod.net",
                    "TTS_API_TOKEN": uuid.uuid4().hex,
                    "COSY_REF_WAV": os.path.join(work, "clip.wav"), "COSY_REF_TEXT": CLIP_TEXT,
                    "POD_COUNT": "2", "POD_API_WORKERS": "1", "COSY_MAX_WORKERS": "3",
                    "NARRATION_DIRECTOR": "off", "JUNK_PROVIDER": "ai_brain", "JUNK_SHADOW": "none",
                    "JUNK_BLOCK_FILTER": "off",
                    # Minutes become seconds; the logic is the same.
                    "BOOT_TIMEOUT_SECONDS": "20", "BOOT_POLL_SECONDS": "1",
                    "PORT_MAPPING_TIMEOUT_SECONDS": "20",
                    "CAPACITY_WAIT_SECONDS": "30", "CAPACITY_POLL_SECONDS": "1",
                    "POD_LOST_SECONDS": "5",
                    **env}
        for k in ("OPENROUTER_API_KEY", "GROQ_API_KEY", "JEV_API_KEY"):
            self.env.pop(k, None)

    def _models(self, into_workspace):
        sys.path.insert(0, ROOT)
        from core.languages import mfa_downloads
        ws = os.path.join(self.dir, "mfa_workspace")
        os.makedirs(ws if into_workspace else MFA_CACHE, exist_ok=True)
        for name in mfa_downloads():
            src, dst = (os.path.join(MFA_CACHE, name), os.path.join(ws, name)) if into_workspace \
                else (os.path.join(ws, name), os.path.join(MFA_CACHE, name))
            if os.path.exists(src) and os.path.getsize(src) > 0 and not os.path.exists(dst):
                shutil.copyfile(src, dst)

    def engine(self, timeout=900, until=None):
        """Run the engine on the book. `until(proc)` may end it early (kill -9)."""
        self._models(into_workspace=True)
        log_path = os.path.join(self.dir, f"engine-{int(time.time() * 1000)}.log")
        with open(log_path, "w") as log:
            proc = subprocess.Popen([sys.executable, "-u", "librerian_engine_v3.py", self.epub],
                                    cwd=self.dir, env=self.env, stdout=log, stderr=subprocess.STDOUT)
            started = time.time()
            while proc.poll() is None and time.time() - started < timeout:
                if until and until(proc):
                    break
                time.sleep(0.5)
            if proc.poll() is None and not until:
                proc.kill()
        proc.wait()
        self._models(into_workspace=False)
        return proc.returncode, open(log_path).read()

    def manifest(self):
        path = os.path.join(self.dir, "library", "audiobooks", TITLE, "manifest.json")
        return json.load(open(path)) if os.path.exists(path) else None

    def quarantined(self):
        ws = os.path.join(self.dir, "mfa_workspace")
        return [f for f in os.listdir(ws) if f.endswith(".quarantined")] if os.path.isdir(ws) else []


def book_complete(run, label):
    m = run.manifest()
    durations = [c.get("duration") or 0 for c in (m or {}).get("chapters", [])]
    check(f"{label}: the whole book is narrated, every chunk with its audio",
          m and durations and all(d > 0 for d in durations) and not run.quarantined(),
          f"{len(durations)} chunks, {len(run.quarantined())} quarantined" if m else "no manifest")
    return m


def none_left(fake, label):
    return check(f"{label}: no pod is left billing", not fake.live(), str(fake.live()))


# --- the scenarios ------------------------------------------------------------------
# A four-minute test book's share of the dollar does not pay for a second
# pod's boot, so the planner rightly gives it one. The scenarios about two pods
# raise the budget; the planner is tested on its own (test_cost_plan).
TWO_PODS = {"POD_COUNT": "2", "VOXIE_BUDGET_16H": "100"}


def healthy(work):
    fake = FakeRunPod()
    try:
        code, log = Run(work, fake, **TWO_PODS).engine()
        check("healthy: the engine finishes", code == 0, log[-300:] if code else "")
        book_complete(Run(work, fake, **TWO_PODS), "healthy")
        check("healthy: two pods, as POD_COUNT asks", len(fake.pods) == 2, str(fake.log))
        check("healthy: both did the narrating", len(fake.pods) == 2
              and all(p["server"].answered > 0 for p in fake.pods.values()),
              str([p["server"].answered for p in fake.pods.values()]))
        none_left(fake, "healthy")
    finally:
        fake.stop()


def never_boots(work):
    fake = FakeRunPod(pods=[{"never_healthy": True}, {}])
    try:
        code, log = Run(work, fake, POD_COUNT="1").engine()
        check("never-boots: the engine finishes", code == 0, log[-300:] if code else "")
        book_complete(Run(work, fake, POD_COUNT="1"), "never-boots")
        stuck = [p for p in fake.pods.values() if p["server"].never_healthy]
        check("never-boots: the pod that never served was terminated within its boot budget",
              stuck and stuck[0]["terminated"] and stuck[0]["terminated"] - stuck[0]["created"] < 20 + 30,
              f"{stuck[0]['terminated'] - stuck[0]['created']:.0f}s" if stuck and stuck[0]["terminated"] else "")
        check("never-boots: another machine was rented in its place", len(fake.pods) == 2, str(fake.log))
        none_left(fake, "never-boots")
    finally:
        fake.stop()


def dies_mid_book(work):
    fake = FakeRunPod(pods=[{"dies_after": 1}, {}])
    try:
        code, log = Run(work, fake, **TWO_PODS).engine()
        check("dies-mid-book: the engine finishes", code == 0, log[-300:] if code else "")
        book_complete(Run(work, fake, **TWO_PODS), "dies-mid-book")
        dead = [p for p in fake.pods.values() if p["server"].dies_after is not None][0]
        check("dies-mid-book: the lost pod is terminated", dead["terminated"] is not None)
        none_left(fake, "dies-mid-book")
    finally:
        fake.stop()


def only_pod_dies(work):
    """The planner often rents one pod; that pod dying must not cost the book."""
    fake = FakeRunPod(pods=[{"dies_after": 2}, {}])
    try:
        code, log = Run(work, fake, POD_COUNT="1").engine()
        check("only-pod-dies: the engine finishes", code == 0, log[-300:] if code else "")
        book_complete(Run(work, fake, POD_COUNT="1"), "only-pod-dies")
        dead = [p for p in fake.pods.values() if p["server"].dies_after is not None][0]
        check("only-pod-dies: the lost pod is terminated within a minute of dying",
              dead["terminated"] and dead["server"].died_at and dead["terminated"] - dead["server"].died_at < 60,
              f"{(dead['terminated'] or time.time()) - (dead['server'].died_at or time.time()):.0f}s")
        check("only-pod-dies: one replacement is rented, and narrates the rest",
              len(fake.pods) == 2 and sum(p["server"].answered for p in fake.pods.values()
                                          if p is not dead) > 0, str(fake.log))
        check("only-pod-dies: no chunk was given up on", "POISON PILL" not in log and "Quarantin" not in log)
        none_left(fake, "only-pod-dies")
    finally:
        fake.stop()


def flaky_server(work):
    fake = FakeRunPod(pods=[{"fail_every": 2}])
    try:
        code, log = Run(work, fake).engine()
        check("flaky-server: the engine finishes", code == 0, log[-300:] if code else "")
        book_complete(Run(work, fake), "flaky-server")
        none_left(fake, "flaky-server")
    finally:
        fake.stop()


def runpod_errors(work):
    fake = FakeRunPod(deploy_errors=[
        "Something went wrong. Please try again later or contact support.",
        "There are no longer any instances available with the requested specifications. "
        "Please refresh and try again."])
    try:
        code, log = Run(work, fake, POD_COUNT="1").engine()
        check("runpod-errors: errors inside HTTP 200 are waited out, and the book is made",
              code == 0 and fake.deploy_attempts >= 3, f"{fake.deploy_attempts} deploy attempts")
        book_complete(Run(work, fake, POD_COUNT="1"), "runpod-errors")
        none_left(fake, "runpod-errors")
    finally:
        fake.stop()


def over_ceiling(work):
    fake = FakeRunPod(pods=[{"price": 9.99}, {}])
    try:
        code, log = Run(work, fake, POD_COUNT="1").engine()
        check("over-ceiling: the engine finishes", code == 0, log[-300:] if code else "")
        dear = [p for p in fake.pods.values() if p["price"] > 1][0]
        check("over-ceiling: the pod billed at $9.99/h was returned within seconds",
              dear["terminated"] and dear["terminated"] - dear["created"] < 10,
              f"{(dear['terminated'] or time.time()) - dear['created']:.1f}s")
        check("over-ceiling: and it narrated nothing", dear["server"].answered == 0)
        check("over-ceiling: the whole run stayed cheap", fake.billed() < 0.05, f"${fake.billed():.4f}")
        none_left(fake, "over-ceiling")
    finally:
        fake.stop()


def out_of_funds(work):
    fake = FakeRunPod(deploy_errors=["Your account balance is too low to rent a pod."] * 20)
    try:
        started = time.time()
        code, log = Run(work, fake, POD_COUNT="1").engine()
        check("out-of-funds: the run stops, with an error", code != 0)
        check("out-of-funds: at once, not after the capacity wait", time.time() - started < 120,
              f"{time.time() - started:.0f}s")
        check("out-of-funds: nothing was rented", not fake.pods)
        check("out-of-funds: it says to top up", "Top up" in log or "top up" in log, log[-200:])
    finally:
        fake.stop()


def open_door(work):
    fake = FakeRunPod(pods=[{"open_door": True}])
    try:
        code, log = Run(work, fake, POD_COUNT="1").engine()
        check("open-door: a pod that answers anyone is refused", code != 0 and "REFUSING TO NARRATE" in log,
              log[-200:])
        check("open-door: before a single chunk", all(p["server"].chunks == 0 for p in fake.pods.values()))
        none_left(fake, "open-door")
    finally:
        fake.stop()


def kill_9(work):
    # Slow enough that the kill lands with part of the book saved, part not.
    fake = FakeRunPod(pods=[{"delay": 2.0}])
    try:
        run = Run(work, fake, **TWO_PODS)

        cache = os.path.join(run.home, ".librasync_chunk_cache")

        def saved():
            return sum(f.endswith(".json") for _, _, files in os.walk(cache) for f in files)

        def mid_book(proc):
            # Killed once the engine has saved some chunks, as a real run is
            # interrupted mid-book (killing on the pods' answers alone caught
            # them all still in flight, with nothing saved to resume from).
            if saved() >= 3:
                os.kill(proc.pid, signal.SIGKILL)
                return True
            return False

        run.engine(until=mid_book)
        paid = sum(p["server"].answered for p in fake.pods.values())
        check("kill-9: (the engine died mid-book, pods still running)", fake.live(), f"{paid} chunks paid")
        sweep = subprocess.run([sys.executable, "mac_ghost.py", "--sweep-pods"], cwd=run.dir, env=run.env,
                               capture_output=True, text=True, timeout=120)
        check("kill-9: the watcher's sweep terminates every pod the dead run left",
              not fake.live(), sweep.stdout[-200:] + sweep.stderr[-200:])
        # What the dead run saved: a chunk answered but still in flight at the
        # kill was paid for and is lost; one in the cache is not paid again.
        cached = saved()
        before = {i: p["server"].answered for i, p in fake.pods.items()}
        code, log = run.engine()
        again = sum(p["server"].answered for i, p in fake.pods.items() if i not in before)
        m = book_complete(run, "kill-9")
        total = len((m or {}).get("chapters", []))
        check("kill-9: the next run resumes, narrating only what the dead run had not saved",
              code == 0 and cached > 0 and again == total - cached,
              f"{cached} saved of {paid} answered before the kill; {again} of {total} narrated again")
        none_left(fake, "kill-9")
    finally:
        fake.stop()


def nothing_affordable(work):
    fake = FakeRunPod(prices={("NVIDIA RTX A5000", "SECURE"): 5.00})
    try:
        code, log = Run(work, fake, POD_COUNT="1").engine()
        check("nothing-affordable: nothing is rented", not fake.pods, str(fake.log))
        check("nothing-affordable: the run ends with an error", code != 0)
        check("nothing-affordable: and says the budget is why", "No card in stock" in log, log[-200:])
    finally:
        fake.stop()


SCENARIOS = {"healthy": healthy, "never-boots": never_boots, "dies-mid-book": dies_mid_book,
             "only-pod-dies": only_pod_dies,
             "flaky-server": flaky_server, "runpod-errors": runpod_errors, "over-ceiling": over_ceiling,
             "out-of-funds": out_of_funds, "open-door": open_door, "kill-9": kill_9,
             "nothing-affordable": nothing_affordable}


def main(names):
    for tool in ("docker", "espeak-ng", "ffmpeg"):
        if not shutil.which(tool):
            print(f"SKIP  {tool} is not installed")
            return 0
    keep = "--keep" in names
    names = [n for n in names if n != "--keep"] or list(SCENARIOS)
    for name in names:
        print(f"\n== {name}", flush=True)
        work = tempfile.mkdtemp(prefix=f"voxie-chaos-{name}-")
        try:
            SCENARIOS[name](work)
        except Exception as e:      # noqa: BLE001 -- a scenario that crashes is a failure, not the end
            check(f"{name}: ran to the end", False, f"{type(e).__name__}: {e}")
        finally:
            if keep:
                print(f"   kept: {work}")
            else:
                shutil.rmtree(work, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
