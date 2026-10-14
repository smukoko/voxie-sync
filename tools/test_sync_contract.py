"""The sync contract: whatever the user does, their place is never lost.

Real readers are messy in specific ways -- they upload the same book twice,
delete and re-upload, want the ebook before the audio, and deliver batches.
Each of those used to break the identity chain in a different place, so each
gets a scenario here. Runs against a COPY of the real database.

    python tools/test_sync_contract.py
"""
import json, os, shutil, sqlite3, sys, tempfile, zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
work = tempfile.mkdtemp(prefix="libra-identity-")
os.makedirs(os.path.join(work, "library"), exist_ok=True)
shutil.copyfile(os.path.join(REPO, "library", "database.db"),
                os.path.join(work, "library", "database.db"))
os.chdir(work)
sys.path.insert(0, REPO)
os.environ["UPLOAD_TOKEN"] = "test-token"

import librerian_gateway_v3 as gw   # noqa: E402  (calls setup_database on our copy)
gw.UPLOAD_TOKEN = "test-token"
app = gw.app.test_client()
DB = os.path.join(work, "library", "database.db")

# Every sync call and the catalogue need an account now. The first one owns
# the books that existed before accounts, as it does on a real server.
import base64 as _b64
from core import accounts as _acc, library
_c = sqlite3.connect(DB)
OWNER = _acc.create_user(_c, "reader", _acc.userkey_for("pw1"))
_acc.claim_unowned(_c, OWNER)
OTHER = _acc.create_user(_c, "stranger", _acc.userkey_for("pw2"))
_c.close()
KOSYNC = {"x-auth-user": "reader", "x-auth-key": _acc.userkey_for("pw1")}
BASIC = {"Authorization": "Basic " + _b64.b64encode(b"reader:pw1").decode()}

def q(sql, *a):
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    r = [dict(x) for x in c.execute(sql, a)]; c.close(); return r

def make_pack(lib_id, title, nodes=3):
    folder = f"library/ebooks/{title}"
    os.makedirs(os.path.join(work, folder), exist_ok=True)
    os.makedirs(os.path.join(work, f"library/audiobooks/{title}"), exist_ok=True)
    manifest = {
        "book": {"lib_id": lib_id, "kobo_hash": None, "abs_id": None,
                 "epub_path": f"{folder}/{title}.kepub.epub"},
        "chapters": [{"chapter_id": 1, "chapter_index": 1,
                      "audio_filename": "Chapter_01.mp3", "duration": 100.0}],
        "sync_nodes": [{"chapter_id": 1, "xpath_string": f"point(/1/2/{i})",
                        "start_time": i * 10.0, "end_time": i * 10.0 + 9,
                        "text_content": f"n{i}"} for i in range(nodes)],
    }
    zp = os.path.join(work, f"{title}_SyncPack.zip")
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr(f"{folder}/manifest.json", json.dumps(manifest))
        # Distinct bytes per book, as real books are: the fingerprint is of the file.
        z.writestr(f"{folder}/{title}.kepub.epub", f"EPUB {lib_id}".encode())
        z.writestr(f"library/audiobooks/{title}/Chapter_01.mp3", b"MP3DATA")
    return zp

def upload(zp):
    with open(zp, "rb") as f:
        return app.post("/upload", data={"file": (f, os.path.basename(zp))},
                        headers={"X-Libra-Token": "test-token"},
                        content_type="multipart/form-data")

ok = True
def check(label, cond):
    global ok
    ok = ok and cond
    print(f"   {'PASS' if cond else 'FAIL'}  {label}")

print("\n1. MIGRATION — existing device pairings carried into book_hashes")
legacy = q("SELECT book_id, kobo_hash FROM books WHERE kobo_hash IS NOT NULL")
rows = q("SELECT * FROM book_hashes")
check(f"{len(legacy)} legacy hash(es) -> {len(rows)} row(s) in book_hashes", len(rows) >= len(legacy))

print("\n2. SAME BOOK UPLOADED TWICE — pairing and abs_id survive")
LIB, TITLE = "LBR-deadbeef0001", "TestBook"
upload(make_pack(LIB, TITLE))
bid = q("SELECT book_id FROM books WHERE lib_id=?", LIB)[0]["book_id"]
gw.LibrarianDB.link_hash_to_book(LIB, "devicehash-A")
sqlite3.connect(DB).execute("UPDATE books SET abs_id='abs-xyz' WHERE book_id=?", (bid,)).connection.commit()
upload(make_pack(LIB, TITLE, nodes=5))                      # the same book again
after = q("SELECT book_id, abs_id FROM books WHERE lib_id=?", LIB)
check("still exactly one row for this book", len(after) == 1)
check("book_id unchanged (progress keys still valid)", after[0]["book_id"] == bid)
check("abs_id preserved", after[0]["abs_id"] == "abs-xyz")
check("device pairing preserved", gw.LibrarianDB.get_book_info_by_hash("devicehash-A")[0] == bid)
check("sync_nodes rebuilt from the new pack", len(q(
    "SELECT 1 FROM sync_nodes WHERE chapter_id IN (SELECT chapter_id FROM chapters WHERE book_id=?)", bid)) == 5)

print("\n3. TWO DEVICES ON ONE BOOK — no divorce")
gw.LibrarianDB.link_hash_to_book(LIB, "devicehash-B")
check("device A still resolves", gw.LibrarianDB.get_book_info_by_hash("devicehash-A")[0] == bid)
check("device B also resolves", gw.LibrarianDB.get_book_info_by_hash("devicehash-B")[0] == bid)

print("\n4. GRANDFATHERED BOOK — random old id adopts a content id, pairing survives")
c = sqlite3.connect(DB)
c.execute("INSERT INTO books (user_id, lib_id, epub_path) VALUES (?, 'LBR-oldrandom99','library/ebooks/Legacy/Legacy.kepub.epub')", (OWNER,))
c.commit()
old_bid = q("SELECT book_id FROM books WHERE lib_id='LBR-oldrandom99'")[0]["book_id"]
gw.LibrarianDB.link_hash_to_book("LBR-oldrandom99", "devicehash-legacy")
upload(make_pack("LBR-content00abc", "Legacy"))             # re-upload, new content id
rows = q("SELECT book_id, lib_id, epub_path FROM books WHERE lib_id IN ('LBR-oldrandom99', 'LBR-content00abc')")
check("adopted onto the same row, no duplicate", len(rows) == 1 and rows[0]["book_id"] == old_bid)
check("row now carries the content-derived id", rows[0]["lib_id"] == "LBR-content00abc")
check("and its ebook moved into the book folder", rows[0]["epub_path"] == "library/audiobooks/Legacy/Legacy.kepub.epub")
check("legacy device still resolves", gw.LibrarianDB.get_book_info_by_hash("devicehash-legacy")[0] == old_bid)

print("\n5. EBOOK-ONLY — survives OPDS, which used to delete it")
EB = "LBR-ebookonly001"
zp = make_pack(EB, "EbookOnly", nodes=0)
import zipfile as _z, json as _j
# rebuild the pack with no chapters at all: an ebook-only publication
with _z.ZipFile(zp, "w") as z:
    man = {"book": {"lib_id": EB, "abs_id": None, "kobo_hash": None,
                    "epub_path": "library/ebooks/EbookOnly/EbookOnly.kepub.epub"},
           "chapters": [], "sync_nodes": []}
    z.writestr("library/ebooks/EbookOnly/manifest.json", _j.dumps(man))
    z.writestr("library/ebooks/EbookOnly/EbookOnly.kepub.epub", b"EPUBDATA")
upload(zp)
# No audio: the book folder holds the ebook alone (it is also where audio would go).
library.clear_audio(os.path.join(work, "library/audiobooks/EbookOnly"))
row = q("SELECT book_id, audio_state FROM books WHERE lib_id=?", EB)
check("registered with audio_state='none'", len(row) == 1 and row[0]["audio_state"] == "none")
eb_id = row[0]["book_id"]
app.get("/opds", headers=BASIC); app.get("/opds", headers=BASIC)   # the call that used to destroy it
check("still present after two OPDS fetches", len(q("SELECT 1 FROM books WHERE lib_id=?", EB)) == 1)
check("its EPUB still on disk", os.path.exists(os.path.join(work, "library/audiobooks/EbookOnly/EbookOnly.kepub.epub")))

print("\n6. PROGRESS WITHOUT AUDIO — position kept, then carried into the audiobook")
gw.LibrarianDB.link_hash_to_book(EB, "devicehash-eb")
gw.record_progress(eb_id, 0.42, "point(/1/2/7)", source="kobo")
r = app.get("/syncs/progress/devicehash-eb", headers=KOSYNC)
check("pull returns the stored position", r.status_code == 200 and r.get_json().get("progress") == "point(/1/2/7)")
upload(make_pack(EB, "EbookOnly", nodes=4))   # the audio arrives later
row = q("SELECT book_id, audio_state FROM books WHERE lib_id=?", EB)
check("same book_id after audio is added", row[0]["book_id"] == eb_id)
check("audio_state now 'ready'", row[0]["audio_state"] == "ready")
kept = q("SELECT kobo_location FROM progress WHERE book_id=?", eb_id)
check("reading position survived into the audiobook", kept and kept[0]["kobo_location"] == "point(/1/2/7)")

print("\n7. DELETE — soft, and a re-upload gets the place back")
r = app.delete(f"/books/{EB}", headers={"X-Libra-Token": "test-token"})
check("delete accepted", r.status_code == 200)
row = q("SELECT deleted_at FROM books WHERE lib_id=?", EB)
check("tombstoned, not removed", len(row) == 1 and row[0]["deleted_at"] is not None)
check("progress retained through the delete", len(q("SELECT 1 FROM progress WHERE book_id=?", eb_id)) == 1)
feed = app.get("/opds", headers=BASIC).get_data(as_text=True)
check("hidden from the catalog", "EbookOnly" not in feed)
check("device pairing retained", gw.LibrarianDB.get_book_info_by_hash("devicehash-eb")[0] == eb_id)
upload(make_pack(EB, "EbookOnly", nodes=2))
row = q("SELECT book_id, deleted_at FROM books WHERE lib_id=?", EB)
check("re-upload revives the same book", row[0]["book_id"] == eb_id and row[0]["deleted_at"] is None)
kept = q("SELECT kobo_location FROM progress WHERE book_id=?", eb_id)
check("and the reader gets their place back", kept[0]["kobo_location"] == "point(/1/2/7)")

print("\n8. UNAUTHENTICATED DELETE is refused")
check("no token -> 401", app.delete(f"/books/{LIB}").status_code == 401)
check("book untouched", len(q("SELECT 1 FROM books WHERE lib_id=? AND deleted_at IS NULL", LIB)) == 1)

print("\n9. RESUMABLE UPLOAD — an interrupted transfer resumes, it does not restart")
import hashlib as _h
RZ = "LBR-resumable001"
zp = make_pack(RZ, "Resumable", nodes=3)
raw = open(zp, "rb").read()
# Deliberately tiny parts: the test pack is a few KB, and the point is to
# exercise multi-part resume, not the production 8MB size.
PART = 256
parts = [raw[i:i + PART] for i in range(0, len(raw), PART)]
assert len(parts) >= 4, f"test pack too small to split ({len(parts)} parts)"
sha = _h.sha256(raw).hexdigest()
pack_id = sha[:32]
AUTH = {"X-Libra-Token": "test-token"}

def send_part(i):
    h = dict(AUTH)
    h.update({"X-Pack-Id": pack_id, "X-Part-Index": str(i),
              "X-Part-Sha256": _h.sha256(parts[i]).hexdigest()})
    return app.post("/upload/part", data=parts[i], headers=h,
                    content_type="application/octet-stream")

# Send all but the last two, as though the connection dropped.
first = send_part(0)
check("first part accepted", first.status_code == 200)
for i in range(1, len(parts) - 2):
    send_part(i)
seen = app.get(f"/upload/status/{pack_id}", headers=AUTH).get_json()["parts"]
check(f"server reports {len(parts) - 2}/{len(parts)} parts held", seen == list(range(len(parts) - 2)))

early = app.post("/upload/commit", headers=AUTH, json={"pack_id": pack_id, "total_parts": len(parts)})
check("committing early is refused with 409", early.status_code == 409)
check("and it names what is missing", early.get_json().get("missing_count") == 2)

# Resume: only the two that never arrived.
for i in range(len(parts) - 2, len(parts)):
    send_part(i)
done = app.post("/upload/commit", headers=AUTH,
                json={"pack_id": pack_id, "total_parts": len(parts),
                      "sha256": sha, "filename": "Resumable_SyncPack.zip"})
check("commit succeeds once complete", done.status_code == 200)
check("book landed in the library", len(q("SELECT 1 FROM books WHERE lib_id=?", RZ)) == 1)
check("parts cleaned up after commit",
      not os.path.isdir(os.path.join(work, "incoming", "parts", pack_id)))

print("\n10. RESUMABLE UPLOAD refuses what it should")
bad = dict(AUTH); bad.update({"X-Pack-Id": pack_id, "X-Part-Index": "0", "X-Part-Sha256": "0" * 64})
check("a part failing its checksum is rejected",
      app.post("/upload/part", data=b"tampered", headers=bad,
               content_type="application/octet-stream").status_code == 400)
trav = dict(AUTH); trav.update({"X-Pack-Id": "../../etc", "X-Part-Index": "0"})
check("a traversal pack id is rejected",
      app.post("/upload/part", data=b"x", headers=trav,
               content_type="application/octet-stream").status_code == 400)
check("status needs a token", app.get(f"/upload/status/{pack_id}").status_code == 401)
check("part needs a token",
      app.post("/upload/part", data=b"x",
               headers={"X-Pack-Id": pack_id, "X-Part-Index": "0"}).status_code == 401)

print("\n11. ACCOUNTS — nothing without a login, and no account sees another's")
check("pull without a login -> 401", app.get("/syncs/progress/devicehash-A").status_code == 401)
check("pull with a wrong key -> 401", app.get("/syncs/progress/devicehash-A",
      headers={"x-auth-user": "reader", "x-auth-key": "0" * 32}).status_code == 401)
check("KOReader's login check accepts the right key", app.get("/users/auth", headers=KOSYNC).status_code == 200)
check("registration is closed by default", app.post("/users/create", json={"username": "x", "password": "y"}).status_code == 403)
check("catalogue without a login -> 401", app.get("/opds").status_code == 401)
check("download without a login -> 401", app.get(f"/download/{LIB}").status_code == 401)
STRANGER = {"x-auth-user": "stranger", "x-auth-key": _acc.userkey_for("pw2")}
check("another account cannot read this account's position",
      app.get("/syncs/progress/devicehash-eb", headers=STRANGER).status_code == 404)
other_feed = app.get("/opds", headers={"Authorization": "Basic " + _b64.b64encode(b"stranger:pw2").decode()})
check("another account's catalogue is empty", other_feed.status_code == 200 and b"<entry>" not in other_feed.data)
check("the old pairing ping marries nothing", app.get(f"/librarian/ping/{LIB}").get_json()["status"] == "ignored")

print("\n12. FINGERPRINT — the device's own id is paired the moment the book lands")
from core.fingerprint import koreader_partial_md5
FP, FT = "LBR-fingerprint01", "Fingerprinted"
upload(make_pack(FP, FT))
fp_book = q("SELECT book_id FROM books WHERE lib_id=?", FP)[0]["book_id"]
check("KOReader's hash of the served file resolves to the book",
      gw.LibrarianDB.get_book_info_by_hash(koreader_partial_md5(f"EPUB {FP}".encode()), OWNER)[0] == fp_book)

print("\n13. EBOOK-ONLY RE-UPLOAD — the audiobook's map survives it")
before = len(q("SELECT 1 FROM sync_nodes WHERE chapter_id IN (SELECT chapter_id FROM chapters WHERE book_id=?)", fp_book))
with zipfile.ZipFile(os.path.join(work, "early.zip"), "w") as z:
    z.writestr(f"library/ebooks/{FT}/manifest.json", json.dumps({
        "book": {"lib_id": FP, "epub_path": f"library/ebooks/{FT}/{FT}.kepub.epub", "audio_state": "pending"},
        "chapters": [], "sync_nodes": []}))
    z.writestr(f"library/ebooks/{FT}/{FT}.kepub.epub", f"EPUB {FP}".encode())
upload(os.path.join(work, "early.zip"))
after = len(q("SELECT 1 FROM sync_nodes WHERE chapter_id IN (SELECT chapter_id FROM chapters WHERE book_id=?)", fp_book))
check(f"map kept ({before} -> {after} nodes)", before == after == 3)
check("still 'ready'", q("SELECT audio_state FROM books WHERE book_id=?", fp_book)[0]["audio_state"] == "ready")

print("\n14. FAILURES ARE NOT SUCCESSES")
with zipfile.ZipFile(os.path.join(work, "broken.zip"), "w") as z:
    z.writestr("library/ebooks/Broken/manifest.json", "{not json")
check("a pack that cannot be injected answers 500", upload(os.path.join(work, "broken.zip")).status_code == 500)
r = app.post(f"/books/{FP}/state", json={"state": "failed"}, headers={"X-Libra-Token": "test-token"})
check("a finished book cannot be marked failed", r.get_json()["updated"] == 0)

print("\n15. NO BOOK TEXT IS KEPT")
check("sync_nodes carry no text", not q("SELECT 1 FROM sync_nodes WHERE text_content != '' LIMIT 1"))
check("word timings carry no words", not q("SELECT 1 FROM word_timings WHERE word IS NOT NULL LIMIT 1"))

print("\n" + ("ALL SCENARIOS PASS" if ok else "SOME SCENARIOS FAILED"))
shutil.rmtree(work, ignore_errors=True)
sys.exit(0 if ok else 1)
