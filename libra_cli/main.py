import click
import hashlib
import json
import os
import requests
import secrets
import shutil
import zipfile
import io
import sys
import subprocess
import tempfile
import re
import time

from core import platform_support
from core.platform_support import IS_LINUX, IS_MAC
from core import accounts, envfile, links
from core.gateway_address import abs_url, as_url, gateway_url, parse_gateway
from libra_cli import voice_script

# Define paths
CONFIG_FILE = os.path.expanduser("~/.librasync.json")
DROPZONE = os.path.expanduser("~/Kobo_Dropzone")
ARCHIVE = os.path.expanduser("~/Kobo_Archive")

VOLUMES_DIR = "/Volumes"

# KOReader's kosync sync strategies, from plugins/kosync.koplugin/main.lua.
# Stored as bare integers in kosync.lua, so the names have to live here.
KOSYNC_PROMPT = 1
KOSYNC_SILENT = 2
KOSYNC_DISABLE = 3
# kosync's CHECKSUM_METHOD.BINARY: match a book by its contents (partialMD5).
KOSYNC_CHECKSUM_BINARY = 0
# How many pages turned before progress is pushed automatically.
KOSYNC_PAGES_BEFORE_UPDATE = 10

# Pinned, with the sha256 GitHub publishes for each asset. "Latest" meant every
# install could get a KOReader nobody had tested against the FAT gate or the
# plugin, and a compromised release would have gone straight onto the device.
# Bump deliberately: run tools/check_kobo_fat.sh and tools/kosim on the new one.
KOREADER_TAG = "v2026.07.1"
KOREADER_ASSET = f"koreader-kobo-{KOREADER_TAG}.zip"
KOREADER_SHA256 = "0f36a62ce73b12516f969e4ad7862cc06920afb03c7bd4db29a0d990bdd84b2f"
NICKELMENU_TAG = "v0.6.0"
NICKELMENU_SHA256 = "322ff9aa863860e8f5f7e0b55cae561c54bf95983b9bce1d19819d1225d064af"


def download_pinned(url, sha256):
    """Fetch a release asset and refuse it unless it is byte-for-byte the pinned one."""
    import hashlib
    r = requests.get(url, timeout=(20, 600))
    r.raise_for_status()
    got = hashlib.sha256(r.content).hexdigest()
    if got != sha256:
        raise Exception(f"{url} has sha256 {got}, expected {sha256}. Refusing to install it.")
    return r.content


def find_kobo_mount():
    """Return the path of a mounted Kobo, or None.

    Detects by signature rather than volume name. Users rename their Kobo and
    the default label varies across models and firmware, so matching only
    "/Volumes/KOBOeReader" reports "no Kobo" for a device that is plugged in and
    working. Every Kobo carries a .kobo/ directory at the volume root, which is
    the reliable marker.
    """
    if not os.path.isdir(VOLUMES_DIR):
        return None

    candidates = []
    for name in sorted(os.listdir(VOLUMES_DIR)):
        path = os.path.join(VOLUMES_DIR, name)
        try:
            if os.path.isdir(os.path.join(path, ".kobo")):
                candidates.append(path)
        except OSError:
            # Unreadable or disappearing mounts must not break detection.
            continue

    if not candidates:
        return None

    # Prefer the stock name when several volumes qualify.
    stock = os.path.join(VOLUMES_DIR, "KOBOeReader")
    return stock if stock in candidates else candidates[0]


def verify_abs_token(abs_base, token):
    """Return (ok, detail) for an Audiobookshelf API token.

    Catches the mistakes that are otherwise silent: the prompt hides input, so a
    pasted IP address or a token whose API key was later deleted both look fine
    until the gateway starts returning 401s that read like a routing problem.
    """
    url = f"{abs_base}/api/libraries"
    try:
        res = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
    except requests.exceptions.RequestException as e:
        # Not the token's fault -- the server may simply not be up yet.
        return True, f"could not reach Audiobookshelf to check ({type(e).__name__}); assuming it is correct"
    if res.status_code == 200:
        libs = res.json().get("libraries", [])
        return True, f"{len(libs)} librar{'y' if len(libs) == 1 else 'ies'} visible"
    if res.status_code in (401, 403):
        return False, "Audiobookshelf rejected that token. Check you pasted the API key, not the address."
    return False, f"Audiobookshelf answered HTTP {res.status_code}."


def extract_verified(zip_bytes, dest):
    """Extract an archive onto the Kobo without tripping the macOS FAT bug.

    Writing this archive to FAT32 with Python's zipfile (or /usr/bin/unzip)
    produces a filesystem macOS considers valid and Linux does not: the msdosfs
    driver hands the *same* 8.3 short name to two different long filenames in
    the same directory. Measured on koreader-kobo-v2026.07.1, eight collisions,
    always the same ones:

        filemanager.lua            <-> filemanagerbookinfo.lua
        NotoNaskhArabic-Bold.ttf   <-> NotoNaskhArabic-Regular.ttf
        readeractivityindicator.lua<-> readerannotation.lua
        dis_arm64.lua              <-> dis_arm64be.lua
        KOSyncClient.lua           <-> KOSyncQueue.lua
        event_map_keyboard.lua     <-> event_map_kindle4.lua
        ... and two more

    The Kobo runs Linux fsck.vfat at boot, which finds the duplicate directory
    entries and renames the second of each pair to FSCK0000.000. KOReader then
    dies on launch with "module 'apps/filemanager/filemanagerbookinfo' not
    found". Nothing is corrupt -- the file contents are intact inside the
    FSCK*.000 blobs -- but the install is dead, and the Mac shows no sign of it
    because fsck_msdos does not check for duplicate short names.

    ditto is Apple's own archive tool and does not produce the collisions.
    Verified by extracting into a FAT32 disk image and running the same Linux
    fsck.vfat the device runs: zipfile 8 duplicates, unzip 8 duplicates,
    ditto 0, reproducibly. So extraction goes through ditto.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        expected = [m for m in z.infolist() if not m.is_dir()]

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp.write(zip_bytes)
        tmp_path = tmp.name
    try:
        result = subprocess.run(
            ["ditto", "-x", "-k", tmp_path, dest], capture_output=True, text=True
        )
        if result.returncode != 0:
            raise Exception(
                f"ditto failed to extract the archive: {result.stderr.strip()}"
            )
    finally:
        os.unlink(tmp_path)

    problems = []
    for member in expected:
        target = os.path.join(dest, member.filename)
        if not os.path.exists(target):
            problems.append(f"missing: {member.filename}")
        elif os.path.getsize(target) != member.file_size:
            problems.append(
                f"truncated: {member.filename} "
                f"({os.path.getsize(target)} of {member.file_size} bytes)"
            )
    return len(expected), problems


def flush_volume(mount_path):
    """Force buffered writes out to the device.

    macOS caches writes to removable media aggressively. Without this the files
    can still be sitting in RAM when the user pulls the cable, which is exactly
    how the FAT ends up inconsistent.
    """
    subprocess.run(["sync"], check=False)
    # A second sync after a beat: the first returns once writes are queued to
    # the device, not once the device has acknowledged them.
    time.sleep(2)
    subprocess.run(["sync"], check=False)


# NickelMenu on the Kobo (firmware 4.23.15505+ shows it as a button in the
# bottom bar). The file keeps the name earlier installs used, so a device is
# edited in place rather than given a second file (8.3 short-name
# collisions on FAT, docs/LESSONS.md); the icons use 8.3-safe names.
NM_CONFIG = ".adds/nm/koreader"
VOXIE_ICON_DIR = ".adds/voxie"
_KOREADER_SH = "/mnt/onboard/.adds/koreader/koreader.sh"
_LEGACY_NM_LINE = f"menu_item:main:KOReader:cmd_spawn:quiet:exec {_KOREADER_SH}"


def repo_root_dir():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def nickelmenu_config():
    """The button says Voxie, with its icon; the label inside credits KOReader."""
    icons = f"/mnt/onboard/{VOXIE_ICON_DIR}"
    return (
        "# Written by Voxie (voxie setup-kobo). Voxie runs on KOReader.\n"
        "experimental:menu_main_15505_label:Voxie\n"
        f"experimental:menu_main_15505_icon:{icons}/nav.png\n"
        f"experimental:menu_main_15505_icon_active:{icons}/navon.png\n"
        f"menu_item:main:Voxie (KOReader):cmd_spawn:quiet:exec {_KOREADER_SH}\n"
    )


def nickelmenu_is_ours(kobo_path):
    """True when the NickelMenu file holds only what a Voxie install wrote."""
    try:
        with open(os.path.join(kobo_path, NM_CONFIG), encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")]
    except OSError:
        return False
    ours = {l for l in nickelmenu_config().splitlines() if l and not l.startswith("#")}
    return bool(lines) and all(l == _LEGACY_NM_LINE or l in ours for l in lines)


def write_nickelmenu(kobo_path, repo_root):
    """The NickelMenu entry and the bottom-bar icons (grey, and black when selected)."""
    icon_dir = os.path.join(kobo_path, VOXIE_ICON_DIR)
    os.makedirs(icon_dir, exist_ok=True)
    for src, dest in (("kobo-nav.png", "nav.png"), ("kobo-nav-active.png", "navon.png")):
        shutil.copyfile(os.path.join(repo_root, "assets", "logo", src), os.path.join(icon_dir, dest))
    nm_path = os.path.join(kobo_path, NM_CONFIG)
    os.makedirs(os.path.dirname(nm_path), exist_ok=True)
    with open(nm_path, "w", encoding="utf-8") as f:
        f.write(nickelmenu_config())


def installed_koreader_version(koreader_path):
    """'v2026.07.1' from KOReader's own git-rev file, or None."""
    try:
        with open(os.path.join(koreader_path, "git-rev"), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def koreader_is_older(installed, tested):
    """True when an installed 'v2025.10-12-gabc' predates 'v2026.07.1'. Unknown is not older."""
    def key(tag):
        m = re.match(r"v?(\d+)\.(\d+)(?:\.(\d+))?", tag or "")
        return tuple(int(x or 0) for x in m.groups()) if m else None
    a, b = key(installed), key(tested)
    return bool(a and b and a < b)


def write_sync_settings(kobo_path, gateway, username, password, log=print):
    """Point the device's progress sync and catalogue at the gateway, logged in.

    Edits KOReader's own settings files through libra_cli.luatable, touching
    only the keys it owns, so whatever else the reader configured survives.
    """
    # A bare IP still means http://<ip>:3000 (core/gateway_address.py).
    sync_url = as_url(gateway)
    settings_dir = os.path.join(kobo_path, ".adds/koreader/settings")
    os.makedirs(settings_dir, exist_ok=True)
    from libra_cli.reading_profile import _load, _write

    # The account the gateway knows this reader by. KOReader's progress
    # sync logs in with username + md5(password) on every call, and the
    # catalogue with the same pair over HTTP Basic -- so writing both here
    # is what makes pairing one step rather than "register on the device".
    if not (username and password):
        raise Exception("No ACCOUNT_USERNAME / ACCOUNT_PASSWORD in ~/.librasync.json. "
                        "Run 'voxie init' and create the account on the gateway first.")
    userkey = accounts.userkey_for(password)

    # Progress sync lives in settings/kosync.lua under "settings"
    # (plugins/kosync.koplugin/main.lua). Forward syncs silently -- you
    # listened on and came back; backward asks, because an audio position
    # behind your page usually means you re-listened to something.
    kosync_path = os.path.join(settings_dir, "kosync.lua")
    kosync = _load(kosync_path)
    ks = kosync.setdefault("settings", {})
    # A reader who already used Progress sync may have it pointed elsewhere,
    # or matching by file name -- KOReader offers both. The gateway pairs a
    # book by its binary fingerprint (core/fingerprint.py), so file-name
    # matching would never find a book, silently.
    previous = ks.get("custom_server")
    if previous and previous.rstrip("/") != sync_url:
        log(f"   ↪️  Progress sync was set to {previous}; it now syncs with Voxie.", fg="yellow")
    if ks.get("checksum_method", KOSYNC_CHECKSUM_BINARY) != KOSYNC_CHECKSUM_BINARY:
        log("   ↪️  Document matching was by file name; set to Binary, which Voxie pairs by.",
            fg="yellow")
    ks.update({"custom_server": sync_url, "auto_sync": True,
               "checksum_method": KOSYNC_CHECKSUM_BINARY,
               "pages_before_update": KOSYNC_PAGES_BEFORE_UPDATE,
               "sync_forward": KOSYNC_SILENT, "sync_backward": KOSYNC_PROMPT,
               "username": username, "userkey": userkey})
    _write(kosync_path, kosync, "./settings/kosync.lua")
    log(f"   ✅ Progress sync: {sync_url} as '{username}' (automatic).", fg="green")

    # auto_sync is refused at startup unless wifi_enable_action is exactly
    # "turn_on" ("Disable auto-sync if beforeWifiAction was reset to prompt
    # behind our back"), and that key lives in the global settings file.
    settings_path = os.path.join(kobo_path, ".adds/koreader/settings.reader.lua")
    global_settings = _load(settings_path)
    global_settings["wifi_enable_action"] = "turn_on"
    _write(settings_path, global_settings, "./settings.reader.lua")
    log("   ✅ Wi-Fi set to connect on demand, so auto-sync stays enabled.", fg="green")

    # The catalogue, with the account's login. An existing opds.lua used to
    # be left alone entirely, so a device that had ever opened KOReader's
    # catalogue browser never got this one; now it is added or updated.
    opds_path = os.path.join(settings_dir, "opds.lua")
    opds = _load(opds_path) or {"servers": {1: {"title": "Project Gutenberg",
                                                "url": "https://m.gutenberg.org/ebooks.opds/?format=opds"},
                                            2: {"title": "Standard Ebooks",
                                                "url": "https://standardebooks.org/feeds/opds"}}}
    servers = opds.setdefault("servers", {})
    ours = f"{sync_url}/opds"
    # Our entry by address, or by name: re-pairing after the gateway moved to
    # HTTPS must update the entry, not add a second "Voxie" beside the old one.
    entry = next((v for v in servers.values() if isinstance(v, dict) and v.get("url") == ours), None) \
        or next((v for v in servers.values() if isinstance(v, dict) and v.get("title") == "Voxie"), None)
    if entry is None:
        servers = {1: {"title": "Voxie", "url": ours},
                   **{i + 2: v for i, v in enumerate(v for _, v in sorted(servers.items()))}}
        opds["servers"] = servers
        entry = servers[1]
    entry.update({"title": "Voxie", "url": ours, "username": username, "password": password})
    _write(opds_path, opds, "./settings/opds.lua")
    log("   ✅ Voxie catalogue added, with your login.", fg="green")



@click.group()
def cli():
    """Voxie: read on your Kobo, listen in Audiobookshelf, never lose your place."""
    pass

# The public RunPod template (LAUNCH_PLAN 4.3-4.4), made public by the user on
# 2026-10-07: cosyvoice-tts:4.1, no token (the engine sends each user's own),
# no start command, its readme from docker/cosyvoice/RUNPOD_TEMPLATE_README.md.
# init uses it without asking; empty would make init ask for a template id.
RUNPOD_PUBLIC_TEMPLATE_ID = "y1s4i5a5qa"
RUNPOD_PROXY_PATTERN = "https://{pod_id}-8000.proxy.runpod.net"


def _load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)
    os.chmod(CONFIG_FILE, 0o600)


def save_server_setup(server, user, password, upload_token, env_path):
    """~/.librasync.json, the engine's copy of the ABS key, the drop folders."""
    config = {
        **server,
        "UPLOAD_TOKEN": upload_token,
        "ACCOUNT_USERNAME": user,
        "ACCOUNT_PASSWORD": password,
        # Anything shelling into this CLI uses the interpreter the dependencies
        # live in: the old setup page once spawned a bare "python3", the system one.
        "PYTHON_PATH": sys.executable,
    }
    _save_config(config)
    # The engine reads its own copy from .env (the healthcheck compares them).
    envfile.update(env_path, {"ABS_API_TOKEN": config["ABS_API_TOKEN"]})
    os.makedirs(DROPZONE, exist_ok=True)
    os.makedirs(ARCHIVE, exist_ok=True)
    return config


def ensure_env_file():
    """The checkout's .env, copied from .env.example the first time."""
    env_path = os.path.join(repo_root_dir(), ".env")
    if not os.path.exists(env_path):
        shutil.copyfile(os.path.join(repo_root_dir(), ".env.example"), env_path)
        os.chmod(env_path, 0o600)
    return env_path


def _step(n, title):
    click.secho(f"\n{n}. {title}", fg="blue", bold=True)


def _ask_account(existing):
    """One login for the Kobo, the catalogue and the Audiobookshelf app."""
    user = click.prompt("   Username", default=existing.get("ACCOUNT_USERNAME") or "reader")
    password = existing.get("ACCOUNT_PASSWORD")
    if password and click.confirm("   Keep your current password?", default=True):
        return user, password
    while True:
        password = click.prompt("   Choose a password (8+ characters)", hide_input=True,
                                confirmation_prompt="   Once more")
        if len(password) >= 8:
            return user, password
        click.secho("   Too short.", fg="red")


def _init_over_ssh(existing, user, password, upload_token):
    """Set up a fresh Linux server over SSH. Returns the config's server keys."""
    from libra_cli import server_setup
    click.echo("   A fresh Ubuntu or Debian server, created with your SSH key")
    click.echo(f"   (recommended: Hetzner's smallest cloud server, {links.HETZNER_URL}).")
    click.echo("   No SSH key yet? Run: ssh-keygen -t ed25519   and add ~/.ssh/id_ed25519.pub at Hetzner.")
    default_host = (existing.get("SERVER_SSH") or "").split("@")[-1] or None
    while True:
        host = click.prompt("   The server's IP address", default=default_host).strip()
        target = host if "@" in host else f"root@{host}"
        ok, detail = server_setup.check_ssh(target)
        if ok:
            break
        click.secho(f"   ❌ Could not log in to {target}: {detail}", fg="red")
        if not click.confirm("   Try again?", default=True):
            raise click.Abort()
    ip = target.split("@")[1]
    other = server_setup.foreign_stack(target)
    if other:
        click.secho(f"   ❌ This server already runs Voxie from {other}, set up by hand. Choose "
                    "option 2 for it, or use a fresh server.", fg="red")
        raise click.Abort()

    click.echo("\n   HTTPS needs a domain name pointing at the server (about €10 a year).")
    click.echo("   Without one, your login and positions travel unencrypted, and a new server")
    click.echo("   means plugging the Kobo in again.")
    domain = click.prompt("   Your domain, e.g. voxie.example.com (Enter for none)",
                          default=existing.get("DOMAIN") or "", show_default=False).strip().lower()
    while domain and not server_setup.resolves_to(domain, ip):
        click.secho(f"   ⚠️  {domain} does not point at {ip} yet. At your domain's registrar, add "
                    f"an A record: {domain} → {ip}.", fg="yellow")
        again = click.prompt("   Enter to check again, or type 'http' to go on without HTTPS",
                             default="", show_default=False)
        if again.strip().lower() == "http":
            domain = ""

    click.echo(f"\n   Setting up {ip} (a few minutes the first time) ...\n")
    settings = {
        "upload_token": upload_token,
        "account_username": user,
        "account_userkey": accounts.userkey_for(password),
        "abs_username": user,
        "abs_password": password,
        "domain": domain,
        "public_host": ip,
    }
    try:
        server_setup.push_code(repo_root_dir(), target)
        abs_token = server_setup.install(target, settings)
    except RuntimeError as e:
        click.secho(f"\n   ❌ {e}", fg="red")
        raise click.Abort()
    return {
        "GATEWAY_IP": domain or ip,
        **({"GATEWAY_URL": f"https://{domain}", "ABS_PUBLIC_URL": f"https://{domain}:13378",
            "DOMAIN": domain} if domain else {}),
        "SERVER_SSH": target,
        "ABS_API_TOKEN": abs_token,
    }


def _init_existing_server(existing, user, password, upload_token):
    """A server you run yourself: its address, then its login (everything done from
    here) or, without one, the ABS key and two lines to run on it."""
    from libra_cli import server_setup
    # An IP or hostname means http://<host>:3000; a full https:// address is
    # kept whole, for a gateway behind Tailscale Funnel or a TLS proxy
    # (core/gateway_address.py). A name beats an IP either way: the address is
    # baked into the Kobo at pairing time.
    while True:
        given = click.prompt("   Where is your server? (https://voxie.your-tailnet.ts.net, "
                             "or an IP/hostname for plain http on port 3000)",
                             default=existing.get("GATEWAY_URL") or existing.get("GATEWAY_IP") or None)
        try:
            host, url = parse_gateway(given)
            break
        except ValueError as e:
            click.secho(f"   ❌ {e}", fg="red")
    if url and url.startswith("http://"):
        click.secho("   ⚠️  Plain http: positions and the catalogue login travel unencrypted.", fg="yellow")
    keys = {"GATEWAY_IP": host, **({"GATEWAY_URL": url} if url else {})}
    if existing.get("ABS_PUBLIC_URL"):
        keys["ABS_PUBLIC_URL"] = existing["ABS_PUBLIC_URL"]
    abs_base = abs_url(keys)

    # With the server's login, its part is done from here: nothing to type on it.
    login = click.prompt("   The server's SSH login, e.g. root@203.0.113.10 (Enter to do its part by hand)",
                         default=existing.get("SERVER_SSH") or "", show_default=bool(existing.get("SERVER_SSH"))).strip()
    if login:
        target = login if "@" in login else f"root@{login}"
        ok, detail = server_setup.check_ssh(target)
        if not ok:
            click.secho(f"   ❌ {server_setup.explain_ssh_error(detail, login)}", fg="red")
        else:
            try:
                found = server_setup.link_existing(
                    target, user, accounts.userkey_for(password))
                ok, detail = verify_abs_token(abs_base, found["abs_token"])
                if ok:
                    click.secho("   ✅ Linked: your account is on the server, its keys are saved here.",
                                fg="green")
                    return {**keys, "SERVER_SSH": target, "ABS_API_TOKEN": found["abs_token"],
                            "_UPLOAD_TOKEN": found["upload_token"]}
                click.secho(f"   ❌ The server's Audiobookshelf key does not work: {detail}", fg="red")
            except RuntimeError as e:
                click.secho(f"   ❌ {e}", fg="red")
        click.echo("   Doing it by hand instead.")

    click.echo(f"\n   Audiobookshelf's API key: python tools/bootstrap_abs.py --url {abs_base}")
    click.echo("   creates the admin, the library and the key; or in Audiobookshelf,")
    click.echo("   Settings → API Keys → Create API Key.")
    # The prompt hides what you type, so a mis-paste (an IP address, a stale
    # key whose API key was deleted in the UI) would only surface much later as
    # a 401 from the gateway that looks like a networking fault.
    token = None
    for attempt in range(3):
        token = click.prompt("   Paste the API key", hide_input=True)
        ok, detail = verify_abs_token(abs_base, token)
        if ok:
            click.secho(f"   ✅ Accepted ({detail}).", fg="green")
            break
        click.secho(f"   ❌ {detail}", fg="red")
    keys["ABS_API_TOKEN"] = token

    click.secho("\n   On the server, put this line in its .env and run 'docker compose up -d':",
                fg="magenta")
    click.secho(f"   UPLOAD_TOKEN={upload_token}", fg="yellow", bold=True)
    click.secho("   Then create your account there once:", fg="magenta")
    click.secho(f"   docker exec librarian_server python tools/gateway_admin.py add-user {user} "
                f"--userkey {accounts.userkey_for(password)}", fg="yellow", bold=True)
    click.prompt("   Press Enter when that is done", default="", show_default=False)
    return keys


def _runpod_key_works(key):
    try:
        r = requests.post(f"https://api.runpod.io/graphql?api_key={key}",
                          json={"query": "query { myself { id } }"}, timeout=20)
        body = r.json()
    except (requests.exceptions.RequestException, ValueError):
        return None   # could not ask: not the key's fault
    # GraphQL reports failure inside an HTTP 200.
    return r.status_code == 200 and not body.get("errors") and bool((body.get("data") or {}).get("myself"))


# The voice director (core/director.py) and the optional JEV junk filter reach
# JEV through OpenRouter. Both spellings are read (core/junk_provider.py); the
# setup writes the documented one.
OPENROUTER_KEY_NAMES = ("OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY")


def openrouter_key(env):
    return next((env[n] for n in OPENROUTER_KEY_NAMES if (env.get(n) or "").strip()), "")


def _openrouter_key_works(key):
    """True, False, or None when OpenRouter could not be asked (not the key's fault)."""
    try:
        r = requests.get("https://openrouter.ai/api/v1/key",
                         headers={"Authorization": f"Bearer {key}"}, timeout=20)
    except requests.exceptions.RequestException:
        return None
    if r.status_code in (401, 403):
        return False
    return True if r.status_code == 200 else None


def apply_director(env_path, key):
    """The voice director's OpenRouter key into .env (written once, kept on later runs)."""
    envfile.update(env_path, {"OPENROUTER_API_KEY": key})


def apply_narration(env_path, provider, runpod_key=None, template=None):
    """Write the narration settings into .env: "kokoro" (this computer) or
    "cosyvoice" on RunPod with its key and template."""
    if provider == "kokoro":
        envfile.update(env_path, {"TTS_PROVIDER": "kokoro"})
        return
    env = envfile.read(env_path)
    envfile.update(env_path, {
        "TTS_PROVIDER": "cosyvoice",
        "RUNPOD_API_KEY": runpod_key,
        "RUNPOD_TEMPLATE_ID": template,
        "RUNPOD_URL_PATTERN": env.get("RUNPOD_URL_PATTERN") or RUNPOD_PROXY_PATTERN,
        "RUNPOD_TRANSPORT": env.get("RUNPOD_TRANSPORT") or "tcp",
        # Kept when set: the GPU server must start with the same token.
        "TTS_API_TOKEN": env.get("TTS_API_TOKEN") or secrets.token_hex(32),
    })


def _choose_narration(env_path):
    """Write TTS_PROVIDER and, for RunPod, its settings into the .env."""
    env = envfile.read(env_path)
    current = env.get("TTS_PROVIDER", "kokoro")
    click.echo(f"   1  RunPod (recommended): a rented GPU reads in a voice you add; a novel in about")
    click.echo(f"      an hour for about $1. {links.RUNPOD_URL}")
    click.echo("   2  This computer, free: preset voices, a novel overnight")
    choice = click.prompt("   Choose", type=click.Choice(["1", "2"]),
                          default="1" if current == "cosyvoice" else "2")
    if choice == "2":
        apply_narration(env_path, "kokoro")
        click.secho("   ✅ Narration on this computer (Kokoro).", fg="green")
        return "kokoro"

    key = env.get("RUNPOD_API_KEY")
    if not (key and click.confirm("   Keep your current RunPod API key?", default=True)):
        click.echo("   RunPod → Settings → API Keys → Create (read & write).")
        while True:
            key = click.prompt("   Paste the RunPod API key", hide_input=True).strip()
            works = _runpod_key_works(key)
            if works is not False:
                break
            click.secho("   ❌ RunPod rejected that key.", fg="red")
    template = RUNPOD_PUBLIC_TEMPLATE_ID or click.prompt(
        "   RunPod template id", default=env.get("RUNPOD_TEMPLATE_ID") or None)
    apply_narration(env_path, "cosyvoice", key, template)
    click.secho("   ✅ Narration on RunPod.", fg="green")
    _offer_director(env_path)
    _offer_voice()
    return "cosyvoice"


def _offer_director(env_path):
    """The voice director's OpenRouter key: kept when one is there, else offered."""
    if openrouter_key(envfile.read(env_path)) and click.confirm(
            "   Keep your current OpenRouter key (the voice director)?", default=True):
        return
    click.echo("\n   The voice director (recommended): JEV reads each scene and tells the narrator")
    click.echo("   how to say every paragraph (hushed, frantic, tender...), about $0.10 a novel.")
    click.echo(f"   It needs an OpenRouter key: {links.OPENROUTER_URL} → Keys → Create Key.")
    while True:
        key = click.prompt("   Paste the OpenRouter key (Enter to skip)", default="",
                           show_default=False, hide_input=True).strip()
        if not key:
            click.echo("   Skipped: the narrator reads without direction. Run `voxie init` to add it later.")
            return
        if _openrouter_key_works(key) is not False:
            apply_director(env_path, key)
            click.secho("   ✅ The voice director is on.", fg="green")
            return
        click.secho("   ❌ OpenRouter rejected that key.", fg="red")


def _offer_voice():
    from core.voices import available
    if available():
        click.echo(f"   Your voices: {', '.join(available())}")
        return
    click.echo("\n   No voice ships with Voxie: RunPod reads in a voice you add, from an 8-15")
    click.echo("   second clip of someone who agreed to it (you, or with their permission).")
    from libra_cli.clip_check import MOOD_ADVICE
    click.echo(f"   {MOOD_ADVICE}")
    if not click.confirm("   Add one now?", default=True):
        click.echo("   Later: voxie voice-add --help")
        return
    from libra_cli.voice import VoiceError, add_voice
    if click.confirm("   Record yourself reading a short text? (best: the narrator copies how you "
                     "read, pauses included)", default=True):
        _print_voice_script()
        path = os.path.expanduser(click.prompt(
            "\n   Record it (Voice Memos, QuickTime...), then give the file's path").strip())
        name = click.prompt("   A name for the voice", default="my voice")
        try:
            result = add_voice(file=path, text=voice_script.TEXT, name=name, language="en",
                               consent="self", source_name=None)
        except VoiceError as e:
            click.secho(f"   ❌ {e}  (try again later: voxie voice-add --script --file ...)", fg="red")
            return
        _report_voice(result)
        return
    source = click.prompt("   A YouTube link, or the path of an audio/video file").strip()
    is_file = os.path.exists(os.path.expanduser(source))
    start = click.prompt("   Where the clip starts (e.g. 1:30)", default="0")
    end = click.prompt("   Where it ends (8-15 seconds later, e.g. 1:42)")
    text = click.prompt("   Exactly what is said in the clip")
    name = click.prompt("   A name for the voice", default="my voice")
    consent = click.prompt("   Whose voice is it? self = yours, permission = the speaker agreed",
                           type=click.Choice(["self", "permission"]))
    try:
        result = add_voice(url=None if is_file else source,
                           file=os.path.expanduser(source) if is_file else None,
                           start=start, end=end, text=text, name=name, language="en",
                           consent=consent, source_name=None)
    except VoiceError as e:
        click.secho(f"   ❌ {e}  (try again later: voxie voice-add --help)", fg="red")
        return
    _report_voice(result)


def _print_voice_script():
    click.echo("\n   Read this, slowly:\n")
    for i, line in enumerate(voice_script.SENTENCES):
        if i:
            click.echo("       (pause: count one)")
        click.secho(f"       {line}", bold=True)
    click.echo("")
    for tip in voice_script.HOW:
        click.echo(f"   - {tip}")


def _report_voice(result):
    click.secho(f"   ✅ Voice '{result['name']}' added: {result['seconds']}s, trimmed to the words.", fg="green")
    for w in result["warnings"]:
        click.secho(f"   ⚠️  {w}", fg="yellow")
    if result.get("retake"):
        click.secho("   ↻ Read it again if you can: every sentence of every book will be read like this "
                    "one. Add it again under another name once you have read it better.",
                    fg="yellow")


def _docker_ready():
    return platform_support.ensure_docker(say=lambda m: click.echo(f"   {m}"))


def _pair_kobo(ctx):
    if not IS_MAC:
        click.echo("   The one-cable Kobo setup runs on a Mac. On this computer, pair any")
        click.echo("   KOReader device by hand: voxie device-settings (docs/KOREADER_DEVICES.md).")
        return False
    if not _docker_ready():
        click.secho("   ⚠️  Docker is not running: start Docker Desktop first. The setup uses it to "
                    "check the Kobo's filesystem before writing anything.", fg="yellow")
    click.echo("   Plug your Kobo into this Mac with its cable and tap Connect on its screen.")
    while True:
        answer = click.prompt("   Press Enter when it is connected (or type 'skip')",
                              default="", show_default=False)
        if answer.strip().lower() == "skip":
            click.echo("   Later: plug it in and run voxie setup-kobo")
            return False
        if find_kobo_mount():
            ctx.invoke(setup_kobo)
            return True
        click.secho("   No Kobo found yet. Is it showing 'Computer detected', and did you tap Connect?",
                    fg="yellow")


def _has_browser():
    # A Mac always has one; Linux only in a desktop session (not over SSH).
    return IS_MAC or bool(os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY"))


@cli.command()
@click.option("--terminal", is_flag=True,
              help="Answer the questions here instead of on the setup page.")
@click.pass_context
def init(ctx, terminal):
    """Set up everything: the server, narration, the watcher and your Kobo.

    Opens the setup page in your browser; --terminal asks here instead.
    Safe to run again: it is also how you change a setting or update the server.
    """
    if not terminal and _has_browser():
        from libra_cli.setup_web.server import serve
        return serve()
    click.secho("\n👋 Voxie setup", fg="blue", bold=True)
    existing = _load_config()
    env_path = ensure_env_file()

    _step(1, "Your account (the Kobo and the Audiobookshelf app log in with it)")
    user, password = _ask_account(existing)
    # Reused when one exists: rotating it on every run would break uploads
    # until the server's .env caught up.
    upload_token = existing.get("UPLOAD_TOKEN") or secrets.token_urlsafe(32)

    _step(2, "Your server")
    click.echo("   1  Set one up for me over SSH (recommended)")
    click.echo("   2  I already run one")
    choice = click.prompt("   Choose", type=click.Choice(["1", "2"]),
                          default="2" if existing.get("GATEWAY_IP") and not existing.get("SERVER_SSH") else "1")
    server = (_init_over_ssh if choice == "1" else _init_existing_server)(
        existing, user, password, upload_token)
    # A server linked over SSH keeps its own upload token.
    upload_token = server.pop("_UPLOAD_TOKEN", upload_token)

    config = save_server_setup(server, user, password, upload_token, env_path)
    click.secho(f"   ✅ Saved to {CONFIG_FILE}", fg="green")

    _step(3, "Narration")
    provider = _choose_narration(env_path)
    if provider == "kokoro" and not _docker_ready():
        click.secho("   ⚠️  Install and start Docker Desktop: narration on this computer uses it to "
                    "align every word with the text.", fg="yellow")

    _step(4, "The watcher (narrates what you drop in ~/Kobo_Dropzone)")
    ctx.invoke(start_ghost)

    _step(5, "Your Kobo")
    _pair_kobo(ctx)

    click.secho("\n🎉 Voxie is ready.", fg="green", bold=True)
    click.echo("   Drop an EPUB into ~/Kobo_Dropzone. On the Kobo: the Voxie button, then the")
    click.echo("   Voxie catalogue. On your phone, any Audiobookshelf app:")
    click.echo(f"      server {abs_url(config)}   user {user}   and your password\n")


def _start_ghost_systemd(repo_root, ghost_path):
    """Linux: the watcher as a systemd user service (the LaunchAgent's twin)."""
    import shutil as _shutil
    if not _shutil.which("systemctl"):
        click.secho("❌ systemctl not found. Run the watcher yourself: "
                    f"cd {repo_root} && python -u mac_ghost.py", fg="red")
        return
    unit_dir = os.path.join(os.getenv("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                            "systemd", "user")
    os.makedirs(unit_dir, exist_ok=True)
    os.makedirs(platform_support.log_dir(), exist_ok=True)
    unit_path = os.path.join(unit_dir, platform_support.SYSTEMD_UNIT)
    with open(unit_path, "w") as f:
        f.write(platform_support.systemd_unit(sys.executable, ghost_path, repo_root,
                                              platform_support.ghost_log_path()))
    click.secho(f"   ✅ Service file written: {unit_path}", fg="green")
    for args in (["daemon-reload"], ["enable", platform_support.SYSTEMD_UNIT],
                 ["restart", platform_support.SYSTEMD_UNIT]):
        r = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
        if r.returncode != 0:
            click.secho(f"\n❌ systemctl --user {' '.join(args)} failed: {r.stderr.strip()}", fg="red")
            return
    click.secho("\n🎉 The watcher is running in the background.", fg="blue", bold=True)
    click.echo("   It restarts if it crashes and starts when you log in. To keep it running")
    click.echo(f"   while you are logged out: loginctl enable-linger {os.getenv('USER', '$USER')}")
    click.echo(f"   Logs: tail -f {platform_support.ghost_log_path()}\n")


@cli.command()
def start_ghost():
    """Installs and starts the watcher as a background service (macOS launchd, Linux systemd)."""
    click.secho("\n👻 Mac Ghost Automator Initialized...", fg="blue", bold=True)
    try:        # an install made before the junk filter became "auto" follows its key from here on
        from libra_cli import updater
        updater.migrate_env(ensure_env_file())
    except Exception:
        pass

    # 1. Locate the ghost script exactly where the CLI is installed
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    ghost_path = os.path.join(repo_root, "mac_ghost.py")

    if not os.path.exists(ghost_path):
        click.secho(f"❌ Error: Cannot find mac_ghost.py at {ghost_path}", fg="red")
        return

    if IS_LINUX:
        return _start_ghost_systemd(repo_root, ghost_path)

    # 2. Define the LaunchAgent paths
    plist_name = "com.librasync.ghost.plist"
    plist_dir = os.path.expanduser("~/Library/LaunchAgents")
    plist_path = os.path.join(plist_dir, plist_name)
    os.makedirs(plist_dir, exist_ok=True)

    # Not /tmp: macOS reaps files there that have not been touched in three
    # days, and an idle ghost touches nothing. The agent goes on writing to a
    # deleted inode, so the one record of an unattended run is gone by the time
    # anyone looks for it. ~/Library/Logs is never swept.
    log_dir = platform_support.log_dir()
    os.makedirs(log_dir, exist_ok=True)

    # 3. Generate the plist content
    python_path = sys.executable # Uses the exact Python environment running the CLI

    # mac_ghost.py invokes the engine as a bare relative path and resolves
    # library/ebooks relatively too, so launchd must start it in the repo root.
    # Without WorkingDirectory the agent runs from / and dies on the first drop.
    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.librasync.ghost</string>
    <key>WorkingDirectory</key>
    <string>{repo_root}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_path}</string>
        <string>{ghost_path}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardErrorPath</key>
    <string>{log_dir}/librasync-ghost.err</string>
    <key>StandardOutPath</key>
    <string>{log_dir}/librasync-ghost.out</string>
</dict>
</plist>
"""

    # 4. Write the configuration file
    with open(plist_path, "w") as f:
        f.write(plist_content)
    click.secho("   ✅ Background Service File created.", fg="green")

    # 5. Retire the pre-rename agent, which used a different label.
    # Because the label differs, loading ours does NOT replace it — both would
    # run, watching the same dropzone, racing each other over the same book.
    legacy_plist = os.path.join(plist_dir, "com.kobosync.ghost.plist")
    if os.path.exists(legacy_plist):
        subprocess.run(["launchctl", "unload", legacy_plist], capture_output=True)
        os.remove(legacy_plist)
        click.secho("   🧹 Removed the old com.kobosync.ghost agent.", fg="yellow")

    # 6. Load and start the service invisibly
    click.echo("   🔄 Booting up the Ghost...")
    subprocess.run(["launchctl", "unload", plist_path], capture_output=True) # Unload first in case of update
    result = subprocess.run(["launchctl", "load", plist_path], capture_output=True)

    if result.returncode == 0:
        click.secho("\n🎉 Mac Ghost is now running invisibly in the background!", fg="blue", bold=True)
        click.echo("   It will automatically restart if it crashes or if you reboot your Mac.")
        click.echo(f"   (To view logs at any time, run: tail -f {log_dir}/librasync-ghost.out)\n")
    else:
        click.secho(f"\n❌ Failed to start the service: {result.stderr.decode()}", fg="red")


@cli.command()
@click.option(
    "--json", "as_json", is_flag=True,
    help="Non-interactive mode: never prompt, print a single JSON result line "
         "to stdout instead of formatted output. Used by the setup page (libra_cli/setup_web)."
)
@click.option(
    "--dicts", "dict_spec", default="default", show_default=True,
    help="Dictionaries to install: 'default', 'none', or comma-separated ids "
         "(list them with `python -m libra_cli.dictionaries --list`)."
)
@click.option("--fonts/--no-fonts", "with_fonts", default=True, show_default=True,
              help="Install the reading fonts (Literata, Crimson Pro, EB Garamond).")
@click.option(
    "--skip-fat-check", is_flag=True,
    help="DANGEROUS: write the reading kit without first proving it survives the "
         "Kobo's own filesystem check. Only for a Mac with no Docker."
)
@click.option("--reading-profile/--no-reading-profile", "with_profile", default=True, show_default=True,
              help="Once per device: tuck away developer plugins and make Literata the default "
                   "font. Reversible on the device; see libra_cli/reading_profile.py.")
@click.option("--look", type=click.Choice(["kobo", "koreader"]), default=None,
              help="How KOReader looks: 'kobo' (the Kobo's home, tabs, library and reading screen) "
                   "or 'koreader' (as it ships). Asked at the end when not given; --json keeps the "
                   "current one. Change it later with `voxie look`; see libra_cli/kobo_look.py.")
def setup_kobo(as_json, dict_spec, with_fonts, skip_fat_check, with_profile, look):
    """Auto-installs KOReader and the Librarian plugin on a USB-connected Kobo."""
    # In --json mode all human-readable logging goes to stderr, so stdout
    # stays clean for exactly one JSON line callers can parse.
    def log(message, **kwargs):
        click.secho(message, err=as_json, **kwargs)

    def fail(error_code):
        if as_json:
            click.echo(json.dumps({"success": False, "error": error_code}))
        return

    log("\n🔌 Kobo Auto-Injector Initialized...", fg="blue", bold=True)

    # macOS only for now: it extracts with ditto because of the Mac's FAT
    # driver, and a Linux or Windows port must pass the FAT gate on a real
    # device before it may write to anyone's Kobo (core/platform_support.py).
    if not IS_MAC:
        log("❌ The Kobo installer runs on macOS only for now. Narration and sync "
            "work on Linux; the installer for Linux is the next port.", fg="red")
        return fail("installer_macos_only")

    # 1. Read the saved IP Address
    if not os.path.exists(CONFIG_FILE):
        log("❌ Error: Configuration not found. Please run 'voxie init' first.", fg="red")
        return fail("config_not_found")

    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)

    gateway_ip = config.get("GATEWAY_IP")
    gateway = gateway_url(config)
    if not gateway:
        log("❌ Error: No Gateway IP found in config.", fg="red")
        return fail("gateway_ip_not_configured")

    # 2. Detect the Kobo via USB (PRODUCTION PATH)
    kobo_path = find_kobo_mount()
    if not kobo_path:
        if as_json:
            # No interactive wait in headless mode — the caller (the setup page)
            # is responsible for polling and re-invoking once connected.
            return fail("kobo_not_detected")

        click.secho("\n⚠️  Kobo not detected!", fg="yellow", bold=True)
        click.echo("   Please plug your Kobo into your Mac using a USB cable.")
        click.prompt("   Press Enter when you see the Kobo drive on your Mac", default="", show_default=False)

        kobo_path = find_kobo_mount()
        if not kobo_path:
            click.secho("❌ Still cannot find the Kobo. Exiting.", fg="red")
            return

    log(f"✅ Kobo detected at {kobo_path}!", fg="green")

    try:
        # ---------------------------------------------------------
        # 🌟 ZERO-TO-HERO: CORE INSTALLATION (NickelMenu + KOReader)
        # ---------------------------------------------------------
        koreader_path = os.path.join(kobo_path, ".adds/koreader")
        existing_koreader = os.path.exists(koreader_path)
        if not existing_koreader:
            log("\n✨ Brand new Kobo detected! Initiating Zero-to-Hero Install...", fg="magenta", bold=True)

            # A. Download & Extract KOReader
            log(f"   ⬇️  Downloading KOReader {KOREADER_TAG} (pinned, sha256-verified)...")
            kr_bytes = download_pinned(
                f"https://github.com/koreader/koreader/releases/download/{KOREADER_TAG}/{KOREADER_ASSET}",
                KOREADER_SHA256)
            log(f"   📦 {KOREADER_ASSET}")

            # The archive contains a top-level koreader/ directory, so it must
            # be unpacked INTO .adds -- extracting to the volume root drops it
            # at /koreader and leaves .adds/koreader without koreader.sh, which
            # NickelMenu then points at and cannot launch.
            adds_dir = os.path.join(kobo_path, ".adds")
            os.makedirs(adds_dir, exist_ok=True)

            log("   📂 Extracting (this writes ~1000 files, give it a moment)...")
            total, problems = extract_verified(kr_bytes, adds_dir)

            # Push the writes to the device, then check again. The first pass
            # can pass on data that is still only in the page cache.
            log("   💾 Flushing writes to the device...")
            flush_volume(kobo_path)
            _, problems_after_flush = extract_verified(kr_bytes, adds_dir)
            problems = problems or problems_after_flush

            if problems:
                preview = "\n      ".join(problems[:10])
                more = f"\n      ... and {len(problems) - 10} more" if len(problems) > 10 else ""
                raise Exception(
                    f"KOReader did not extract cleanly — {len(problems)} of {total} files "
                    f"are missing or truncated:\n      {preview}{more}\n"
                    "   The Kobo's FAT32 volume is probably damaged. Run "
                    "'diskutil repairVolume /Volumes/<your kobo>', then try again."
                )

            # Fail loudly rather than leaving a device that looks installed.
            launcher = os.path.join(koreader_path, "koreader.sh")
            if not os.path.exists(launcher):
                raise Exception(
                    f"KOReader extracted but {launcher} is missing — the release "
                    "layout has changed and NickelMenu would have nothing to launch."
                )
            log(f"   ✅ KOReader installed ({total} files verified).", fg="green")

            # B. Download & Inject NickelMenu
            log(f"   ⬇️  Downloading NickelMenu {NICKELMENU_TAG} (pinned, sha256-verified)...")
            nm_bytes = download_pinned(
                f"https://github.com/pgaskin/NickelMenu/releases/download/{NICKELMENU_TAG}/KoboRoot.tgz",
                NICKELMENU_SHA256)
            kobo_root_dir = os.path.join(kobo_path, ".kobo")
            os.makedirs(kobo_root_dir, exist_ok=True)
            with open(os.path.join(kobo_root_dir, "KoboRoot.tgz"), "wb") as f:
                f.write(nm_bytes)

            # C. Write NickelMenu Config
            write_nickelmenu(kobo_path, repo_root_dir())
            log("   ✅ NickelMenu bootloader installed: 'Voxie' in the Kobo's bottom bar.", fg="green")
        else:
            # Someone who already runs KOReader keeps it: their version, their
            # launcher (NickelMenu or KFMon) and their settings. Only Voxie's
            # own keys are changed below, in place.
            installed = installed_koreader_version(koreader_path)
            log(f"\n✅ KOReader already installed ({installed or 'version unknown'}): kept as it is, "
                "with your settings.", fg="cyan")
            # Our own NickelMenu entry, from an earlier Voxie install, gets the
            # current name and icon. Anyone else's launcher is left alone.
            if nickelmenu_is_ours(kobo_path):
                write_nickelmenu(kobo_path, repo_root_dir())
                log("   ✅ Kobo's bottom bar: 'Voxie', with its icon.", fg="green")
            if koreader_is_older(installed, KOREADER_TAG):
                log(f"   ⚠️  Voxie is tested with KOReader {KOREADER_TAG}. If sync misbehaves, "
                    "update KOReader from its own ☰ menu → Update.", fg="yellow")

        # ---------------------------------------------------------
        # 💉 PHASE 2: VOXIE INJECTION
        # ---------------------------------------------------------
        log("\n   💉 Injecting Voxie Protocols...")

        plugin_dir = os.path.join(kobo_path, ".adds/koreader/plugins/librarian_sync.koplugin")
        os.makedirs(plugin_dir, exist_ok=True)

        # librarian_sync.koplugin/ is the single source of truth for the plugin:
        # main.lua (the template, with the gateway's address substituted) and
        # the modules beside it, such as the landing rules.
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        source_dir = os.path.join(repo_root, "librarian_sync.koplugin")
        for name in sorted(n for n in os.listdir(source_dir) if n.endswith(".lua")):
            with open(os.path.join(source_dir, name), "r") as f:
                lua_script = f.read().replace("__GATEWAY_URL__", gateway)
            with open(os.path.join(plugin_dir, name), "w") as f:
                f.write(lua_script)
        log(f"   ✅ Sidecar Plugin injected! (Targeting {gateway})", fg="green")

        write_sync_settings(kobo_path, gateway, config.get("ACCOUNT_USERNAME"),
                            config.get("ACCOUNT_PASSWORD"), log)

        # ---------------------------------------------------------
        # 📚 PHASE 3: READING KIT (dictionaries + fonts)
        # ---------------------------------------------------------
        # Staged and FAT-checked on the Mac as one payload before anything is
        # written, so a failure here leaves the device as it was. See
        # libra_cli/reading_kit.py for why the gate refuses without Docker.
        from libra_cli.reading_kit import install_reading_kit, parse_dict_spec
        kit = install_reading_kit(
            kobo_path, dict_ids=parse_dict_spec(dict_spec), with_fonts=with_fonts,
            fat_gate=not skip_fat_check, log=log,
        )

        profile = {"applied": False, "disabled": [], "font": None}
        if with_profile:
            from libra_cli.reading_profile import apply_profile
            profile = apply_profile(kobo_path, fonts_installed=bool(kit["fonts"]), log=log,
                                    keep_plugins=existing_koreader)
            from libra_cli.reading_profile import restyle_books, default_backup_dir
            profile["restyled"] = restyle_books(kobo_path, fonts_installed=bool(kit["fonts"]) or
                                                os.path.exists(os.path.join(kobo_path, "fonts", "Literata-Regular.ttf")),
                                                backup_dir=default_backup_dir(), log=log)

        # ---------------------------------------------------------
        # 🎨 LAST STEP: HOW IT LOOKS (and progress sync off the screen)
        # ---------------------------------------------------------
        from libra_cli import kobo_look
        if look is None and not as_json:
            look = _ask_look(kobo_look.current_look(kobo_path))
        look_result = _apply_look(kobo_path, look or kobo_look.current_look(kobo_path),
                                  not skip_fat_check, log)

        # Stop Spotlight indexing this volume. macOS writes a search index
        # (.Spotlight-V100) onto the Kobo and keeps writing to it in the
        # background -- extra churn on an unjournalled FAT32 volume that is
        # about to be unplugged. This marker is the documented way to opt out
        # and needs no privileges.
        never_index = os.path.join(kobo_path, ".metadata_never_index")
        if not os.path.exists(never_index):
            open(never_index, "w").close()

        # Everything is written; get it onto the device before anyone unplugs.
        log("\n   💾 Flushing all writes to the device...")
        flush_volume(kobo_path)

    except Exception as e:
        log(f"❌ Kobo setup failed: {e}", fg="red")
        return fail(str(e))

    # Eject here rather than asking the user to. An unmount is what actually
    # commits the FAT metadata, and a cable pulled a moment early leaves a
    # filesystem that a later fsck "repairs" by turning live files into
    # FSCK*.000 -- which is how a verified-good install becomes a KOReader that
    # crashes on launch with core modules missing.
    ejected = subprocess.run(
        ["diskutil", "eject", kobo_path], capture_output=True, text=True
    ).returncode == 0

    if as_json:
        click.echo(json.dumps({"success": True, "gatewayIp": gateway_ip, "gatewayUrl": gateway, "ejected": ejected,
                               "koreaderExisting": existing_koreader,
                               "dictionaries": kit["dictionaries"], "fonts": kit["fonts"],
                               "profile": profile, "look": look_result}))
        return

    click.secho("\n🎉 Kobo Setup Complete!", fg="blue", bold=True)
    if ejected:
        click.secho("   ✅ Kobo safely ejected — you can unplug the cable now.", fg="green", bold=True)
    else:
        click.secho("   ⚠️ Could not eject automatically. Eject it in Finder and WAIT", fg="yellow", bold=True)
        click.secho("      until the drive disappears before unplugging.", fg="yellow", bold=True)
    click.echo("   If this was a brand new Kobo, it will say 'Updating' and reboot.")
    click.echo("   Once it reboots, tap 'NickelMenu' -> 'KOReader' to start reading!\n")

def _ask_look(current):
    click.echo("\n   🎨 Last step: how should reading on your Kobo look?")
    click.echo("      kobo      like the Kobo's own: a home screen with your books, tabs along the")
    click.echo("                bottom, and the Kobo's reading screen and toolbar")
    click.echo("      koreader  KOReader as it ships")
    click.echo("   Either can be changed later with `voxie look kobo` or `voxie look koreader`.")
    return click.prompt("   Look", type=click.Choice(["kobo", "koreader"]), default=current)


def _apply_look(kobo_path, look, fat_gate, log):
    """The look and the background-Wi-Fi patch. A refusal is reported, never fatal."""
    from libra_cli import kobo_look
    try:
        return kobo_look.apply(kobo_path, look, fat_gate=fat_gate, log=log, download=download_pinned)
    except kobo_look.LookRefused as e:
        log(f"   ⚠️  {e}", fg="yellow")
        return {"look": kobo_look.current_look(kobo_path), "refused": str(e)}


@cli.command("look")
@click.argument("look", type=click.Choice(["kobo", "koreader"]))
@click.option("--skip-fat-check", is_flag=True,
              help="DANGEROUS: write without first proving it survives the Kobo's own filesystem check.")
def look_command(look, skip_fat_check):
    """Make KOReader on the plugged-in Kobo look like a Kobo, or like KOReader again."""
    if not IS_MAC:
        click.secho("❌ Changing the Kobo's look runs on macOS only for now.", fg="red")
        sys.exit(1)
    kobo_path = find_kobo_mount()
    if not kobo_path:
        click.secho("❌ No Kobo found. Plug it in and tap Connect on its screen.", fg="red")
        sys.exit(1)
    result = _apply_look(kobo_path, look, not skip_fat_check, click.secho)
    flush_volume(kobo_path)
    ejected = subprocess.run(["diskutil", "eject", kobo_path], capture_output=True).returncode == 0
    if result.get("refused"):
        sys.exit(1)
    click.secho("✅ Done." + (" Kobo ejected: unplug it and open Voxie." if ejected
                             else " Eject the Kobo in Finder before unplugging it."), fg="green")


@cli.command("device-settings")
def device_settings():
    """What to type into KOReader on a device paired by hand (any KOReader device)."""
    if not os.path.exists(CONFIG_FILE):
        click.secho("❌ No configuration yet. Run 'voxie init' first.", fg="red")
        return
    with open(CONFIG_FILE) as f:
        config = json.load(f)
    base = gateway_url(config)
    user, password = config.get("ACCOUNT_USERNAME"), config.get("ACCOUNT_PASSWORD")
    if not (base and user and password):
        click.secho("❌ The configuration has no server or account. Run 'voxie init'.", fg="red")
        return
    click.secho("\nPair any KOReader device (docs/KOREADER_DEVICES.md has the steps):\n", bold=True)
    rows = [("Progress sync server", base), ("Username", user), ("Password", password),
            ("Document matching", "Binary"), ("Catalogue (OPDS)", f"{base}/opds"),
            ("Plugin (optional)", f"{base}/librarian/plugin.zip")]
    width = max(len(k) for k, _ in rows)
    for key, value in rows:
        click.echo(f"   {key:<{width}}  {value}")
    if base.startswith("http://"):
        click.secho("\n   Plain http: fine at home; for anywhere else see docs/SERVER_REFERENCE.md (Tailscale Funnel).",
                    fg="yellow")
    click.echo("\n   The catalogue and the plugin ask for the same username and password.\n")


@cli.command("set-abs")
@click.option("--url", help="Your Audiobookshelf's public address, e.g. https://abs.example.com")
@click.option("--clear", is_flag=True, help="Forget the Audiobookshelf this account uses.")
@click.option("--allow-http", is_flag=True,
              help="Send the token to a gateway without HTTPS. It crosses the network in clear text.")
def set_abs(url, clear, allow_http):
    """Connect your account to your own Audiobookshelf (the token is stored encrypted)."""
    if not os.path.exists(CONFIG_FILE):
        sys.exit("Run `voxie init` first.")
    config = json.load(open(CONFIG_FILE))
    user, password = config.get("ACCOUNT_USERNAME"), config.get("ACCOUNT_PASSWORD")
    if not (user and password):
        sys.exit("No account login in ~/.librasync.json; run `voxie init`.")
    # GATEWAY_URL (https://...) when the gateway sits behind TLS.
    base = gateway_url(config) or ""
    if not base.startswith("https://") and not allow_http and not clear:
        sys.exit(f"The gateway is {base}, without HTTPS, so the token would travel in clear text.\n"
                 "Put the gateway behind HTTPS and set GATEWAY_URL in ~/.librasync.json, "
                 "or pass --allow-http if you accept that.")
    headers = {"x-auth-user": user, "x-auth-key": accounts.userkey_for(password)}
    try:
        if clear:
            r = requests.delete(f"{base}/librarian/abs", headers=headers, timeout=30)
        else:
            url = url or click.prompt("Audiobookshelf address")
            token = click.prompt("Audiobookshelf API token (Settings → Users → your user)", hide_input=True)
            r = requests.put(f"{base}/librarian/abs", headers=headers, json={"url": url, "token": token}, timeout=60)
    except requests.exceptions.RequestException as e:
        sys.exit(f"The gateway did not answer ({e}).")
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if not r.ok:
        sys.exit(f"❌ {body.get('error') or body.get('message') or f'HTTP {r.status_code}'}")
    if clear:
        click.secho("✅ This account no longer syncs with an Audiobookshelf.", fg="green")
    else:
        click.secho(f"✅ Connected to {body['url']} as '{body.get('abs_user')}'. The token is stored encrypted.",
                    fg="green")


@cli.command("voice-add")
@click.option("--url", help="YouTube link; a ?t= in it is taken as the start.")
@click.option("--file", "file_path", type=click.Path(),
              help="A local audio or video file (MP3, M4A, WAV, MP4, MOV...) instead of a link.")
@click.option("--source-name", help="What to record as the clip's source (default: the file's name).")
@click.option("--start", help="Where the clip starts: 90, 1:30 or 00:01:30.")
@click.option("--end", help="Where it ends (3-30 seconds after the start; 12s if omitted).")
@click.option("--text", help="Exactly what is said in the clip.")
@click.option("--script", "use_script", is_flag=True,
              help="The clip is you reading the text `voxie voice-script` prints (use with --file).")
@click.option("--name", help="What to call the voice, e.g. 'my voice'.")
@click.option("--language", default="en", show_default=True, type=click.Choice(["en"]),
              help="The language this voice reads. Voxie narrates English only for now.")
@click.option("--consent", type=click.Choice(["self", "permission"]), required=True,
              help="self: it is your own voice. permission: the speaker agreed to be cloned.")
@click.option("--json", "as_json", is_flag=True, help="Print one JSON result line (for scripts).")
def voice_add(url, file_path, source_name, start, end, text, use_script, name, language, consent, as_json):
    """Add a narrator voice from a short clip and its transcript. No voice ships with libra-sync.

    The clip is checked first: trimmed to the typed words, refused if it says
    something else, warned about for clipping, a faint recording, or noise and
    music in the pauses. The clip's mood becomes the narrator's: calm, clear,
    one voice only, no laughs, sighs or music.
    """
    from libra_cli.voice import VoiceError, add_voice
    if use_script:
        url, start, end, text = None, None, None, voice_script.TEXT
    elif not text:
        click.secho("❌ Give --text (exactly what is said), or --script with a recording of the script.", fg="red")
        sys.exit(1)
    try:
        result = add_voice(url=url, file=file_path, start=start, end=end, text=text,
                           name=name, language=language, consent=consent, source_name=source_name)
    except VoiceError as e:
        if as_json:
            click.echo(json.dumps({"success": False, "error": str(e)}))
        else:
            click.secho(f"❌ {e}", fg="red")
        sys.exit(1)
    if as_json:
        click.echo(json.dumps({"success": True, **result}))
        return
    click.secho(f"✅ Voice '{result['name']}' added: {result['seconds']}s, {result['lufs']} LUFS.", fg="green")
    if result.get("heard"):
        click.echo(f"   Whisper heard: “{result['heard'][:200]}” ({result['match']:.0%} of your words).")
    for w in result["warnings"]:
        click.secho(f"   ⚠️  {w}", fg="yellow")
    if result.get("retake"):
        click.secho("   ↻ Worth reading again: the narrator copies this pace and these pauses.", fg="yellow")
    click.echo(f"   Narrate with it:  --voice {result['name']}   (or COSY_VOICE={result['name']} in .env)")


def _watcher_installed():
    if IS_LINUX:
        return os.path.exists(os.path.join(os.getenv("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                                           "systemd", "user", platform_support.SYSTEMD_UNIT))
    return os.path.exists(os.path.expanduser("~/Library/LaunchAgents/com.librasync.ghost.plist"))


def _book_running():
    """True while an engine narrates a book: a restart now would stop it."""
    try:
        return subprocess.run(["pgrep", "-f", "librerian_engine_v3.py"], capture_output=True).returncode == 0
    except OSError:
        return False


def run_update(on_line, with_server=True):
    """Update this Voxie: the checkout, what it needs, its settings, the watcher, the server.

    Used by `voxie update` and the setup page's Update button. Raises
    updater.UpdateError for what the person must act on. Returns what was done.
    """
    from libra_cli import updater
    root = repo_root_dir()
    res = updater.update_checkout(root, on_line)
    if res["deps"]:
        updater.install_dependencies(root, on_line)
    for line in updater.migrate_env(ensure_env_file()):
        on_line("Settings: " + line + ".")
    done = {**res, "watcher": "", "server": ""}
    if res["changed"] and _watcher_installed():
        if _book_running():
            done["watcher"] = "later"
            on_line("A book is being narrated, so the watcher keeps its old code until it is done: "
                    "then run  voxie start-ghost.")
        else:
            on_line("Restarting the watcher ...")
            start_ghost.callback()
            done["watcher"] = "restarted"
    target = _load_config().get("SERVER_SSH")
    if with_server and target and res["changed"]:
        try:
            updater.update_server(root, target, on_line)
            done["server"] = "updated"
        except updater.UpdateError as e:
            done["server"] = "skipped"
            on_line(f"Your server was not updated: {e}")
    return done


@cli.command("update")
@click.option("--server/--no-server", default=True, help="Also update the Voxie server, when you have one.")
def update_cmd(server):
    """Update Voxie to the latest version (this computer, its watcher, your server)."""
    from libra_cli import updater
    try:
        done = run_update(lambda line: click.echo(f"   {line}"), with_server=server)
    except updater.UpdateError as e:
        click.secho(f"❌ {e}", fg="red")
        sys.exit(1)
    if done["changed"]:
        click.secho(f"✅ Voxie is now {done['to']}.", fg="green")
        if done["server"] == "":
            click.echo("   Nothing to update on a server. The Kobo's plugin changes only when you run "
                       "voxie setup-kobo again with the cable.")


@cli.command("status")
def status_cmd():
    """What Voxie is doing with the book you dropped, and how long is left."""
    from core import progress
    status = progress.read()
    click.echo(progress.describe(status))
    if status and status.get("stage") not in ("done", "failed"):
        quiet = (time.time() - status.get("updated", time.time())) / 60
        if quiet > 10:
            click.secho(f"   Nothing has changed for {quiet:.0f} minutes. Is the watcher running? "
                        "voxie start-ghost; its log: ~/Library/Logs/librasync-ghost.out", fg="yellow")


@cli.command("voice-script")
def voice_script_cmd():
    """Print the short text to read aloud to make your narrator's voice, and how to read it."""
    _print_voice_script()
    click.echo("\n   Record it, then:  voxie voice-add --script --file recording.m4a --name \"my voice\" --consent self")


@cli.command("voices")
@click.option("--json", "as_json", is_flag=True, help="Print one JSON line (for scripts).")
def list_voices(as_json):
    """List the narrator voices you have added."""
    from core.voices import available, describe
    if as_json:
        click.echo(json.dumps([{"name": v.name, "label": v.label, "language": v.language,
                                "consented": v.consent is not None}
                               for v in available().values()]))
    else:
        click.echo(describe())


if __name__ == '__main__':
    cli()
