#!/usr/bin/env python3
"""Where to listen: the exact moments in a book that the text fixes touched.

Verifying narration by ear should not mean scrubbing through sixteen hours.
This reads the book's sync map and prints the book-clock timestamp -- the same
clock Audiobookshelf's player shows -- of every block worth hearing:

  heading   a chapter number rewritten to words ("Chapter Nine.")
  capitals  a fully capitalised block that normalize_caps lowered
  number    a digit, time or range the model has to read on its own
  abbrev    Mr. / Dr. / St. and friends

    python tools/listen_checklist.py --title "Screwtape"
    python tools/listen_checklist.py --lib-id LBR-dd157de7f7a1 --kind number,abbrev
"""
import argparse
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "library", "database.db")

KINDS = {
    "heading": re.compile(r"^Chapter [A-Z][a-z]+(?:-[A-Z][a-z]+)*(?: Hundred(?: [A-Z][a-z-]+)?)?\.$"),
    # A block that was entirely capitals now reads in title case; find short,
    # mostly-capitalised-initial lines, which is what those become.
    "capitals": re.compile(r"^(?:[A-Z][a-z']+[,.]?\s+){1,7}(?:[A-Z][a-z']+|\d[\d:]*)[,.:]?(?:\s+\d[\d:.,]*)?\.{0,3}$"),
    "number": re.compile(r"\b\d{1,4}(?::\d{2})?(?:\s?[-–]\s?\d+)?\b"),
    "abbrev": re.compile(r"\b(?:Mr|Mrs|Dr|St|Ave|Rev|Prof|Sr|Jr)\."),
}


def clock(seconds):
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--lib-id")
    g.add_argument("--title", help="substring of the book's folder name")
    ap.add_argument("--kind", default=",".join(KINDS), help=f"any of {','.join(KINDS)}")
    ap.add_argument("--limit", type=int, default=40, help="per kind")
    a = ap.parse_args()

    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    book = (c.execute("SELECT book_id, lib_id, epub_path FROM books WHERE lib_id = ?", (a.lib_id,)).fetchone()
            if a.lib_id else
            c.execute("SELECT book_id, lib_id, epub_path FROM books WHERE epub_path LIKE ? AND deleted_at IS NULL",
                      (f"%{a.title}%",)).fetchone())
    if not book:
        sys.exit("no such book in library/database.db")

    rows = c.execute("""SELECT n.start_time, n.text_content, ch.audio_filename AS title
                        FROM sync_nodes n JOIN chapters ch ON ch.chapter_id = n.chapter_id
                        WHERE ch.book_id = ? ORDER BY n.start_time""", (book["book_id"],)).fetchall()
    print(f"{os.path.basename(os.path.dirname(book['epub_path']))}  ({book['lib_id']}, {len(rows)} nodes)")
    for kind in [k.strip() for k in a.kind.split(",") if k.strip() in KINDS]:
        pat = KINDS[kind]
        hits = []
        for r in rows:
            text = " ".join((r["text_content"] or "").split())
            if kind in ("heading", "capitals"):
                if pat.match(text) and (kind == "heading" or len(text) < 70):
                    hits.append((r["start_time"], r["title"], text))
            else:
                m = pat.search(text)
                if m:
                    lo = max(0, m.start() - 40)
                    hits.append((r["start_time"], r["title"], "…" + text[lo:m.end() + 40] + "…"))
        print(f"\n== {kind}: {len(hits)} ==")
        for t, chap, text in hits[:a.limit]:
            print(f"  {clock(t):>9}  {(chap or '')[:22]:22}  {text[:90]}")
        if len(hits) > a.limit:
            print(f"  ... {len(hits) - a.limit} more (--limit)")


if __name__ == "__main__":
    main()
