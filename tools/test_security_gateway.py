"""Layer 4 (docs/TESTING.md): the gateway attacked, endpoint by endpoint.

The real gateway app on a fresh database, attacked as an outsider (no login,
wrong logins), as a second account reaching for the first account's books,
and through crafted Sync Packs and identifiers (zip-slip, symlinks, absolute
paths, path traversal). Every route the app defines must be accounted for
here: a new route without an expectation fails this test, so nobody adds an
endpoint without deciding who may call it.

    python tools/test_security_gateway.py
"""
import base64
import io
import json
import os
import stat
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = tempfile.mkdtemp(prefix="voxie-attack-")
os.chdir(WORK)
os.makedirs("library", exist_ok=True)
os.environ.update(UPLOAD_TOKEN="owner-upload-token", ABS_URL="", ABS_API_TOKEN="")
sys.path.insert(0, ROOT)
import librerian_gateway_v3 as gw  # noqa: E402
from core import accounts  # noqa: E402

gw.UPLOAD_TOKEN = "owner-upload-token"
app = gw.app.test_client()
passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


with gw.get_db() as db:
    ALICE = accounts.create_user(db, "alice", accounts.userkey_for("alice-pw"))
    accounts.claim_unowned(db, ALICE)
    MALLORY = accounts.create_user(db, "mallory", accounts.userkey_for("mallory-pw"))
    MALLORY_UPLOAD = accounts.new_secret()
    db.execute("UPDATE users SET upload_token_hash = ? WHERE user_id = ?",
               (accounts._token_digest(MALLORY_UPLOAD), MALLORY))
    db.commit()


def kosync(user, pw):
    return {"x-auth-user": user, "x-auth-key": accounts.userkey_for(pw)}


def basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


ALICE_K, MALLORY_K = kosync("alice", "alice-pw"), kosync("mallory", "mallory-pw")
ALICE_B, MALLORY_B = basic("alice", "alice-pw"), basic("mallory", "mallory-pw")
OWNER_UP, MALLORY_UP = {"X-Libra-Token": "owner-upload-token"}, {"X-Libra-Token": MALLORY_UPLOAD}


def pack(members, title="Book", lib_id="LBR-aaaaaaaaaaaa", symlink=None):
    folder = f"library/audiobooks/{title}"
    manifest = {"book": {"lib_id": lib_id, "epub_path": f"{folder}/{title}.kepub.epub", "title": title},
                "chapters": [{"chapter_id": 1, "chapter_index": 1, "audio_filename": "001_A_One.mp3",
                              "duration": 10.0}],
                "sync_nodes": [{"chapter_id": 1, "xpath_string": "point(/1/2/1)", "start_time": 0.0,
                                "end_time": 5.0}]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{folder}/manifest.json", json.dumps(manifest))
        z.writestr(f"{folder}/{title}.kepub.epub", f"EPUB {lib_id}".encode())
        for name, data in members:
            z.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink[0])
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, symlink[1])
    return buf.getvalue()


def upload(data, headers, name="Book_SyncPack.zip"):
    return app.post("/upload", data={"file": (io.BytesIO(data), name)}, headers=headers,
                    content_type="multipart/form-data")


# --- A real book for alice --------------------------------------------------------
r = upload(pack([("library/audiobooks/Book/Chapter_01_One.mp3", b"MP3")]), OWNER_UP)
check("the owner's upload token stores a book", r.status_code == 200, f"HTTP {r.status_code}")
with gw.get_db() as db:
    HASH = db.execute("SELECT kobo_hash FROM book_hashes LIMIT 1").fetchone()[0]

# --- 1. Every route, without and with a wrong login ---------------------------------
# What each route requires. A route missing from here fails the test.
OPEN = {"/health", "/librarian/ping", "/librarian/ping/<librarian_id>", "/users/create"}
REQUESTS = {
    "/users/auth": ("GET", "/users/auth"),
    "/syncs/progress": ("PUT", "/syncs/progress"),
    "/syncs/progress/<document_hash>": ("GET", f"/syncs/progress/{HASH}"),
    "/librarian/abs": ("GET", "/librarian/abs"),
    "/librarian/audio/<document_hash>": ("GET", f"/librarian/audio/{HASH}"),
    "/librarian/map/<document_hash>": ("GET", f"/librarian/map/{HASH}"),
    "/upload": ("POST", "/upload"),
    "/upload/status/<pack_id>": ("GET", "/upload/status/" + "a" * 32),
    "/upload/part": ("POST", "/upload/part"),
    "/upload/commit": ("POST", "/upload/commit"),
    "/opds": ("GET", "/opds"),
    "/librarian/plugin.zip": ("GET", "/librarian/plugin.zip"),
    "/download/<lib_id>": ("GET", "/download/LBR-aaaaaaaaaaaa"),
    "/books/<lib_id>": ("DELETE", "/books/LBR-aaaaaaaaaaaa"),
    "/books/<lib_id>/state": ("POST", "/books/LBR-aaaaaaaaaaaa/state"),
    "/maintenance/orphans": ("GET", "/maintenance/orphans"),
}
routes = {str(r.rule) for r in gw.app.url_map.iter_rules() if not str(r.rule).startswith("/static")}
unknown = routes - OPEN - set(REQUESTS)
check("every route has a decided access rule (a new one must be added here)", not unknown, str(sorted(unknown)))
WRONG = [{}, kosync("alice", "wrong"), basic("alice", "wrong"), {"X-Libra-Token": "guess"},
         {"x-auth-user": "nobody", "x-auth-key": "x"}]
for rule, (method, path) in sorted(REQUESTS.items()):
    codes = {app.open(path, method=method, headers=h).status_code for h in map(dict, WRONG)}
    check(f"{method} {rule}: refused without a valid login", codes <= {401, 403}, str(sorted(codes)))
check("registration is closed unless the server opens it",
      app.post("/users/create", json={"username": "x", "password": "y"}).status_code == 403)

# --- 2. Another account cannot reach alice's book -----------------------------------
for method, path, headers in [("GET", f"/syncs/progress/{HASH}", MALLORY_K),
                              ("GET", f"/librarian/audio/{HASH}", MALLORY_K),
                              ("GET", f"/librarian/map/{HASH}", MALLORY_K),
                              ("GET", "/download/LBR-aaaaaaaaaaaa", MALLORY_B),
                              ("DELETE", "/books/LBR-aaaaaaaaaaaa", MALLORY_UP)]:
    r = app.open(path, method=method, headers=headers, json={"state": "failed"} if method == "POST" else None)
    check(f"mallory cannot {method} alice's {path.split('/')[1]}/{path.split('/')[2] if path.count('/') > 1 else ''}",
          r.status_code in (401, 403, 404), f"HTTP {r.status_code}")
feed = app.get("/opds", headers=MALLORY_B)
check("mallory's catalogue does not list alice's book", feed.status_code == 200 and b"LBR-aaaaaaaaaaaa" not in feed.data)
r = app.post("/books/LBR-aaaaaaaaaaaa/state", headers=MALLORY_UP, json={"state": "failed"})
check("mallory's state change touches none of alice's books", (r.get_json() or {}).get("updated") == 0, str(r.get_json()))
with gw.get_db() as db:
    deleted, state = db.execute("SELECT deleted_at, audio_state FROM books WHERE lib_id = 'LBR-aaaaaaaaaaaa'").fetchone()
check("alice's book is still there, its state unchanged", deleted is None and state == "ready", f"{deleted}, {state}")
r = app.put("/syncs/progress", headers=MALLORY_K, json={"document": HASH, "progress": "/body/p[1]",
                                                        "percentage": 0.5, "device": "x", "device_id": "x"})
with gw.get_db() as db:
    moved = db.execute("""SELECT COUNT(*) FROM progress p JOIN books b ON b.book_id = p.book_id
                          WHERE b.lib_id = 'LBR-aaaaaaaaaaaa'""").fetchone()[0]
check("mallory pushing a position for alice's fingerprint does not move alice's", moved == 0, f"HTTP {r.status_code}")

# --- 3. Crafted packs ---------------------------------------------------------------
outside = os.path.join(WORK, "pwned.txt")
for label, members, sym in [
        ("a member that climbs out with ../", [("library/../../pwned.txt", b"x")], None),
        ("an absolute path", [(outside, b"x")], None),
        ("a member outside library/", [("pwned.txt", b"x")], None),
        ("a symlink, then a write through it", [("library/audiobooks/Book/link/x.txt", b"x")],
         ("library/audiobooks/Book/link", "/tmp"))]:
    r = upload(pack(members, symlink=sym), OWNER_UP)
    check(f"a pack with {label} is refused and writes nothing",
          r.status_code >= 400 and not os.path.exists(outside) and not os.path.exists("/tmp/x.txt"),
          f"HTTP {r.status_code}")
r = upload(pack([], title="../../etc"), OWNER_UP)
check("a book title that climbs out is refused", r.status_code >= 400 and not os.path.exists(os.path.join(WORK, "..", "etc", "manifest.json")))
check("a pack name is reduced to its file name", upload(b"PK", OWNER_UP, name="../../evil.zip").status_code >= 400
      and not os.path.exists(os.path.join(WORK, "..", "evil.zip")))
check("an upload that is not a zip is refused", upload(b"not a zip", OWNER_UP).status_code >= 400)

# --- 4. Identifiers that try to climb out --------------------------------------------
for pid in ("../../x", "a" * 15, "A" * 32, "a" * 65, "a" * 30 + "/.."):
    r = app.get(f"/upload/status/{pid}", headers=OWNER_UP)
    check(f"pack id {pid[:12]!r} is refused", r.status_code in (400, 404), f"HTTP {r.status_code}")
r = app.post("/upload/part", headers={**OWNER_UP, "X-Pack-Id": "../../x", "X-Part-Index": "0"}, data=b"x")
check("a part with a climbing pack id is refused", r.status_code == 400)
r = app.post("/upload/part", headers={**OWNER_UP, "X-Pack-Id": "b" * 32, "X-Part-Index": "-1"}, data=b"x")
check("a negative part index is refused", r.status_code == 400)
for lib in ("..%2F..%2Fetc%2Fpasswd", "../../etc/passwd", "LBR-aaaaaaaaaaaa%00"):
    r = app.get(f"/download/{lib}", headers=ALICE_B)
    check(f"download {lib[:20]!r} serves nothing outside the library", r.status_code in (401, 404) and b"root:" not in r.data)

# --- 5. Uploads in progress belong to their account ------------------------------------
pid = "c" * 32
app.post("/upload/part", headers={**OWNER_UP, "X-Pack-Id": pid, "X-Part-Index": "0"}, data=b"part zero")
seen = app.get(f"/upload/status/{pid}", headers=MALLORY_UP).get_json() or {}
check("another account does not see an upload in progress", not seen.get("parts"), str(seen))
r = app.post("/upload/commit", headers=MALLORY_UP, json={"pack_id": pid, "total_parts": 1})
check("nor can it commit it", r.status_code in (404, 409), f"HTTP {r.status_code}")

# --- 6. Errors do not leak the machine -------------------------------------------------
r = upload(pack([("library/../../pwned.txt", b"x")]), OWNER_UP)
body = r.get_data(as_text=True)
check("an error names the problem, not the server's paths or a traceback",
      "Traceback" not in body and WORK not in body, body[:120])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
