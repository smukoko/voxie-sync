"""setup_kobo's sync and catalogue settings: logged in, and nothing else touched.

Runs write_sync_settings on a fake device seeded with settings files in the
exact shape a real Kobo holds (copied from the author's device on 2026-09-26,
with the login replaced), and checks what KOReader will read back.
"""
import hashlib, os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from libra_cli import luatable            # noqa: E402
from libra_cli.main import write_sync_settings   # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  ({detail})" if detail else ""))


def device(files):
    root = tempfile.mkdtemp()
    for rel, body in files.items():
        path = os.path.join(root, ".adds/koreader", rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").write(body)
    return root


def read(root, rel):
    return luatable.loads(open(os.path.join(root, ".adds/koreader", rel)).read())


KOSYNC = '''-- ./settings/kosync.lua
return {
    ["settings"] = {
        ["auto_sync"] = true,
        ["checksum_method"] = 0,
        ["custom_server"] = "http://1.2.3.4:3000",
        ["pages_before_update"] = 10,
        ["send_metadata"] = false,
        ["sync_backward"] = 1,
        ["sync_forward"] = 2,
        ["userkey"] = "202cb962ac59075b964b07152d234b70",
        ["username"] = "123",
    },
}'''
OPDS = '''-- ./settings/opds.lua
return {
    ["downloads"] = {},
    ["pending_syncs"] = {},
    ["servers"] = {
        [1] = {
            ["title"] = "Project Gutenberg",
            ["url"] = "https://m.gutenberg.org/ebooks.opds/?format=opds",
        },
        [2] = {
            ["title"] = "My Calibre",
            ["url"] = "http://192.168.1.5:8080/opds",
            ["username"] = "me",
            ["password"] = "secret",
        },
    },
}'''
SETTINGS = '''return {
    ["cre_font"] = "Literata",
    ["wifi_enable_action"] = "prompt",
    ["lastfile"] = "/mnt/onboard/book.epub",
}'''

root = device({"settings/kosync.lua": KOSYNC, "settings/opds.lua": OPDS, "settings.reader.lua": SETTINGS})
write_sync_settings(root, "9.9.9.9", "reader", "pw!", log=lambda *a, **k: None)

k = read(root, "settings/kosync.lua")["settings"]
check("progress sync points at the gateway", k["custom_server"] == "http://9.9.9.9:3000")
check("and is logged in with username + md5(password), as KOReader does",
      k["username"] == "reader" and k["userkey"] == hashlib.md5(b"pw!").hexdigest())
check("the reader's other sync settings survive", k["checksum_method"] == 0 and k["send_metadata"] is False)

o = read(root, "settings/opds.lua")
servers = [o["servers"][i] for i in sorted(o["servers"])]
ours = [s for s in servers if s["url"] == "http://9.9.9.9:3000/opds"]
check("the Voxie catalogue is added to an existing list", len(ours) == 1 and servers[0] is ours[0])
check("...with the login KOReader sends as HTTP Basic", ours[0].get("username") == "reader" and ours[0].get("password") == "pw!")
check("the reader's own catalogues and their logins survive",
      any(s["title"] == "My Calibre" and s["password"] == "secret" for s in servers) and len(servers) == 3)
check("downloads and pending syncs survive", o["downloads"] == {} and o["pending_syncs"] == {})

g = read(root, "settings.reader.lua")
check("Wi-Fi is set to turn on for auto-sync", g["wifi_enable_action"] == "turn_on")
check("nothing else in the global settings moves", g["cre_font"] == "Literata" and g["lastfile"] == "/mnt/onboard/book.epub")

write_sync_settings(root, "9.9.9.9", "reader", "new-pw", log=lambda *a, **k: None)
o = read(root, "settings/opds.lua")
check("running it again updates the login instead of adding a duplicate",
      sum(1 for s in o["servers"].values() if s["url"].startswith("http://9.9.9.9")) == 1
      and read(root, "settings/kosync.lua")["settings"]["userkey"] == hashlib.md5(b"new-pw").hexdigest())

# The gateway moves behind HTTPS (Tailscale Funnel): one re-pairing moves
# Progress sync and the catalogue, and leaves no stale http entry behind.
write_sync_settings(root, "https://voxie.example.ts.net/", "reader", "new-pw", log=lambda *a, **k: None)
k = read(root, "settings/kosync.lua")["settings"]
o = read(root, "settings/opds.lua")
voxie = [s for s in o["servers"].values() if s.get("title") == "Voxie"]
check("an https:// gateway is used as given, for Progress sync",
      k["custom_server"] == "https://voxie.example.ts.net", k["custom_server"])
check("and for the catalogue, replacing the old http entry rather than adding one",
      len(voxie) == 1 and voxie[0]["url"] == "https://voxie.example.ts.net/opds"
      and not any(s["url"].startswith("http://9.9.9.9") for s in o["servers"].values()), voxie)
check("the reader's other catalogues survive the move",
      any(s["url"] == "http://192.168.1.5:8080/opds" for s in o["servers"].values()))

fresh = device({})
write_sync_settings(fresh, "9.9.9.9", "reader", "pw", log=lambda *a, **k: None)
check("a brand-new device gets all three files", read(fresh, "settings/opds.lua")["servers"][1]["title"] == "Voxie"
      and read(fresh, "settings/kosync.lua")["settings"]["auto_sync"] is True)

try:
    write_sync_settings(device({}), "9.9.9.9", None, None, log=lambda *a, **k: None)
    check("without an account it refuses rather than writing a device that cannot log in", False)
except Exception:
    check("without an account it refuses rather than writing a device that cannot log in", True)

print("=" * 60)
print(f"{len(results)} checks: {sum(results)} passed, {len(results) - sum(results)} FAILED")
sys.exit(0 if all(results) else 1)
