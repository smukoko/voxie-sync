"""Where a book lives on disk: one folder per book, ebook and audio together.

Audiobookshelf makes one library item per folder. Books used to be split into
library/ebooks/<title>/ (the kepub) and library/audiobooks/<title>/ (the MP3s),
so ABS listed every narrated book twice. The kepub now sits beside its audio in
library/audiobooks/<title>/, which is the folder ABS already knew: its item id,
listening progress and chapters survive the move.

library/ebooks/ is still read, never written: packs built before this change
carry their ebook there, and canonical_member() moves it on extraction.
"""
import os

LIBRARY = "library"
BOOKS = os.path.join(LIBRARY, "audiobooks")
LEGACY_EBOOKS = os.path.join(LIBRARY, "ebooks")

# What counts as a book's audio when a regeneration replaces it. Everything
# else in the folder (the kepub, the manifest) belongs to the book, not the run.
AUDIO_EXTENSIONS = (".mp3", ".m4b", ".m4a", ".flac", ".ogg", ".opus", ".wav", ".aac")


def book_dir(title):
    return os.path.join(BOOKS, title)


def is_audio(name):
    return name.lower().endswith(AUDIO_EXTENSIONS)


def audio_files(folder):
    """The audio files directly in a book folder, sorted; [] if there is none."""
    if not os.path.isdir(folder):
        return []
    return sorted(f for f in os.listdir(folder)
                  if is_audio(f) and os.path.isfile(os.path.join(folder, f)))


def clear_audio(folder):
    """Remove a book's audio and nothing else. Returns the names removed."""
    removed = audio_files(folder)
    for name in removed:
        os.remove(os.path.join(folder, name))
    return removed


def canonical_member(path):
    """A pack member's path in the one-folder layout.

    library/ebooks/<title>/x -> library/audiobooks/<title>/x; anything else is
    returned unchanged. Works on '/'-separated zip names and on OS paths.
    """
    norm = path.replace(os.sep, "/")
    legacy = LEGACY_EBOOKS.replace(os.sep, "/") + "/"
    if norm.startswith(legacy):
        return BOOKS.replace(os.sep, "/") + "/" + norm[len(legacy):]
    return norm


def find_kepub(title_fragment, root="."):
    """Kepubs whose folder name contains `title_fragment`, in either layout."""
    import glob
    found = []
    for base in (BOOKS, LEGACY_EBOOKS):
        found += glob.glob(os.path.join(root, base, f"*{title_fragment}*", "*.kepub.epub"))
    return sorted(found)


def migrate_layout(conn, root=".", apply=False, log=print):
    """Move every book still in library/ebooks/<title>/ into its book folder.

    Files keep their bytes (the KOReader fingerprint is a hash of them), the
    database's epub_path follows the kepub, and nothing is overwritten: a file
    that differs from one already in the book folder is reported and left.
    Returns (moved, conflicts). With apply=False nothing changes.
    """
    import filecmp
    legacy_root = os.path.join(root, LEGACY_EBOOKS)
    moved, conflicts = [], []
    if not os.path.isdir(legacy_root):
        return moved, conflicts
    for title in sorted(os.listdir(legacy_root)):
        src_dir = os.path.join(legacy_root, title)
        if not os.path.isdir(src_dir):
            continue
        dest_dir = os.path.join(root, BOOKS, title)
        for name in sorted(os.listdir(src_dir)):
            src, dest = os.path.join(src_dir, name), os.path.join(dest_dir, name)
            if not os.path.isfile(src):
                conflicts.append((src, "not a regular file"))
                continue
            if os.path.exists(dest) and not filecmp.cmp(src, dest, shallow=False):
                conflicts.append((src, f"differs from {dest}"))
                continue
            rel_src, rel_dest = os.path.relpath(src, root), os.path.relpath(dest, root)
            log(f"   {rel_src} -> {rel_dest}")
            moved.append((rel_src, rel_dest))
            if not apply:
                continue
            os.makedirs(dest_dir, exist_ok=True)
            if os.path.exists(dest):
                os.remove(src)          # byte-identical copy already in place
            else:
                os.replace(src, dest)
            conn.execute("UPDATE books SET epub_path = ? WHERE epub_path = ?", (rel_dest, rel_src))
            conn.commit()
        if apply and not os.listdir(src_dir):
            os.rmdir(src_dir)
    return moved, conflicts
