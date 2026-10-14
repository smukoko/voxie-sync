#!/usr/bin/env python3
"""Set up, or update, the Voxie server on a Debian or Ubuntu machine.

`voxie init` runs this as root over SSH, after copying the code to /opt/voxie:

    ssh root@<server> python3 /opt/voxie/server/install_server.py  < settings.json

The settings arrive as JSON on stdin, never in argv, where they would show in
`ps` and in shell history: upload_token, account_username, account_userkey,
abs_username, abs_password, domain ("" for plain http) and public_host (the
server's IP). Progress goes to stderr, so the person running `voxie init`
watches it; stdout carries one line, VOXIE_ABS_TOKEN=<key>, for the Mac.

Safe to re-run: it is also how a server is updated. Existing secrets, the
library and Audiobookshelf's database are kept. Standard library only: a fresh
server has python3 and nothing else of ours.
"""
import base64
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)
sys.path.insert(0, os.path.join(APP, "tools"))

from core import envfile  # noqa: E402

ENV = os.path.join(APP, ".env")
GATEWAY = "http://127.0.0.1:3000"
ABS_PLAIN_PORT, ABS_LOCAL_PORT = 13378, 13379   # 13379: behind Caddy, loopback only


def log(message=""):
    print(message, file=sys.stderr, flush=True)


def run(cmd, check=True, **kw):
    """Run with output on stderr (stdout is reserved for the token line)."""
    if not kw.get("capture_output"):
        kw.setdefault("stdout", sys.stderr)
    r = subprocess.run(cmd, **kw)
    if check and r.returncode != 0:
        raise SystemExit(f"❌ {' '.join(cmd)} failed (exit {r.returncode}).")
    return r


def ensure_docker():
    if shutil.which("docker") and subprocess.run(["docker", "compose", "version"],
                                                 capture_output=True).returncode == 0:
        return
    log("🐳 Installing Docker (get.docker.com, Docker's own installer) ...")
    with tempfile.NamedTemporaryFile("wb", suffix=".sh", delete=False) as f:
        f.write(urllib.request.urlopen("https://get.docker.com", timeout=60).read())  # nosec B310: fixed https URL
    run(["sh", f.name])
    os.unlink(f.name)


def write_env(s):
    current = envfile.read(ENV)
    if not os.path.exists(ENV):
        with open(ENV, "w") as f:
            f.write("# The Voxie server's settings, written by server/install_server.py.\n"
                    "# Re-running `voxie init` updates them and keeps the rest.\n")
    domain = s["domain"]
    changes = {
        "TZ": current.get("TZ") or "UTC",
        "PORT": "3000",
        "UPLOAD_TOKEN": s["upload_token"],
        "DOMAIN": domain or None,
        "COMPOSE_PROFILES": "https" if domain else None,
        # Behind Caddy, the gateway and ABS answer only on the loopback, so
        # nothing reaches them unencrypted from outside.
        "GATEWAY_BIND": "127.0.0.1:3000" if domain else None,
        "ABS_BIND": f"127.0.0.1:{ABS_LOCAL_PORT}" if domain else None,
        # The address the gateway gives out for /librarian/plugin.zip.
        "GATEWAY_PUBLIC_URL": f"https://{domain}" if domain else f"http://{s['public_host']}:3000",
    }
    if not current.get("LIBRA_SECRET_KEY"):
        # Encrypts each account's own ABS token. Generated here and never
        # printed; without it stored tokens cannot be read back.
        changes["LIBRA_SECRET_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    envfile.update(ENV, changes)


def wait_for(url, what, seconds=240):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:  # nosec B310: our own local services
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(3)
    log(f"   ⚠️  {what} did not answer at {url} within {seconds}s.")
    return False


def abs_token_works(abs_base, token):
    if not token or token.startswith("your_"):
        return False
    req = urllib.request.Request(f"{abs_base}/api/libraries",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # nosec B310: this server's own ABS
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def ensure_abs_token(s, abs_base):
    token = envfile.read(ENV).get("ABS_API_TOKEN", "")
    if abs_token_works(abs_base, token):
        log("📚 Audiobookshelf: already set up, key still valid.")
        return token
    log("📚 Setting up Audiobookshelf (admin, library, API key) ...")
    from bootstrap_abs import AbsError, bootstrap
    try:
        token = bootstrap(abs_base, s["abs_username"], s["abs_password"],
                          reset_key=True, log=log)
    except AbsError as e:
        raise SystemExit(
            f"❌ Audiobookshelf: {e}. If it already has an admin with another password, "
            "run `voxie init` again with that password.")
    envfile.update(ENV, {"ABS_API_TOKEN": token})
    return token


def ensure_account(s):
    user, key = s["account_username"], s["account_userkey"]
    base = ["docker", "exec", "librarian_server", "python", "tools/gateway_admin.py"]
    r = subprocess.run(base + ["add-user", user, "--userkey", key], capture_output=True, text=True)
    if r.returncode == 0:
        log(f"👤 Account '{user}' created.")
        return
    if "already exists" in (r.stderr + r.stdout):
        run(base + ["set-password", user, "--userkey", key], capture_output=True)
        log(f"👤 Account '{user}': password updated.")
        return
    raise SystemExit(f"❌ Could not create the account: {(r.stderr or r.stdout).strip()}")


def main():
    s = json.load(sys.stdin)
    os.chdir(APP)
    ensure_docker()
    write_env(s)

    if not s["domain"]:
        # A server moving from https back to plain http: a profiled-out Caddy
        # is not stopped by `up`, and it would keep port 13378 from ABS.
        subprocess.run(["docker", "rm", "-f", "voxie_caddy"], capture_output=True)
    log("🚀 Starting the gateway and Audiobookshelf (the first build takes a few minutes) ...")
    run(["docker", "compose", "up", "-d", "--build"])
    abs_base = f"http://127.0.0.1:{ABS_LOCAL_PORT if s['domain'] else ABS_PLAIN_PORT}"
    if not wait_for(f"{abs_base}/status", "Audiobookshelf"):
        raise SystemExit("❌ Audiobookshelf did not start: docker compose logs audiobookshelf")
    wait_for(f"{GATEWAY}/health", "The gateway")

    token = ensure_abs_token(s, abs_base)
    ensure_account(s)

    # The gateway reads ABS_API_TOKEN at start, and keeps verified logins in
    # memory: a recreate gives it the new key and forgets a changed password.
    run(["docker", "compose", "up", "-d", "--force-recreate", "librarian"])
    if not wait_for(f"{GATEWAY}/health", "The gateway"):
        raise SystemExit("❌ The gateway did not come back: docker compose logs librarian")

    if s["domain"]:
        log(f"🔒 Waiting for the certificate for {s['domain']} ...")
        if wait_for(f"https://{s['domain']}/health", "HTTPS", seconds=180):
            log(f"   ✅ https://{s['domain']} answers.")
        else:
            log("   Check that ports 80 and 443 are open: docker compose logs caddy")

    log("✅ Server ready.")
    print(f"VOXIE_ABS_TOKEN={token}", flush=True)


if __name__ == "__main__":
    main()
