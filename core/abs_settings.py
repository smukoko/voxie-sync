"""Each account's own Audiobookshelf: where it is, and its API token, encrypted.

The first account (the self-hoster) uses the server's ABS from the environment.
Every other account brings its own, and its token is a bearer credential to
that person's library, so it is stored only encrypted:

    users.abs_token = "fernet:" + Fernet(LIBRA_SECRET_KEY).encrypt(token)

LIBRA_SECRET_KEY lives in the server's .env, never beside the database, so a
database backup or leak carries no usable token. Several keys may be listed,
comma-separated: the first encrypts, all decrypt, which is how a key is
rotated (`gateway_admin.py rotate-abs-key` re-encrypts under the first).

Generate one straight into the file, without printing it:

    python3 -c "import base64,secrets;print('LIBRA_SECRET_KEY='+base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())" >> .env

A URL someone else typed is also a way to make this server send requests
where they choose, so an account other than the owner may only name a public
address, checked when it is set and again on every use (DNS can change in
between), and ABS calls never follow redirects.

Importing this has no side effects; the key is read when it is needed.
"""
import ipaddress
import os
import socket
from urllib.parse import urlparse

PREFIX = "fernet:"


class SecretKeyMissing(RuntimeError):
    pass


def _box():
    from cryptography.fernet import Fernet, MultiFernet
    keys = [k.strip() for k in os.environ.get("LIBRA_SECRET_KEY", "").split(",") if k.strip()]
    if not keys:
        raise SecretKeyMissing(
            "LIBRA_SECRET_KEY is not set on the gateway, so an Audiobookshelf token cannot be "
            "stored encrypted. See core/abs_settings.py for the one line that creates it.")
    try:
        return MultiFernet([Fernet(k.encode()) for k in keys])
    except ValueError as e:
        raise SecretKeyMissing(f"LIBRA_SECRET_KEY is not a valid Fernet key ({e}).")


def encrypt(token):
    return PREFIX + _box().encrypt(token.encode("utf-8")).decode("ascii")


def decrypt(stored):
    """The token, or None when it cannot be read (no key, wrong key, tampered).

    A value without the prefix predates encryption and is returned as is;
    encrypt_existing() converts those once a key exists.
    """
    if not stored:
        return None
    if not stored.startswith(PREFIX):
        return stored
    from cryptography.fernet import InvalidToken
    try:
        return _box().decrypt(stored[len(PREFIX):].encode("ascii")).decode("utf-8")
    except (SecretKeyMissing, InvalidToken):
        return None


def check_url(url, allow_private=False):
    """A clean base URL, or ValueError saying what is wrong with it."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("The Audiobookshelf address must start with http:// or https://")
    if parsed.username or parsed.password:
        raise ValueError("Put the token in the token field, not in the address.")
    if parsed.query or parsed.fragment:
        raise ValueError("The Audiobookshelf address cannot carry ?query or #fragment.")
    if not allow_private:
        _require_public(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def _require_public(host, port):
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValueError(f"'{host}' does not resolve.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if not ip.is_global or ip.is_multicast:
            raise ValueError(f"'{host}' is a private or local address ({ip}); this server "
                             "can only reach an Audiobookshelf on the public internet.")


def set_abs(conn, user_id, url, token, allow_private=False):
    """Store an account's ABS. The URL is checked, the token encrypted."""
    url = check_url(url, allow_private)
    token = (token or "").strip()
    if not token:
        raise ValueError("An Audiobookshelf API token is required.")
    conn.execute("UPDATE users SET abs_url = ?, abs_token = ? WHERE user_id = ?",
                 (url, encrypt(token), user_id))
    conn.commit()
    return url


def clear_abs(conn, user_id):
    conn.execute("UPDATE users SET abs_url = NULL, abs_token = NULL WHERE user_id = ?", (user_id,))
    conn.commit()


def stored_abs(conn, user_id, allow_private=False):
    """(url, token) this account set, or (None, None).

    None as well when the token cannot be decrypted or the address now
    resolves somewhere private: better no sync than a request sent there.
    """
    row = conn.execute("SELECT abs_url, abs_token FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if not row or not row[0] or not row[1]:
        return None, None
    token = decrypt(row[1])
    if token is None:
        print(f"   ⚠️ Account {user_id}: its Audiobookshelf token cannot be decrypted "
              "(LIBRA_SECRET_KEY missing or changed).")
        return None, None
    try:
        url = check_url(row[0], allow_private)
    except ValueError as e:
        print(f"   ⚠️ Account {user_id}: Audiobookshelf address refused ({e}).")
        return None, None
    return url, token


def verify(url, token, http=None, timeout=10):
    """(True, ABS username) if the token works there, else (False, reason)."""
    import requests
    http = http or requests
    try:
        r = http.get(f"{url}/api/me", headers={"Authorization": f"Bearer {token}"},
                     timeout=timeout, allow_redirects=False)
    except requests.exceptions.RequestException as e:
        return False, f"Audiobookshelf did not answer at {url} ({type(e).__name__})."
    if 300 <= r.status_code < 400:
        return False, f"{url} redirects to {r.headers.get('Location')}; use that address instead."
    if r.status_code in (401, 403):
        return False, "Audiobookshelf refused the token."
    if not r.ok:
        return False, f"Audiobookshelf answered HTTP {r.status_code}."
    try:
        return True, r.json().get("username") or "?"
    except ValueError:
        return False, f"{url} answered, but not like Audiobookshelf."


def encrypt_existing(conn):
    """Encrypt tokens stored before encryption. Returns how many. Needs the key."""
    rows = conn.execute("SELECT user_id, abs_token FROM users WHERE abs_token IS NOT NULL "
                        "AND abs_token NOT LIKE ?", (PREFIX + "%",)).fetchall()
    for user_id, token in rows:
        conn.execute("UPDATE users SET abs_token = ? WHERE user_id = ?", (encrypt(token), user_id))
    conn.commit()
    return len(rows)


def rotate(conn):
    """Re-encrypt every token under the first key in LIBRA_SECRET_KEY."""
    box = _box()
    rows = conn.execute("SELECT user_id, abs_token FROM users WHERE abs_token LIKE ?",
                        (PREFIX + "%",)).fetchall()
    for user_id, stored in rows:
        fresh = box.rotate(stored[len(PREFIX):].encode("ascii")).decode("ascii")
        conn.execute("UPDATE users SET abs_token = ? WHERE user_id = ?", (PREFIX + fresh, user_id))
    conn.commit()
    return len(rows)
