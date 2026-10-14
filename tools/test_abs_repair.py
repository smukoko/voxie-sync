"""After a pack lands, the gateway leaves ABS with the book's real duration.

Twisted (2026-10-07) kept stale tracks from the run it replaced, and after
they were dropped its duration stayed 73,407 s over 48,610 s of files: ABS's
track update never recomputes the duration, and a rescan recomputes it only
when an audio file changed. The fake below behaves like ABS 2.36.0 did when
measured (tools/integration/free_route.py checks the real one).

Offline: runs in a temp directory against a fake ABS.
"""
import contextlib, io, os, sys, tempfile, time
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ.update(UPLOAD_TOKEN="t", ABS_URL="http://abs.test", ABS_API_TOKEN="x")
sys.path.insert(0, ROOT)
import librerian_gateway_v3 as gw  # noqa: E402
from core import library  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


FOLDER = os.path.join(library.BOOKS, "Twisted")
os.makedirs(FOLDER)
for n in ("Chapter_01_One.mp3", "Chapter_02_Two.mp3"):
    open(os.path.join(FOLDER, n), "wb").write(b"mp3")
    os.utime(os.path.join(FOLDER, n), (1, 1))
with gw.get_db() as db:
    db.execute("INSERT INTO books (book_id, lib_id, title, epub_path, user_id) VALUES (1, 'LBR-1', 'Twisted', ?, 0)",
               (os.path.join(FOLDER, "Twisted.kepub.epub"),))
    db.commit()


class FakeABS:
    """ABS as measured: PATCH /tracks keeps the stored duration; an item scan,
    only if a file changed on disk, rebuilds the tracks from the item's own
    file list (libraryFiles), duplicates included (Twisted, 2026-10-10:
    78 tracks became 112), and recomputes the duration from them."""

    # It stands in for the whole requests module, so it carries its exception.
    RequestException = __import__("requests").RequestException

    def __init__(self, files, duration, library_files=None):
        self.files, self.duration, self.calls, self.scanned_at = files, duration, [], 2
        seen = set()
        self.library = library_files if library_files is not None else \
            [f for f in files if not (f["ino"] in seen or seen.add(f["ino"]))]

    def _resp(self, body=None):
        return mock.Mock(status_code=200, json=lambda: body or {})

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        if url.endswith("/api/libraries"):
            return self._resp({"libraries": [{"id": "lib"}]})
        return self._resp({"media": {"audioFiles": self.files, "duration": self.duration,
                                     "metadata": {"title": "Twisted"}},
                           "libraryFiles": [{"ino": f["ino"], "fileType": "audio"} for f in self.library]})

    def post(self, url, json=None, **kw):
        self.calls.append(("POST", url, json))
        if url.endswith("/api/items/li/scan"):
            changed = any(os.path.getmtime(os.path.join(FOLDER, n)) > self.scanned_at for n in os.listdir(FOLDER))
            if changed:
                self.files = [dict(f) for f in self.library]
                self.duration = sum(f["duration"] for f in self.files)
                self.scanned_at = time.time() + 1
        return self._resp()

    def patch(self, url, json=None, **kw):
        self.calls.append(("PATCH", url, json))
        keep = [o["ino"] for o in json["orderedFileData"]]
        self.files = [next(f for f in self.files if f["ino"] == i) for i in keep]
        return self._resp()

    def delete(self, url, **kw):
        # It will not forget an item, so a doubled one gets the patch-only
        # repair. RebuildingABS below is the one that forgets.
        self.calls.append(("DELETE", url))
        return mock.Mock(status_code=500)


def af(ino, index, name, duration):
    return {"ino": ino, "index": index, "duration": duration, "metadata": {"filename": name}}


def run(fake, expected):
    with mock.patch.object(gw, "requests", fake), mock.patch.object(gw, "resolve_abs_id", return_value="li"), \
            mock.patch.object(gw, "ABS_POLL_SECONDS", 0):
        gw.sync_abs_chapters(0, 1, "LBR-1", expected, timeout=2)
    return [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/chapters")]


# --- Twisted: tracks right, duration stale -----------------------------------
fake = FakeABS([af("1", 1, "Chapter_01_One.mp3", 100.0), af("2", 2, "Chapter_02_Two.mp3", 50.0)], 999.9)
written = run(fake, 150.0)
check("a stale duration is recomputed by ABS", abs(fake.duration - 150.0) < 0.01, f"({fake.duration})")
check("the book's files were touched so the rescan sees them",
      all(os.path.getmtime(os.path.join(FOLDER, n)) > 2 for n in os.listdir(FOLDER)))
check("chapters are written after the repair, one per file",
      written and [c["start"] for c in written[-1][2]["chapters"]] == [0, 100.0], f"({written[-1:]})")

# --- Stale duplicates: one file listed twice ---------------------------------
for n in os.listdir(FOLDER):
    os.utime(os.path.join(FOLDER, n), (1, 1))
fake = FakeABS([af("1", 1, "Chapter_01_One.mp3", 100.0), af("2", 2, "Chapter_02_Two.mp3", 50.0),
                af("2", 3, "Chapter_02_Two.mp3", 50.0)], 200.0)
written = run(fake, 150.0)
patches = [c for c in fake.calls if c[0] == "PATCH"]
check("a file listed twice is listed once", patches and [o["ino"] for o in patches[0][2]["orderedFileData"]] == ["1", "2"],
      f"({patches})")
check("and the duration is recomputed", abs(fake.duration - 150.0) < 0.01, f"({fake.duration})")
check("chapters follow the files, not the duplicate",
      written and len(written[-1][2]["chapters"]) == 2, f"({written[-1:]})")

# --- ABS's own file list holds a file twice and ABS will not forget it --------
# A rescan rebuilds the tracks from that list: it would bring the duplicate
# back. The tracks are made one per file, and nothing is rescanned.
for n in os.listdir(FOLDER):
    os.utime(os.path.join(FOLDER, n), (1, 1))
listed = [af("1", 1, "Chapter_01_One.mp3", 100.0), af("2", 2, "Chapter_02_Two.mp3", 50.0),
          af("2", 3, "Chapter_02_Two.mp3", 50.0)]
fake = FakeABS([dict(f) for f in listed], 200.0, library_files=[dict(f) for f in listed])
out = io.StringIO()
with contextlib.redirect_stdout(out):
    written = run(fake, 150.0)
check("a file ABS lists twice in its own file list: the tracks end one per file",
      [f["ino"] for f in fake.files] == ["1", "2"], f"({[f['ino'] for f in fake.files]})")
check("and it is not rescanned, which would bring the duplicate back",
      not any(c[1].endswith("/items/li/scan") for c in fake.calls)
      and all(os.path.getmtime(os.path.join(FOLDER, n)) < 2 for n in os.listdir(FOLDER)))
check("it says why", "not rescanned" in out.getvalue(), out.getvalue().strip()[-120:])
check("its chapters still follow the files", written and len(written[-1][2]["chapters"]) == 2, f"({written[-1:]})")

fake = FakeABS([dict(f) for f in listed[:2]], 999.9, library_files=[dict(f) for f in listed])
said = []
with mock.patch.object(gw, "requests", fake), mock.patch.object(gw, "resolve_abs_id", return_value="li"):
    with gw.get_db() as db:
        db.execute("UPDATE books SET audio_state = 'ready' WHERE book_id = 1")
        db.commit()
    fixed = gw.repair_abs_library(log=said.append)
check("at start-up such an item, its tracks already one per file, is left alone and reported",
      fixed == [] and not any(c[0] in ("PATCH", "POST") for c in fake.calls)
      and len(fake.files) == 2 and any("twice" in line for line in said), f"({fixed}, {said})")


# --- A doubled item is forgotten and comes back, the uploader's place kept ----
class RebuildingABS:
    """ABS as the Twisted cure went by hand (2026-10-11) and as ABS 2.36.0
    answered in Docker (2026-10-12): forgetting an item keeps its files on
    disk, drops its progress and keeps its bookmarks, which can no longer be
    removed (404); the next library scan finds the folder again and makes a new
    item, under a new id, listing each file once. Items are found by watermark,
    through the gateway's own resolve_abs_id."""

    RequestException = __import__("requests").RequestException

    def __init__(self, delete_status=200, progress_status=200, comes_back=True, comes_back_doubled=False,
                 progress=None):
        self.doubled = [af("1", 1, "Chapter_01_One.mp3", 100.0), af("2", 2, "Chapter_02_Two.mp3", 50.0),
                        af("2", 3, "Chapter_02_Two.mp3", 50.0)]
        self.items = {"old": {"files": [dict(f) for f in self.doubled], "library": [dict(f) for f in self.doubled],
                              "duration": 200.0}}
        self.progress = {"old": progress} if progress else {}
        self.bookmarks = [{"libraryItemId": "old", "time": 60, "title": "Fagin"},
                          {"libraryItemId": "elsewhere", "time": 5, "title": "Another book"}]
        self.delete_status, self.progress_status = delete_status, progress_status
        self.comes_back, self.comes_back_doubled = comes_back, comes_back_doubled
        self.calls, self.due, self.made = [], None, 0

    @staticmethod
    def _resp(body=None, status=200):
        return mock.Mock(status_code=status, json=lambda: body if body is not None else {})

    def get(self, url, params=None, **kw):
        self.calls.append(("GET", url, params))
        path = url.replace("http://abs.test", "")
        if path == "/api/libraries":
            return self._resp({"libraries": [{"id": "lib"}]})
        if path == "/api/libraries/lib/items":
            if self.due is not None:          # the scan's new item shows on the listing after the next
                self.due -= 1
                if self.due < 0:
                    self.due, self.made = None, self.made + 1
                    files = self.doubled if self.comes_back_doubled else self.doubled[:2]
                    self.items[f"new{self.made}"] = {"files": [dict(f) for f in files],
                                                     "library": [dict(f) for f in files], "duration": 150.0}
            return self._resp({"results": [{"id": i, "media": {"metadata": {"asin": "LBR-1"}}} for i in self.items]})
        if path == "/api/me":
            return self._resp({"bookmarks": list(self.bookmarks)})
        if path.startswith("/api/me/progress/"):
            if self.progress_status != 200:
                return self._resp(status=self.progress_status)
            p = self.progress.get(path.rsplit("/", 1)[1])
            return self._resp(p, 200) if p else self._resp(status=404)
        item = self.items.get(path.rsplit("/", 1)[1])
        if item is None:
            return self._resp(status=404)
        return self._resp({"media": {"audioFiles": item["files"], "duration": item["duration"],
                                     "metadata": {"title": "Twisted"}},
                           "libraryFiles": [{"ino": f["ino"], "fileType": "audio"} for f in item["library"]]})

    def delete(self, url, params=None, **kw):
        self.calls.append(("DELETE", url, params))
        path = url.replace("http://abs.test", "")
        if path.startswith("/api/me/item/"):
            abs_id, at = path.split("/")[4], float(path.rsplit("/", 1)[1])
            mark = next((b for b in self.bookmarks if b["libraryItemId"] == abs_id and b["time"] == at), None)
            if abs_id not in self.items or mark is None:
                return self._resp(status=404)
            self.bookmarks.remove(mark)
            return self._resp()
        abs_id = path.rsplit("/", 1)[1]
        if self.delete_status == 200:
            self.items.pop(abs_id, None)
            self.progress.pop(abs_id, None)
        return self._resp(status=self.delete_status)

    def post(self, url, json=None, **kw):
        self.calls.append(("POST", url, json))
        path = url.replace("http://abs.test", "")
        if path == "/api/libraries/lib/scan" and not self.items and self.comes_back:
            self.due = 1
        elif path.startswith("/api/me/item/") and path.endswith("/bookmark"):
            self.bookmarks.append({"libraryItemId": path.split("/")[4], **json})
        return self._resp()

    def patch(self, url, json=None, **kw):
        self.calls.append(("PATCH", url, json))
        path = url.replace("http://abs.test", "")
        abs_id = path.split("/")[3] if path.startswith("/api/items/") else path.rsplit("/", 1)[1]
        if abs_id not in self.items:
            return mock.Mock(status_code=404, raise_for_status=mock.Mock(side_effect=ValueError("404")))
        if path.endswith("/tracks"):
            keep = [o["ino"] for o in json["orderedFileData"]]
            item = self.items[abs_id]
            item["files"] = [next(f for f in item["files"] if f["ino"] == i) for i in keep]
        else:
            self.progress[abs_id] = dict(json, lastUpdate=2)
        return self._resp()


def rebuild(fake, expected=150.0, timeout=2):
    out = io.StringIO()
    with gw.get_db() as db:
        db.execute("UPDATE books SET abs_id = 'old' WHERE book_id = 1")
        db.commit()
    with contextlib.redirect_stdout(out), mock.patch.object(gw, "requests", fake), \
            mock.patch.object(gw, "ABS_POLL_SECONDS", 0):
        gw.sync_abs_chapters(0, 1, "LBR-1", expected, timeout=timeout)
    with gw.get_db() as db:
        stored = db.execute("SELECT abs_id FROM books WHERE book_id = 1").fetchone()["abs_id"]
    chapters = [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/chapters")]
    return out.getvalue(), stored, chapters


PLACE = {"currentTime": 120.0, "duration": 200.0, "progress": 0.6, "isFinished": False, "lastUpdate": 1,
         "ebookLocation": "epubcfi(/6/4)", "ebookProgress": 0.4}
fake = RebuildingABS(progress=dict(PLACE))
said, stored, chapters = rebuild(fake)
deletes = [c for c in fake.calls if c[0] == "DELETE" and "/api/items/" in c[1]]
check("a doubled item is forgotten, once, by its id",
      [c[1] for c in deletes] == ["http://abs.test/api/items/old"], f"({deletes})")
check("never with ?hard=1, which deletes the files",
      not any("hard" in str(c[1]) + str(c[2] if len(c) > 2 else "") for c in fake.calls))
check("its place is read before it is forgotten",
      deletes and fake.calls.index(("GET", "http://abs.test/api/me/progress/old", None))
      < fake.calls.index(deletes[0]))
check("the new item, found by watermark, is the one stored", stored == "new1" and set(fake.items) == {"new1"},
      f"({stored}, {set(fake.items)})")
new = fake.progress.get("new1") or {}
check("the listening position is carried over, against the new item's duration",
      new.get("currentTime") == 120.0 and new.get("duration") == 150.0 and abs(new.get("progress", 0) - 0.8) < 1e-9
      and new.get("isFinished") is False, f"({new})")
check("and the place in ABS's own ebook reader", new.get("ebookLocation") == "epubcfi(/6/4)"
      and new.get("ebookProgress") == 0.4, f"({new})")
check("its bookmarks are carried over, none left behind on the old id; another book's are left alone",
      sorted((b["libraryItemId"], b["title"]) for b in fake.bookmarks)
      == [("elsewhere", "Another book"), ("new1", "Fagin")], f"({fake.bookmarks})")
check("chapters are written on the new item, one per file",
      chapters and chapters[-1][1].endswith("/items/new1/chapters") and len(chapters[-1][2]["chapters"]) == 2,
      f"({chapters[-1:]})")
check("the new item lists each file once, so it is neither patched nor rescanned",
      not any("/items/new1/tracks" in c[1] or c[1].endswith("/items/new1/scan") for c in fake.calls))
check("the log says what was carried over",
      "forgotten" in said and "carried over the listening position 00:02:00 and 1 bookmark(s)" in said,
      said.strip()[-200:])

fake = RebuildingABS()
said, stored, chapters = rebuild(fake)
check("an item nobody has listened to is rebuilt too, with no position written",
      stored == "new1" and not any(c[0] == "PATCH" and "/api/me/progress/" in c[1] for c in fake.calls)
      and "carried over no listening position and 1 bookmark(s)" in said, said.strip()[-200:])

fake = RebuildingABS(progress=dict(PLACE, isFinished=True))
said, stored, chapters = rebuild(fake)
check("a finished book stays finished", fake.progress.get("new1", {}).get("isFinished") is True
      and fake.progress["new1"]["progress"] == 1.0, f"({fake.progress})")

fake = RebuildingABS(progress=dict(PLACE), progress_status=500)
said, stored, chapters = rebuild(fake)
check("a place ABS will not tell is never forgotten: no delete, the patch-only repair instead",
      not any(c[0] == "DELETE" for c in fake.calls) and stored == "old"
      and [f["ino"] for f in fake.items["old"]["files"]] == ["1", "2"] and "not rebuilt" in said, said.strip()[-200:])

fake = RebuildingABS(progress=dict(PLACE), delete_status=500)
said, stored, chapters = rebuild(fake)
check("an item ABS will not forget keeps its place and gets the patch-only repair",
      fake.progress.get("old", {}).get("currentTime") == 120.0 and stored == "old"
      and sorted((b["libraryItemId"], b["title"]) for b in fake.bookmarks)
      == [("elsewhere", "Another book"), ("old", "Fagin")]
      and [f["ino"] for f in fake.items["old"]["files"]] == ["1", "2"]
      and chapters and chapters[-1][1].endswith("/items/old/chapters"), said.strip()[-200:])

fake = RebuildingABS(progress=dict(PLACE), comes_back_doubled=True)
said, stored, chapters = rebuild(fake)
check("an item that comes back doubled is not forgotten again: patched, and its place still carried over",
      len([c for c in fake.calls if c[0] == "DELETE" and "/api/items/" in c[1]]) == 1 and stored == "new1"
      and [f["ino"] for f in fake.items["new1"]["files"]] == ["1", "2"]
      and fake.progress.get("new1", {}).get("currentTime") == 120.0, said.strip()[-200:])

fake = RebuildingABS(progress=dict(PLACE), comes_back=False)
said, stored, chapters = rebuild(fake, timeout=1)
check("an item that never comes back: the log keeps the place to set by hand",
      not chapters and "Not carried over from the forgotten item old: the listening position 00:02:00 "
      "and 1 bookmark(s)" in said, said.strip()[-240:])

fake = RebuildingABS(progress=dict(PLACE))
said, stored, chapters = rebuild(fake, expected=300.0, timeout=1)
check("half a book (the scan not caught up) is never forgotten",
      not any(c[0] == "DELETE" for c in fake.calls) and "old" in fake.items)

# --- A healthy item: nothing is touched --------------------------------------
for n in os.listdir(FOLDER):
    os.utime(os.path.join(FOLDER, n), (1, 1))
fake = FakeABS([af("1", 1, "Chapter_01_One.mp3", 100.0), af("2", 2, "Chapter_02_Two.mp3", 50.0)], 150.0)
written = run(fake, 150.0)
check("a healthy item is neither patched nor rescanned",
      not any(c[0] == "PATCH" or c[1].endswith("/items/li/scan") for c in fake.calls))
check("its files are left alone", all(os.path.getmtime(os.path.join(FOLDER, n)) < 2 for n in os.listdir(FOLDER)))
check("its chapters are written", len(written) == 1)

# --- Files still arriving: no repair on half a book ----------------------------
fake = FakeABS([af("1", 1, "Chapter_01_One.mp3", 100.0)], 100.0)
written = run(fake, 150.0)
check("a scan that has not caught up is waited for, not repaired or written",
      not written and not any(c[0] == "PATCH" or c[1].endswith("/items/li/scan") for c in fake.calls))

# --- At start-up: every broken item in the library, and only those ----------
with gw.get_db() as db:
    db.execute("UPDATE books SET audio_state = 'ready' WHERE book_id = 1")
    db.execute("INSERT INTO books (book_id, lib_id, title, epub_path, user_id, audio_state) "
               "VALUES (2, 'LBR-2', 'Fine', 'library/audiobooks/Fine/Fine.kepub.epub', 0, 'ready')")
    db.execute("INSERT INTO books (book_id, lib_id, title, epub_path, user_id, audio_state) "
               "VALUES (3, 'LBR-3', 'Ebook', 'library/audiobooks/Ebook/Ebook.kepub.epub', 0, 'none')")
    db.commit()


class FakeLibrary(FakeABS):
    """Two items: li (Twisted, stale) and li2 (healthy)."""

    def __init__(self):
        super().__init__([af("1", 1, "Chapter_01_One.mp3", 100.0), af("2", 2, "Chapter_02_Two.mp3", 50.0)], 999.9)
        self.fine = {"media": {"audioFiles": [af("9", 1, "Chapter_01_X.mp3", 10.0)], "duration": 10.0}}

    def get(self, url, **kw):
        if "/api/items/li2" in url:
            self.calls.append(("GET", url))
            return self._resp(self.fine)
        return super().get(url, **kw)


for n in os.listdir(FOLDER):
    os.utime(os.path.join(FOLDER, n), (1, 1))
fake = FakeLibrary()
ids = {"LBR-1": "li", "LBR-2": "li2", "LBR-3": "li3"}
with mock.patch.object(gw, "requests", fake), \
        mock.patch.object(gw, "resolve_abs_id", side_effect=lambda lib_id, uid: ids[lib_id]):
    fixed = gw.repair_abs_library(log=lambda *a: None)
check("the start-up sweep repairs the stale item, and only it", fixed == ["LBR-1"], f"({fixed})")
check("its duration is right again", abs(fake.duration - 150.0) < 0.01, f"({fake.duration})")
check("a book without audio is not looked at", not any("li3" in c[1] for c in fake.calls))

# --- One broken item never stops the sweep ------------------------------------
class Unreadable(FakeLibrary):
    """li2 answers garbage; li is stale, as before."""

    def get(self, url, **kw):
        if "/api/items/li2" in url:
            self.calls.append(("GET", url))
            return mock.Mock(status_code=502, json=mock.Mock(side_effect=ValueError("not JSON")))
        return super().get(url, **kw)


for n in os.listdir(FOLDER):
    os.utime(os.path.join(FOLDER, n), (1, 1))
fake, said = Unreadable(), []
ids = {"LBR-1": "li", "LBR-2": "li2", "LBR-3": "li3"}
with mock.patch.object(gw, "requests", fake), \
        mock.patch.object(gw, "resolve_abs_id", side_effect=lambda lib_id, uid: ids[lib_id]):
    fixed = gw.repair_abs_library(log=said.append)
check("an item that cannot be read is reported, and the others are still repaired",
      fixed == ["LBR-1"] and any("unreadable" in line for line in said), f"({fixed}, {said})")

fake = FakeLibrary()
with mock.patch.object(gw, "requests", fake), \
        mock.patch.object(gw, "resolve_abs_id", side_effect=lambda lib_id, uid: None):
    fixed = gw.repair_abs_library(log=lambda *a: None)
check("a book ABS does not show yet is skipped, not guessed at",
      fixed == [] and not any(c[0] == "GET" and "/api/items/" in c[1] for c in fake.calls))


class PatchFails(FakeABS):
    def patch(self, url, json=None, **kw):
        raise ConnectionError("ABS went away")


out = io.StringIO()
fake = PatchFails([af("1", 1, "Chapter_01_One.mp3", 100.0), af("1", 2, "Chapter_01_One.mp3", 100.0)], 200.0)
with contextlib.redirect_stdout(out), mock.patch.object(gw, "requests", fake):
    try:
        gw._repair_abs_item("http://abs.test", {}, "li", 1, [af("1", 1, "Chapter_01_One.mp3", 100.0)])
        raised = False
    except Exception:
        raised = True
check("a repair ABS refuses is reported, never raised into the sweep or the upload",
      not raised and "repair failed (ABS went away)" in out.getvalue(), out.getvalue().strip())

sys.path.insert(0, os.path.join(ROOT, "tools"))
import gateway_admin  # noqa: E402


class Ping:
    def __init__(self, ups):
        self.ups, self.asked = list(ups), 0

    def get(self, url, timeout=None):
        self.asked += 1
        return mock.Mock(status_code=200 if self.ups.pop(0) else 502)


swept = []
gateway = mock.Mock(repair_abs_library=lambda: swept.append(1) or ["LBR-1"])
ping = Ping([False, False, True])
code = gateway_admin.abs_repair(wait=60, http=ping, gateway=gateway, sleep=lambda s: None)
check("abs-repair waits for Audiobookshelf to boot, then sweeps", code == 0 and ping.asked == 3 and swept == [1])
swept.clear()
code = gateway_admin.abs_repair(wait=0, http=Ping([False]), gateway=gateway, sleep=lambda s: None)
check("an Audiobookshelf that never answers ends it without a sweep",
      isinstance(code, str) and "does not answer" in code and not swept, f"({code})")
start = open(os.path.join(ROOT, "start.sh")).read()
check("the container runs it in the background at every start",
      "gateway_admin.py abs-repair" in start and ") &" in start and start.index("abs-repair") < start.index("exec gunicorn"))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
