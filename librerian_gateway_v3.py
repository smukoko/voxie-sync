"""The sync gateway: KOReader's progress-sync protocol, answered against the map.

Speaks kosync to the Kobo, keeps each reader's position, and translates it to
and from Audiobookshelf seconds through the sync map (`sync_nodes`, and
`word_timings` where a book has them). Every request belongs to an account:
KOReader already sends `x-auth-user` / `x-auth-key` on every sync call, and the
OPDS catalogue logs in with HTTP Basic.

Books pair with devices by fingerprint. The KOReader hash of the served KEPUB
(core/fingerprint.py) is recorded when the map arrives, so there is no
handshake and no state shared between requests.
"""
import hashlib
import hmac
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
import zipfile
from datetime import datetime, timezone
from xml.etree.ElementTree import Element, SubElement, tostring

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_file

from core import abs_settings, accounts, landing, library, page_audio
from core.database import get_connection, setup_database
from core.fingerprint import koreader_partial_md5

load_dotenv()

ABS_URL = os.getenv("ABS_URL")
ABS_API_TOKEN = os.getenv("ABS_API_TOKEN")
DB_PATH = os.path.join("library", "database.db")

# The self-hoster's upload secret (`voxie init` writes it to
# ~/.librasync.json). It acts for the first account. Other accounts carry their
# own upload tokens.
UPLOAD_TOKEN = os.getenv("UPLOAD_TOKEN")

# KOReader's "Register" button. Off by default: an account is created by
# `python tools/gateway_admin.py add-user`. Accounts are isolated from each
# other, so turning this on exposes no one's data, only the server's capacity.
ALLOW_REGISTRATION = os.getenv("ALLOW_REGISTRATION", "0") == "1"

# Whether this gateway stores and serves book files (OPDS, downloads) at all.
# A self-hoster's gateway does, for its first account. A hosted sync service
# should not: it keeps maps and positions, never books.
SERVE_BOOKS = os.getenv("SERVE_BOOKS", "1") == "1"

ABS_TIMEOUT = 10   # an ABS that hangs must not hang the Kobo's request with it

app = Flask(__name__)
# No CORS: the clients are KOReader and the Mac, never a web page, and
# allowing every origin let any site a reader visits call this server.
logging.getLogger('werkzeug').setLevel(logging.INFO)

UPLOAD_FOLDER = 'incoming'


def get_db():
    conn = get_connection(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def clean_xpath(raw_xpath):
    """Strips KOReader's span/text offsets and forces the DB's point(/1...) format."""
    clean = raw_xpath.replace("point(/1", "").rstrip(")")
    clean = re.sub(r'/span\[\d+\].*', '', clean)
    clean = re.sub(r'/text\(\).*', '', clean)
    return f"point(/1{clean})"


# --- Accounts ---------------------------------------------------------------

def kosync_user(req):
    """user_id for KOReader's x-auth-user / x-auth-key, or None."""
    with get_db() as conn:
        return accounts.verify(conn, req.headers.get("x-auth-user"), req.headers.get("x-auth-key"))


def basic_user(req):
    """user_id for an HTTP Basic login (the OPDS catalogue), or None."""
    auth = req.authorization
    if not auth:
        return None
    with get_db() as conn:
        return accounts.verify_password(conn, auth.username, auth.password)


def upload_user(req):
    """user_id for an X-Libra-Token, or None. The env token acts for the owner."""
    token = req.headers.get("X-Libra-Token")
    if not token:
        return None
    with get_db() as conn:
        # Constant time: a plain == leaks how many leading characters match.
        if UPLOAD_TOKEN and hmac.compare_digest(token.encode(), UPLOAD_TOKEN.encode()):
            return accounts.owner_id(conn) or 0
        return accounts.user_for_upload_token(conn, token)


def _unauthorized():
    return jsonify({"message": "Unauthorized"}), 401


def abs_for(user_id):
    """(base_url, token) of this account's Audiobookshelf, or (None, None).

    The first account uses the server's own ABS from the environment unless it
    set one. Other accounts bring their own (core/abs_settings.py): the token
    is stored encrypted, and the address must be public.
    """
    with get_db() as conn:
        owner = accounts.owner_id(conn)
        is_owner = user_id in (owner, 0) or owner is None
        base, token = abs_settings.stored_abs(conn, user_id, allow_private=is_owner)
    if base:
        return base, token
    if is_owner:
        return ABS_URL, ABS_API_TOKEN
    return None, None


# --- Book lookup ------------------------------------------------------------

class LibrarianDB:
    @staticmethod
    def get_book_info_by_hash(kobo_hash, user_id=None):
        """(book_id, lib_id, abs_id) for a device hash within one account."""
        try:
            with get_db() as conn:
                sql = """SELECT b.book_id, b.lib_id, b.abs_id
                         FROM book_hashes h JOIN books b ON b.book_id = h.book_id
                         WHERE h.kobo_hash = ?"""
                args = [kobo_hash]
                if user_id is not None:
                    sql += " AND b.user_id = ?"
                    args.append(user_id)
                row = conn.execute(sql + " ORDER BY b.deleted_at IS NOT NULL LIMIT 1", args).fetchone()
                if row:
                    return row['book_id'], row['lib_id'], row['abs_id']
        except Exception as e:
            print(f"   ❌ DB Error: {e}")
        return None, None, None

    @staticmethod
    def link_hash_to_book(lib_id, kobo_hash, user_id=None):
        """Pair a device hash with a book. Append-only: one book answers to
        several hashes (devices, re-downloads), and nothing is ever taken from
        another book."""
        try:
            with get_db() as conn:
                sql, args = "SELECT book_id FROM books WHERE lib_id = ?", [lib_id]
                if user_id is not None:
                    sql += " AND user_id = ?"
                    args.append(user_id)
                row = conn.execute(sql, args).fetchone()
                if row:
                    conn.execute("""INSERT OR IGNORE INTO book_hashes (kobo_hash, book_id, first_seen)
                                    VALUES (?, ?, datetime('now'))""", (kobo_hash, row['book_id']))
                    conn.commit()
                    return True
        except Exception as e:
            print(f"   ❌ DB Linking Error: {e}")
        return False

    @staticmethod
    def update_abs_id(book_id, abs_id):
        try:
            with get_db() as conn:
                conn.execute("UPDATE books SET abs_id = ? WHERE book_id = ?", (abs_id, book_id))
                conn.commit()
        except Exception as e:
            print(f"   ❌ DB Update Error: {e}")


def resolve_abs_id(lib_id, user_id):
    """Find the ABS item carrying our watermark (the ASIN tag on every MP3)."""
    base, token = abs_for(user_id)
    if not base:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    try:
        libs = requests.get(f"{base}/api/libraries", headers=headers, timeout=ABS_TIMEOUT, allow_redirects=False)
        libs.raise_for_status()
        for lib in libs.json().get("libraries", []):
            items = requests.get(f"{base}/api/libraries/{lib['id']}/items",  # nosec B113: timeout=ABS_TIMEOUT is passed
                                 headers=headers, timeout=ABS_TIMEOUT * 3, allow_redirects=False)
            items.raise_for_status()
            for item in items.json().get("results", []):
                if item.get("media", {}).get("metadata", {}).get("asin", "") == lib_id:
                    return item.get("id")
    except Exception as e:
        print(f"   ❌ ABS Search Error: {e}")
    return None


def book_for_device(kobo_hash, user_id):
    """(book_id, lib_id, abs_id, audio_state) or Nones; resolves abs_id lazily."""
    book_id, lib_id, abs_id = LibrarianDB.get_book_info_by_hash(kobo_hash, user_id)
    if not book_id:
        return None, None, None, None
    with get_db() as conn:
        state = conn.execute("SELECT audio_state FROM books WHERE book_id = ?",
                             (book_id,)).fetchone()["audio_state"]
    # Only a book with audio is worth an ABS search. An ebook-only book used to
    # scan the whole ABS library on every page-turn push.
    if not abs_id and state == "ready":
        abs_id = resolve_abs_id(lib_id, user_id)
        if abs_id:
            LibrarianDB.update_abs_id(book_id, abs_id)
    elif abs_id and abs_item_gone(abs_id, user_id):
        # The item was removed from ABS and made again (a library re-added, a
        # damaged item rebuilt: Twisted, 2026-10-11): same book, new id. The
        # cached id answered 404 to every push and nothing looked it up again.
        fresh = resolve_abs_id(lib_id, user_id)
        print(f"   🔁 ABS item {abs_id} is gone; the book is now {fresh or 'not in ABS'}.")
        LibrarianDB.update_abs_id(book_id, fresh)
        abs_id = fresh
    return book_id, lib_id, abs_id, state


def abs_item_gone(abs_id, user_id):
    """True only when this account's ABS says the item does not exist (404).

    An ABS that is down or slow is not a gone item: the id is kept, and the
    caller's own ABS call fails the way it always has.
    """
    base, token = abs_for(user_id)
    if not base:
        return False
    try:
        r = requests.get(f"{base}/api/items/{abs_id}", headers={"Authorization": f"Bearer {token}"},
                         timeout=ABS_TIMEOUT, allow_redirects=False)
    except requests.RequestException:
        return False
    return getattr(r, "status_code", None) == 404


# --- Progress ---------------------------------------------------------------

def record_progress(book_id, percentage, kobo_location, source="kobo", abs_written=None, read_at=None):
    """Store the reader's position locally. This is the record; ABS is a copy.

    `read_at` (epoch seconds) is when the page was read, when the device says
    so; otherwise now.
    """
    try:
        with get_db() as conn:
            conn.execute("""INSERT INTO progress (book_id, percentage, kobo_location, updated_at, source, abs_written)
                            VALUES (?, ?, ?, datetime(COALESCE(?, strftime('%s', 'now')), 'unixepoch'), ?, ?)
                            ON CONFLICT(book_id) DO UPDATE SET
                                percentage = excluded.percentage,
                                kobo_location = excluded.kobo_location,
                                updated_at = excluded.updated_at,
                                source = excluded.source,
                                abs_written = excluded.abs_written""",
                         (book_id, percentage, kobo_location, read_at, source, abs_written))
            conn.commit()
    except Exception as e:
        print(f"   ❌ Could not store progress locally: {e}")


def stored_progress(book_id):
    try:
        with get_db() as conn:
            return conn.execute(
                """SELECT percentage, kobo_location, updated_at, abs_written,
                          CAST(strftime('%s', updated_at) AS INTEGER) AS updated_epoch
                   FROM progress WHERE book_id = ?""", (book_id,)).fetchone()
    except Exception as e:
        print(f"   ❌ Could not read stored progress: {e}")
        return None


def book_map(book_id):
    """(nodes, chapters) of a book in core/landing.py's shape. Positions only."""
    with get_db() as conn:
        node_rows = conn.execute("""SELECT n.xpath_string, n.start_time, n.end_time
                                    FROM sync_nodes n JOIN chapters c ON n.chapter_id = c.chapter_id
                                    WHERE c.book_id = ? ORDER BY n.start_time""", (book_id,)).fetchall()
        chapter_rows = conn.execute("""SELECT audio_filename, duration FROM chapters
                                       WHERE book_id = ? ORDER BY chapter_index""", (book_id,)).fetchall()
    nodes = [(landing.kobo_path(x), s or 0.0, e) for x, s, e in node_rows]
    return nodes, landing.audio_chapters([tuple(r) for r in chapter_rows])


def page_start(book_id, nodes, kobo_location, node_span):
    """Where the page starts in the audio: inside its paragraph when the KEPUB
    and the map's word timings say so (core/page_audio.py), else the
    paragraph's start. The page's text is read from the KEPUB and never kept."""
    try:
        path = landing.node_path_for_position(nodes, kobo_location)
        with get_db() as conn:
            row = conn.execute("SELECT epub_path FROM books WHERE book_id = ?", (book_id,)).fetchone()
            node_ids = [r[0] for r in conn.execute(
                """SELECT n.node_id FROM sync_nodes n JOIN chapters c ON n.chapter_id = c.chapter_id
                   WHERE c.book_id = ? AND n.xpath_string = ? ORDER BY n.start_time""",
                (book_id, f"point(/1{path})"))] if path else []
            words = [conn.execute("""SELECT char_start, char_end, start_time FROM word_timings
                                     WHERE node_id = ? ORDER BY word_index""", (n,)).fetchall()
                     for n in node_ids]
        if not path or not row or not row["epub_path"] or not os.path.exists(row["epub_path"]):
            return node_span[0]
        found = page_audio.page_start_time(os.path.abspath(row["epub_path"]), landing.kobo_path(kobo_location),
                                           path, node_span, [[tuple(w) for w in ws] for ws in words])
        if found is not None and found > node_span[0]:
            print(f"   📍 The page starts {found - node_span[0]:.1f}s into its paragraph.")
            return found
    except Exception as e:  # a refinement: any doubt keeps the paragraph's start
        print(f"   ⚠️ Page start inside the paragraph not found ({e}); using the paragraph's start.")
    return node_span[0]


def calculate_and_push_to_abs(kobo_location, kobo_percentage, abs_id, book_id, user_id=None,
                              read_at=None):
    """Translate a Kobo position to seconds and write it to ABS.

    Returns the audio time now standing in ABS on this reader's behalf: the
    time written, or the time observed and deliberately left alone. None when
    nothing could be written, or when the listening is newer than `read_at`.
    """
    target = None
    node_span = None
    total_duration = 0.0

    try:
        nodes, chapters = book_map(book_id)
        total_duration = chapters[-1][2] if chapters else 0.0
        node_span = landing.time_for_position(nodes, kobo_location)
        if node_span:
            target = page_start(book_id, nodes, kobo_location, node_span)
        else:
            print("   ⚠️ XPath missing from the map. Falling back to percentage.")
    except Exception as e:
        print(f"   ❌ DB Query Error: {e}")
        return None

    if target is None and total_duration > 0:
        target = total_duration * kobo_percentage
    if target is None:
        return None

    base, token = abs_for(user_id)
    if not base:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    try:
        # Leave the audio alone if the listener is already inside the paragraph
        # the reader is on. KOReader pushes the paragraph it shows, and the map
        # can only name that paragraph's start -- so opening a book and closing
        # it again used to rewind the audio by up to a paragraph (115s here).
        if node_span is not None or read_at is not None:
            current = requests.get(f"{base}/api/me/progress/{abs_id}",
                                   headers=headers, timeout=ABS_TIMEOUT, allow_redirects=False)
            if current.status_code == 200:
                listened = current.json()
                now = listened.get("currentTime") or 0.0
                # A page read offline arrives when Wi-Fi returns, possibly after
                # the phone already reported listening done later than that
                # reading. The audio is the newer place: leave it, and let the
                # next pull bring the reader to it.
                if read_at is not None and (listened.get("lastUpdate") or 0) / 1000.0 > read_at:
                    print(f"   ⏸️  Listening is newer than this page (read {int(time.time() - read_at)}s ago); "
                          "not moving the audio.")
                    return None
                if node_span is not None and node_span[0] <= now <= (node_span[1] or node_span[0]):
                    print(f"   ⏸️  Audio already at {now:.1f}s inside this paragraph "
                          f"({node_span[0]:.1f}-{node_span[1]:.1f}s); not moving it.")
                    return now

        payload = {"duration": total_duration, "currentTime": target,
                   "progress": target / total_duration if total_duration > 0 else 0}
        r = requests.patch(f"{base}/api/me/progress/{abs_id}", headers=headers,
                           json=payload, timeout=ABS_TIMEOUT, allow_redirects=False)
        r.raise_for_status()
        h, rem = divmod(int(target), 3600)
        print(f"   ✅ Audiobookshelf updated to {h:02d}:{rem // 60:02d}:{rem % 60:02d}")
        return target
    except Exception as e:
        print(f"   ❌ Audiobookshelf API Error: {e}")
        return None


# --- Sync Pack ingest ---------------------------------------------------------

def safe_extract(zip_ref, dest):
    """Extract a Sync Pack into the one-folder layout, refusing any member that
    escapes `dest`/library/.

    Members are written at library.canonical_member(name): a pack built before
    the ebook moved in beside its audio still lands as one book folder, so
    Audiobookshelf never sees a second item for it.
    """
    dest_root = os.path.realpath(dest)
    allowed_root = os.path.join(dest_root, "library")
    plan = []
    for member in zip_ref.infolist():
        # Symlinks are stored as regular entries with a link bit in the mode;
        # extracting one would let a later member write through it.
        if (member.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError(f"Sync Pack contains a symlink: {member.filename}")
        target = os.path.realpath(os.path.join(dest_root, library.canonical_member(member.filename)))
        if os.path.commonpath([target, allowed_root]) != allowed_root:
            raise ValueError(f"Sync Pack escapes library/: {member.filename}")
        plan.append((member, target))
    # Checked in full before anything is written: a bad member late in the
    # pack must not leave the early ones half-applied.
    for member, target in plan:
        if member.is_dir():
            os.makedirs(target, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with zip_ref.open(member) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)


def _book_folders(names):
    """The canonical book folders (library/audiobooks/<title>) a pack writes to."""
    prefix = library.BOOKS.replace(os.sep, "/") + "/"
    folders = set()
    for name in names:
        norm = library.canonical_member(name)
        if norm.startswith(prefix) and "/" in norm[len(prefix):]:
            folders.add(os.path.join(library.BOOKS, norm[len(prefix):].split("/", 1)[0]))
    return folders


def _clear_superseded_audio(zip_ref):
    """Remove the audio of the books this pack brings new audio for.

    Folders are keyed by title but chapter filenames come from headings, so a
    regenerated book whose headings changed used to leave the old MP3s beside
    the new ones, and ABS played both (measured: a 2497s book over a 1147s map).
    Only audio is removed: the kepub lives in the same folder, and the folder
    itself stays so ABS keeps the same item, with its listening progress.
    """
    carrying = {library.canonical_member(n) for n in zip_ref.namelist() if library.is_audio(n)}
    for folder in sorted(_book_folders(carrying)):
        if library.clear_audio(folder):
            print(f"   🧹 Cleared superseded audio in {folder}")


def _opf_title_author(epub_bytes):
    try:
        with zipfile.ZipFile(io.BytesIO(epub_bytes)) as z:
            container = z.read("META-INF/container.xml").decode("utf-8", "replace")
            opf_path = re.search(r'full-path="([^"]+)"', container).group(1)
            opf = z.read(opf_path).decode("utf-8", "replace")
        title = re.search(r"<dc:title[^>]*>(.*?)</dc:title>", opf, re.S)
        author = re.search(r"<dc:creator[^>]*>(.*?)</dc:creator>", opf, re.S)
        clean = lambda m: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip() if m else None
        return clean(title), clean(author)
    except Exception:
        return None, None


def _ingest_pack(zip_path, label, user_id):
    """Fold a Sync Pack into the library for one account. Returns (body, status).

    Only the account that owns this server's files (the self-hoster) has the
    book and audio extracted. For any other account the pack is read for its
    map and fingerprint and then discarded: a hosted sync service keeps maps
    and positions, never books.
    """
    try:
        with get_db() as conn:
            owner = accounts.owner_id(conn)
        store_files = SERVE_BOOKS and user_id in (owner, 0)

        with zipfile.ZipFile(zip_path, 'r') as z:
            names = z.namelist()
            # One per pack, in either layout: library/audiobooks/<book>/ now,
            # library/ebooks/<book>/ in packs built before.
            manifest_name = next((n for n in names if n.endswith("/manifest.json")
                                  and _book_folders([n])), None)
            if manifest_name is None:
                raise ValueError("pack carries no library/audiobooks/<book>/manifest.json")
            manifest = json.loads(z.read(manifest_name))
            b = manifest['book']
            epub_bytes = z.read(b['epub_path']) if b.get('epub_path') in names else None
            # Where the kepub is served from once extracted.
            epub_path = library.canonical_member(b['epub_path']) if b.get('epub_path') else None
            carries_audio = any(library.is_audio(n) for n in names)
            if store_files:
                if carries_audio:
                    _clear_superseded_audio(z)
                safe_extract(z, ".")
                # The manifest is working data, not a library file.
                extracted_manifest = library.canonical_member(manifest_name)
                if os.path.exists(extracted_manifest):
                    os.remove(extracted_manifest)
        os.remove(zip_path)

        hashes = set()
        if b.get("koreader_md5"):
            hashes.add(b["koreader_md5"])
        if epub_bytes:
            hashes.add(koreader_partial_md5(epub_bytes))
        title, author = _opf_title_author(epub_bytes) if epub_bytes else (None, None)
        title = b.get("title") or title
        author = b.get("author") or author

        with get_db() as conn:
            c = conn.cursor()
            lib_id = b.get('lib_id')
            existing = c.execute("SELECT book_id, audio_state FROM books WHERE lib_id = ? AND user_id = ?",
                                 (lib_id, user_id)).fetchone()
            if existing is None and epub_path:
                # Books registered before identity came from content had random
                # ids; the title-keyed path is the only way to find them again.
                existing = c.execute("SELECT book_id, audio_state FROM books WHERE epub_path IN (?, ?) AND user_id = ?",
                                     (epub_path, b['epub_path'], user_id)).fetchone()

            if existing is not None:
                book_id = existing['book_id']
                c.execute("""UPDATE books SET lib_id = ?, epub_path = COALESCE(?, epub_path),
                                 title = COALESCE(?, title), author = COALESCE(?, author)
                             WHERE book_id = ?""", (lib_id, epub_path if store_files else None,
                                                    title, author, book_id))
                if b.get('abs_id'):
                    c.execute("UPDATE books SET abs_id = ? WHERE book_id = ?", (b['abs_id'], book_id))
            else:
                c.execute("""INSERT INTO books (user_id, lib_id, abs_id, epub_path, title, author)
                             VALUES (?, ?, ?, ?, ?, ?)""",
                          (user_id, lib_id, b.get('abs_id'), epub_path if store_files else None, title, author))
                book_id = c.lastrowid

            has_audio = bool(manifest.get('chapters'))
            if has_audio:
                # Only the derived rows are rebuilt, and only when the pack
                # brings new ones. An ebook-only pack -- every regeneration's
                # early publish, every --no-audio re-drop -- used to delete the
                # map of a book whose audio was still in ABS.
                c.execute("""DELETE FROM word_timings WHERE node_id IN (
                               SELECT node_id FROM sync_nodes WHERE chapter_id IN (
                                 SELECT chapter_id FROM chapters WHERE book_id = ?))""", (book_id,))
                c.execute("DELETE FROM sync_nodes WHERE chapter_id IN (SELECT chapter_id FROM chapters WHERE book_id = ?)", (book_id,))
                c.execute("DELETE FROM chapters WHERE book_id = ?", (book_id,))

                chapter_id_map = {}
                for ch in manifest['chapters']:
                    c.execute("INSERT INTO chapters (book_id, chapter_index, audio_filename, duration) VALUES (?, ?, ?, ?)",
                              (book_id, ch.get('chapter_index'),
                               ch.get('audio_filename', f"Chapter_{ch.get('chapter_index', 0)}.mp3"),
                               ch.get('duration', 0.0)))
                    chapter_id_map[ch.get('chapter_id', ch.get('id'))] = c.lastrowid

                # Positions only: the map is stored without the book's text.
                node_id_map = {}
                for node in manifest.get('sync_nodes', []):
                    ch_id = chapter_id_map.get(node.get('chapter_id'))
                    if ch_id:
                        c.execute("INSERT INTO sync_nodes (chapter_id, xpath_string, start_time, end_time, text_content) VALUES (?, ?, ?, ?, '')",
                                  (ch_id, node.get('xpath_string'), node.get('start_time'), node.get('end_time')))
                        node_id_map[node.get('node_id')] = c.lastrowid

                word_rows = [(node_id_map[w['node_id']], w.get('word_index'), None,
                              w.get('char_start'), w.get('char_end'), w.get('start_time'), w.get('end_time'))
                             for w in manifest.get('word_timings', []) if w.get('node_id') in node_id_map]
                if word_rows:
                    c.executemany("INSERT INTO word_timings (node_id, word_index, word, char_start, char_end, start_time, end_time)"
                                  " VALUES (?, ?, ?, ?, ?, ?, ?)", word_rows)
                new_state = 'ready'
            elif existing is not None and existing['audio_state'] == 'ready':
                new_state = 'ready'   # the audio is still there; an ebook re-upload does not remove it
            elif b.get('audio_state') == 'pending':
                new_state = 'pending'
            else:
                new_state = 'none'
            c.execute("UPDATE books SET audio_state = ?, deleted_at = NULL WHERE book_id = ?", (new_state, book_id))

            for h in hashes:
                c.execute("INSERT OR IGNORE INTO book_hashes (kobo_hash, book_id, first_seen) VALUES (?, ?, datetime('now'))",
                          (h, book_id))
            conn.commit()
        print(f"✅ {label}: book {book_id} ({lib_id}) {new_state}, "
              f"{len(manifest.get('sync_nodes', []))} nodes, paired {len(hashes)} fingerprint(s)")

        if has_audio and store_files:
            threading.Thread(target=sync_abs_chapters, daemon=True,
                             args=(user_id, book_id, lib_id,
                                   sum(ch.get('duration') or 0 for ch in manifest['chapters']))).start()
        return {"message": "Sync Pack stored and injected", "book_id": book_id, "state": new_state}, 200

    except Exception as e:
        print(f"❌ Ingest failed for {label}: {e}")
        if os.path.exists(zip_path):
            os.remove(zip_path)
        # Not 200. The Mac archives a book only once the gateway says it has it.
        return {"error": str(e)}, 500


def sync_abs_chapters(user_id, book_id, lib_id, expected_total, timeout=900):
    """After a pack lands: rescan ABS, then write the item's chapters ourselves.

    ABS keeps an item's chapter markers when its files are replaced under the
    same names, so every regeneration left the previous run's chapters: in
    Screwtape 32 of 33 were up to 17.8 minutes early. Chapters are rebuilt
    from ABS's own file durations, so a marker is exactly where its file starts.
    An item whose own file list holds a file twice is rebuilt first
    (_forget_doubled_item), the uploader's place carried over to the new one.
    """
    base, token = abs_for(user_id)
    if not base:
        return
    headers = {"Authorization": f"Bearer {token}"}
    try:
        _scan_abs_libraries(base, headers)
    except Exception as e:
        print(f"   ⚠️ ABS rescan failed ({e}); chapters not rewritten.")
        return
    deadline = time.time() + timeout
    repaired = rebuilt = False
    carried = None   # the uploader's place on an item forgotten below, until the new item has it
    while time.time() < deadline:
        time.sleep(ABS_POLL_SECONDS)
        abs_id = resolve_abs_id(lib_id, user_id)
        if not abs_id or (carried and abs_id == carried["old_id"]):
            continue
        try:
            item = requests.get(f"{base}/api/items/{abs_id}", params={"expanded": 1},
                                headers=headers, timeout=ABS_TIMEOUT, allow_redirects=False).json()
            files = sorted(item["media"].get("audioFiles") or [], key=lambda f: f["index"])
            unique = _unique_tracks(files)
            total = sum(f["duration"] for f in unique)
            stale = abs((item["media"].get("duration") or 0) - total) > 1.0
            if not rebuilt and _files_listed_twice(item) and abs(total - expected_total) <= 5:
                # Only once the scan has caught up with this pack: forgetting
                # an item is not something to do to half a book.
                rebuilt = True
                carried = _forget_doubled_item(base, headers, abs_id, _files_listed_twice(item))
                if carried:
                    _scan_abs_libraries(base, headers)
                    continue
                # Not forgotten: the patch below still gives it one track per file.
            if files and not repaired and (len(unique) < len(files) or
                                           (stale and abs(total - expected_total) <= 5)):
                _repair_abs_item(base, headers, abs_id, book_id, unique if len(unique) < len(files) else None,
                                 rescan=not _files_listed_twice(item))
                repaired = True
                continue
            if not files or abs(total - expected_total) > 5:
                continue   # the scan has not caught up with the new files yet
            files = unique
            chapters, start = [], 0.0
            for i, f in enumerate(files):
                name = re.sub(r"^Chapter_\d+_", "", os.path.splitext(f["metadata"]["filename"])[0]).strip()
                chapters.append({"id": i, "start": round(start, 3), "end": round(start + f["duration"], 3),
                                 "title": name if len(files) > 1 else (item["media"]["metadata"].get("title") or name)})
                start += f["duration"]
            if carried:
                _carry_over(base, headers, carried, abs_id, total)
                carried = None
            r = requests.post(f"{base}/api/items/{abs_id}/chapters", headers=headers,
                              json={"chapters": chapters}, timeout=ABS_TIMEOUT, allow_redirects=False)
            LibrarianDB.update_abs_id(book_id, abs_id)
            print(f"   📑 ABS chapters for {lib_id}: {len(chapters)} written ({r.status_code}).")
            return
        except Exception as e:
            print(f"   ⚠️ ABS chapter sync retrying ({str(e)[:80]})")
    print(f"   ⚠️ ABS never showed {lib_id} with {expected_total:.1f}s of audio; chapters left as they are.")
    if carried:
        print(f"   ⚠️ Not carried over from the forgotten item {carried['old_id']}: {_describe_carried(carried)}. "
              "Set it again by hand in Audiobookshelf.")


ABS_POLL_SECONDS = 15


def _scan_abs_libraries(base, headers):
    for lib in requests.get(f"{base}/api/libraries", headers=headers, timeout=ABS_TIMEOUT, allow_redirects=False).json().get("libraries", []):
        requests.post(f"{base}/api/libraries/{lib['id']}/scan", headers=headers, timeout=ABS_TIMEOUT * 3, allow_redirects=False)  # nosec B113: timeout=ABS_TIMEOUT is passed


def _forget_doubled_item(base, headers, abs_id, twice):
    """Make ABS forget an item whose own file list holds a file twice, keeping
    the uploader's place on it. Returns what to carry over, or None when the
    item was not forgotten.

    No repair from outside fixes such an item: a rescan rebuilds its tracks
    from that list, duplicates included, and its duration stays wrong (#70).
    Forgotten (never ?hard=1, which deletes the files) and found again by a
    library scan, it comes back as a new item listing each file once: the cure
    done by hand on Twisted, 2026-10-11. The new item has a new id and none of
    the old one's progress, so the account's position and bookmarks are read
    first and put back on it (_carry_over). Other ABS users' places on the book
    are lost (accepted, user 2026-10-12). Nothing is forgotten unless both
    reads succeed.
    """
    try:
        r = requests.get(f"{base}/api/me/progress/{abs_id}", headers=headers,
                         timeout=ABS_TIMEOUT, allow_redirects=False)
        if r.status_code not in (200, 404):
            raise RuntimeError(f"progress answered {r.status_code}")
        progress = r.json() if r.status_code == 200 else None
        me = requests.get(f"{base}/api/me", headers=headers, timeout=ABS_TIMEOUT, allow_redirects=False)
        if me.status_code != 200:
            raise RuntimeError(f"/api/me answered {me.status_code}")
        bookmarks = [b for b in me.json().get("bookmarks") or [] if b.get("libraryItemId") == abs_id]
        # ABS drops a forgotten item's progress but keeps its bookmarks, and
        # once the item is gone they cannot be removed (404, ABS 2.36.0): taken
        # off first, they are not left behind beside the copies _carry_over makes.
        taken = []
        try:
            for b in bookmarks:
                r = requests.delete(f"{base}/api/me/item/{abs_id}/bookmark/{b.get('time') or 0}",
                                    headers=headers, timeout=ABS_TIMEOUT, allow_redirects=False)
                if not 200 <= r.status_code < 300:
                    raise RuntimeError(f"a bookmark's delete answered {r.status_code}")
                taken.append(b)
            gone = requests.delete(f"{base}/api/items/{abs_id}", headers=headers,
                                   timeout=ABS_TIMEOUT, allow_redirects=False)
            if not 200 <= gone.status_code < 300:
                raise RuntimeError(f"the delete answered {gone.status_code}")
        except Exception:
            for b in taken:   # the item stays: so do its bookmarks
                requests.post(f"{base}/api/me/item/{abs_id}/bookmark", headers=headers,
                              json={"time": b.get("time") or 0, "title": b.get("title") or ""},
                              timeout=ABS_TIMEOUT, allow_redirects=False)
            raise
    except Exception as e:
        print(f"   ⚠️ ABS item {abs_id} lists {twice} file(s) twice and was not rebuilt ({str(e)[:80]}).")
        return None
    carried = {"old_id": abs_id, "progress": progress, "bookmarks": bookmarks}
    print(f"   🔁 ABS item {abs_id} listed {twice} file(s) twice: forgotten, to come back from a library scan "
          f"with {_describe_carried(carried)}.")
    return carried


def _carry_over(base, headers, carried, abs_id, duration):
    """Put the forgotten item's position and bookmarks on the item that replaced it."""
    p, done = carried["progress"], []
    try:
        if p:
            at = p.get("currentTime") or 0.0
            body = {"currentTime": at, "duration": duration, "isFinished": bool(p.get("isFinished")),
                    "progress": 1.0 if p.get("isFinished") else (min(at / duration, 1.0) if duration else 0.0)}
            # The kepub sits in the same item: a place read in ABS's own reader too.
            body.update({k: p[k] for k in ("ebookLocation", "ebookProgress") if p.get(k) is not None})
            r = requests.patch(f"{base}/api/me/progress/{abs_id}", headers=headers, json=body,
                               timeout=ABS_TIMEOUT, allow_redirects=False)
            r.raise_for_status()
            done.append("progress")
        for b in carried["bookmarks"]:
            r = requests.post(f"{base}/api/me/item/{abs_id}/bookmark", headers=headers,
                              json={"time": b.get("time") or 0, "title": b.get("title") or ""},
                              timeout=ABS_TIMEOUT, allow_redirects=False)
            r.raise_for_status()
            done.append("bookmark")
    except Exception as e:
        print(f"   ⚠️ ABS item {abs_id} replaces {carried['old_id']}, but only {len(done)} of "
              f"{bool(p) + len(carried['bookmarks'])} carried over ({str(e)[:60]}); "
              f"it had {_describe_carried(carried)}. Set the rest again by hand.")
        return
    print(f"   🔁 ABS item {abs_id} replaces {carried['old_id']}: carried over {_describe_carried(carried)}.")


def _describe_carried(carried):
    p = carried["progress"]
    if p and p.get("isFinished"):
        place = "the listening position (finished)"
    elif p:
        h, rem = divmod(int(p.get("currentTime") or 0), 3600)
        place = f"the listening position {h:02d}:{rem // 60:02d}:{rem % 60:02d}"
    else:
        place = "no listening position"
    return f"{place} and {len(carried['bookmarks'])} bookmark(s)"


def _files_listed_twice(item):
    """How many audio files ABS's own file list (libraryFiles) holds more than once.

    Twisted's item listed 34 of its 78 files twice there (2026-10-10). A rescan
    rebuilds the tracks from that list, so it brought every duplicate back:
    78 tracks became 112 and the duration stayed 73,407 s over 48,610 s of
    audio. Such an item is never rescanned: when a pack lands it is forgotten
    and found again (_forget_doubled_item); otherwise it is patched to one
    track per file.
    """
    inos = [f.get("ino") for f in item.get("libraryFiles") or [] if f.get("fileType") == "audio"]
    return len(inos) - len(set(inos))


def _unique_tracks(files):
    """One entry per file (inode), the first, which is the one ABS itself keeps."""
    seen = set()
    return [f for f in files if not (f["ino"] in seen or seen.add(f["ino"]))]


def repair_abs_library(log=print):
    """Repair every book's ABS item that has a file listed twice or a stale duration.

    sync_abs_chapters repairs a book when its pack lands; this finds the ones
    broken before that existed (Twisted) and runs when the container starts
    (start.sh, through `gateway_admin.py abs-repair`), so a deploy heals them
    with no command to type. Returns the lib_ids it repaired.
    """
    with get_db() as conn:
        rows = conn.execute("""SELECT book_id, lib_id, user_id FROM books
                               WHERE audio_state = 'ready' AND deleted_at IS NULL""").fetchall()
    repaired = []
    for row in rows:
        base, token = abs_for(row["user_id"])
        abs_id = resolve_abs_id(row["lib_id"], row["user_id"]) if base else None
        if not abs_id:
            continue
        headers = {"Authorization": f"Bearer {token}"}
        try:
            item = requests.get(f"{base}/api/items/{abs_id}", params={"expanded": 1},
                                headers=headers, timeout=ABS_TIMEOUT, allow_redirects=False).json()
        except (requests.RequestException, ValueError) as e:
            log(f"   ⚠️ ABS item {abs_id} unreadable ({str(e)[:60]}).")
            continue
        files = sorted(item.get("media", {}).get("audioFiles") or [], key=lambda f: f["index"])
        unique = _unique_tracks(files)
        stale = abs((item["media"].get("duration") or 0) - sum(f["duration"] for f in unique)) > 1.0
        if files and (len(unique) < len(files) or stale):
            twice = _files_listed_twice(item)
            if twice and len(unique) == len(files):
                # Tracks already one per file: the rescan that would fix the
                # duration is what brings the duplicates back. Say so instead.
                log(f"   ⚠️ ABS item {abs_id} lists {twice} file(s) twice in its own file list, so a "
                    "rescan cannot fix its duration; it heals when the book's audio is replaced.")
                continue
            _repair_abs_item(base, headers, abs_id, row["book_id"], unique if len(unique) < len(files) else None,
                             rescan=not twice)
            repaired.append(row["lib_id"])
    return repaired


def _repair_abs_item(base, headers, abs_id, book_id, unique=None, rescan=True):
    """Make ABS forget stale tracks and recompute the item's duration.

    Twisted (2026-10-07) kept tracks from the run it replaced, and once those
    were dropped its duration stayed 73,407 s over 48,610 s of files: ABS's
    track update (PATCH /tracks) never recomputes the duration, and a rescan
    recomputes it only when it sees an audio file change. Measured on
    ABS 2.36.0: a newer mtime on the files, then a rescan of the item, gives
    the right duration (999.9 s stored, 60.0 s after). Not when `rescan` is
    False: an item whose file list holds a file twice (_files_listed_twice)
    gets its duplicates back from a rescan.
    """
    try:
        if unique:
            requests.patch(f"{base}/api/items/{abs_id}/tracks", headers=headers,
                           json={"orderedFileData": [{"ino": f["ino"]} for f in unique]},
                           timeout=ABS_TIMEOUT, allow_redirects=False)
        if not rescan:
            print(f"   🔧 ABS item {abs_id}: {'dropped stale tracks; ' if unique else ''}not rescanned, "
                  "its own file list holds a file twice and a rescan would bring the duplicates back.")
            return
        with get_db() as conn:
            row = conn.execute("SELECT epub_path FROM books WHERE book_id = ?", (book_id,)).fetchone()
        folder = os.path.join(library.BOOKS, os.path.basename(os.path.dirname(row["epub_path"] or ""))) if row else ""
        now = time.time()
        names = library.audio_files(folder) if folder else []
        for name in names:
            os.utime(os.path.join(folder, name), (now, now))
        requests.post(f"{base}/api/items/{abs_id}/scan", headers=headers,  # nosec B113: timeout is passed
                      timeout=ABS_TIMEOUT * 3, allow_redirects=False)
        print(f"   🔧 ABS item {abs_id}: {'dropped stale tracks, ' if unique else ''}"
              f"rescanned {len(names)} file(s) for its duration.")
    except Exception as e:
        print(f"   ⚠️ ABS item repair failed ({str(e)[:80]}).")


# --- Setup ------------------------------------------------------------------

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
setup_database(DB_PATH)
with get_db() as _conn:
    # The map is kept as positions only. Books stored before this carried
    # every paragraph's and every word's text; clear it once.
    _conn.execute("UPDATE sync_nodes SET text_content = '' WHERE text_content != ''")
    _conn.execute("UPDATE word_timings SET word = NULL WHERE word IS NOT NULL")
    _conn.commit()
    _users = _conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    # Tokens stored before encryption existed, encrypted once a key is set.
    try:
        _n = abs_settings.encrypt_existing(_conn)
        if _n:
            print(f"🔐 Encrypted {_n} Audiobookshelf token(s) stored in plain text.")
    except abs_settings.SecretKeyMissing:
        if _conn.execute("SELECT 1 FROM users WHERE abs_token IS NOT NULL AND abs_token NOT LIKE 'fernet:%'").fetchone():
            print("⚠️  Audiobookshelf tokens are stored in plain text: set LIBRA_SECRET_KEY (core/abs_settings.py).")
    # Books stored before titles were kept: read them from their own EPUB.
    for _row in _conn.execute("""SELECT book_id, epub_path FROM books
                                  WHERE title IS NULL AND epub_path IS NOT NULL""").fetchall():
        if os.path.exists(_row["epub_path"]):
            with open(_row["epub_path"], "rb") as _f:
                _t, _a = _opf_title_author(_f.read())
            _conn.execute("UPDATE books SET title = ?, author = ? WHERE book_id = ?", (_t, _a, _row["book_id"]))
    _conn.commit()

print("=" * 60)
print("🌉 LIBRARIAN GATEWAY ONLINE")
if not ABS_URL:
    print("⚠️  ABS_URL is not set — no progress can be pushed to Audiobookshelf.")
if not _users:
    print("⚠️  No accounts yet: every sync call is refused. Create one with\n"
          "    python tools/gateway_admin.py add-user <name>")
print("=" * 60)


# --- Routes -----------------------------------------------------------------

@app.route('/health', methods=['GET'])
def health():
    """Liveness probe. No auth: it leaks nothing."""
    try:
        with get_db() as conn:
            conn.execute("SELECT 1 FROM books LIMIT 1")
        return jsonify({"status": "ok", "database": True}), 200
    except Exception:
        return jsonify({"status": "degraded", "database": False}), 503


@app.route('/users/create', methods=['POST'])
def create_user():
    """KOReader's Register. 201 created, 402 taken (what KOReader expects)."""
    if not ALLOW_REGISTRATION:
        return jsonify({"message": "Registration is closed on this server"}), 403
    data = request.get_json(silent=True) or {}
    with get_db() as conn:
        uid = accounts.create_user(conn, data.get("username"), data.get("password"))
        if uid is None:
            return jsonify({"message": "Username is already registered"}), 402
        if accounts.owner_id(conn) == uid:
            accounts.claim_unowned(conn, uid)
    return jsonify({"username": data.get("username")}), 201


@app.route('/users/auth', methods=['GET'])
def authorize_user():
    if kosync_user(request) is None:
        return _unauthorized()
    return jsonify({"authorized": "OK"}), 200


@app.route('/librarian/ping', methods=['GET'])
@app.route('/librarian/ping/<librarian_id>', methods=['GET'])
def sidecar_ping(librarian_id=None):
    """Kept for plugins older than v1.4. Pairing is by fingerprint now; the
    ping used to marry the next request from *any* device to the last pinged
    book, which is why it no longer does anything."""
    return jsonify({"status": "ignored"}), 200


def reading_time(value):
    """When the pushed page was read, as epoch seconds, or None for "now".

    Only the Voxie plugin sends it (KOSync's own push carries no time): with
    the page it read offline, the moment Wi-Fi returns. A time in the future
    is clamped to now, and anything older than four weeks (KOReader's own
    queue limit) or unreadable is ignored.
    """
    try:
        t = float(value)
    except (TypeError, ValueError):
        return None
    now = time.time()
    if t < now - 28 * 86400:
        return None
    return min(t, now)


@app.route('/syncs/progress', methods=['PUT'])
def push_progress():
    user_id = kosync_user(request)
    if user_id is None:
        return _unauthorized()
    data = request.get_json(silent=True) or {}
    kobo_hash = data.get("document", "")
    percentage = float(data.get("percentage") or 0)
    kobo_location = data.get("progress", "")
    print(f"\n📥 [PUSH] {kobo_hash[:12]} {percentage * 100:.2f}% {kobo_location}")

    book_id, lib_id, abs_id, state = book_for_device(kobo_hash, user_id)
    if not book_id:
        print("   ⚠️ Unknown document for this account.")
        return jsonify({"document": kobo_hash, "timestamp": int(time.time())}), 200

    read_at = reading_time(data.get("read_at"))
    abs_written = None
    if abs_id:
        abs_written = calculate_and_push_to_abs(kobo_location, percentage, abs_id, book_id, user_id,
                                                read_at=read_at)
    # Recorded after ABS, with what ABS now holds on the reader's behalf: the
    # next pull can then tell "the listener moved" from "nothing has moved".
    record_progress(book_id, percentage, kobo_location, source="kobo", abs_written=abs_written,
                    read_at=read_at)
    return jsonify({"document": kobo_hash, "timestamp": int(time.time())}), 200


# Whether to append /text().N to the position handed back to KOReader. OFF: the
# reader reads the KEPUB, whose text lives inside koboSpan children, so a
# paragraph has no direct text node and `/p[157]/text().42` addresses nothing --
# KOReader then opens the book at page one. Turning it on needs the offset
# mapped to a koboSpan and from narrated text back to the book's text.
EMIT_TEXT_OFFSETS = False


def prepare_kobo_xpath(raw_xpath, char_offset=0):
    """Convert a stored point(/1...) path into a pointer KOReader can resolve."""
    clean = raw_xpath.replace("point(/1", "").rstrip(")")
    if EMIT_TEXT_OFFSETS and char_offset > 0:
        return f"{clean}/text().{char_offset}"
    return clean


def _stored_answer(document_hash, row, note):
    print(f"   📖 {note}")
    return jsonify({"document": document_hash, "progress": row["kobo_location"],
                    "percentage": row["percentage"] or 0,
                    "device": "Voxie", "device_id": "librarian_gateway"}), 200


@app.route('/syncs/progress/<document_hash>', methods=['GET'])
def pull_progress(document_hash):
    user_id = kosync_user(request)
    if user_id is None:
        return _unauthorized()
    print(f"\n📤 [PULL] {document_hash[:12]}")

    book_id, lib_id, abs_id, state = book_for_device(document_hash, user_id)
    if not book_id:
        return jsonify({"message": "Document not found"}), 404

    stored = stored_progress(book_id)
    if not abs_id:
        if not stored or not stored["kobo_location"]:
            return jsonify({"message": "No progress"}), 404
        return _stored_answer(document_hash, stored, "No audiobook: returning the stored position.")

    base, token = abs_for(user_id)
    try:
        r = requests.get(f"{base}/api/me/progress/{abs_id}",
                         headers={"Authorization": f"Bearer {token}"}, timeout=ABS_TIMEOUT, allow_redirects=False)
        if r.status_code == 404:
            abs_progress = None
        else:
            r.raise_for_status()
            abs_progress = r.json()
    except Exception as e:
        # An ABS outage costs the translation, never the reader's place.
        print(f"   ❌ ABS unreachable ({e}).")
        if stored and stored["kobo_location"]:
            return _stored_answer(document_hash, stored, "ABS unreachable: returning the stored position.")
        return jsonify({"message": "No progress"}), 404

    if abs_progress is None:
        if stored and stored["kobo_location"]:
            return _stored_answer(document_hash, stored, "No listening yet: returning the stored position.")
        return jsonify({"message": "No progress"}), 404

    current_abs_time = abs_progress.get("currentTime") or 0.0
    abs_updated = (abs_progress.get("lastUpdate") or 0) / 1000.0

    if stored and stored["kobo_location"]:
        # The listener has not moved since the reader last pushed: the audio
        # still says what this gateway wrote (or saw and left alone). The
        # reader's own exact position is the answer. Translating it would
        # rewind to the paragraph's start, and KOReader would then offer
        # "Sync to previous location?" on every open.
        if stored["abs_written"] is not None and abs(current_abs_time - stored["abs_written"]) < 1.0:
            return _stored_answer(document_hash, stored, "Audio unchanged since the last push.")
        # The reader moved after the listener: newest activity wins.
        if stored["updated_epoch"] and abs_updated and stored["updated_epoch"] >= abs_updated:
            return _stored_answer(document_hash, stored, "Reading is newer than listening.")

    kobo_xpath = translate_audio_position(book_id, current_abs_time)
    if kobo_xpath is None:
        if stored and stored["kobo_location"]:
            return _stored_answer(document_hash, stored, "No map node for this time: returning the stored position.")
        return jsonify({"message": "Document not found"}), 404
    return jsonify({"document": document_hash, "progress": kobo_xpath,
                    "percentage": abs_progress.get("progress", 0),
                    "device": "Audiobookshelf", "device_id": "librarian_gateway"}), 200


def translate_audio_position(book_id, current_abs_time):
    """The KOReader xpointer for an audio time, or None if the map has none.

    3 s back for context, never across a chapter start (core/landing.py, the
    same rules the plugin applies offline). A paragraph, with no offset: see
    EMIT_TEXT_OFFSETS.
    """
    try:
        nodes, chapters = book_map(book_id)
        position = landing.landing_position(nodes, chapters, current_abs_time)
    except Exception as e:
        print(f"   ❌ Reverse Map Error: {e}")
        return None
    if position is None:
        return None
    kobo_xpath = prepare_kobo_xpath(position)
    print(f"   🎯 Snapping to {kobo_xpath}")
    return kobo_xpath


@app.route('/librarian/abs', methods=['GET', 'PUT', 'DELETE'])
def account_abs():
    """This account's own Audiobookshelf. Same login as the device (kosync headers).

    PUT {"url": ..., "token": ...} checks the token against that ABS before
    storing it, encrypted. No answer ever contains the token.
    """
    user_id = kosync_user(request)
    if user_id is None:
        return _unauthorized()
    with get_db() as conn:
        is_owner = user_id == accounts.owner_id(conn)
        if request.method == "DELETE":
            abs_settings.clear_abs(conn, user_id)
            return jsonify({"configured": False}), 200
        if request.method == "GET":
            row = conn.execute("SELECT abs_url FROM users WHERE user_id = ?", (user_id,)).fetchone()
            base, _ = abs_for(user_id)
            return jsonify({"url": row["abs_url"] if row else None, "configured": bool(base),
                            "server_default": bool(is_owner and base and not (row and row["abs_url"]))}), 200
    data = request.get_json(silent=True) or {}
    try:
        url = abs_settings.check_url(data.get("url"), allow_private=is_owner)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    ok, who = abs_settings.verify(url, (data.get("token") or "").strip())
    if not ok:
        return jsonify({"error": who}), 400
    try:
        with get_db() as conn:
            abs_settings.set_abs(conn, user_id, url, data.get("token"), allow_private=is_owner)
    except abs_settings.SecretKeyMissing:
        print("⚠️  An account tried to set its Audiobookshelf, but LIBRA_SECRET_KEY is not set.")
        return jsonify({"error": "This server cannot store Audiobookshelf tokens yet (no encryption key)."}), 503
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"url": url, "configured": True, "abs_user": who}), 200


@app.route('/librarian/audio/<document_hash>', methods=['GET'])
def audio_position(document_hash):
    """Where the audiobook is, always -- for the plugin's menu.

    /syncs/progress answers KOSync, and there the reader's own place wins when
    it is the newer one, so it cannot also say where the audio is.
    """
    user_id = kosync_user(request)
    if user_id is None:
        return _unauthorized()
    book_id, _, abs_id, _ = book_for_device(document_hash, user_id)
    if not book_id or not abs_id:
        return jsonify({"message": "No audiobook"}), 404
    base, token = abs_for(user_id)
    try:
        r = requests.get(f"{base}/api/me/progress/{abs_id}",
                         headers={"Authorization": f"Bearer {token}"}, timeout=ABS_TIMEOUT, allow_redirects=False)
        if r.status_code == 404:
            return jsonify({"message": "No listening yet"}), 404
        r.raise_for_status()
        abs_progress = r.json()
    except Exception:
        return jsonify({"message": "Audiobookshelf unreachable"}), 502
    xpath = translate_audio_position(book_id, abs_progress.get("currentTime") or 0.0)
    if xpath is None:
        return jsonify({"message": "No map node for this time"}), 404
    return jsonify({"document": document_hash, "progress": xpath,
                    "percentage": abs_progress.get("progress", 0),
                    "device": "Audiobookshelf", "device_id": "librarian_gateway"}), 200


@app.route('/librarian/map/<document_hash>', methods=['GET'])
def device_map(document_hash):
    """This book's map for the device to keep: the landing rules then work offline.

    Positions only (paragraph paths and times, the audiobook's chapters), as
    core/landing.build_map makes it, zlib-compressed. The plugin checks the
    sha256 on every load and re-downloads a map that changed or is damaged;
    `?have=<sha256>` answers 304 when its copy is still this one. Same login
    as the sync, and only the account that owns the book gets its map.
    """
    user_id = kosync_user(request)
    if user_id is None:
        return _unauthorized()
    book_id, _, _, _ = book_for_device(document_hash, user_id)
    if not book_id:
        return jsonify({"message": "Document not found"}), 404
    with get_db() as conn:
        node_rows = conn.execute("""SELECT n.xpath_string, n.start_time, n.end_time
                                    FROM sync_nodes n JOIN chapters c ON n.chapter_id = c.chapter_id
                                    WHERE c.book_id = ?""", (book_id,)).fetchall()
        chapter_rows = conn.execute("""SELECT audio_filename, duration FROM chapters
                                       WHERE book_id = ? ORDER BY chapter_index""", (book_id,)).fetchall()
    if not node_rows:
        return jsonify({"message": "No audiobook map"}), 404
    packed, digest, length = landing.pack_map(
        landing.build_map([tuple(r) for r in node_rows], [tuple(r) for r in chapter_rows]))
    headers = {"X-Voxie-Map-Sha256": digest, "X-Voxie-Map-Length": str(length),
               "Cache-Control": "no-store"}
    if request.args.get("have") == digest:
        return Response(status=304, headers=headers)
    return Response(packed, 200, headers=headers, mimetype="application/octet-stream")


# --- Uploads ----------------------------------------------------------------

@app.route('/upload', methods=['POST'])
def upload_sync_pack():
    user_id = upload_user(request)
    if user_id is None:
        return {"error": "Unauthorized"}, 401
    if 'file' not in request.files:
        return {"error": "No file part"}, 400
    file = request.files['file']
    if file.filename == '' or not file.filename.endswith('.zip'):
        return {"error": "Invalid file. Must be a .zip Sync Pack"}, 400
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    # basename(): the client controls this string.
    zip_path = os.path.join(UPLOAD_FOLDER, os.path.basename(file.filename))
    file.save(zip_path)
    return _ingest_pack(zip_path, file.filename, user_id)


# Resumable upload. A pack is hundreds of MB across a home uplink, so a dropped
# connection costs only the parts that were lost.
PART_DIR = os.path.join(UPLOAD_FOLDER, "parts")


def _pack_dir(pack_id, user_id):
    """Directory for one account's pack, or None unless the id is a plain hash.

    Per account: the pack id is chosen by the client, so with one shared
    folder an account could add parts to, or commit, another's upload.
    """
    if not re.fullmatch(r"[0-9a-f]{16,64}", pack_id or "") or user_id is None:
        return None
    return os.path.join(PART_DIR, str(int(user_id)), pack_id)


@app.route('/upload/status/<pack_id>', methods=['GET'])
def upload_status(pack_id):
    user_id = upload_user(request)
    if user_id is None:
        return {"error": "Unauthorized"}, 401
    d = _pack_dir(pack_id, user_id)
    if d is None:
        return {"error": "Invalid pack id"}, 400
    have = sorted(int(n.split(".")[0]) for n in os.listdir(d)
                  if n.endswith(".part") and n.split(".")[0].isdigit()) if os.path.isdir(d) else []
    return {"pack_id": pack_id, "parts": have}, 200


@app.route('/upload/part', methods=['POST'])
def upload_part():
    user_id = upload_user(request)
    if user_id is None:
        return {"error": "Unauthorized"}, 401
    d = _pack_dir(request.headers.get("X-Pack-Id", ""), user_id)
    if d is None:
        return {"error": "Invalid pack id"}, 400
    try:
        index = int(request.headers.get("X-Part-Index", ""))
    except ValueError:
        return {"error": "Missing or invalid X-Part-Index"}, 400
    if index < 0:
        return {"error": "Negative part index"}, 400
    payload = request.get_data()
    if not payload:
        return {"error": "Empty part"}, 400
    expected = request.headers.get("X-Part-Sha256")
    if expected and expected != hashlib.sha256(payload).hexdigest():
        return {"error": "Part checksum mismatch"}, 400
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f"{index:06d}.partial")   # renamed, so a half write never looks complete
    with open(tmp, "wb") as f:
        f.write(payload)
    os.replace(tmp, os.path.join(d, f"{index:06d}.part"))
    return {"received": index, "bytes": len(payload)}, 200


@app.route('/upload/commit', methods=['POST'])
def upload_commit():
    user_id = upload_user(request)
    if user_id is None:
        return {"error": "Unauthorized"}, 401
    data = request.get_json(silent=True) or {}
    pack_id = data.get("pack_id", "")
    d = _pack_dir(pack_id, user_id)
    if d is None or not os.path.isdir(d):
        return {"error": "Unknown pack id"}, 404
    try:
        total = int(data.get("total_parts"))
    except (TypeError, ValueError):
        return {"error": "Missing total_parts"}, 400
    have = {int(n.split(".")[0]) for n in os.listdir(d) if n.endswith(".part")}
    missing = [i for i in range(total) if i not in have]
    if missing:
        return {"error": "Incomplete upload", "missing": missing[:50], "missing_count": len(missing)}, 409
    filename = os.path.basename(data.get("filename") or f"{pack_id}.zip")
    if not filename.endswith(".zip"):
        return {"error": "Invalid file. Must be a .zip Sync Pack"}, 400
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    zip_path = os.path.join(UPLOAD_FOLDER, filename)
    digest = hashlib.sha256()
    with open(zip_path, "wb") as out:
        for i in range(total):
            with open(os.path.join(d, f"{i:06d}.part"), "rb") as part:
                for block in iter(lambda: part.read(1024 * 1024), b""):
                    digest.update(block)
                    out.write(block)
    if data.get("sha256") and data["sha256"] != digest.hexdigest():
        os.remove(zip_path)
        shutil.rmtree(d, ignore_errors=True)
        return {"error": "Reassembled pack does not match its checksum"}, 400
    shutil.rmtree(d, ignore_errors=True)
    return _ingest_pack(zip_path, filename, user_id)


# --- Library ----------------------------------------------------------------

def _books_of(user_id):
    with get_db() as conn:
        return conn.execute("""SELECT book_id, lib_id, epub_path, title, author FROM books
                               WHERE user_id = ? AND epub_path IS NOT NULL AND deleted_at IS NULL""",
                            (user_id,)).fetchall()


@app.route('/opds', methods=['GET', 'HEAD'])
def native_opds():
    """This account's books, as an OPDS feed. Read-only, always."""
    user_id = basic_user(request)
    if user_id is None or not SERVE_BOOKS:
        return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Voxie"'})
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    feed = Element('feed', xmlns='http://www.w3.org/2005/Atom')
    feed.set('xmlns:opds', 'http://opds-spec.org/2010/catalog')
    SubElement(feed, 'id').text = 'urn:uuid:librarian-master-catalog'
    SubElement(feed, 'title').text = 'Voxie'
    SubElement(feed, 'updated').text = now
    for row in _books_of(user_id):
        entry = SubElement(feed, 'entry')
        SubElement(entry, 'title').text = row['title'] or os.path.basename(row['epub_path']).split('.kepub')[0]
        # Without an author KOReader names the download "Unknown Author - <title>.epub".
        if row['author']:
            SubElement(SubElement(entry, 'author'), 'name').text = row['author']
        SubElement(entry, 'id').text = row['lib_id']
        SubElement(entry, 'updated').text = now
        link = SubElement(entry, 'link')
        link.set('rel', 'http://opds-spec.org/acquisition')
        # Relative: KOReader resolves it against the catalogue's own address
        # (url.absolute in opdsbrowser.lua), so a gateway behind HTTPS hands
        # out HTTPS downloads. "http://<host>/..." pointed a device that came
        # in through Tailscale Funnel at a port nothing listens on.
        link.set('href', f'/download/{row["lib_id"]}')
        link.set('type', 'application/epub+zip')
    resp = Response(tostring(feed, encoding='utf-8', xml_declaration=True), 200)
    resp.headers["Content-Type"] = "application/atom+xml; charset=utf-8"
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


PLUGIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "librarian_sync.koplugin")


def plugin_files():
    """The plugin's Lua files: main.lua (the template) and the modules beside it."""
    return sorted(n for n in os.listdir(PLUGIN_DIR) if n.endswith(".lua"))


def public_base(req):
    """This gateway's address as the device sees it.

    GATEWAY_PUBLIC_URL when set; otherwise what a TLS proxy reports in front
    of us (Tailscale Funnel sets X-Forwarded-Proto and X-Forwarded-Host, as
    Caddy and nginx can), else the request itself.
    """
    configured = (os.getenv("GATEWAY_PUBLIC_URL") or "").strip().rstrip("/")
    if configured:
        return configured
    scheme = (req.headers.get("X-Forwarded-Proto") or req.scheme).split(",")[0].strip()
    host = (req.headers.get("X-Forwarded-Host") or req.host).split(",")[0].strip()
    return f"{scheme}://{host}"


@app.route('/librarian/plugin.zip', methods=['GET'])
def plugin_zip():
    """The KOReader plugin, set to this gateway, for a device paired by hand.

    The installer writes the plugin on a Mac; anyone else (another e-reader,
    a Kobo on Windows or Linux) downloads this and unzips it into KOReader's
    plugins folder. The same template, substituted the same way, so there is
    still one plugin. A login is asked like the catalogue's, so nothing here
    answers an anonymous request.
    """
    if basic_user(request) is None:
        return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Voxie"'})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in plugin_files():
            with open(os.path.join(PLUGIN_DIR, name), encoding="utf-8") as f:
                lua = f.read().replace("__GATEWAY_URL__", public_base(request))
            z.writestr(f"librarian_sync.koplugin/{name}", lua)
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name="librarian_sync.koplugin.zip")


@app.route('/download/<lib_id>', methods=['GET'])
def download_book(lib_id):
    user_id = basic_user(request)
    if user_id is None or not SERVE_BOOKS:
        return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Voxie"'})
    row = next((r for r in _books_of(user_id) if r['lib_id'] == lib_id), None)
    if row and os.path.exists(row['epub_path']):
        return send_file(os.path.abspath(row['epub_path']), as_attachment=True)
    return "Book not found", 404


@app.route('/books/<lib_id>', methods=['DELETE'])
def delete_book(lib_id):
    """Remove a book's files but keep its identity, pairings and position."""
    user_id = upload_user(request)
    if user_id is None:
        return {"error": "Unauthorized"}, 401
    with get_db() as conn:
        row = conn.execute("SELECT book_id, epub_path, abs_id, deleted_at FROM books WHERE lib_id = ? AND user_id = ?",
                           (lib_id, user_id)).fetchone()
        if row is None:
            return {"error": f"No book with lib_id {lib_id}"}, 404
        if row["deleted_at"]:
            return {"message": "Already deleted", "lib_id": lib_id, "deleted_at": row["deleted_at"]}, 200
        removed = []
        if row["epub_path"]:
            title_folder = os.path.basename(os.path.dirname(row["epub_path"]))
            for base in (library.BOOKS, library.LEGACY_EBOOKS):
                folder = os.path.join(base, title_folder)
                if os.path.isdir(folder):
                    shutil.rmtree(folder)
                    removed.append(folder)
        conn.execute("UPDATE books SET deleted_at = datetime('now'), audio_state = 'none', abs_id = NULL WHERE book_id = ?",
                     (row["book_id"],))
        conn.commit()
    abs_removed = _abs_forget_item(user_id, row["abs_id"] or resolve_abs_id(lib_id, user_id))
    return {"message": "Book deleted", "lib_id": lib_id, "removed": removed,
            "abs_item_removed": abs_removed}, 200


def _abs_forget_item(user_id, abs_id):
    """Remove a deleted book's item from ABS's database. Its files are gone already.

    ABS keeps an item whose folder disappeared, flagged missing, forever: A
    Doll's House, A Modest Proposal and Sleepy Hollow stayed listed long after
    they were deleted. This DELETE never touches files (ABS deletes them only
    with ?hard=1, which is never sent). Best-effort: the book is deleted either way.
    """
    base, token = abs_for(user_id)
    if not (base and abs_id):
        return False
    try:
        r = requests.delete(f"{base}/api/items/{abs_id}",
                            headers={"Authorization": f"Bearer {token}"}, timeout=ABS_TIMEOUT, allow_redirects=False)
        print(f"   🗑️ ABS item {abs_id} removed ({r.status_code}).")
        return r.ok
    except requests.exceptions.RequestException as e:
        print(f"   ⚠️ ABS item {abs_id} not removed ({e}); `gateway_admin.py abs-cleanup` will find it.")
        return False


@app.route('/books/<lib_id>/state', methods=['POST'])
def set_book_state(lib_id):
    """Say what happened to a book's audio. The Mac reports 'failed' when a run
    dies after the ebook was published early, which otherwise stays 'pending'."""
    user_id = upload_user(request)
    if user_id is None:
        return {"error": "Unauthorized"}, 401
    state = (request.get_json(silent=True) or {}).get("state")
    if state not in ("none", "pending", "failed"):
        return {"error": "state must be none, pending or failed"}, 400
    with get_db() as conn:
        n = conn.execute("UPDATE books SET audio_state = ? WHERE lib_id = ? AND user_id = ? AND audio_state != 'ready'",
                         (state, lib_id, user_id)).rowcount
        conn.commit()
    return {"lib_id": lib_id, "state": state, "updated": n}, 200


@app.route('/maintenance/orphans', methods=['GET'])
def list_orphans():
    """Report inconsistencies. Never repairs them."""
    user_id = upload_user(request)
    if user_id is None:
        return {"error": "Unauthorized"}, 401
    findings = []
    with get_db() as conn:
        for row in conn.execute("""SELECT lib_id, epub_path, audio_state FROM books
                                   WHERE deleted_at IS NULL AND user_id = ?""", (user_id,)):
            if not row["epub_path"]:
                continue
            title_folder = os.path.basename(os.path.dirname(row["epub_path"]))
            if row["audio_state"] == "ready" and not library.audio_files(library.book_dir(title_folder)):
                findings.append({"lib_id": row["lib_id"], "issue": "marked ready but no audio files"})
            if os.path.dirname(row["epub_path"]) != library.book_dir(title_folder):
                findings.append({"lib_id": row["lib_id"], "issue": "ebook outside its book folder (run migrate-layout)"})
            if not os.path.exists(row["epub_path"]):
                findings.append({"lib_id": row["lib_id"], "issue": "epub file missing on disk"})
    return {"orphans": findings, "count": len(findings)}, 200


if __name__ == '__main__':
    # Dev entrypoint only. The container serves through gunicorn -- see start.sh.
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 3000)))  # nosec B104: a container's own port
