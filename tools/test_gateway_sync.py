"""The sync decisions the gateway makes, checked against a mocked Audiobookshelf.

Each case is a behaviour verified in real KOReader 2026.07.1 on 2026-09-26
(Linux build in Docker, driving the gateway's own endpoints):

  - a page that begins inside inline markup must still find its paragraph
  - pushing the paragraph the audio is already inside must not rewind it
  - the context rollback on a pull must not cross the start of a chapter

and since 2026-10-04 (offline translation): one landing rule with or without
word timings (core/landing.py, the plugin's too), the device's map, and a page
read offline that arrives after newer listening.

Offline: runs in a temp directory, never touches library/ or a real server.
"""
import io, json, os, sys, tempfile, time, zipfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ.update(UPLOAD_TOKEN="t", ABS_URL="http://abs.test", ABS_API_TOKEN="x")
sys.path.insert(0, ROOT)
import librerian_gateway_v3 as gw  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


def P(n):
    return f"point(/1/body/DocFragment[3]/body/p[{n}])"


# Two chapters. "One": two 50s chunks; "Two": one 100s chunk.
nodes = [(10, 1, P(1), 0.0, 20.0), (11, 1, P(2), 20.0, 50.0),
         (12, 2, P(10), 50.0, 100.0),
         (13, 3, P(20), 100.0, 130.0), (14, 3, P(21), 130.0, 200.0)]
manifest = {
    "book": {"lib_id": "LBR-000000000001", "epub_path": "library/ebooks/B/B.kepub.epub"},
    "chapters": [{"chapter_id": 1, "chapter_index": 1, "audio_filename": "001_A_One.mp3", "duration": 50.0},
                 {"chapter_id": 2, "chapter_index": 2, "audio_filename": "002_B_One.mp3", "duration": 50.0},
                 {"chapter_id": 3, "chapter_index": 3, "audio_filename": "003_A_Two.mp3", "duration": 100.0}],
    "sync_nodes": [{"node_id": i, "chapter_id": c, "xpath_string": x, "start_time": s, "end_time": e}
                   for i, c, x, s, e in nodes],
    "word_timings": [{"node_id": 12, "word_index": 0, "word": "late", "char_start": 0, "char_end": 4,
                      "start_time": 95.0, "end_time": 96.0},
                     {"node_id": 13, "word_index": 0, "word": "first", "char_start": 0, "char_end": 5,
                      "start_time": 100.0, "end_time": 100.5}],
}
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("library/ebooks/B/B.kepub.epub", b"x")
    z.writestr("library/audiobooks/B/Chapter_01_One.mp3", b"x")
    z.writestr("library/ebooks/B/manifest.json", json.dumps(manifest))
buf.seek(0)
c = gw.app.test_client()
from core import accounts
with gw.get_db() as db:
    accounts.create_user(db, "reader", accounts.userkey_for("pw"))
    accounts.claim_unowned(db, accounts.owner_id(db))
AUTH = {"x-auth-user": "reader", "x-auth-key": accounts.userkey_for("pw")}
c.post("/upload", headers={"X-Libra-Token": "t"}, data={"file": (buf, "p.zip")},
       content_type="multipart/form-data")
with gw.get_db() as db:
    book_id = db.execute("SELECT book_id FROM books").fetchone()[0]
    db.execute("UPDATE books SET abs_id='abs' WHERE book_id=?", (book_id,))
    db.execute("INSERT INTO book_hashes (kobo_hash, book_id) VALUES ('H', ?)", (book_id,))
    db.commit()


def abs_at(t, updated_ms=None):
    r = mock.Mock(status_code=200)
    r.json.return_value = {"currentTime": t, "progress": t / 200.0,
                           "lastUpdate": updated_ms if updated_ms is not None else int(time.time() * 1000) + 60_000}
    r.raise_for_status = lambda: None
    return r


def push(location, abs_now):
    with mock.patch.object(gw.requests, "get", return_value=abs_at(abs_now)), \
         mock.patch.object(gw.requests, "patch") as p:
        gw.calculate_and_push_to_abs(location, 0.05, "abs", book_id, user_id=1)
    return p.call_args.kwargs["json"]["currentTime"] if p.called else None


def pull(abs_now, updated_ms=None, side_effect=None):
    kw = {"side_effect": side_effect} if side_effect else {"return_value": abs_at(abs_now, updated_ms)}
    with mock.patch.object(gw.requests, "get", **kw):
        return c.get("/syncs/progress/H", headers=AUTH).get_json()["progress"]


def kobo_push(location, abs_now):
    with mock.patch.object(gw.requests, "get", return_value=abs_at(abs_now, 0)), \
         mock.patch.object(gw.requests, "patch") as p:
        c.put("/syncs/progress", headers=AUTH, json={"document": "H", "progress": location,
                                                    "percentage": 0.4, "device": "k", "device_id": "k"})
    return p.call_args.kwargs["json"]["currentTime"] if p.called else None


sent = push("/body/DocFragment[3]/body/p[10]/em/span[1]/text().3", abs_now=180.0)
check("inline markup resolves to its paragraph", sent == 50.0, f"(pushed {sent}, p[10] starts at 50.0)")

sent = push("/body/DocFragment[3]/body/p[10]/span[2]/text().9", abs_now=60.0)
check("audio inside the pushed paragraph is left alone", sent is None, f"(pushed {sent})")

sent = push("/body/DocFragment[3]/body/p[21]/span[1]/text().0", abs_now=60.0)
check("reading ahead still moves the audio", sent == 130.0, f"(pushed {sent})")

landed = pull(101.0)
check("1s into a chapter lands on its first paragraph", landed == "/body/DocFragment[3]/body/p[20]",
      f"(landed {landed}; the rollback word is in the previous chapter)")

landed = pull(99.0)
check("mid-chapter rollback still works", landed == "/body/DocFragment[3]/body/p[10]", f"(landed {landed})")

# One rule with or without word timings: 3 s back, by the paragraphs' own
# times. Until 2026-10-04 a book without word timings (Redeeming Love on the
# gateway, 2026-09-26) rolled back a whole paragraph instead; on books with
# dense word timings the paragraph rule lands where the word rule did in 99.4%
# of 9,935 samples (Redeeming Love, Screwtape), and the offline map has no words.
landed = pull(40.0)
check("without word timings, 3 s back is still the paragraph being heard",
      landed == "/body/DocFragment[3]/body/p[2]", f"(landed {landed})")
landed = pull(21.0)
check("...and 1 s into a paragraph, the one before it",
      landed == "/body/DocFragment[3]/body/p[1]", f"(landed {landed})")

# The pull decides between the reader's own place and the audio's.
kobo_push("/body/DocFragment[3]/body/p[21]/span[1]/text().4", abs_now=60.0)   # reader moves ahead: ABS set to 130
landed = pull(130.0, updated_ms=0)
check("audio unchanged since the push: the reader's exact place comes back",
      landed == "/body/DocFragment[3]/body/p[21]/span[1]/text().4", f"(got {landed})")
with mock.patch.object(gw.requests, "get", return_value=abs_at(130.0, 0)):
    audio = c.get("/librarian/audio/H", headers=AUTH).get_json()
check("the plugin's audio endpoint always says where the AUDIO is",
      audio["progress"] == "/body/DocFragment[3]/body/p[20]" and audio["device"] == "Audiobookshelf", f"(got {audio})")
check("...and needs the login too", c.get("/librarian/audio/H").status_code == 401)
landed = pull(150.0, updated_ms=1000)   # ABS moved, but that update is older than the push
check("reading newer than listening: the reader's place wins", landed.endswith("text().4"), f"(got {landed})")
landed = pull(0, side_effect=gw.requests.ConnectionError("down"))
check("ABS unreachable: the stored place, not an error", landed.endswith("text().4"), f"(got {landed})")
landed = pull(160.0)    # the listener moved after the reader did
check("listening newer than reading: the audio position is translated",
      landed == "/body/DocFragment[3]/body/p[21]", f"(got {landed}; 3 s of context)")

check("a chapter is a run of same-title chunks, as the listener sees it",
      gw.book_map(book_id)[1] == [("One", 0.0, 100.0), ("Two", 100.0, 200.0)], gw.book_map(book_id)[1])

# --- The device's map (plugin 1.6 keeps it, to translate with no network) ----
import hashlib, zlib  # noqa: E402
r = c.get("/librarian/map/H")
check("the map needs the login", r.status_code == 401)
r = c.get("/librarian/map/H", headers=AUTH)
digest = r.headers.get("X-Voxie-Map-Sha256")
raw = zlib.decompress(r.data) if r.status_code == 200 else b"{}"
book_map = json.loads(raw)
check("the book's map, compressed, with the checksum of what is sent",
      r.status_code == 200 and digest == hashlib.sha256(r.data).hexdigest()
      and int(r.headers["X-Voxie-Map-Length"]) == len(raw), f"({r.status_code})")
check("positions and chapters only, in the device's form",
      book_map.get("nodes", [None])[0] == ["/body/DocFragment[3]/body/p[1]", 0.0, 20.0]
      and book_map.get("chapters") == [["One", 0.0, 100.0], ["Two", 100.0, 200.0]]
      and set(book_map) == {"v", "nodes", "chapters"})
r = c.get(f"/librarian/map/H?have={digest}", headers=AUTH)
check("a device that already has this map gets 304 and no body", r.status_code == 304 and not r.data)
with gw.get_db() as db:
    accounts.create_user(db, "other", accounts.userkey_for("pw2"))
OTHER = {"x-auth-user": "other", "x-auth-key": accounts.userkey_for("pw2")}
check("another account never gets this book's map",
      c.get("/librarian/map/H", headers=OTHER).status_code == 404)


# --- A page read offline, pushed when Wi-Fi returns ---------------------------
def offline_push(location, read_at, abs_now, abs_updated_ms):
    with mock.patch.object(gw.requests, "get", return_value=abs_at(abs_now, abs_updated_ms)), \
         mock.patch.object(gw.requests, "patch") as p:
        c.put("/syncs/progress", headers=AUTH, json={"document": "H", "progress": location, "percentage": 0.3,
                                                    "device": "Voxie", "device_id": "k", "read_at": read_at})
    return p.call_args.kwargs["json"]["currentTime"] if p.called else None


now = time.time()
sent = offline_push("/body/DocFragment[3]/body/p[2]/span[1]/text().0", read_at=now - 3600,
                    abs_now=150.0, abs_updated_ms=int((now - 600) * 1000))
check("a page read before the newer listening does not move the audio", sent is None, f"(pushed {sent})")
landed = pull(150.0, updated_ms=int((now - 600) * 1000))
check("...and the next pull brings the reader to the audio",
      landed == "/body/DocFragment[3]/body/p[21]", f"(got {landed})")
sent = offline_push("/body/DocFragment[3]/body/p[2]/span[1]/text().0", read_at=now - 60,
                    abs_now=150.0, abs_updated_ms=int((now - 600) * 1000))
check("a page read after the listening moves the audio", sent == 20.0, f"(pushed {sent})")
landed = pull(20.0, updated_ms=int((now - 600) * 1000))
check("...and the reader keeps that page", landed.endswith("p[2]/span[1]/text().0"), f"(got {landed})")
with gw.get_db() as db:
    stored = db.execute("SELECT CAST(strftime('%s', updated_at) AS INTEGER) FROM progress").fetchone()[0]
check("the reading is recorded at the time it was read, not when it arrived", abs(stored - (now - 60)) < 2,
      f"({int(now - stored)}s ago)")
check("a read time in the future counts as now", abs(gw.reading_time(now + 9999) - time.time()) < 2)
check("an unreadable or ancient read time is ignored",
      gw.reading_time("soon") is None and gw.reading_time(now - 40 * 86400) is None and gw.reading_time(None) is None)

# --- the book's ABS item was rebuilt: same book, new id (Twisted, 2026-10-11) -------
def gone_then(url, **kw):
    if url.endswith("/api/items/abs"):
        return mock.Mock(status_code=404)
    return abs_at(0.0, 0)


with mock.patch.object(gw.requests, "get", side_effect=gone_then), \
        mock.patch.object(gw, "resolve_abs_id", return_value="abs2") as found, \
        mock.patch.object(gw.requests, "patch") as p:
    c.put("/syncs/progress", headers=AUTH, json={"document": "H", "progress": "/body/DocFragment[3]/body/p[21]",
                                                "percentage": 0.7, "device": "k", "device_id": "k"})
with gw.get_db() as db:
    cached = db.execute("SELECT abs_id FROM books WHERE book_id=?", (book_id,)).fetchone()[0]
check("a push after the ABS item was rebuilt reaches the new item",
      p.called and p.call_args.args[0].endswith("/api/me/progress/abs2"),
      f"({p.call_args.args[0] if p.called else 'no PATCH'})")
check("and the new id is kept, found by the book's watermark", cached == "abs2" and found.called, f"({cached})")


def abs_down(url, **kw):
    raise gw.requests.ConnectionError("ABS is restarting")


with mock.patch.object(gw.requests, "get", side_effect=abs_down), \
        mock.patch.object(gw, "resolve_abs_id") as found:
    gw.book_for_device("H", 1)
with gw.get_db() as db:
    cached = db.execute("SELECT abs_id FROM books WHERE book_id=?", (book_id,)).fetchone()[0]
check("an ABS that does not answer is not a gone item: the id is kept, nothing is searched",
      cached == "abs2" and not found.called, f"({cached})")

# --- Read -> listen from the page's first sentence (core/page_audio.py, 2026-10-14) -----
# p[21] (130-200 s) holds two sentences; a page that opens on the second must
# start the audio two words before it, not at the paragraph's start.
with gw.get_db() as db:
    epub = db.execute("SELECT epub_path FROM books WHERE book_id=?", (book_id,)).fetchone()[0]
    p21 = db.execute("SELECT node_id FROM sync_nodes WHERE xpath_string=?", (P(21),)).fetchone()[0]
    text = "One two three four. Five six seven eight."
    at = 0
    for n, w in enumerate(text.split()):
        at = text.index(w, at)
        db.execute("INSERT INTO word_timings (node_id, word_index, word, char_start, char_end, start_time, end_time)"
                   " VALUES (?, ?, NULL, ?, ?, ?, ?)", (p21, n, at, at + len(w), 130.0 + 5 * n, 134.0 + 5 * n))
        at += len(w)
    db.commit()
paras = "".join(f"<p>p{i}</p>" for i in range(1, 21))
os.makedirs(os.path.dirname(epub), exist_ok=True)
with zipfile.ZipFile(epub, "w") as z:
    z.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
               '<rootfiles><rootfile full-path="content.opf"/></rootfiles></container>')
    z.writestr("content.opf", '<package xmlns="http://www.idpf.org/2007/opf"><manifest>'
               '<item id="a" href="a.xhtml"/><item id="b" href="b.xhtml"/><item id="c" href="c.xhtml"/></manifest>'
               '<spine><itemref idref="a"/><itemref idref="b"/><itemref idref="c"/></spine></package>')
    for name in ("a.xhtml", "b.xhtml"):
        z.writestr(name, "<html><body><p>x</p></body></html>")
    z.writestr("c.xhtml", f"<html><body>{paras}<p><span>One two three four. </span><span>Five six seven eight.</span></p></body></html>")
sent = push("/body/DocFragment[3]/body/p[21]/span[2]/text().0", abs_now=60.0)
check("a page opening on a paragraph's second sentence starts the audio there, two words early",
      sent == 140.0, f"(pushed {sent}; 'Five' is at 150 s, the paragraph at 130 s)")
sent = push("/body/DocFragment[3]/body/p[21]/span[1]/text().0", abs_now=60.0)
check("a page opening on a paragraph's first word keeps the paragraph's start", sent == 130.0, f"(pushed {sent})")
sent = push("/body/DocFragment[3]/body/p[21]/span[2]/text().0", abs_now=135.0)
check("audio already inside that paragraph is still left alone", sent is None, f"(pushed {sent})")

print("=" * 60)
print(f"{passed + failed} checks: {passed} passed, {failed} FAILED")
sys.exit(1 if failed else 0)
