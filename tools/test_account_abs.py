"""Each account's own Audiobookshelf: encrypted at rest, public addresses only.

The last blocker for a paid beta: an account other than the server's owner
syncs with its own ABS, and the token that opens that person's library is
never stored readable. Offline: ABS is mocked, DNS is patched.
"""
import os, socket, sys, tempfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
from cryptography.fernet import Fernet  # noqa: E402
KEY1, KEY2 = Fernet.generate_key().decode(), Fernet.generate_key().decode()
os.environ.update(UPLOAD_TOKEN="t", ABS_URL="http://abs.internal", ABS_API_TOKEN="owner-token",
                  LIBRA_SECRET_KEY=KEY1)
sys.path.insert(0, ROOT)
import librerian_gateway_v3 as gw  # noqa: E402
from core import abs_settings, accounts  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


with gw.get_db() as db:
    OWNER = accounts.create_user(db, "owner", accounts.userkey_for("pw"))
    BOB = accounts.create_user(db, "bob", accounts.userkey_for("bobpw"))
BOB_AUTH = {"x-auth-user": "bob", "x-auth-key": accounts.userkey_for("bobpw")}
OWNER_AUTH = {"x-auth-user": "owner", "x-auth-key": accounts.userkey_for("pw")}
c = gw.app.test_client()
PUBLIC = "https://93.184.215.14"     # an IP literal: resolves without the network


def abs_answers(status=200, body=None, headers=None):
    r = mock.Mock(status_code=status, ok=200 <= status < 300, headers=headers or {})
    r.json.return_value = body if body is not None else {"username": "bob-in-abs"}
    return r


def raw_token(user_id):
    with gw.get_db() as db:
        return db.execute("SELECT abs_token FROM users WHERE user_id = ?", (user_id,)).fetchone()[0]


# --- The addresses an account may name ----------------------------------------
for bad in ("http://127.0.0.1:13378", "http://10.0.0.5", "http://192.168.1.2", "http://169.254.169.254",
            "http://[::1]", "http://localhost", "ftp://93.184.215.14", "https://user:pw@93.184.215.14"):
    try:
        abs_settings.check_url(bad)
        ok = False
    except ValueError:
        ok = True
    check(f"refuses {bad}", ok)
check("accepts a public address", abs_settings.check_url(PUBLIC + "/abs/") == PUBLIC + "/abs")
check("the owner may name its own private ABS",
      abs_settings.check_url("http://audiobookshelf:80", allow_private=True) == "http://audiobookshelf:80")

# --- Setting it through the gateway --------------------------------------------
r = c.put("/librarian/abs", json={"url": PUBLIC, "token": "bob-secret-token"})
check("no login, no settings", r.status_code == 401)

with mock.patch.object(gw.requests, "get", return_value=abs_answers()) as get:
    r = c.put("/librarian/abs", headers=BOB_AUTH, json={"url": PUBLIC, "token": "bob-secret-token"})
check("an account sets its own ABS", r.status_code == 200 and r.get_json()["abs_user"] == "bob-in-abs",
      f"({r.status_code} {r.get_json()})")
check("the token is verified against that ABS without following redirects",
      get.call_args.args[0] == f"{PUBLIC}/api/me" and get.call_args.kwargs.get("allow_redirects") is False)
stored = raw_token(BOB)
check("the token is stored encrypted", stored.startswith("fernet:") and "bob-secret-token" not in stored)
check("no answer contains the token", "bob-secret-token" not in r.get_data(as_text=True)
      and "bob-secret-token" not in c.get("/librarian/abs", headers=BOB_AUTH).get_data(as_text=True))
check("the gateway uses it for that account", gw.abs_for(BOB) == (PUBLIC, "bob-secret-token"))
check("the owner still uses the server's ABS", gw.abs_for(OWNER) == ("http://abs.internal", "owner-token"))

with mock.patch.object(gw.requests, "get", return_value=abs_answers()):
    r = c.put("/librarian/abs", headers=BOB_AUTH, json={"url": "http://10.1.2.3", "token": "x"})
check("a private address is refused for a customer", r.status_code == 400 and raw_token(BOB) == stored)

with mock.patch.object(gw.requests, "get", return_value=abs_answers(401)):
    r = c.put("/librarian/abs", headers=BOB_AUTH, json={"url": PUBLIC, "token": "wrong"})
check("a token ABS refuses is not stored", r.status_code == 400 and raw_token(BOB) == stored, f"({r.get_json()})")

with mock.patch.object(gw.requests, "get", return_value=abs_answers(302, headers={"Location": "http://169.254.169.254/"})):
    r = c.put("/librarian/abs", headers=BOB_AUTH, json={"url": PUBLIC, "token": "x"})
check("an ABS that redirects is refused, not followed", r.status_code == 400 and "redirects" in r.get_json()["error"])

# --- Every later call is checked again, and never follows a redirect -----------
with gw.get_db() as db:
    book = db.execute("INSERT INTO books (user_id, lib_id, abs_id, audio_state) VALUES (?, 'LBR-b', 'li_b', 'ready')",
                      (BOB,)).lastrowid
    db.execute("INSERT INTO book_hashes (kobo_hash, book_id) VALUES ('HB', ?)", (book,))
    db.commit()
# The item exists (the gateway checks its cached id is still there); no progress yet.
with mock.patch.object(gw.requests, "get",
                       side_effect=lambda url, **kw: abs_answers(200 if "/api/items/" in url else 404)) as get:
    c.get("/syncs/progress/HB", headers=BOB_AUTH)
check("a pull asks the account's own ABS, with its token, without redirects",
      get.call_args.args[0] == f"{PUBLIC}/api/me/progress/li_b"
      and all(call.args[0].startswith(PUBLIC) and call.kwargs["headers"]["Authorization"] == "Bearer bob-secret-token"
              and call.kwargs.get("allow_redirects") is False for call in get.call_args_list),
      f"({get.call_args_list})")

real_gai = socket.getaddrinfo
with mock.patch.object(socket, "getaddrinfo",
                       lambda host, *a, **k: [(2, 1, 6, "", ("10.0.0.9", 443))] if host == "abs.bob.test" else real_gai(host, *a, **k)):
    with gw.get_db() as db:
        db.execute("UPDATE users SET abs_url = 'https://abs.bob.test' WHERE user_id = ?", (BOB,))
        db.commit()
    check("an address that now resolves privately is not used", gw.abs_for(BOB) == (None, None))
with gw.get_db() as db:
    db.execute("UPDATE users SET abs_url = ? WHERE user_id = ?", (PUBLIC, BOB))
    db.commit()

# --- The key --------------------------------------------------------------------
os.environ["LIBRA_SECRET_KEY"] = KEY2
check("a different key cannot read the token", gw.abs_for(BOB) == (None, None))
os.environ["LIBRA_SECRET_KEY"] = f"{KEY2},{KEY1}"
check("with the old key listed second it still reads", gw.abs_for(BOB) == (PUBLIC, "bob-secret-token"))
with gw.get_db() as db:
    check("rotate re-encrypts every token", abs_settings.rotate(db) == 1)
os.environ["LIBRA_SECRET_KEY"] = KEY2
check("after rotation the new key alone reads it", gw.abs_for(BOB) == (PUBLIC, "bob-secret-token"))

del os.environ["LIBRA_SECRET_KEY"]
with mock.patch.object(gw.requests, "get", return_value=abs_answers()):
    r = c.put("/librarian/abs", headers=BOB_AUTH, json={"url": PUBLIC, "token": "new"})
check("without a key nothing is stored in plain text", r.status_code == 503 and raw_token(BOB).startswith("fernet:"))
os.environ["LIBRA_SECRET_KEY"] = KEY2

with gw.get_db() as db:
    db.execute("UPDATE users SET abs_token = 'legacy-plain' WHERE user_id = ?", (OWNER,))
    db.commit()
    n = abs_settings.encrypt_existing(db)
check("a token stored in plain text is encrypted once a key exists",
      n == 1 and raw_token(OWNER).startswith("fernet:") and abs_settings.decrypt(raw_token(OWNER)) == "legacy-plain")

r = c.delete("/librarian/abs", headers=BOB_AUTH)
check("an account can forget its ABS", r.status_code == 200 and raw_token(BOB) is None and gw.abs_for(BOB) == (None, None))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
