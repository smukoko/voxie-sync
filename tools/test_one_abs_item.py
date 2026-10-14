"""One Audiobookshelf item per book: the ebook lives beside its audio.

ABS makes one item per folder, and books used to be split between
library/ebooks/<title>/ and library/audiobooks/<title>/, so every narrated book
was listed twice; deleted books stayed listed as missing items forever.

Offline: runs in a temp directory against a mocked ABS.
"""
import io, json, os, sqlite3, sys, tempfile, zipfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ.update(UPLOAD_TOKEN="t", ABS_URL="http://abs.test", ABS_API_TOKEN="x")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import librerian_gateway_v3 as gw  # noqa: E402
from core import accounts, library  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


with gw.get_db() as db:
    accounts.create_user(db, "reader", accounts.userkey_for("pw"))
    accounts.claim_unowned(db, accounts.owner_id(db))
c = gw.app.test_client()


def manifest(lib_id, epub_path, chapters=()):
    return {"book": {"lib_id": lib_id, "epub_path": epub_path, "title": "T"},
            "chapters": [{"chapter_id": i, "chapter_index": i, "audio_filename": n, "duration": 10.0}
                         for i, n in enumerate(chapters, 1)],
            "sync_nodes": [], "word_timings": []}


def upload(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    buf.seek(0)
    # The chapter rewrite after an audio pack talks to ABS in a thread; keep it offline.
    with mock.patch.object(gw, "sync_abs_chapters"):
        return c.post("/upload", headers={"X-Libra-Token": "t"}, data={"file": (buf, "p.zip")},
                      content_type="multipart/form-data")


def epub_path_of(lib_id):
    with gw.get_db() as db:
        return db.execute("SELECT epub_path FROM books WHERE lib_id = ?", (lib_id,)).fetchone()[0]


# --- A pack in the new layout ------------------------------------------------
A = "library/audiobooks/A"
r = upload({f"{A}/A.kepub.epub": b"kepub-A", f"{A}/Chapter_01_One.mp3": b"a1",
            f"{A}/manifest.json": json.dumps(manifest("LBR-00000000000a", f"{A}/A.kepub.epub",
                                                      ["Chapter_01_One.mp3"]))})
check("a one-folder pack is accepted", r.status_code == 200, f"({r.status_code} {r.get_json()})")
check("its ebook and audio share one folder",
      sorted(os.listdir(A)) == ["A.kepub.epub", "Chapter_01_One.mp3"], f"({sorted(os.listdir(A))})")
check("its epub_path is the book folder", epub_path_of("LBR-00000000000a") == f"{A}/A.kepub.epub")

# --- A pack built before the change (queued on the Mac, or rebuild_map) -----
B = "library/audiobooks/B"
r = upload({"library/ebooks/B/B.kepub.epub": b"kepub-B", f"{B}/Chapter_01_One.mp3": b"b1",
            "library/ebooks/B/manifest.json": json.dumps(manifest("LBR-00000000000b", "library/ebooks/B/B.kepub.epub",
                                                                  ["Chapter_01_One.mp3"]))})
check("a legacy pack is accepted", r.status_code == 200, f"({r.status_code})")
check("a legacy pack's ebook lands beside its audio", os.path.exists(f"{B}/B.kepub.epub"))
check("a legacy pack creates no second folder for ABS", not os.path.exists("library/ebooks/B"))
check("a legacy pack's manifest is not left in the library", not os.path.exists(f"{B}/manifest.json"))
check("a legacy pack's epub_path points at the book folder", epub_path_of("LBR-00000000000b") == f"{B}/B.kepub.epub",
      f"({epub_path_of('LBR-00000000000b')})")
with mock.patch.object(gw, "basic_user", return_value=1):
    d = c.get("/download/LBR-00000000000b")
check("the moved ebook is still served", d.status_code == 200 and d.data == b"kepub-B", f"({d.status_code})")

# --- Regeneration replaces audio, keeps the ebook -----------------------------
r = upload({f"{A}/A.kepub.epub": b"kepub-A", f"{A}/Chapter_01_Renamed.mp3": b"a2",
            f"{A}/manifest.json": json.dumps(manifest("LBR-00000000000a", f"{A}/A.kepub.epub",
                                                      ["Chapter_01_Renamed.mp3"]))})
check("a regenerated book drops the superseded chapter",
      sorted(os.listdir(A)) == ["A.kepub.epub", "Chapter_01_Renamed.mp3"], f"({sorted(os.listdir(A))})")

# An ebook-only pack (every regeneration's early publish) never touches audio.
r = upload({f"{A}/A.kepub.epub": b"kepub-A", f"{A}/manifest.json": json.dumps(manifest("LBR-00000000000a", f"{A}/A.kepub.epub"))})
check("an ebook-only pack leaves the audio alone", "Chapter_01_Renamed.mp3" in os.listdir(A), f"({os.listdir(A)})")

check("clear_audio removes audio and nothing else",
      library.clear_audio(A) == ["Chapter_01_Renamed.mp3"] and os.listdir(A) == ["A.kepub.epub"])

# A member that escapes the library is refused before anything is written.
r = upload({"library/audiobooks/E/ok.mp3": b"x", "library/ebooks/../../escape.txt": b"x",
            "library/audiobooks/E/manifest.json": json.dumps(manifest("LBR-00000000000e", "x"))})
check("an escaping member is refused", r.status_code == 500 and not os.path.exists("../escape.txt"))
check("and nothing of that pack was written", not os.path.exists("library/audiobooks/E"))

# --- The early ebook pack on the Mac carries no old audio ----------------------
import mac_ghost  # noqa: E402
os.makedirs("library/audiobooks/M")
for name, data in {"M.kepub.epub": b"k", "manifest.json": b"{}", "Chapter_01_Old.mp3": b"old"}.items():
    open(f"library/audiobooks/M/{name}", "wb").write(data)
json.dump({"ebook_dir": "library/audiobooks/M", "lib_id": "LBR-m", "title": "M"}, open("early.json", "w"))
shipped = {}


def fake_upload(zip_filename):
    shipped["names"] = sorted(zipfile.ZipFile(zip_filename).namelist())
    return True


with mock.patch.object(mac_ghost, "upload_with_retries", fake_upload):
    mac_ghost.ship_ebook_early("early.json")
check("the early ebook pack leaves the previous run's audio behind",
      shipped.get("names") == ["library/audiobooks/M/M.kepub.epub", "library/audiobooks/M/manifest.json"],
      f"({shipped.get('names')})")

# --- migrate-layout: books already on disk ------------------------------------
mig = tempfile.mkdtemp()
os.makedirs(f"{mig}/library/ebooks/Old")
os.makedirs(f"{mig}/library/audiobooks/Old")
open(f"{mig}/library/ebooks/Old/Old.kepub.epub", "wb").write(b"old-kepub")
open(f"{mig}/library/audiobooks/Old/Chapter_01.mp3", "wb").write(b"o1")
os.makedirs(f"{mig}/library/ebooks/Clash")
os.makedirs(f"{mig}/library/audiobooks/Clash")
open(f"{mig}/library/ebooks/Clash/Clash.kepub.epub", "wb").write(b"one")
open(f"{mig}/library/audiobooks/Clash/Clash.kepub.epub", "wb").write(b"two")
conn = sqlite3.connect(":memory:")
conn.execute("CREATE TABLE books (epub_path TEXT)")
conn.execute("INSERT INTO books VALUES ('library/ebooks/Old/Old.kepub.epub')")

moved, conflicts = library.migrate_layout(conn, root=mig, log=lambda *_: None)
check("migrate-layout without --apply changes nothing",
      os.path.exists(f"{mig}/library/ebooks/Old/Old.kepub.epub") and len(moved) == 1
      and conn.execute("SELECT epub_path FROM books").fetchone()[0] == "library/ebooks/Old/Old.kepub.epub")
moved, conflicts = library.migrate_layout(conn, root=mig, apply=True, log=lambda *_: None)
check("migrate-layout moves the ebook beside its audio",
      sorted(os.listdir(f"{mig}/library/audiobooks/Old")) == ["Chapter_01.mp3", "Old.kepub.epub"]
      and not os.path.exists(f"{mig}/library/ebooks/Old"))
check("migrate-layout updates epub_path",
      conn.execute("SELECT epub_path FROM books").fetchone()[0] == "library/audiobooks/Old/Old.kepub.epub")
check("migrate-layout never overwrites a different file",
      open(f"{mig}/library/audiobooks/Clash/Clash.kepub.epub", "rb").read() == b"two"
      and os.path.exists(f"{mig}/library/ebooks/Clash/Clash.kepub.epub") and len(conflicts) == 1)
check("migrate-layout is idempotent", library.migrate_layout(conn, root=mig, apply=True, log=lambda *_: None)[0] == [])

# --- Deleting a book removes its ABS item, never its files through ABS ---------
with gw.get_db() as db:
    db.execute("UPDATE books SET abs_id = 'li_b' WHERE lib_id = 'LBR-00000000000b'")
    db.commit()
with mock.patch.object(gw.requests, "delete", return_value=mock.Mock(ok=True, status_code=200)) as dele:
    r = c.delete("/books/LBR-00000000000b", headers={"X-Libra-Token": "t"})
call = dele.call_args
check("a deleted book's ABS item is removed", r.get_json().get("abs_item_removed") is True
      and call.args[0] == "http://abs.test/api/items/li_b", f"({call})")
check("ABS is never asked to delete files", "hard" not in json.dumps(call.kwargs.get("params") or {})
      and "hard" not in call.args[0])
check("the book folder is gone", not os.path.exists(B))

# --- abs-cleanup: ghosts are decided from the disk ----------------------------
import gateway_admin  # noqa: E402
os.makedirs("library/audiobooks/Here", exist_ok=True)
items = [{"id": "li_here", "path": "/library/audiobooks/Here", "media": {"metadata": {"title": "Here"}}},
         {"id": "li_dup", "path": "/library/ebooks/Here", "media": {"metadata": {"title": "Here"}}},
         {"id": "li_gone", "path": "/library/audiobooks/A Doll's House", "media": {"metadata": {"title": "Doll"}}},
         {"id": "li_other", "path": "/elsewhere/x", "media": {"metadata": {"title": "Other"}}}]


class FakeABS:
    deleted = []

    def get(self, url, **kw):
        body = {"libraries": [{"id": "lib1", "name": "Books"}]} if url.endswith("/api/libraries") else {"results": items}
        return mock.Mock(json=lambda: body)

    def delete(self, url, **kw):
        self.deleted.append((url, kw))
        return mock.Mock(ok=True, status_code=200)


fake = FakeABS()
ghosts = [i["id"] for _, i, _ in gateway_admin.abs_ghosts("http://abs.test", "x", fake)]
check("abs-cleanup finds the duplicate and the deleted book", ghosts == ["li_dup", "li_gone"], f"({ghosts})")
gateway_admin.abs_cleanup(apply=False, http=fake)
check("abs-cleanup without --apply deletes nothing", fake.deleted == [])
gateway_admin.abs_cleanup(apply=True, http=fake)
check("abs-cleanup --apply forgets exactly the ghosts, without ?hard",
      [u for u, _ in fake.deleted] == ["http://abs.test/api/items/li_dup", "http://abs.test/api/items/li_gone"]
      and all("hard" not in json.dumps(kw.get("params") or {}) for _, kw in fake.deleted))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
