"""`voxie init` sets everything up: the server over SSH, narration, the watcher, the Kobo.

The user's three steps (2026-10-04): install, `voxie init`, plug in the Kobo.
Offline: SSH, the server's installer, launchd and the Kobo are faked; what is
checked is what reaches them (secrets on stdin, never in a command line) and
what init leaves behind (~/.librasync.json, .env).
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from click.testing import CliRunner  # noqa: E402

from core import envfile, links  # noqa: E402
from core.gateway_address import abs_url  # noqa: E402
from libra_cli import main as cli_main, server_setup  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


tmp = tempfile.mkdtemp()

# --- 1. .env edited in place -----------------------------------------------------
env = os.path.join(tmp, "a.env")
with open(env, "w") as f:
    f.write("# keep me\nTTS_PROVIDER=kokoro\n# RUNPOD_API_KEY=commented\nRUNPOD_TEMPLATE_ID=abc #old\nGONE=1\n")
envfile.update(env, {"TTS_PROVIDER": "cosyvoice", "RUNPOD_API_KEY": "k", "GONE": None})
text = open(env).read()
check("envfile: comments and order kept", text.startswith("# keep me\nTTS_PROVIDER=cosyvoice\n# RUNPOD_API_KEY=commented"), repr(text))
check("envfile: a missing key is appended, a None removed", "RUNPOD_API_KEY=k\n" in text and "GONE" not in text)
check("envfile: read strips a trailing comment", envfile.read(env)["RUNPOD_TEMPLATE_ID"] == "abc")
check("envfile: the file is private", oct(os.stat(env).st_mode & 0o777) == "0o600")

# --- 2. Audiobookshelf's address --------------------------------------------------
check("abs_url: plain http on the gateway's host", abs_url({"GATEWAY_IP": "1.2.3.4"}) == "http://1.2.3.4:13378")
check("abs_url: behind TLS, the public https address",
      abs_url({"GATEWAY_IP": "v.example.com", "ABS_PUBLIC_URL": "https://v.example.com:13378/"})
      == "https://v.example.com:13378")
ghost_src = open(os.path.join(ROOT, "mac_ghost.py")).read() + open(os.path.join(ROOT, "tools/healthcheck.py")).read()
check("no client builds http://<host>:13378 itself any more", ":13378" not in ghost_src)

# --- 3. SSH: what reaches the server -------------------------------------------
cmd = server_setup.ssh_command("root@1.2.3.4", "true")
check("ssh never prompts for a password", "BatchMode=yes" in cmd and "StrictHostKeyChecking=accept-new" in cmd)
r255 = lambda *a, **k: types.SimpleNamespace(returncode=255, stdout="", stderr="Permission denied (publickey).")
ok, detail = server_setup.check_ssh("root@1.2.3.4", run=r255)
check("check_ssh: a refused key is reported", not ok and "publickey" in detail, detail)

inspect = lambda folder: (lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=folder + "\n", stderr=""))
check("a stack set up by hand elsewhere stops init", server_setup.foreign_stack("root@x", run=inspect("/opt/old-stack")) == "/opt/old-stack")
check("our own stack is an update, not a conflict", server_setup.foreign_stack("root@x", run=inspect("/opt/voxie")) is None)

# A server you already run, linked over SSH (2026-10-08: no commands to type on it).
def fake_server(env_text, add_user_exists=False):
    calls = []

    def run(cmd, **kw):
        remote = " ".join(cmd[cmd.index("root@x") + 1:]) if "root@x" in cmd else ""
        calls.append(remote)
        if remote.startswith("docker inspect"):
            return mock.Mock(returncode=0, stdout="/opt/old-stack\n", stderr="")
        if remote.startswith("cat /opt/old-stack/.env"):
            return mock.Mock(returncode=0, stdout=env_text, stderr="")
        if "add-user" in remote and add_user_exists:
            return mock.Mock(returncode=1, stdout="", stderr="'anna' already exists")
        return mock.Mock(returncode=0, stdout="ok", stderr="")
    return run, calls


run, calls = fake_server("# comment\nUPLOAD_TOKEN=tok123  # the Mac's\nABS_API_TOKEN='abs-from-server'\n")
found = server_setup.link_existing("root@x", "anna", "md5key", run=run)
check("link: the server's own upload token and ABS key are read from its .env",
      found == {"upload_token": "tok123", "abs_token": "abs-from-server"}, str(found))
check("link: the account is created in the running gateway",
      any("gateway_admin.py add-user anna --userkey md5key" in c for c in calls))
run, calls = fake_server("UPLOAD_TOKEN=tok\n", add_user_exists=True)
server_setup.link_existing("root@x", "anna", "md5key", run=run)
check("link: an existing account gets the password instead", any("set-password anna --userkey md5key" in c for c in calls))
run, _ = fake_server("ABS_API_TOKEN=x\n")
try:
    server_setup.link_existing("root@x", "anna", "k", run=run)
    said = ""
except RuntimeError as e:
    said = str(e)
check("link: a server that takes no uploads says so", "no UPLOAD_TOKEN" in said, said)


def link_error(run):
    try:
        server_setup.link_existing("root@x", "anna", "k", run=run)
        return ""
    except RuntimeError as e:
        return str(e)


nothing_runs = lambda cmd, **kw: mock.Mock(returncode=1, stdout="", stderr="No such object")  # noqa: E731
check("link: a server with no Voxie running says so", "No Voxie server" in link_error(nothing_runs))
run, _ = fake_server("UPLOAD_TOKEN=t\n")
check("link: an unreadable .env says so", "Could not read" in link_error(
    lambda cmd, **kw: mock.Mock(returncode=1, stdout="", stderr="denied") if "cat" in cmd else run(cmd, **kw)))
check("link: an account the server refuses says why", "Could not create the account" in link_error(
    lambda cmd, **kw: mock.Mock(returncode=1, stdout="", stderr="database is locked") if "add-user" in cmd else run(cmd, **kw)))
check("a non-root login uses sudo", "sudo" in " ".join(
    (lambda c: c)(server_setup.ssh_command("ubuntu@x", *server_setup._sudo("ubuntu@x"), "true"))))

seen = {}
def fake_install(cmd, input=None, **kw):
    seen["cmd"], seen["input"] = cmd, input
    return types.SimpleNamespace(returncode=0, stdout="progress\nVOXIE_ABS_TOKEN=abs-key-123\n")
settings = {"abs_password": "s3cret-pass", "account_userkey": "md5", "upload_token": "up"}
token = server_setup.install("root@1.2.3.4", settings, run=fake_install)
check("install returns the server's ABS key", token == "abs-key-123")
check("secrets go on stdin, never in the command line",
      "s3cret-pass" not in " ".join(seen["cmd"]) and json.loads(seen["input"])["abs_password"] == "s3cret-pass")

# push_code ships the committed tree, through tar on the server.
repo = os.path.join(tmp, "repo")
os.makedirs(repo)
subprocess.run(["git", "init", "-q", repo], check=True)
open(os.path.join(repo, "docker-compose.yml"), "w").write("services: {}\n")
subprocess.run(["git", "-C", repo, "add", "."], check=True)
subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"], check=True)
got = {}
def fake_tar(cmd, stdin=None, **kw):
    got["cmd"], got["tar"] = cmd, stdin.read()
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")
server_setup.push_code(repo, "root@1.2.3.4", run=fake_tar)
check("push_code: a tar of the checkout, extracted in /opt/voxie",
      b"docker-compose.yml" in got["tar"] and "tar -x -C /opt/voxie" in " ".join(got["cmd"]))

# --- 4. The server's .env --------------------------------------------------------
spec = importlib.util.spec_from_file_location("install_server", os.path.join(ROOT, "server/install_server.py"))
inst = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inst)
inst.ENV = os.path.join(tmp, "server.env")
base = {"upload_token": "up", "domain": "voxie.example.com", "public_host": "1.2.3.4"}
inst.write_env(base)
e1 = envfile.read(inst.ENV)
check("domain: Caddy on, the gateway and ABS on the loopback only",
      e1.get("COMPOSE_PROFILES") == "https" and e1.get("GATEWAY_BIND", "").startswith("127.0.0.1")
      and e1.get("ABS_BIND", "").startswith("127.0.0.1") and e1["GATEWAY_PUBLIC_URL"] == "https://voxie.example.com")
check("LIBRA_SECRET_KEY generated", len(e1.get("LIBRA_SECRET_KEY", "")) > 40)
inst.write_env({**base, "domain": ""})
e2 = envfile.read(inst.ENV)
check("no domain: plain http on public ports, Caddy off",
      "COMPOSE_PROFILES" not in e2 and "GATEWAY_BIND" not in e2 and e2["GATEWAY_PUBLIC_URL"] == "http://1.2.3.4:3000")
check("re-running keeps LIBRA_SECRET_KEY (stored ABS tokens stay readable)",
      e2["LIBRA_SECRET_KEY"] == e1["LIBRA_SECRET_KEY"])

compose = open(os.path.join(ROOT, "docker-compose.yml")).read()
check("compose: Caddy only in the https profile", 'profiles: ["https"]' in compose and "caddy:2.10.2" in compose)
check("compose: the ports follow the binds", "${GATEWAY_BIND:-3000}:3000" in compose and "${ABS_BIND:-13378}:80" in compose)
check("Caddyfile: one domain, gateway and ABS", open(os.path.join(ROOT, "server/Caddyfile")).read().count("{$DOMAIN}") == 2)

# --- 5. Referral links: said once, said openly ------------------------------------
readme = open(os.path.join(ROOT, "README.md")).read()
check("README uses core/links.py's links", links.HETZNER_URL in readme and links.RUNPOD_URL in readme)
check("README names the voice director's service", links.OPENROUTER_URL in readme and "OPENROUTER_API_KEY" in readme)
check("README says they are referral links", "referral" in readme.lower())
check("README keeps the free route", "free" in readme.lower() and "your own computer" in readme.lower())
check("the export refuses an unfilled referral link",
      links.PLACEHOLDER in open(os.path.join(ROOT, "tools/export_public.sh")).read())
# The donate link is repeated in three files GitHub and FLOSS/fund read on
# their own; one that drifts sends donations nowhere.
ko_fi = links.DONATE_URL.rsplit("/", 1)[1]
funding_json = json.load(open(os.path.join(ROOT, "funding.json")))
check("README links core/links.py's donate page", links.DONATE_URL in readme)
check("FUNDING.yml names the same Ko-fi page",
      f"ko_fi: {ko_fi}" in open(os.path.join(ROOT, ".github/FUNDING.yml")).read())
check("funding.json pays into the same Ko-fi page",
      [c["address"] for c in funding_json["funding"]["channels"]] == [links.DONATE_URL])
check("funding.json's plans use only channels it declares",
      all(set(p["channels"]) <= {c["guid"] for c in funding_json["funding"]["channels"]}
          for p in funding_json["funding"]["plans"]))

# --- 6. voxie init, end to end -------------------------------------------------
home = os.path.join(tmp, "home")
root = os.path.join(tmp, "checkout")
os.makedirs(home)
os.makedirs(root)
shutil.copyfile(os.path.join(ROOT, ".env.example"), os.path.join(root, ".env.example"))
calls = []
# 203.0.113.0/24 is reserved for documentation: even a leak past the fakes
# could not reach anyone's machine.
fakes = {
    "check_ssh": lambda t: (True, "logged in"),
    "foreign_stack": lambda t: None,
    "resolves_to": lambda d, ip: True,
    "push_code": lambda r, t: calls.append(("push", t)),
    "install": lambda t, s: calls.append(("install", t, s)) or "abs-key-xyz",
}
ghost = mock.MagicMock()
kobo = mock.MagicMock()
with mock.patch.object(cli_main, "CONFIG_FILE", os.path.join(home, ".librasync.json")), \
        mock.patch.object(cli_main, "DROPZONE", os.path.join(home, "Kobo_Dropzone")), \
        mock.patch.object(cli_main, "ARCHIVE", os.path.join(home, "Kobo_Archive")), \
        mock.patch.object(cli_main, "repo_root_dir", lambda: root), \
        mock.patch.object(cli_main, "_runpod_key_works", lambda k: True), \
        mock.patch.object(cli_main, "_docker_ready", lambda: True), \
        mock.patch.object(cli_main, "_offer_voice", lambda: None), \
        mock.patch.object(cli_main, "_openrouter_key_works", lambda k: k == "sk-or-good"), \
        mock.patch.object(cli_main, "find_kobo_mount", lambda: "/Volumes/KOBOeReader"), \
        mock.patch.object(cli_main, "IS_MAC", True), \
        mock.patch.multiple(server_setup, **fakes), \
        mock.patch.object(cli_main.start_ghost, "callback", ghost), \
        mock.patch.object(cli_main.setup_kobo, "callback", kobo):
    answers = "\n".join([
        "anna", "correct horse", "correct horse",   # account
        "1", "203.0.113.10", "voxie.example.com",     # server over SSH, with a domain
        "1", "rp-key",                               # RunPod (the public template, not asked)
        "sk-or-bad", "sk-or-good",                   # the voice director: a rejected key, then a good one
        "",                                          # Kobo plugged in
    ]) + "\n"
    result = CliRunner().invoke(cli_main.cli, ["init", "--terminal"], input=answers)
check("init runs to the end", result.exit_code == 0, result.output[-600:] + repr(result.exception))
cfg = json.load(open(os.path.join(home, ".librasync.json"))) if os.path.exists(os.path.join(home, ".librasync.json")) else {}
check("init: the server was set up over SSH", [c[0] for c in calls] == ["push", "install"], str(calls)[:200])
sent = calls[1][2] if len(calls) > 1 else {}
import hashlib  # noqa: E402
check("init: one login for the Kobo and the ABS app",
      sent.get("abs_username") == "anna" and sent.get("abs_password") == "correct horse"
      and sent.get("account_userkey") == hashlib.md5(b"correct horse").hexdigest())
check("init: https address and ABS on https",
      cfg.get("GATEWAY_URL") == "https://voxie.example.com" and cfg.get("ABS_PUBLIC_URL") == "https://voxie.example.com:13378")
check("init: the ABS key reaches the config and the engine's .env",
      cfg.get("ABS_API_TOKEN") == "abs-key-xyz" and envfile.read(os.path.join(root, ".env")).get("ABS_API_TOKEN") == "abs-key-xyz")
renv = envfile.read(os.path.join(root, ".env"))
check("init: RunPod written to .env, with a GPU token",
      renv.get("TTS_PROVIDER") == "cosyvoice" and renv.get("RUNPOD_API_KEY") == "rp-key"
      and renv.get("RUNPOD_TEMPLATE_ID") == cli_main.RUNPOD_PUBLIC_TEMPLATE_ID and len(renv.get("TTS_API_TOKEN", "")) == 64)
check("init: the voice director's key, once OpenRouter accepts it",
      renv.get("OPENROUTER_API_KEY") == "sk-or-good" and "rejected" in result.output)
check("init: the watcher is started and the Kobo is set up", ghost.called and kobo.called)
check("init: no secret is echoed", "correct horse" not in result.output and "rp-key" not in result.output
      and "sk-or-good" not in result.output
      and "abs-key-xyz" not in result.output)

# --- 7. voxie init with a server you already run, linked by its login -----------------
def init_existing(link):
    home2 = os.path.join(tmp, "home2-" + str(len(calls)))
    os.makedirs(home2, exist_ok=True)
    calls.append("run")
    with mock.patch.object(cli_main, "CONFIG_FILE", os.path.join(home2, ".librasync.json")), \
            mock.patch.object(cli_main, "DROPZONE", os.path.join(home2, "Kobo_Dropzone")), \
            mock.patch.object(cli_main, "ARCHIVE", os.path.join(home2, "Kobo_Archive")), \
            mock.patch.object(cli_main, "repo_root_dir", lambda: root), \
            mock.patch.object(cli_main, "_docker_ready", lambda: True), \
            mock.patch.object(cli_main, "_offer_voice", lambda: None), \
            mock.patch.object(cli_main, "find_kobo_mount", lambda: "/Volumes/KOBOeReader"), \
            mock.patch.object(cli_main, "IS_MAC", True), \
            mock.patch.object(cli_main, "verify_abs_token", lambda base, t: (t in ("abs-from-server", "typed-key"), "ok")), \
            mock.patch.multiple(server_setup, check_ssh=lambda t: (True, ""), link_existing=link), \
            mock.patch.object(cli_main.start_ghost, "callback", mock.MagicMock()), \
            mock.patch.object(cli_main.setup_kobo, "callback", mock.MagicMock()):
        answers = ["bea", "long password", "long password", "2", "https://voxie.example.org", "root@203.0.113.30"]
        answers += ["typed-key", ""] if link is failing_link else []
        answers += ["2", ""]                         # narration on this computer, Kobo plugged in
        result = CliRunner().invoke(cli_main.cli, ["init", "--terminal"], input="\n".join(answers) + "\n")
    cfg = json.load(open(os.path.join(home2, ".librasync.json"))) if os.path.exists(os.path.join(home2, ".librasync.json")) else {}
    return result, cfg


def failing_link(t, u, k):
    raise RuntimeError("No Voxie server (a container called librarian_server) runs there.")


result, cfg = init_existing(lambda t, u, k: {"upload_token": "server-tok", "abs_token": "abs-from-server"})
check("init, existing server with its login: nothing to type on the server",
      result.exit_code == 0 and cfg.get("UPLOAD_TOKEN") == "server-tok" and cfg.get("ABS_API_TOKEN") == "abs-from-server"
      and "docker exec" not in result.output, result.output[-400:] + repr(result.exception))
result, cfg = init_existing(failing_link)
check("init: a login that cannot link falls back to the two steps by hand",
      result.exit_code == 0 and "No Voxie server" in result.output and "docker exec" in result.output
      and cfg.get("ABS_API_TOKEN") == "typed-key", result.output[-400:] + repr(result.exception))

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
