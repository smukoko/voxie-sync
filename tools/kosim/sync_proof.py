#!/usr/bin/env python3
"""The real KOReader, both ways, against the real gateway and the book's real audio.

tools/verify_sync.py proves the map against the book and the audio; this
puts KOReader 2026.07.1 itself in the loop (tools/kosim), with Whisper as the
judge:

  audio to page   the fake Audiobookshelf says the listener is at second t.
                  KOReader opens the book fresh, pulls (KOSync, auto), jumps,
                  and pushes the page it landed on. That page must be the one
                  holding the paragraph core/landing.py gives for t, and what
                  is said at t must be on it.
  page to audio   KOReader opens the book at a paragraph and pushes; the
                  gateway writes an audio time to the fake Audiobookshelf.
                  What is said at that second must be the paragraph at the
                  top of KOReader's page.

Run setup.sh first (with the book's audio folder in its library), then:

    python tools/kosim/sync_proof.py --world /tmp/kosim --repo . \\
        --book library/audiobooks/Twisted --samples 4

The gateway and the fake Audiobookshelf are started here, on 127.0.0.1,
against the world's own copy of the database. Nothing real is touched.
"""
import argparse
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from core import landing  # noqa: E402
import tools.verify_sync as vs  # noqa: E402

KOSIM_USER, KOSIM_KEY = "kosim", "4c5c29ba37e0eacf842c160844d04b9a"   # setup.sh's kosync.lua
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)


def kosync_settings(world, backward):
    """KOSync as setup.sh writes it, pushing after every page, with `backward`
    (1 prompt, 2 silent, 3 off) for a pull that would move the reader back."""
    path = os.path.join(world, "home", ".config", "koreader", "settings", "kosync.lua")
    with open(path, "w") as f:
        f.write('return { ["settings"] = { ["auto_sync"] = true, ["checksum_method"] = 0,\n'
                '  ["custom_server"] = "http://host.docker.internal:3099", ["pages_before_update"] = 1,\n'
                f'  ["sync_backward"] = {backward}, ["sync_forward"] = 2,\n'
                f'  ["userkey"] = "{KOSIM_KEY}", ["username"] = "{KOSIM_USER}" }} }}\n')


def run_koreader(world, title, seconds, shot):
    subprocess.run(["docker", "run", "--rm", "--platform", "linux/arm64",
                    "--add-host=host.docker.internal:host-gateway",
                    "-v", f"{world}/lib/koreader:/ko", "-v", f"{world}/home:/home/tester",
                    "-v", f"{world}/books:/books", "-v", f"{world}/out:/out", "-v", f"{world}/run.sh:/run.sh",
                    "-e", f"KOSIM_BOOK={title}", "koemu", "sh", "/run.sh", str(seconds), shot],
                   check=True, capture_output=True)


class World:
    """The kosim world: its database, its fake ABS state, the book's folder in it."""

    def __init__(self, world, repo, title):
        self.dir, self.repo, self.title = world, repo, title
        self.db = os.path.join(world, "gw", "library", "database.db")
        self.abs_state = os.path.join(world, "abs_state.json")
        self.sdr = os.path.join(world, "books", f"{title}.sdr")
        self.procs = []

    def start(self):
        env = {**os.environ, "PYTHONPATH": self.repo, "ABS_URL": "http://127.0.0.1:13399",
               "ABS_API_TOKEN": "x", "UPLOAD_TOKEN": "t", "PORT": "3099", "PYTHONUNBUFFERED": "1"}
        gw = os.path.join(self.dir, "gw")
        r = subprocess.run([sys.executable, os.path.join(self.repo, "tools", "gateway_admin.py"), "list"],
                           cwd=gw, env=env, capture_output=True, text=True)
        if KOSIM_USER not in r.stdout:
            subprocess.run([sys.executable, os.path.join(self.repo, "tools", "gateway_admin.py"), "add-user",
                            KOSIM_USER, "--userkey", KOSIM_KEY], cwd=gw, env=env, check=True, capture_output=True)
        self.log = open(os.path.join(self.dir, "out", "gateway.log"), "a")
        self.procs.append(subprocess.Popen([sys.executable, "mock_abs.py"], cwd=self.dir,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        self.procs.append(subprocess.Popen([sys.executable, "-u", os.path.join(self.repo, "librerian_gateway_v3.py")],
                                           cwd=gw, env=env, stdout=self.log, stderr=subprocess.STDOUT))
        import httpx
        for _ in range(60):
            try:
                if httpx.get("http://127.0.0.1:3099/health", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1)
        raise RuntimeError("the gateway did not start; see out/gateway.log")

    def stop(self):
        for p in self.procs:
            p.terminate()
        for p in self.procs:
            p.wait(timeout=20)

    def fresh(self, abs_time, abs_updated_ms, last_xpointer=None):
        """The book as never opened on this device, the listener at `abs_time`."""
        shutil.rmtree(self.sdr, ignore_errors=True)
        with sqlite3.connect(self.db) as c:
            c.execute("DELETE FROM progress")
        json.dump({"currentTime": abs_time, "duration": 1e6, "lastUpdate": abs_updated_ms, "patches": []},
                  open(self.abs_state, "w"))
        if last_xpointer:
            os.makedirs(self.sdr)
            with open(os.path.join(self.sdr, "metadata.epub.lua"), "w") as f:
                f.write(f'return {{\n    ["last_xpointer"] = "{last_xpointer}",\n}}\n')

    def pushed(self):
        """The position KOReader pushed last, or None."""
        with sqlite3.connect(self.db) as c:
            row = c.execute("SELECT kobo_location FROM progress WHERE source = 'kobo'").fetchone()
        return row[0] if row else None

    def patches(self):
        return json.load(open(self.abs_state)).get("patches", [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", required=True)
    ap.add_argument("--repo", default=ROOT, help="the checkout whose gateway runs")
    ap.add_argument("--book", required=True, help="the book's folder: manifest, kepub, stitched audio")
    ap.add_argument("--title", help="as setup.sh was given it (default: the folder's name)")
    ap.add_argument("--samples", type=int, default=4)
    ap.add_argument("--seed", type=int, default=2)
    ap.add_argument("--wait", type=int, default=35, help="seconds KOReader runs: pull, jump, the 10 s push delay")
    args = ap.parse_args()
    world, repo = os.path.abspath(args.world), os.path.abspath(args.repo)
    title = args.title or os.path.basename(os.path.normpath(args.book))

    manifest = json.load(open(os.path.join(args.book, "manifest.json")))
    rows = sorted(manifest["chapters"], key=lambda c: c["chapter_index"])
    book_map = landing.build_map([(n["xpath_string"], n["start_time"], n["end_time"]) for n in manifest["sync_nodes"]],
                                 [(c["audio_filename"], c["duration"]) for c in rows])
    nodes, chapters = [tuple(n) for n in book_map["nodes"]], [tuple(c) for c in book_map["chapters"]]
    text_of, order = {}, []
    for p, _, _ in nodes:
        if p not in text_of:
            order.append(p)
    for n in manifest["sync_nodes"]:
        text_of.setdefault(landing.kobo_path(n["xpath_string"]), n.get("text_content") or "")
    index = {p: k for k, p in enumerate(order)}
    rng = random.Random(args.seed)
    work = tempfile.mkdtemp(prefix="kosim_proof_")
    audio = vs.Audio(args.book, work)
    w = World(world, repo, title)
    w.start()
    try:
        # --- audio to page ------------------------------------------------------
        kosync_settings(world, backward=1)
        landed_ok = heard_ok = 0
        times = sorted(rng.uniform(60.0, audio.total - 60.0) for _ in range(args.samples))
        for n, t in enumerate(times):
            w.fresh(t, int(time.time() * 1000))
            run_koreader(world, title, args.wait, f"audio-to-page-{n}")
            want = landing.landing_position(nodes, chapters, t)
            got = w.pushed()
            top = landing.time_for_position(nodes, got) if got else None
            top_path = landing.paragraph_path(got) if got else None
            # KOReader shows the page that holds `want`: its top is `want` or a
            # paragraph just before it, on the same page.
            k_want = index[want]
            k_top = index.get(top_path)
            while k_top is None and top_path and top_path.count("/") > 3:
                top_path = top_path.rsplit("/", 1)[0]
                k_top = index.get(top_path)
            on_page = k_top is not None and 0 <= k_want - k_top <= 6
            landed_ok += on_page
            shown = vs.page_text([text_of[p] for p in order], k_top) if k_top is not None else ""
            clip, _ = audio.clip(t, 6.0)
            said = " ".join(x for x, _ in vs.heard(clip))
            heard_ok += bool(said) and vs.similar(said, shown) >= 0.6
            print(f"INFO  t={t:.0f}s: landing {want} ({k_want}); KOReader pushed {got} ({k_top}); "
                  f"heard {said[:60]!r}")
        check("audio to page: KOReader opens on the page holding the paragraph the landing rules give",
              landed_ok == len(times), f"{landed_ok}/{len(times)}")
        check("audio to page: what is said at that second is on the page KOReader opened",
              heard_ok == len(times), f"{heard_ok}/{len(times)}")

        # --- page to audio ------------------------------------------------------
        kosync_settings(world, backward=3)
        usable = [p for p in order if vs.recognisable(text_of[p])]
        picks = sorted(rng.sample(usable, args.samples), key=lambda p: index[p])
        wrote_ok = said_ok = 0
        for n, p in enumerate(picks):
            w.fresh(0.0, 0, last_xpointer=f"{p}/text().0")
            run_koreader(world, title, args.wait, f"page-to-audio-{n}")
            got, patches = w.pushed(), w.patches()
            at = patches[-1]["currentTime"] if patches else None
            top = landing.time_for_position(nodes, got) if got else None
            wrote_ok += at is not None and top is not None and abs(at - top[0]) < 0.05
            top_path = landing.paragraph_path(got) if got else None
            while top_path and top_path not in text_of and top_path.count("/") > 3:
                top_path = top_path.rsplit("/", 1)[0]
            if at is not None and top_path in text_of:
                clip, start = audio.clip(at - 1.0, 9.0, anchor=at)
                t0 = vs.onset(vs.heard(clip), text_of[top_path])
                said_ok += t0 is not None and abs(start + t0 - at) <= 1.5
            print(f"INFO  opened at {p}; KOReader pushed {got}; ABS set to {at}")
        check("page to audio: the gateway writes the second of the paragraph KOReader reports",
              wrote_ok == len(picks), f"{wrote_ok}/{len(picks)}")
        check("page to audio: at that second the narrator says that paragraph's first words",
              said_ok == len(picks), f"{said_ok}/{len(picks)}")
    finally:
        w.stop()
        shutil.rmtree(work, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} claims hold")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
