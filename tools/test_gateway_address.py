"""The gateway's address comes from one place, so HTTPS is one setting.

Before plugin 1.5 every client built "http://<ip>:3000" itself -- the plugin
template, Progress sync, the catalogue, the watcher's upload, the health
check -- so a gateway behind Tailscale Funnel or Caddy could not be used at
all. DEPLOY.md listed it as a known limit.
"""
import json, os, re, sys, tempfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from core.gateway_address import as_url, gateway_url, parse_gateway  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


check("an IP keeps meaning http://<ip>:3000, so existing setups are untouched",
      gateway_url({"GATEWAY_IP": "91.99.12.34"}) == "http://91.99.12.34:3000")
check("GATEWAY_URL wins, without a trailing slash",
      gateway_url({"GATEWAY_IP": "x", "GATEWAY_URL": "https://voxie.ts.net/"}) == "https://voxie.ts.net")
check("nothing configured is None, not 'http://None:3000'", gateway_url({}) is None)
check("a full URL is kept as written", as_url("https://a.example:8443") == "https://a.example:8443")
check("init: a bare host stays a host", parse_gateway("sync.example.com") == ("sync.example.com", None))
check("init: an https address keeps its scheme and port",
      parse_gateway("https://voxie.tail1.ts.net/") == ("voxie.tail1.ts.net", "https://voxie.tail1.ts.net"))
try:
    parse_gateway("ftp://x")
    check("init: a non-web address is refused", False)
except ValueError:
    check("init: a non-web address is refused", True)

# The watcher uploads to the same address.
home = tempfile.mkdtemp()
cfg = {"GATEWAY_IP": "voxie.tail1.ts.net", "GATEWAY_URL": "https://voxie.tail1.ts.net", "UPLOAD_TOKEN": "t"}
json.dump(cfg, open(os.path.join(home, ".librasync.json"), "w"))
import mac_ghost  # noqa: E402
with mock.patch.object(mac_ghost, "CONFIG_FILE", os.path.join(home, ".librasync.json")):
    mac_ghost.configure()
check("the watcher uploads over https when the gateway is behind TLS",
      mac_ghost.DO_SERVER_URL == "https://voxie.tail1.ts.net/upload", mac_ghost.DO_SERVER_URL)

# The installer substitutes the whole URL into the plugin template.
main_src = open(os.path.join(ROOT, "libra_cli", "main.py"), encoding="utf-8").read()
check("setup-kobo writes the gateway's URL into the plugin",
      '.replace("__GATEWAY_URL__", gateway)' in main_src and "__GATEWAY_IP__" not in main_src)
check("init stores GATEWAY_URL only when a full address was given",
      'keys = {"GATEWAY_IP": host, **({"GATEWAY_URL": url} if url else {})}' in main_src)

# Nobody builds the address by hand any more.
offenders = []
for path in ("libra_cli/main.py", "mac_ghost.py", "tools/healthcheck.py"):
    for n, line in enumerate(open(os.path.join(ROOT, path), encoding="utf-8"), 1):
        if re.search(r"http://\{[^}]+\}:3000", line):
            offenders.append(f"{path}:{n}")
check("no client builds http://<host>:3000 itself", not offenders, f"{offenders}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
