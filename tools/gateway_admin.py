#!/usr/bin/env python3
"""Accounts on a sync gateway. Run where the gateway's database lives.

    python tools/gateway_admin.py add-user alice            # prints a new password once
    python tools/gateway_admin.py add-user alice --userkey <md5>   # adopt an existing KOReader login
    python tools/gateway_admin.py set-password alice
    python tools/gateway_admin.py upload-token alice        # prints a new upload token once
    python tools/gateway_admin.py list
    python tools/gateway_admin.py set-abs alice --url https://abs.example.com   # asks for the token, hidden
    python tools/gateway_admin.py clear-abs alice
    python tools/gateway_admin.py rotate-abs-key            # re-encrypt tokens under LIBRA_SECRET_KEY's first key
    python tools/gateway_admin.py migrate-layout [--apply]  # ebook beside its audio: one ABS item per book
    python tools/gateway_admin.py abs-cleanup [--apply]     # forget ABS items whose folder is gone
    python tools/gateway_admin.py abs-repair                # fix items with stale tracks or duration

On the Docker host:  docker exec -it librarian_server python tools/gateway_admin.py list

The first account created owns every book that existed before accounts did.
Passwords and tokens are shown once and stored only as salted hashes.
"""
import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import abs_settings, accounts, library  # noqa: E402
from core.database import get_connection, setup_database   # noqa: E402

DB = os.environ.get("LIBRA_DB", os.path.join("library", "database.db"))

# Where ABS sees the library (docker-compose mounts ./library at /library) and
# where this process sees it. Only used to test whether an item's folder exists.
ABS_LIBRARY_ROOT = os.environ.get("ABS_LIBRARY_ROOT", "/library")


def _local_path(abs_path):
    if abs_path == ABS_LIBRARY_ROOT or abs_path.startswith(ABS_LIBRARY_ROOT.rstrip("/") + "/"):
        return os.path.join(library.LIBRARY, os.path.relpath(abs_path, ABS_LIBRARY_ROOT))
    return None


def abs_ghosts(base, token, http):
    """(item, reason) for every ABS item whose folder is not on disk.

    Decided from the disk, not from ABS's own "missing" flag, which is set only
    after a scan notices: an item is a ghost when the folder it points at does
    not exist here. An item outside the mounted library is never judged.
    """
    headers = {"Authorization": f"Bearer {token}"}
    ghosts = []
    for lib in http.get(f"{base}/api/libraries", headers=headers, timeout=20).json().get("libraries", []):
        items = http.get(f"{base}/api/libraries/{lib['id']}/items", params={"limit": 0},
                         headers=headers, timeout=60).json().get("results", [])
        for item in items:
            local = _local_path(item.get("path") or "")
            if local is not None and not os.path.exists(local):
                ghosts.append((lib, item, f"{item.get('path')} is gone"))
    return ghosts


def abs_cleanup(apply=False, http=None):
    """Forget ABS items whose folder is gone. Never deletes files (no ?hard=1)."""
    import requests
    http = http or requests
    base, token = os.environ.get("ABS_URL", "").rstrip("/"), os.environ.get("ABS_API_TOKEN")
    if not (base and token):
        return "ABS_URL and ABS_API_TOKEN must be set"
    ghosts = abs_ghosts(base, token, http)
    for lib, item, why in ghosts:
        title = (item.get("media") or {}).get("metadata", {}).get("title")
        print(f"   {'removing' if apply else 'would remove'} [{lib['name']}] {title!r} ({item['id']}): {why}")
        if apply:
            r = http.delete(f"{base}/api/items/{item['id']}",
                            headers={"Authorization": f"Bearer {token}"}, timeout=20)
            if not r.ok:
                print(f"      ⚠️ ABS answered {r.status_code}")
    print(f"{len(ghosts)} ghost item(s)." + ("" if apply or not ghosts else "  Re-run with --apply."))
    return 0


def abs_repair(wait=0, http=None, gateway=None, sleep=None):
    """Repair ABS items with a file listed twice or a stale duration.

    start.sh runs this as the container starts, so a deploy heals items broken
    before the gateway repaired them itself (Twisted). Audiobookshelf boots
    beside the gateway, so it is waited for up to `wait` seconds.
    """
    import time
    import requests
    http, sleep = http or requests, sleep or time.sleep
    base = os.environ.get("ABS_URL", "").rstrip("/")
    if not base:
        return "ABS_URL must be set"
    deadline = time.monotonic() + wait
    while True:
        try:
            if http.get(f"{base}/ping", timeout=5).status_code == 200:
                break
        except requests.RequestException:
            pass
        if time.monotonic() >= deadline:
            return f"Audiobookshelf at {base} does not answer; nothing repaired."
        sleep(5)
    if gateway is None:
        import librerian_gateway_v3 as gateway
    fixed = gateway.repair_abs_library()
    print(f"{len(fixed)} Audiobookshelf item(s) repaired" + (": " + ", ".join(fixed) if fixed else "."))
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add-user")
    add.add_argument("username")
    add.add_argument("--userkey", help="md5 of an existing KOReader password, to keep a device's login")
    pw = sub.add_parser("set-password")
    pw.add_argument("username")
    pw.add_argument("--userkey", help="md5 of the new password, chosen elsewhere (voxie init)")
    tok = sub.add_parser("upload-token")
    tok.add_argument("username")
    sub.add_parser("list")
    sa = sub.add_parser("set-abs", help="an account's own Audiobookshelf (token read hidden, stored encrypted)")
    sa.add_argument("username")
    sa.add_argument("--url", required=True)
    sa.add_argument("--token-stdin", action="store_true", help="read the token from stdin instead of a prompt")
    sa.add_argument("--no-verify", action="store_true", help="store without asking that ABS whether the token works")
    ca = sub.add_parser("clear-abs")
    ca.add_argument("username")
    sub.add_parser("rotate-abs-key")
    mig = sub.add_parser("migrate-layout", help="move library/ebooks/<book> into library/audiobooks/<book>")
    mig.add_argument("--apply", action="store_true", help="without it, only print what would move")
    rep = sub.add_parser("abs-repair", help="fix ABS items with a file listed twice or a stale duration")
    rep.add_argument("--wait", type=int, default=0,
                     help="seconds to wait for Audiobookshelf to answer first (start.sh: it boots too)")
    cln = sub.add_parser("abs-cleanup", help="remove ABS items whose folder no longer exists (never files)")
    cln.add_argument("--apply", action="store_true", help="without it, only print what would be removed")
    args = ap.parse_args()

    setup_database(DB)
    conn = get_connection(DB)

    if args.cmd == "add-user":
        password = None if args.userkey else accounts.new_secret(12)
        userkey = args.userkey or accounts.userkey_for(password)
        uid = accounts.create_user(conn, args.username, userkey)
        if uid is None:
            sys.exit(f"'{args.username}' already exists")
        if accounts.owner_id(conn) == uid:
            n = accounts.claim_unowned(conn, uid)
            print(f"First account: took ownership of {n} existing book(s).")
        print(f"user_id  {uid}\nusername {args.username}")
        if password:
            print(f"password {password}\nuserkey  {userkey}   (what KOReader stores)")
    elif args.cmd == "set-password":
        if args.userkey:
            accounts.set_key(conn, args.username, args.userkey)
            print(f"{args.username}: password updated.")
        else:
            password = accounts.new_secret(12)
            accounts.set_key(conn, args.username, accounts.userkey_for(password))
            print(f"password {password}\nuserkey  {accounts.userkey_for(password)}")
    elif args.cmd == "migrate-layout":
        moved, conflicts = library.migrate_layout(conn, apply=args.apply)
        for path, why in conflicts:
            print(f"   ⚠️ left in place: {path} ({why})")
        print(f"{'Moved' if args.apply else 'Would move'} {len(moved)} file(s); {len(conflicts)} left in place."
              + ("" if args.apply else "  Re-run with --apply."))
    elif args.cmd == "abs-cleanup":
        sys.exit(abs_cleanup(apply=args.apply))
    elif args.cmd == "abs-repair":
        sys.exit(abs_repair(wait=args.wait))
    elif args.cmd == "upload-token":
        token = accounts.new_secret(32)
        accounts.set_upload_token(conn, args.username, token)
        print(f"upload token {token}")
    elif args.cmd == "list":
        owner = accounts.owner_id(conn)
        for row in conn.execute("""SELECT u.user_id, u.username, u.created_at,
                                          (SELECT COUNT(*) FROM books b WHERE b.user_id = u.user_id),
                                          u.abs_url, u.abs_token
                                   FROM users u ORDER BY u.user_id"""):
            if row[4]:
                sealed = "encrypted" if (row[5] or "").startswith(abs_settings.PREFIX) else "PLAIN TEXT"
                abs_state = f"ABS {row[4]} ({sealed})"
            else:
                abs_state = "ABS: the server's" if row[0] == owner else "ABS: none"
            print(f"{row[0]:>4}  {row[1]:20} created {row[2]}  books {row[3]}  {abs_state}")
    elif args.cmd in ("set-abs", "clear-abs"):
        row = conn.execute("SELECT user_id FROM users WHERE username = ?", (args.username,)).fetchone()
        if row is None:
            sys.exit(f"no account '{args.username}'")
        uid = row[0]
        if args.cmd == "clear-abs":
            abs_settings.clear_abs(conn, uid)
            print(f"{args.username}: Audiobookshelf cleared.")
            return
        is_owner = uid == accounts.owner_id(conn)
        try:
            url = abs_settings.check_url(args.url, allow_private=is_owner)
        except ValueError as e:
            sys.exit(str(e))
        # Never from argv: it would sit in shell history and in `ps`.
        token = sys.stdin.readline().strip() if args.token_stdin else getpass.getpass("Audiobookshelf API token: ")
        if not args.no_verify:
            ok, who = abs_settings.verify(url, token)
            if not ok:
                sys.exit(who)
            print(f"Token works: Audiobookshelf user '{who}'.")
        try:
            abs_settings.set_abs(conn, uid, url, token, allow_private=is_owner)
        except (abs_settings.SecretKeyMissing, ValueError) as e:
            sys.exit(str(e))
        print(f"{args.username}: Audiobookshelf set to {url} (token stored encrypted).")
    elif args.cmd == "rotate-abs-key":
        try:
            print(f"Re-encrypted {abs_settings.rotate(conn)} token(s) under the first LIBRA_SECRET_KEY.")
        except abs_settings.SecretKeyMissing as e:
            sys.exit(str(e))


if __name__ == "__main__":
    main()
