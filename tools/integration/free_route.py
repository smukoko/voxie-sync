#!/usr/bin/env python3
"""Layer 2 (docs/TESTING.md): the free route end to end, with the real programs.

Nothing here is faked except the reader: a real Audiobookshelf (its pinned
image), the real gateway, the real watcher and engine narrating with Kokoro on
the CPU and aligning with MFA, exactly as a user with no paid keys runs them.
Everything happens in a scratch folder with its own home: the user's library,
config and Kobo are never touched.

The book is generated (original prose, an EPUB 3 table of contents, three
chapters), so the test carries no copyrighted text and covers nav-only books.

What it proves, each a PASS/FAIL line:
  1. a dropped English book is narrated, uploaded and archived;
  2. Audiobookshelf shows it once, with the manifest's duration, a chapter
     marker where every file starts, and the book's own chapter titles;
  3. the gateway serves the device's map, intact;
  4. sync both ways through the real APIs: a Kobo page moves the audiobook
     to that paragraph, and listening moves the Kobo back, never across a
     chapter start;
  5. dropping the same book again (a regeneration) leaves Audiobookshelf with
     one track per file and the right duration: no stale tracks;
  6. a book dropped without audio first, then with it, stays one book;
  7. a book that is not English goes to the Kobo as an ebook only.

    python tools/integration/free_route.py            # ~15 minutes; needs Docker
    python tools/integration/free_route.py --keep     # leave the scratch folder

Needs Docker (Audiobookshelf, MFA), ffmpeg, espeak-ng and the engine's Python
requirements (Kokoro). No keys, no network services beyond image pulls.
"""
import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
ABS_IMAGE = "ghcr.io/advplyr/audiobookshelf:2.36.0"     # docker-compose.yml's pin
USER, PASSWORD = "reader", "a long test password"

results = []


def userkey():
    """What KOReader sends as x-auth-key for the test account."""
    from core import accounts
    return accounts.userkey_for(PASSWORD)


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)
    return ok


def step(text):
    print(f"\n== {text}", flush=True)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- the book ----------------------------------------------------------------------
CHAPTERS = [
    ("The Harbour", [
        "The morning boat came in late, and the harbour was already full of people waiting for news.",
        "Anna stood at the end of the wooden pier with her coat pulled tight against the cold wind.",
        "She had written to her brother every week for a year, and every week the answer had been the same.",
        "Today the captain waved a small brown parcel above his head, and the whole crowd went quiet.",
    ]),
    ("The Letter", [
        "Inside the parcel there was a letter, a pressed flower and a map drawn in careful blue ink.",
        "Her brother wrote that he had found work on a farm beyond the hills, and that he was well.",
        "He asked her to follow the road along the river until she reached a white house with a red door.",
        "Anna read the letter three times, then folded it and put it in the pocket of her coat.",
    ]),
    ("The Road", [
        "She left the next morning before the sun was up, carrying bread, cheese and a bottle of water.",
        "The road followed the river through fields of tall grass, and the birds sang in the hedges.",
        "By evening she could see the hills, dark against the sky, and a light in a distant window.",
        "When she knocked on the red door, her brother opened it, and for a long time neither of them spoke.",
    ]),
]


def make_epub(path, title, chapters=CHAPTERS, language="en"):
    """An EPUB 3 book with only a nav table of contents, one file per chapter."""
    nav_items = "".join(f'<li><a href="c{i}.xhtml">{t}</a></li>' for i, (t, _) in enumerate(chapters, 1))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:'
                   'container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/'
                   'oebps-package+xml"/></rootfiles></container>')
        items = "".join(f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>'
                        for i in range(1, len(chapters) + 1))
        spine = "".join(f'<itemref idref="c{i}"/>' for i in range(1, len(chapters) + 1))
        z.writestr("OEBPS/content.opf",
                   '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" '
                   'version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   f'<dc:identifier id="id">urn:uuid:{uuid.uuid5(uuid.NAMESPACE_DNS, title)}</dc:identifier>'
                   f'<dc:title>{title}</dc:title><dc:creator>Voxie Test</dc:creator>'
                   f'<dc:language>{language}</dc:language></metadata><manifest>'
                   '<item id="nav" href="nav.xhtml" properties="nav" media-type="application/xhtml+xml"/>'
                   f'{items}</manifest><spine>{spine}</spine></package>')
        z.writestr("OEBPS/nav.xhtml",
                   '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" '
                   'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Contents</title></head><body>'
                   f'<nav epub:type="toc"><ol>{nav_items}</ol></nav></body></html>')
        for i, (heading, paras) in enumerate(chapters, 1):
            body = "".join(f"<p>{p}</p>" for p in paras)
            z.writestr(f"OEBPS/c{i}.xhtml",
                       '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
                       f'<head><title>{heading}</title></head><body><h1>{heading}</h1>{body}</body></html>')


# --- the server: Audiobookshelf and the gateway --------------------------------------
class Server:
    def __init__(self, work):
        self.dir = os.path.join(work, "server")
        # Made here, not by Docker: Docker Desktop creates a missing bind-mount
        # folder inside its VM, where corrupt_duration cannot reach the database.
        for d in ("library", "abs-config", "abs-metadata"):
            os.makedirs(os.path.join(self.dir, d), exist_ok=True)
        self.abs_port, self.gw_port = free_port(), free_port()
        self.abs_url = f"http://127.0.0.1:{self.abs_port}"
        self.gw_url = f"http://127.0.0.1:{self.gw_port}"
        self.container = f"voxie-it-abs-{uuid.uuid4().hex[:8]}"
        self.upload_token = uuid.uuid4().hex
        self.abs_token = None
        self.gateway = None

    def start(self):
        # On Linux the container's root would own the files the test reads and
        # edits (corrupt_duration); Docker Desktop maps ownership by itself.
        user = ["--user", f"{os.getuid()}:{os.getgid()}"] if sys.platform.startswith("linux") else []
        subprocess.run(["docker", "run", "-d", "--name", self.container, *user, "-p", f"{self.abs_port}:80",
                        "-v", f"{os.path.join(self.dir, 'library')}:/library",
                        "-v", f"{os.path.join(self.dir, 'abs-config')}:/config",
                        "-v", f"{os.path.join(self.dir, 'abs-metadata')}:/metadata", ABS_IMAGE],
                       check=True, capture_output=True)
        self.wait_abs()
        import httpx
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        from bootstrap_abs import bootstrap
        self.abs_token = bootstrap(self.abs_url, "root", PASSWORD, library_path="/library")
        env = {**os.environ, "PORT": str(self.gw_port), "UPLOAD_TOKEN": self.upload_token,
               "ABS_URL": self.abs_url, "ABS_API_TOKEN": self.abs_token, "PYTHONUNBUFFERED": "1"}
        self.gateway = subprocess.Popen([sys.executable, os.path.join(ROOT, "librerian_gateway_v3.py")],
                                        cwd=self.dir, env=env, stdout=open(os.path.join(self.dir, "gateway.log"), "w"),
                                        stderr=subprocess.STDOUT)
        for _ in range(60):
            try:
                if httpx.get(f"{self.gw_url}/health", timeout=5).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        subprocess.run([sys.executable, os.path.join(ROOT, "tools", "gateway_admin.py"), "add-user", USER,
                        "--userkey", userkey()],
                       cwd=self.dir, check=True, capture_output=True)

    def wait_abs(self):
        import httpx
        for _ in range(120):
            try:
                if httpx.get(f"{self.abs_url}/ping", timeout=5).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(2)
        raise SystemExit("Audiobookshelf did not start")

    def corrupt_duration(self, item_id, seconds):
        """What Twisted's item looked like: the right files, a wrong stored duration."""
        subprocess.run(["docker", "stop", self.container], check=True, capture_output=True)
        # Audiobookshelf's schema needs a newer SQLite than some Pythons ship
        # ("malformed database schema" on macOS's), so a current one edits it.
        sql = (f"UPDATE books SET duration = {float(seconds)} WHERE id = "
               f"(SELECT mediaId FROM libraryItems WHERE id = '{item_id}');")
        subprocess.run(["docker", "run", "--rm", "-v", f"{os.path.join(self.dir, 'abs-config')}:/config",
                        "alpine:3.20", "sh", "-c",
                        f'apk add -q sqlite >/dev/null && sqlite3 /config/absdatabase.sqlite "{sql}"'],
                       check=True, capture_output=True)
        subprocess.run(["docker", "start", self.container], check=True, capture_output=True)
        self.wait_abs()

    def gateway_call(self, expression):
        """Run one of the gateway's own functions where it runs, against the real ABS."""
        env = {**os.environ, "UPLOAD_TOKEN": self.upload_token, "ABS_URL": self.abs_url,
               "ABS_API_TOKEN": self.abs_token}
        code = f"import sys; sys.path.insert(0, {ROOT!r}); import librerian_gateway_v3 as g; {expression}"
        r = subprocess.run([sys.executable, "-c", code], cwd=self.dir, env=env, capture_output=True, text=True)
        print(r.stdout[-400:] + r.stderr[-400:])

    def stop(self):
        if self.gateway:
            self.gateway.terminate()
        subprocess.run(["docker", "rm", "-f", self.container], capture_output=True)

    def db(self, sql, *args):
        import sqlite3
        conn = sqlite3.connect(os.path.join(self.dir, "library", "database.db"))
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, args)]
        finally:
            conn.close()


# --- the reader's computer: a copy of the code, its own home --------------------------
class Mac:
    def __init__(self, work, server):
        self.dir, self.home = os.path.join(work, "mac"), os.path.join(work, "home")
        shutil.copytree(ROOT, self.dir, ignore=shutil.ignore_patterns(
            ".git", "library", "mfa_workspace", "voices", ".env", "training_lab", "reports", "__pycache__",
            "*.wav", "*.mp3", "incoming", "archive", "config", "metadata", "node_modules", ".venv"))
        for d in ("Kobo_Dropzone", "Kobo_Dropzone_NoAudio", "Kobo_Archive"):
            os.makedirs(os.path.join(self.home, d), exist_ok=True)
        with open(os.path.join(self.dir, ".env"), "w") as f:
            # The free route: nothing paid, exactly what .env.example gives.
            f.write("TTS_PROVIDER=kokoro\nJUNK_PROVIDER=ai_brain\nNARRATION_DIRECTOR=off\n"
                    "KOKORO_DEVICE=cpu\nTTS_API_TOKEN=" + uuid.uuid4().hex + "\n")
        json.dump({"GATEWAY_IP": "127.0.0.1", "GATEWAY_URL": server.gw_url, "ABS_PUBLIC_URL": server.abs_url,
                   "UPLOAD_TOKEN": server.upload_token, "ABS_API_TOKEN": server.abs_token,
                   "ACCOUNT_USERNAME": USER, "ACCOUNT_PASSWORD": PASSWORD},
                  open(os.path.join(self.home, ".librasync.json"), "w"))

    def drop(self, epub, folder="Kobo_Dropzone", timeout=3600, env=None):
        """What the watcher does when a file lands in `folder`. Returns its log."""
        target = os.path.join(self.home, folder, os.path.basename(epub))
        shutil.copyfile(epub, target)
        code = ("import sys, mac_ghost; mac_ghost.configure(); "
                f"mac_ghost.GhostHandler(no_audio={folder == 'Kobo_Dropzone_NoAudio'})._process(sys.argv[1])")
        # The scratch home starts empty; Kokoro's weights (~330 MB) come from the
        # real cache when there is one, rather than downloading on every run.
        hf = os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface")
        env = {**os.environ, "HOME": self.home, "PYTHONUNBUFFERED": "1", "HF_HOME": hf, **(env or {})}
        r = subprocess.run([sys.executable, "-c", code, target], cwd=self.dir, env=env,
                           capture_output=True, text=True, timeout=timeout)
        log = r.stdout + r.stderr
        with open(os.path.join(self.dir, f"drop-{int(time.time())}.log"), "w") as f:
            f.write(log)
        return log

    def manifest(self, title):
        return json.load(open(os.path.join(self.dir, "library", "audiobooks", title, "manifest.json")))


# --- Audiobookshelf, as the phone app sees it ----------------------------------------
def abs_item(server, lib_id, want_duration=None, wait=600, want_starts=None):
    """The ABS item tagged with lib_id, once its duration (and, given, its chapter
    starts) match, or the wait ends. The gateway rewrites the markers after the
    scan, polling every 15 s, so they lag the duration a little."""
    import httpx
    head = {"Authorization": f"Bearer {server.abs_token}"}
    deadline, item = time.time() + wait, None
    while True:
        for lib in httpx.get(f"{server.abs_url}/api/libraries", headers=head, timeout=30).json()["libraries"]:
            httpx.post(f"{server.abs_url}/api/libraries/{lib['id']}/scan", headers=head, timeout=60)
            for it in httpx.get(f"{server.abs_url}/api/libraries/{lib['id']}/items", headers=head,
                                timeout=60).json().get("results", []):
                if it["media"]["metadata"].get("asin") == lib_id:
                    item = httpx.get(f"{server.abs_url}/api/items/{it['id']}", params={"expanded": 1},
                                     headers=head, timeout=60).json()
        marks = [c["start"] for c in item["media"].get("chapters", [])] if item else []
        if item and (want_duration is None or abs(item["media"].get("duration", 0) - want_duration) < 1.0) \
                and (want_starts is None or (len(marks) == len(want_starts)
                                             and all(abs(a - b) <= 0.5 for a, b in zip(marks, want_starts)))):
            return item
        if time.time() >= deadline:
            return item
        time.sleep(10)


def narrated(log):
    """The engine finished and the whole book went up, not only the early ebook
    (which also says "Upload complete", and hid a failed narration once)."""
    return ("Engine finished successfully" in log and "Sync Pack built" in log
            and "to Archive" in log and "crashed" not in log)


def check_audiobook(server, mac, title, label):
    m = mac.manifest(title)
    lib_id, total = m["book"]["lib_id"], sum(c["duration"] or 0 for c in m["chapters"])
    # Where each stitched file starts, from the manifest alone.
    from core import landing
    starts = [round(s, 1) for _, s, _ in landing.audio_chapters(
        [(c["audio_filename"], c["duration"]) for c in sorted(m["chapters"], key=lambda c: c["chapter_index"])])]
    item = abs_item(server, lib_id, total, want_starts=starts)
    check(f"{label}: Audiobookshelf shows the book", item is not None)
    if not item:
        return m, None
    files = item["media"]["audioFiles"]
    inos = [f["ino"] for f in files]
    check(f"{label}: one track per file, no stale ones", len(inos) == len(set(inos)),
          f"{len(inos)} tracks, {len(set(inos))} files")
    check(f"{label}: its duration equals the manifest's", abs(item["media"]["duration"] - total) < 1.0,
          f"{item['media']['duration']:.1f}s vs {total:.1f}s")
    starts, t = [], 0.0
    for f in sorted(files, key=lambda f: f["index"]):
        starts.append(round(t, 1))
        t += f["duration"]
    marks = [round(c["start"], 1) for c in item["media"]["chapters"]]
    check(f"{label}: a chapter marker where every file starts",
          len(marks) == len(starts) and all(abs(a - b) <= 0.5 for a, b in zip(marks, starts)),
          f"{marks} vs {starts}")
    titles = [c["title"] for c in item["media"]["chapters"]]
    check(f"{label}: the chapters carry the book's own titles",
          titles == [t for t, _ in CHAPTERS], str(titles))
    return m, item


# --- sync, both ways, through the real APIs -------------------------------------------
def check_sync(server, mac, title, item):
    import httpx
    import zlib
    from core import landing
    m = mac.manifest(title)
    fp = m["book"]["koreader_md5"]
    auth = {"x-auth-user": USER, "x-auth-key": userkey()}
    r = httpx.get(f"{server.gw_url}/librarian/map/{fp}", headers=auth, timeout=30)
    check("the gateway serves the device's map", r.status_code == 200, f"HTTP {r.status_code}")
    if r.status_code != 200:
        return
    check("the map is intact (its sha256)", hashlib.sha256(r.content).hexdigest() == r.headers.get("X-Voxie-Map-Sha256"))
    book_map = json.loads(zlib.decompress(r.content))
    nodes, chapters = [tuple(n) for n in book_map["nodes"]], [tuple(c) for c in book_map["chapters"]]
    # Kobo -> phone: the reader is on a paragraph of the second chapter, in
    # KOReader's own form (a KEPUB's text sits in koboSpans).
    path, start, _ = next(n for n in nodes if n[1] >= chapters[1][1] + 1.0)
    r = httpx.put(f"{server.gw_url}/syncs/progress", headers=auth, timeout=60,
                  json={"document": fp, "progress": path + "/span[1]/text().0", "percentage": 0.5,
                        "device": "Kobo", "device_id": "it"})
    head = {"Authorization": f"Bearer {server.abs_token}"}
    seen = None
    for _ in range(20):
        p = httpx.get(f"{server.abs_url}/api/me/progress/{item['id']}", headers=head, timeout=30)
        if p.status_code == 200:
            seen = p.json().get("currentTime")
            if seen is not None and abs(seen - start) < 4.0:
                break
        time.sleep(1)
    check("Kobo -> phone: the audiobook moves to the paragraph read", seen is not None and abs(seen - start) < 4.0,
          f"push HTTP {r.status_code}; ABS at {seen}, paragraph starts {start:.1f}s")
    # Phone -> Kobo: listening stops two seconds into the third chapter.
    t = chapters[2][1] + 2.0
    httpx.patch(f"{server.abs_url}/api/me/progress/{item['id']}", headers=head, timeout=30,
                json={"currentTime": t, "duration": chapters[-1][2], "progress": t / chapters[-1][2]})
    g = httpx.get(f"{server.gw_url}/syncs/progress/{fp}", headers=auth, timeout=60).json()
    want = landing.landing_position(nodes, chapters, t)
    landed = landing.time_for_position(nodes, g.get("progress", ""))
    check("phone -> Kobo: the Kobo opens where the listening stopped", g.get("progress", "").startswith(want),
          f"{g.get('progress')} vs {want}")
    check("phone -> Kobo: never across a chapter start (3 s back, not into chapter 2)",
          landed is not None and landed[0] >= chapters[2][1] - landing.CHAPTER_SLACK, str(landed))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="leave the scratch folder for inspection")
    args = ap.parse_args()
    for tool in ("docker", "ffmpeg"):
        if not shutil.which(tool):
            print(f"SKIP  {tool} is not installed")
            return 0
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("SKIP  Docker is not running")
        return 0
    # A kill (CI's timeout, Ctrl-C from another shell) still runs the cleanup
    # below: no gateway or Audiobookshelf container is left running.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    work = tempfile.mkdtemp(prefix="voxie-it-")
    server = Server(work)
    try:
        step("Audiobookshelf and the gateway")
        server.start()
        check("both are up, with an account", server.abs_token and server.db("SELECT * FROM users"))
        mac = Mac(work, server)
        books = os.path.join(work, "books")
        os.makedirs(books)
        title = "The Red Door"
        epub = os.path.join(books, "the-red-door.epub")
        make_epub(epub, title)

        step("1. A book dropped into Kobo_Dropzone")
        log = mac.drop(epub)
        check("narrated, uploaded and archived", narrated(log), "" if narrated(log) else log[-2000:])
        check("on the free route: Kokoro, no paid service", "Kokoro" in log or "kokoro" in log)
        m, item = check_audiobook(server, mac, title, "first run")
        rows = server.db("SELECT audio_state FROM books WHERE lib_id = ?", m["book"]["lib_id"])
        check("the gateway has the book with its audio", rows and rows[0]["audio_state"] == "ready", str(rows))

        step("2. Sync, both ways")
        if item:
            check_sync(server, mac, title, item)

        step("3. The same book again, narrated differently: a regeneration")
        # Faster speech and no chunk cache: every file keeps its name but not its
        # length, which is what left Twisted's item stale (2026-10-07).
        shutil.rmtree(os.path.join(mac.home, ".librasync_chunk_cache"), ignore_errors=True)
        first_total = sum(c["duration"] or 0 for c in m["chapters"])
        log = mac.drop(os.path.join(mac.home, "Kobo_Archive", os.path.basename(epub)), env={"KOKORO_SPEED": "1.25"})
        check("narrated and uploaded again", narrated(log), "" if narrated(log) else log[-2000:])
        m2, item = check_audiobook(server, mac, title, "regenerated")
        second_total = sum(c["duration"] or 0 for c in m2["chapters"])
        check("the new narration really is a different length", abs(second_total - first_total) > 3,
              f"{first_total:.1f}s then {second_total:.1f}s")

        step("3b. An item whose duration went stale is repaired by the gateway")
        if item:
            server.corrupt_duration(item["id"], 99999.0)
            stale = abs_item(server, m2["book"]["lib_id"], wait=0)
            check("(the stored duration really is wrong now)", stale and stale["media"]["duration"] > 99000,
                  str(stale and stale["media"]["duration"]))
            server.gateway_call(f"g._repair_abs_item(g.ABS_URL, {{'Authorization': 'Bearer ' + g.ABS_API_TOKEN}}, "
                                f"{item['id']!r}, {server.db('SELECT book_id FROM books WHERE lib_id = ?', m2['book']['lib_id'])[0]['book_id']})")
            fixed = abs_item(server, m2["book"]["lib_id"], second_total, wait=120)
            check("the gateway's repair gives ABS the manifest's duration again",
                  fixed and abs(fixed["media"]["duration"] - second_total) < 1.0,
                  f"{fixed and fixed['media']['duration']:.1f}s vs {second_total:.1f}s")

        step("4. NoAudio first, audio later: one book")
        title2, epub2 = "The Letter Home", os.path.join(books, "the-letter-home.epub")
        make_epub(epub2, title2, CHAPTERS[1:] + CHAPTERS[:1])
        mac.drop(epub2, "Kobo_Dropzone_NoAudio")
        before = server.db("SELECT book_id, audio_state FROM books WHERE title = ?", title2)
        check("published without audio", before and before[0]["audio_state"] == "none", str(before))
        mac.drop(os.path.join(mac.home, "Kobo_Archive", os.path.basename(epub2)))
        after = server.db("SELECT book_id, audio_state FROM books WHERE title = ?", title2)
        check("then given its audio, as the same book", after and after[0]["audio_state"] == "ready"
              and after[0]["book_id"] == before[0]["book_id"] and len(after) == 1, str(after))

        step("5. A book that is not English")
        title3, epub3 = "La Porte Rouge", os.path.join(books, "la-porte-rouge.epub")
        make_epub(epub3, title3, [("Le Port", ["Le bateau du matin est arrivé en retard, et le port était "
                                               "déjà plein de gens qui attendaient des nouvelles de la mer."])],
                  language="fr")
        log = mac.drop(epub3)
        rows = server.db("SELECT audio_state FROM books WHERE title = ?", title3)
        check("it reaches the Kobo as an ebook, not narrated", rows and rows[0]["audio_state"] == "none"
              and "English only" in log, str(rows))
    finally:
        server.stop()
        if args.keep:
            print(f"\nScratch folder kept: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
