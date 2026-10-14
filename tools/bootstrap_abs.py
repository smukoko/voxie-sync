#!/usr/bin/env python3
"""Set up a brand-new Audiobookshelf instance without clicking through the UI.

A fresh ABS install needs three things before Voxie can use it: a root
user, a library pointing at the audiobook folder, and an API key. Doing that by
hand is the longest manual step in the whole install, so it is scripted here.

    python tools/bootstrap_abs.py --url http://<host>:13378

Prompts for the credentials it needs and prints the API key at the end -- that
is the value `voxie init` asks for. ABS returns an API key exactly once, so
the key is not recoverable afterwards; rerun with --reset-key to issue another.

Safe to re-run: every step checks whether it has already been done.
"""
import argparse
import getpass
import json
import sys
import urllib.error
import urllib.request


class AbsError(Exception):
    pass


def call(url, path, payload=None, token=None, method=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url.rstrip("/") + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode().strip()
            if not body:
                return {}
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                # /init answers with a plain-text body, not JSON. Returning the
                # text keeps the caller working instead of dying on a success.
                return {"_text": body}
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        raise AbsError(f"{method or 'POST'} {path} -> HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise AbsError(f"cannot reach {url}{path}: {e.reason}") from None


def find_token(login_response):
    """Pull the bearer token out of a login response.

    ABS moved the token around when it introduced refresh tokens, so accept
    whichever field this server version actually uses rather than pinning one.
    """
    for key in ("accessToken", "token"):
        if login_response.get(key):
            return login_response[key]
    user = login_response.get("user") or {}
    for key in ("accessToken", "token"):
        if user.get(key):
            return user[key]
    raise AbsError(f"no token in login response; got keys {sorted(login_response)}")


def bootstrap(url, username, password, library_name="Librarian Library",
              library_path="/library", key_name="voxie", reset_key=False, log=print):
    """Root user, library and API key on the ABS at `url`; returns the key.

    The server setup (server/install_server.py) calls this too, so the steps
    live here once. The key is returned, never logged.
    """
    log(f"🔍 Checking {url} ...")
    status = call(url, "/status", method="GET")
    log(f"   Audiobookshelf {status.get('serverVersion')} | initialised: {status.get('isInit')}")
    if not username or not password:
        raise AbsError("username and password are both required")

    # 1. Root user -- only possible while the server has never been set up.
    if not status.get("isInit"):
        log("\n👤 Creating the root user ...")
        call(url, "/init", {"newRoot": {"username": username, "password": password}})
        log("   created")
    else:
        log("\n👤 Server already initialised, skipping root user creation")

    # 2. Log in.
    log("\n🔑 Logging in ...")
    token = find_token(call(url, "/login", {"username": username, "password": password}))
    log("   authenticated")

    # 3. Library. Without one pointing at the audiobook folder, generated books
    #    have nowhere to appear and the gateway can never resolve an abs_id.
    log(f"\n📚 Library at {library_path} ...")
    libraries = call(url, "/api/libraries", token=token, method="GET").get("libraries", [])
    existing = next(
        (l for l in libraries
         if any(f.get("fullPath") == library_path for f in l.get("folders", []))),
        None,
    )
    if existing:
        log(f"   already exists: '{existing['name']}'")
    else:
        created = call(url, "/api/libraries", {
            "name": library_name,
            "folders": [{"fullPath": library_path}],
            "mediaType": "book",
        }, token=token)
        lib = created.get("library", created)
        log(f"   created '{lib.get('name')}'")

    # 4. API key.
    log(f"\n🎟️  API key '{key_name}' ...")
    keys = call(url, "/api/api-keys", token=token, method="GET").get("apiKeys", [])
    clash = next((k for k in keys if k.get("name") == key_name), None)
    if clash and not reset_key:
        raise AbsError(
            f"a key named '{key_name}' already exists and its secret cannot be "
            f"read back. Re-run with --reset-key to replace it."
        )
    if clash:
        call(url, f"/api/api-keys/{clash['id']}", token=token, method="DELETE")
        log("   removed the previous key")

    me = call(url, "/api/me", token=token, method="GET")
    created = call(url, "/api/api-keys", {
        "name": key_name,
        "userId": me.get("id"),
        "isActive": True,
    }, token=token)
    api_key = (created.get("apiKey") or {}).get("apiKey") or created.get("apiKey")
    if not isinstance(api_key, str):
        raise AbsError(f"could not find the key in the response: {json.dumps(created)[:300]}")
    return api_key


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="e.g. http://1.2.3.4:13378")
    ap.add_argument("--username")
    ap.add_argument("--password")
    ap.add_argument("--library-name", default="Librarian Library")
    ap.add_argument("--library-path", default="/library",
                    help="path INSIDE the ABS container (docker-compose mounts ./library there)")
    ap.add_argument("--key-name", default="voxie")
    ap.add_argument("--reset-key", action="store_true",
                    help="delete an existing key of the same name and issue a new one")
    args = ap.parse_args()

    username = args.username or input("   Admin username: ").strip()
    password = args.password or getpass.getpass("   Admin password: ")
    api_key = bootstrap(args.url, username, password, args.library_name,
                        args.library_path, args.key_name, args.reset_key)

    print("\n" + "=" * 64)
    print("✅ Audiobookshelf is ready.")
    print("=" * 64)
    print("\nAPI key (shown once — this is what `voxie init` asks for):\n")
    print(f"   {api_key}\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AbsError as e:
        print(f"\n❌ {e}", file=sys.stderr)
        sys.exit(1)
