import sqlite3

def get_connection(db_path="library/database.db"):
    """Returns a connection to the SQLite database.

    WAL and a real busy timeout, because the engine runs up to thirty TTS
    workers against this file at once. In the default rollback-journal mode
    every writer takes an exclusive lock on the whole database, and the
    default busy timeout is five seconds -- so "database is locked" under a
    full swarm was an ordinary event rather than an exceptional one. WAL lets
    readers run alongside the writer and keeps the contention to writers only.
    """
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        pass  # an older file or a read-only mount still works, just slower
    return conn

def setup_database(db_path):
    """Initialize the V3 Relational Database Schema."""
    conn = get_connection(db_path)
    cursor = conn.cursor()

    # Accounts own books: two readers may upload the same EPUB, so a book is
    # unique per (user_id, lib_id), not per lib_id. user_id 0 is "this
    # machine" -- the engine's single-user database, and on the gateway the
    # rows made before accounts existed, claimed by the first account.
    cursor.execute('''CREATE TABLE IF NOT EXISTS books (
                        book_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL DEFAULT 0,
                        lib_id TEXT NOT NULL,         -- LBR- + sha256 of the dropped EPUB
                        abs_id TEXT,                  -- The Audiobookshelf item id (NULL until found)
                        kobo_hash TEXT,               -- legacy single pairing; see book_hashes
                        epub_path TEXT,
                        audio_state TEXT NOT NULL DEFAULT 'none',
                        deleted_at TEXT,
                        title TEXT,
                        author TEXT,
                        UNIQUE(user_id, lib_id)
                    );''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS chapters (
                        chapter_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        book_id INTEGER,
                        chapter_index INTEGER,
                        audio_filename TEXT,
                        duration REAL,
                        FOREIGN KEY(book_id) REFERENCES books(book_id))''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS sync_nodes (
                        node_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chapter_id INTEGER,
                        xpath_string TEXT,
                        start_time REAL,
                        end_time REAL,
                        text_content TEXT,
                        FOREIGN KEY(chapter_id) REFERENCES chapters(chapter_id))''')

    # Word-level timings, one row per narrated word.
    #
    # MFA has always returned these -- its output carries a "words" tier and a
    # "phones" tier -- and core/tts_worker.py built the full per-word list for
    # every chunk and then kept only the first word's start and the last word's
    # end, discarding everything between. That is why sync resolution is a
    # paragraph: a 115-second paragraph is a 115-second window to land in when
    # switching between reading and listening.
    #
    # Keyed to the node rather than carrying its own xpath, because the xpath is
    # the same for every word in a paragraph and storing it per word would
    # repeat a long string tens of thousands of times per book. char_start and
    # char_end are offsets into that node's text_content, which is what
    # librerian_gateway_v3.prepare_kobo_xpath already accepts and emits as
    # /text().N -- a capability built and unused since it was written, because
    # its only caller passes 0.
    #
    # Volume is not a concern: The Screwtape Letters is ~31,000 words and a
    # sixteen-hour novel ~165,000, which is a few MB of SQLite.
    cursor.execute("""CREATE TABLE IF NOT EXISTS word_timings (
                        word_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        node_id INTEGER NOT NULL,
                        word_index INTEGER NOT NULL,
                        word TEXT,
                        char_start INTEGER,
                        char_end INTEGER,
                        start_time REAL,
                        end_time REAL,
                        FOREIGN KEY(node_id) REFERENCES sync_nodes(node_id))""")
    cursor.execute("""CREATE INDEX IF NOT EXISTS idx_word_node
                      ON word_timings(node_id, word_index)""")
    cursor.execute("""CREATE INDEX IF NOT EXISTS idx_word_time
                      ON word_timings(node_id, start_time)""")

    # A book legitimately has many device hashes over its life -- several
    # devices, a re-download, a different KOReader matching method. When this
    # was a single books.kobo_hash column it forced a "divorce protocol" that
    # stripped the hash off whichever row held it before re-attaching it, so
    # one device coming back silently unpaired another. Append-only, many-to-one.
    cursor.execute('''CREATE TABLE IF NOT EXISTS book_hashes (
                        kobo_hash TEXT NOT NULL,
                        book_id INTEGER NOT NULL,
                        first_seen TEXT,
                        PRIMARY KEY (kobo_hash, book_id))''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_book_hashes_book ON book_hashes(book_id)')

    # Progress is kept here, not only pushed to Audiobookshelf. ABS can only
    # hold a position for a book that has audio, so an ebook-only book had
    # nowhere to store one -- and an ABS rescan that changed abs_id orphaned it.
    # ABS is now a projection of this table rather than the record itself.
    cursor.execute('''CREATE TABLE IF NOT EXISTS progress (
                        book_id INTEGER PRIMARY KEY,
                        percentage REAL,
                        kobo_location TEXT,
                        updated_at TEXT,
                        source TEXT,
                        FOREIGN KEY(book_id) REFERENCES books(book_id))''')

    # Lifecycle is stated, never inferred. The OPDS route used to read "no
    # audiobook folder" as "this book was deleted" and delete the ebook and its
    # sync map to match -- which also meant an ebook-only book could not exist,
    # and a batch upload could be destroyed while it was still arriving.
    existing_cols = {r[1] for r in cursor.execute("PRAGMA table_info(books)")}
    if "audio_state" not in existing_cols:
        # none = ebook only (legitimate) | pending = generation queued
        # ready = audio + sync map present | failed = generation failed, ebook still good
        cursor.execute("ALTER TABLE books ADD COLUMN audio_state TEXT NOT NULL DEFAULT 'none'")
        cursor.execute('''UPDATE books SET audio_state = 'ready'
                          WHERE book_id IN (SELECT DISTINCT book_id FROM chapters)''')
        print("   🏷️  Added audio_state; existing books with chapters marked ready.")
    if "deleted_at" not in existing_cols:
        # A tombstone rather than a row deletion: a device that still holds the
        # book keeps pushing progress, and re-uploading the same content must
        # find its old position again.
        cursor.execute("ALTER TABLE books ADD COLUMN deleted_at TEXT")
        print("   🏷️  Added deleted_at tombstone column.")

    _migrate_accounts(cursor)

    cursor.execute('CREATE INDEX IF NOT EXISTS idx_time ON sync_nodes(chapter_id, start_time, end_time)')

    # Carry any hash already paired on the old column across, so a device that
    # is working today keeps working after the upgrade. Idempotent: re-running
    # this is a no-op once the rows exist.
    cursor.execute('''INSERT OR IGNORE INTO book_hashes (kobo_hash, book_id, first_seen)
                      SELECT kobo_hash, book_id, datetime('now') FROM books
                      WHERE kobo_hash IS NOT NULL''')
    migrated = cursor.rowcount
    if migrated > 0:
        print(f"   🔗 Migrated {migrated} device hash(es) into book_hashes.")

    conn.commit()
    conn.close()
    print("📦 Database initialized successfully.")

def _columns(cursor, table):
    return {r[1] for r in cursor.execute(f"PRAGMA table_info({table})")}


def _migrate_accounts(cursor):
    """Accounts, and the two tables whose uniqueness they change. Idempotent.

    `books` had lib_id UNIQUE, which lets one reader's upload of an EPUB adopt
    another reader's row of the same EPUB. `book_hashes` had kobo_hash as its
    primary key, so two readers holding the same file could not both pair it.
    SQLite cannot alter a constraint, so both are rebuilt, keeping every id.
    """
    cursor.execute("""CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT UNIQUE NOT NULL,
                        key_hash TEXT NOT NULL,
                        upload_token_hash TEXT UNIQUE,
                        abs_url TEXT,
                        abs_token TEXT,
                        created_at TEXT)""")

    if "user_id" not in _columns(cursor, "books"):
        cols = _columns(cursor, "books")
        cursor.execute("""CREATE TABLE books_v2 (
                            book_id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id INTEGER NOT NULL DEFAULT 0,
                            lib_id TEXT NOT NULL,
                            abs_id TEXT,
                            kobo_hash TEXT,
                            epub_path TEXT,
                            audio_state TEXT NOT NULL DEFAULT 'none',
                            deleted_at TEXT,
                            title TEXT,
                            author TEXT,
                            UNIQUE(user_id, lib_id))""")
        keep = [c for c in ("book_id", "lib_id", "abs_id", "kobo_hash", "epub_path",
                            "audio_state", "deleted_at") if c in cols]
        # nosec B608: `keep` is a fixed list of column names, never input.
        cursor.execute(f"INSERT INTO books_v2 ({', '.join(keep)}) SELECT {', '.join(keep)} FROM books")  # nosec B608
        cursor.execute("DROP TABLE books")
        cursor.execute("ALTER TABLE books_v2 RENAME TO books")
        print("   🔐 books: now unique per (user_id, lib_id).")
    for col in ("title", "author"):
        if col not in _columns(cursor, "books"):
            cursor.execute(f"ALTER TABLE books ADD COLUMN {col} TEXT")

    pk = [r[1] for r in cursor.execute("PRAGMA table_info(book_hashes)") if r[5]]
    if pk == ["kobo_hash"]:
        cursor.execute("""CREATE TABLE book_hashes_v2 (
                            kobo_hash TEXT NOT NULL,
                            book_id INTEGER NOT NULL,
                            first_seen TEXT,
                            PRIMARY KEY (kobo_hash, book_id))""")
        cursor.execute("INSERT INTO book_hashes_v2 SELECT kobo_hash, book_id, first_seen FROM book_hashes")
        cursor.execute("DROP TABLE book_hashes")
        cursor.execute("ALTER TABLE book_hashes_v2 RENAME TO book_hashes")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_book_hashes_book ON book_hashes(book_id)")
        print("   🔐 book_hashes: one file may now pair with a book in each account.")

    # The audio time this gateway last wrote to ABS for the book, or observed
    # and deliberately left alone. If ABS still says exactly that, the listener
    # has not moved since, and the reader's own exact position is the answer.
    if "abs_written" not in _columns(cursor, "progress"):
        cursor.execute("ALTER TABLE progress ADD COLUMN abs_written REAL")


def register_book(db_path, librarian_id):
    """Registers the Watermark into its dedicated column and returns the integer ID."""
    conn = get_connection(db_path)
    cursor = conn.cursor()

    # We now insert into lib_id, NOT abs_id!
    cursor.execute('''
        INSERT OR IGNORE INTO books (lib_id)
        VALUES (?)
    ''', (librarian_id,))

    # Retrieve using lib_id
    cursor.execute('SELECT book_id FROM books WHERE lib_id = ?', (librarian_id,))
    book_id = cursor.fetchone()[0]

    conn.commit()
    conn.close()
    return book_id

def apply_global_time_offsets(db_path, book_id):
    """Stitches relative chapter times into one continuous global timeline for Audiobookshelf."""
    print(f"\n🧵 Stitching Global Timeline for {book_id}...")
    conn = get_connection(db_path)
    cursor = conn.cursor()

    # Get all chapters for this book in strict chronological order
    cursor.execute('''
        SELECT chapter_id, duration FROM chapters
        WHERE book_id = ?
        ORDER BY chapter_index ASC
    ''', (book_id,))
    chapters = cursor.fetchall()

    running_total_offset = 0.0

    for chapter_id, duration in chapters:
        # Ensure duration isn't None to avoid math errors
        duration = duration or 0.0

        if running_total_offset > 0:
            # Add the running total to every node in this specific chapter
            cursor.execute('''
                UPDATE sync_nodes
                SET start_time = start_time + ?,
                    end_time = end_time + ?
                WHERE chapter_id = ?
            ''', (running_total_offset, running_total_offset, chapter_id))

            # And to the words inside those nodes. MFA reports times relative to
            # the chunk it aligned, so every word starts life on a chunk-local
            # clock exactly as its node does -- shifting the nodes alone left
            # The Screwtape Letters with a 11,239s audiobook whose word timings
            # all landed inside the first 149.6s, which reads as "word sync is
            # broken" rather than as an offset that was never applied.
            cursor.execute('''
                UPDATE word_timings
                SET start_time = start_time + ?,
                    end_time = end_time + ?
                WHERE node_id IN (SELECT node_id FROM sync_nodes WHERE chapter_id = ?)
            ''', (running_total_offset, running_total_offset, chapter_id))

        # Update the offset for the NEXT chapter in the loop
        running_total_offset += duration

    conn.commit()
    conn.close()
    print(f"   ✅ Global Timeline locked. Total Book Duration: {running_total_offset:.2f}s")
    return running_total_offset
