"""The setup page: `voxie init` as a page in the browser, run on this computer.

Nothing here is hosted anywhere. The page is served on 127.0.0.1 for the
length of the setup, and does what the terminal wizard does, through the same
functions (libra_cli.main, libra_cli.server_setup): the server over SSH,
narration, a voice, the watcher, the Kobo.

Any website open in the same browser can send requests to 127.0.0.1, so every
/api call must carry the session key, which only the opened page knows: it is
in the URL's #fragment, which the browser never sends anywhere. A custom
header also makes the browser refuse cross-site requests outright (they would
need a CORS preflight, which this server never answers), and the Host header
must be this server's own, which stops DNS rebinding.
"""
import hmac
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser

from flask import Flask, abort, jsonify, request, send_from_directory

from core import accounts, links
from core.gateway_address import abs_url, gateway_url, parse_gateway
from libra_cli import main as cli
from libra_cli import server_setup

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
LOGO_DIR = os.path.join(cli.repo_root_dir(), "assets", "logo")
IDLE_LIMIT = 3 * 3600   # a forgotten tab does not keep a server running for days


class Job:
    """A long step (server install, voice, Kobo) whose log the page polls."""

    def __init__(self, work):
        self.id = uuid.uuid4().hex
        self.lines, self.status, self.result, self.error = [], "running", None, None
        threading.Thread(target=self._run, args=(work,), daemon=True).start()

    def log(self, line):
        if line.strip():
            self.lines.append(line)

    def _run(self, work):
        try:
            self.result = work(self)
            self.status = "done"
        except Exception as e:   # shown on the page, in its words
            self.error = str(e) or type(e).__name__
            self.status = "failed"

    def view(self, start):
        return {"lines": self.lines[start:], "next": len(self.lines),
                "status": self.status, "result": self.result, "error": self.error}


def create_app(key, port_holder):
    app = Flask(__name__, static_folder=None)
    jobs = {}
    session = {"upload_token": None, "user": None, "password": None, "last": time.time()}

    @app.before_request
    def guard():
        session["last"] = time.time()
        host = request.host.split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            abort(403)
        if request.path.startswith("/api/"):
            given = request.headers.get("X-Voxie-Key", "")
            if not hmac.compare_digest(given, key):
                abort(403)

    @app.after_request
    def headers(resp):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; media-src 'self' blob:; style-src 'self'; script-src 'self'; "
            "font-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        return resp

    def body():
        return request.get_json(silent=True) or {}

    def start(work):
        job = Job(work)
        jobs[job.id] = job
        return jsonify({"job": job.id})

    # ---------------------------------------------------------------- page --
    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/static/<path:name>")
    def static_file(name):
        return send_from_directory(STATIC, name)

    @app.get("/logo/<path:name>")
    def logo(name):
        return send_from_directory(LOGO_DIR, name)

    # --------------------------------------------------------------- state --
    @app.get("/api/state")
    def state():
        from core import envfile
        from core.voices import available
        config = cli._load_config()
        env = envfile.read(cli.ensure_env_file())
        if session["upload_token"] is None:
            # Reused when one exists: a new one would break uploads until the
            # server's .env caught up.
            session["upload_token"] = config.get("UPLOAD_TOKEN") or secrets.token_urlsafe(32)
        runpod_key = env.get("RUNPOD_API_KEY", "")
        return jsonify({
            "isMac": cli.IS_MAC,
            "username": config.get("ACCOUNT_USERNAME") or "",
            "hasPassword": bool(config.get("ACCOUNT_PASSWORD")),
            "serverSsh": config.get("SERVER_SSH", ""),
            "serverHost": (config.get("SERVER_SSH") or "").split("@")[-1],
            "domain": config.get("DOMAIN", ""),
            "existingAddress": config.get("GATEWAY_URL") or config.get("GATEWAY_IP") or "",
            "configured": bool(config.get("GATEWAY_IP")),
            "currentAbsUrl": abs_url(config) if config.get("GATEWAY_IP") else "",
            "provider": env.get("TTS_PROVIDER", "kokoro"),
            "hasRunpodKey": bool(runpod_key) and not runpod_key.startswith("your_"),
            "hasOpenRouterKey": bool(cli.openrouter_key(env)),
            "templateId": cli.RUNPOD_PUBLIC_TEMPLATE_ID or env.get("RUNPOD_TEMPLATE_ID", ""),
            "publicTemplate": bool(cli.RUNPOD_PUBLIC_TEMPLATE_ID),
            "voices": list(available()),
            "sshKey": server_setup.public_key(),
            "links": {"hetzner": links.HETZNER_URL, "runpod": links.RUNPOD_URL,
                      "openrouter": links.OPENROUTER_URL,
                      "disclosure": links.DISCLOSURE, "donate": links.DONATE_URL},
        })

    # ------------------------------------------------------------- account --
    @app.post("/api/account")
    def account():
        b = body()
        user = (b.get("username") or "").strip()
        password = b.get("password") or ""
        if not password and b.get("keepPassword"):
            password = cli._load_config().get("ACCOUNT_PASSWORD") or ""
        if not user or any(c.isspace() for c in user):
            return jsonify({"ok": False, "field": "username", "error": "Choose a username without spaces."})
        if len(password) < 8:
            return jsonify({"ok": False, "field": "password", "error": "Use at least 8 characters."})
        session["user"], session["password"] = user, password
        return jsonify({"ok": True})

    # -------------------------------------------------------------- server --
    @app.post("/api/ssh/key")
    def ssh_key():
        try:
            return jsonify({"ok": True, "sshKey": server_setup.create_key()})
        except RuntimeError as e:
            return jsonify({"ok": False, "error": str(e)})

    def target_for(host):
        host = (host or "").strip()
        return host if "@" in host else f"root@{host}"

    @app.post("/api/ssh/check")
    def ssh_check():
        host = (body().get("host") or "").strip()
        if not host:
            return jsonify({"ok": False, "error": "Paste the server's IP address."})
        target = target_for(host)
        ok, detail = server_setup.check_ssh(target)
        if not ok:
            return jsonify({"ok": False, "error": server_setup.explain_ssh_error(detail, host)})
        other = server_setup.foreign_stack(target)
        if other:
            return jsonify({"ok": False, "error": (
                f"This server already runs Voxie from {other}, set up by hand. Choose "
                "\"I already run a server\" for it, or use a fresh server.")})
        return jsonify({"ok": True})

    @app.post("/api/dns/check")
    def dns_check():
        b = body()
        domain = (b.get("domain") or "").strip().lower()
        ip = target_for(b.get("host")).split("@")[1]
        return jsonify({"ok": server_setup.resolves_to(domain, ip), "ip": ip})

    def need_account():
        if not session["user"]:
            abort(409)

    @app.post("/api/server/install")
    def server_install():
        need_account()
        b = body()
        target = target_for(b.get("host"))
        ip = target.split("@")[1]
        domain = (b.get("domain") or "").strip().lower()
        user, password, upload_token = session["user"], session["password"], session["upload_token"]

        def work(job):
            job.log(f"Copying Voxie to {ip} ...")
            server_setup.push_code(cli.repo_root_dir(), target)
            abs_token = server_setup.install(target, {
                "upload_token": upload_token,
                "account_username": user,
                "account_userkey": accounts.userkey_for(password),
                "abs_username": user,
                "abs_password": password,
                "domain": domain,
                "public_host": ip,
            }, on_line=job.log)
            server = {
                "GATEWAY_IP": domain or ip,
                **({"GATEWAY_URL": f"https://{domain}", "ABS_PUBLIC_URL": f"https://{domain}:13378",
                    "DOMAIN": domain} if domain else {}),
                "SERVER_SSH": target,
                "ABS_API_TOKEN": abs_token,
            }
            config = cli.save_server_setup(server, user, password, upload_token, cli.ensure_env_file())
            return {"gatewayUrl": gateway_url(config), "absUrl": abs_url(config)}
        return start(work)

    @app.post("/api/server/existing")
    def server_existing():
        need_account()
        b = body()
        try:
            host, url = parse_gateway(b.get("address") or "")
        except ValueError as e:
            return jsonify({"ok": False, "field": "address", "error": str(e)})
        if not host:
            return jsonify({"ok": False, "field": "address", "error": "Paste your server's address."})
        keys = {"GATEWAY_IP": host, **({"GATEWAY_URL": url} if url else {})}
        userkey = accounts.userkey_for(session["password"])
        token = (b.get("absKey") or "").strip()
        upload_token = session["upload_token"]
        ssh = (b.get("ssh") or "").strip()
        linked = False
        if ssh:
            # With a login, the server's part is done from here (nothing typed
            # on the server): its own upload token and ABS key, and the account.
            target = target_for(ssh)
            ok, detail = server_setup.check_ssh(target)
            if not ok:
                return jsonify({"ok": False, "field": "ssh", "error": server_setup.explain_ssh_error(detail, ssh)})
            try:
                found = server_setup.link_existing(target, session["user"], userkey)
            except RuntimeError as e:
                return jsonify({"ok": False, "field": "ssh", "error": str(e)})
            upload_token, linked = found["upload_token"], True
            session["upload_token"] = upload_token
            token = token or found["abs_token"]
            keys["SERVER_SSH"] = target
        ok, detail = cli.verify_abs_token(abs_url(keys), token)
        if not ok:
            return jsonify({"ok": False, "field": "absKey", "error": detail})
        keys["ABS_API_TOKEN"] = token
        config = cli.save_server_setup(keys, session["user"], session["password"],
                                       upload_token, cli.ensure_env_file())
        return jsonify({"ok": True, "absUrl": abs_url(config), "linked": linked,
                        "envLine": f"UPLOAD_TOKEN={upload_token}",
                        "addUser": ("docker exec librarian_server python tools/gateway_admin.py "
                                    f"add-user {session['user']} --userkey {userkey}")})

    # ----------------------------------------------------------- narration --
    @app.post("/api/runpod/check")
    def runpod_check():
        works = cli._runpod_key_works((body().get("key") or "").strip())
        return jsonify({"ok": works is not False,
                        "error": None if works is not False else "RunPod did not accept this key."})

    @app.post("/api/openrouter/check")
    def openrouter_check():
        works = cli._openrouter_key_works((body().get("key") or "").strip())
        return jsonify({"ok": works is not False,
                        "error": None if works is not False else "OpenRouter did not accept this key."})

    @app.post("/api/narration")
    def narration():
        from core import envfile
        b = body()
        env_path = cli.ensure_env_file()
        provider = b.get("provider")
        if provider == "kokoro":
            cli.apply_narration(env_path, "kokoro")
        elif provider == "runpod":
            key = (b.get("key") or "").strip() or envfile.read(env_path).get("RUNPOD_API_KEY", "")
            template = cli.RUNPOD_PUBLIC_TEMPLATE_ID or (b.get("template") or "").strip()
            if not key or not template:
                return jsonify({"ok": False, "error": "RunPod needs an API key and a template id."})
            cli.apply_narration(env_path, "cosyvoice", key, template)
            # Only a new key is written: an empty field keeps the one in .env.
            director_key = (b.get("openrouterKey") or "").strip()
            if director_key:
                cli.apply_director(env_path, director_key)
        else:
            abort(400)
        return jsonify({"ok": True, "docker": cli._docker_ready()})

    # A voice is checked into a staging folder first: the page plays the
    # trimmed clip and shows what the check found, and only "Keep" puts it in
    # voices/ (LAUNCH_PLAN 5b). Nothing half-made ever reaches the narrator.
    staged = {}

    def drop_staged(token):
        entry = staged.pop(token, None)
        if entry:
            shutil.rmtree(entry["dir"], ignore_errors=True)

    @app.post("/api/voice")
    def voice():
        from core.voices import VOICES_DIR
        from libra_cli.voice import slug
        f = request.files.get("file")
        form = request.form
        tmpdir = None
        path = None
        if f and f.filename:
            tmpdir = tempfile.mkdtemp(prefix="voxie-voice-")
            path = os.path.join(tmpdir, os.path.basename(f.filename))
            f.save(path)
        url = (form.get("url") or "").strip() or None
        if not (url or path):
            return jsonify({"ok": False, "error": "Paste a link or choose a file."})
        name = slug(form.get("name") or "")
        if name and os.path.exists(os.path.join(VOICES_DIR, f"{name}.wav")):
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)
            return jsonify({"ok": False, "error": f"A voice called '{name}' already exists. Pick another name."})

        def work(job):
            from libra_cli.voice import add_voice
            job.log("Cutting the clip and listening to it ...")
            stage = tempfile.mkdtemp(prefix="voxie-voice-stage-")
            try:
                r = add_voice(url=None if path else url, file=path, start=form.get("start") or None,
                              end=form.get("end") or None, text=form.get("text", ""),
                              name=form.get("name") or None, language=form.get("language") or "en",
                              consent=form.get("consent"), source_name=f.filename if path else None,
                              voices_dir=stage)
            except Exception:
                shutil.rmtree(stage, ignore_errors=True)
                raise
            finally:
                if tmpdir:
                    shutil.rmtree(tmpdir, ignore_errors=True)
            token = uuid.uuid4().hex
            staged[token] = {"dir": stage, "name": r["name"]}
            return {"staged": token, "name": r["name"], "seconds": r["seconds"],
                    "warnings": r["warnings"], "heard": r.get("heard"), "advice": r.get("advice")}
        return start(work)

    @app.get("/api/voice/preview/<token>")
    def voice_preview(token):
        entry = staged.get(token) or abort(404)
        return send_from_directory(entry["dir"], f"{entry['name']}.wav", mimetype="audio/wav")

    @app.post("/api/voice/keep")
    def voice_keep():
        from core.voices import VOICES_DIR
        token = body().get("staged") or ""
        entry = staged.get(token)
        if not entry:
            return jsonify({"ok": False, "error": "That clip is gone; add it again."})
        name = entry["name"]
        os.makedirs(VOICES_DIR, exist_ok=True)
        if os.path.exists(os.path.join(VOICES_DIR, f"{name}.wav")):
            return jsonify({"ok": False, "error": f"A voice called '{name}' already exists. Pick another name."})
        for ext in (".json", ".wav"):       # the clip last: a voice exists once its .wav does
            shutil.move(os.path.join(entry["dir"], name + ext), os.path.join(VOICES_DIR, name + ext))
        drop_staged(token)
        return jsonify({"ok": True, "name": name})

    @app.post("/api/voice/discard")
    def voice_discard():
        drop_staged(body().get("staged") or "")
        return jsonify({"ok": True})

    @app.post("/api/watcher")
    def watcher():
        ok, error = start_watcher()
        return jsonify({"ok": ok, "error": error})

    # ---------------------------------------------------------------- kobo --
    @app.get("/api/kobo")
    def kobo():
        path = cli.find_kobo_mount() if cli.IS_MAC else None
        return jsonify({"connected": bool(path), "name": os.path.basename(path) if path else None,
                        "docker": cli._docker_ready()})

    @app.post("/api/docker/open")
    def docker_open():
        if cli.IS_MAC:
            subprocess.run(["open", "-a", "Docker"], capture_output=True)
        return jsonify({"ok": True})

    @app.post("/api/kobo/install")
    def kobo_install():
        # The look is the install's last step (libra_cli/kobo_look.py); anything
        # else than the two known ones keeps what the Kobo has.
        look = body().get("look")
        look = look if look in ("kobo", "koreader") else None
        return start(lambda job: setup_kobo(job.log, look))

    @app.get("/api/jobs/<job_id>")
    def job_view(job_id):
        job = jobs.get(job_id) or abort(404)
        return jsonify(job.view(int(request.args.get("from", 0))))

    # ---------------------------------------------------------------- done --
    @app.post("/api/open-dropzone")
    def open_dropzone():
        os.makedirs(cli.DROPZONE, exist_ok=True)
        opener = "open" if cli.IS_MAC else "xdg-open"
        if shutil.which(opener):
            subprocess.Popen([opener, cli.DROPZONE])
        return jsonify({"ok": True, "path": cli.DROPZONE})

    @app.post("/api/finish")
    def finish():
        port_holder["stop"] = True
        return jsonify({"ok": True})

    app.config["session"] = session
    return app


def start_watcher():
    """(ok, error): `voxie start-ghost`, as its own process (it loads launchd)."""
    r = subprocess.run([sys.executable, "-m", "libra_cli.main", "start-ghost"],
                       capture_output=True, text=True, cwd=cli.repo_root_dir())
    failures = [l for l in (r.stdout + r.stderr).splitlines() if "❌" in l]
    if r.returncode == 0 and not failures:
        return True, None
    return False, failures[-1].replace("❌", "").strip() if failures else "The watcher did not start."


def setup_kobo(on_line, look=None):
    """`voxie setup-kobo --json [--look LOOK]`, its log line by line; returns its result."""
    proc = subprocess.Popen([sys.executable, "-m", "libra_cli.main", "setup-kobo", "--json",
                             *(["--look", look] if look else [])],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            cwd=cli.repo_root_dir())
    out = []
    reader = threading.Thread(target=lambda: out.append(proc.stdout.read()), daemon=True)
    reader.start()
    for line in proc.stderr:
        on_line(line.rstrip("\n"))
    proc.wait()
    reader.join()
    result = None
    for line in reversed("".join(out).splitlines()):
        try:
            result = json.loads(line)
            break
        except ValueError:
            continue
    if not result or not result.get("success"):
        raise RuntimeError(_kobo_error((result or {}).get("error") or "the installer stopped"))
    return result


_KOBO_ERRORS = {
    "kobo_not_detected": "The Kobo is not connected any more. Plug it in and tap Connect.",
    "config_not_found": "Finish the server step first.",
    "gateway_ip_not_configured": "Finish the server step first.",
    "installer_macos_only": "The Kobo installer runs on a Mac.",
}


def _kobo_error(code):
    return _KOBO_ERRORS.get(code, code)


def serve(open_browser=True, port=0):
    """Serve the page on 127.0.0.1 until the person finishes, then return."""
    from werkzeug.serving import make_server
    key = secrets.token_urlsafe(24)
    holder = {"stop": False}
    app = create_app(key, holder)
    server = make_server("127.0.0.1", port, app, threaded=True)
    url = f"http://127.0.0.1:{server.server_port}/#k={key}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print("\n  Voxie setup is open in your browser.")
    print(f"  If it did not open: {url}")
    print("  Keep this window open until the setup is done. Ctrl-C stops it.\n")
    if open_browser:
        webbrowser.open(url)
    session = app.config["session"]
    try:
        while not holder["stop"] and time.time() - session["last"] < IDLE_LIMIT:
            time.sleep(0.5)
        time.sleep(1)   # let the last response reach the page
    except KeyboardInterrupt:
        pass
    server.shutdown()
    print("  Setup page closed.")
