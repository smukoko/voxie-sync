"""Pairing a device by hand: the plugin download and HTTPS-safe catalogue links.

Any KOReader device (a Kobo set up without a Mac, PocketBook, Kindle,
Android) pairs through KOReader's own Progress sync and catalogue; the
plugin comes from /librarian/plugin.zip, set to the gateway's own public
address. And the catalogue's download links are relative, because KOReader
resolves them against the catalogue's address: "http://<host>/download/..."
sent a device that came in over HTTPS (Tailscale Funnel) to a dead port.
"""
import base64, io, os, shutil, subprocess, sys, tempfile, zipfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
os.environ.update(UPLOAD_TOKEN="t", ABS_URL="http://abs.test", ABS_API_TOKEN="x")
os.environ.pop("GATEWAY_PUBLIC_URL", None)
sys.path.insert(0, ROOT)
import librerian_gateway_v3 as gw  # noqa: E402
from core import accounts  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


with gw.get_db() as db:
    accounts.create_user(db, "reader", accounts.userkey_for("pw"))
c = gw.app.test_client()
LOGIN = {"Authorization": "Basic " + base64.b64encode(b"reader:pw").decode()}


def plugin(headers=None):
    r = c.get("/librarian/plugin.zip", headers={**LOGIN, **(headers or {})})
    z = zipfile.ZipFile(io.BytesIO(r.data))
    return r, z.namelist(), z.read("librarian_sync.koplugin/main.lua").decode()


r = c.get("/librarian/plugin.zip")
check("anonymous: 401 with a login prompt, like the catalogue",
      r.status_code == 401 and "Basic" in r.headers.get("WWW-Authenticate", ""))
check("a wrong password is refused too",
      c.get("/librarian/plugin.zip", headers={"Authorization": "Basic " + base64.b64encode(b"reader:no").decode()})
      .status_code == 401)

r, names, lua = plugin()
check("logged in: a zip holding exactly the plugin folder",
      r.status_code == 200 and names == ["librarian_sync.koplugin/main.lua",
                                         "librarian_sync.koplugin/voxie_landing.lua"], f"{r.status_code} {names}")
check("set to the address it was downloaded from, no placeholder left",
      'local GATEWAY = ("http://localhost")' in lua and "__GATEWAY_URL__" not in lua)
template = open(os.path.join(ROOT, "librarian_sync.koplugin", "main.lua"), encoding="utf-8").read()
check("the same template the installer uses, nothing else changed",
      lua == template.replace("__GATEWAY_URL__", "http://localhost"))

_, _, lua = plugin({"X-Forwarded-Proto": "https", "X-Forwarded-Host": "voxie.tail1.ts.net"})
check("behind Tailscale Funnel it points at the https address",
      'local GATEWAY = ("https://voxie.tail1.ts.net")' in lua)
with mock.patch.dict(os.environ, {"GATEWAY_PUBLIC_URL": "https://sync.example.org/"}):
    _, _, lua = plugin({"X-Forwarded-Proto": "http"})
check("GATEWAY_PUBLIC_URL wins over anything the request says",
      'local GATEWAY = ("https://sync.example.org")' in lua)

if shutil.which("luajit"):
    path = os.path.join(tempfile.mkdtemp(), "main.lua")
    open(path, "w").write(lua)
    ok = subprocess.run(["luajit", "-e", f"assert(loadfile({path!r}))"], capture_output=True, text=True)
    check("the downloaded plugin compiles under LuaJIT", ok.returncode == 0, ok.stderr.strip()[:120])
else:
    print("SKIP  luajit not installed")

# --- the catalogue's download links --------------------------------------------
row = {"title": "Walden", "author": "Thoreau", "epub_path": "library/audiobooks/Walden/Walden.kepub.epub",
       "lib_id": "LBR-0123456789ab"}
with mock.patch.object(gw, "_books_of", return_value=[row]):
    feed = c.get("/opds", headers={**LOGIN, "X-Forwarded-Proto": "https",
                                   "X-Forwarded-Host": "voxie.tail1.ts.net"}).data.decode()
check("catalogue downloads are relative, so they follow the catalogue's https address",
      'href="/download/LBR-0123456789ab"' in feed and 'href="http' not in feed, feed[-300:])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
