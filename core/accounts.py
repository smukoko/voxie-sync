"""Accounts for the sync gateway, in the shape KOReader already speaks.

KOReader's progress sync logs in with a username and `userkey = md5(password)`
and sends both as `x-auth-user` / `x-auth-key` on every call (see
plugins/kosync.koplugin/KOSyncClient.lua). So an account here is exactly that
pair, and no device needs anything new to authenticate.

The userkey is only ever stored as a salted PBKDF2 hash. It is an md5 of a
password -- weak on its own -- so it is treated as the secret it is.

The OPDS catalogue uses HTTP Basic with the raw password, which is checked by
hashing it to the same md5 first.

Pure sqlite and stdlib: importing this has no side effects.
"""
import hashlib
import hmac
import secrets

ITERATIONS = 60_000
_verified = {}   # (username, key) -> user_id; a process-lifetime cache of good logins


def _hash(secret, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt.encode(), ITERATIONS).hex()
    return f"pbkdf2${ITERATIONS}${salt}${digest}"


def _check(secret, stored):
    try:
        _, iterations, salt, digest = stored.split("$")
    except (AttributeError, ValueError):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt.encode(), int(iterations)).hex()
    return hmac.compare_digest(candidate, digest)


def userkey_for(password):
    """What KOReader sends as x-auth-key for this password."""
    # KOReader's protocol, not our choice: its key is the password's MD5.
    return hashlib.md5(password.encode("utf-8"), usedforsecurity=False).hexdigest()


def create_user(conn, username, userkey, upload_token=None):
    """Returns the new user_id, or None when the username is taken."""
    username = (username or "").strip()
    if not username or not userkey:
        raise ValueError("username and userkey are required")
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        return None
    cur = conn.execute(
        """INSERT INTO users (username, key_hash, upload_token_hash, created_at)
           VALUES (?, ?, ?, datetime('now'))""",
        (username, _hash(userkey), _token_digest(upload_token) if upload_token else None))
    conn.commit()
    return cur.lastrowid


def set_key(conn, username, userkey):
    conn.execute("UPDATE users SET key_hash = ? WHERE username = ?", (_hash(userkey), username))
    conn.commit()
    for k in [k for k in _verified if k[0] == username]:
        _verified.pop(k, None)


def verify(conn, username, userkey):
    """user_id for a valid kosync login, else None."""
    if not username or not userkey:
        return None
    cached = _verified.get((username, userkey))
    if cached:
        return cached
    row = conn.execute("SELECT user_id, key_hash FROM users WHERE username = ?", (username,)).fetchone()
    if row and _check(userkey, row[1]):
        _verified[(username, userkey)] = row[0]
        return row[0]
    return None


def verify_password(conn, username, password):
    """user_id for an HTTP Basic login (raw password), else None."""
    return verify(conn, username, userkey_for(password or ""))


def _token_digest(token):
    # Upload tokens are long random strings, so an unsalted sha256 is enough
    # and keeps the lookup an indexed equality.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def set_upload_token(conn, username, token):
    conn.execute("UPDATE users SET upload_token_hash = ? WHERE username = ?",
                 (_token_digest(token), username))
    conn.commit()


def user_for_upload_token(conn, token):
    if not token:
        return None
    row = conn.execute("SELECT user_id FROM users WHERE upload_token_hash = ?",
                       (_token_digest(token),)).fetchone()
    return row[0] if row else None


def new_secret(nbytes=24):
    return secrets.token_urlsafe(nbytes)


def owner_id(conn):
    """The first account: the self-hoster, and the owner of rows made before accounts."""
    row = conn.execute("SELECT MIN(user_id) FROM users").fetchone()
    return row[0] if row and row[0] is not None else None


def claim_unowned(conn, user_id):
    """Give books made before accounts existed (user_id 0) to `user_id`."""
    n = conn.execute("UPDATE books SET user_id = ? WHERE user_id = 0", (user_id,)).rowcount
    conn.commit()
    return n
