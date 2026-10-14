#!/usr/bin/env python3
"""Rebuild a book's sync map from alignments already on disk. No GPU, no audio.

For a book whose narration and chapter MP3s are right but whose map came from
older mapping code -- the word-timing drift fixed on 2026-09-26, say. Chapter
rows and their durations are kept exactly, so the map still describes the audio
already in Audiobookshelf; only sync_nodes and word_timings are recomputed from
mfa_workspace/, through the same core.tts_worker.inject_chunk_map the engine
uses. The database is backed up first.

    python tools/rebuild_map.py --lib-id LBR-dd157de7f7a1 [--pack]

--pack writes <title>_MapPack.zip: the KEPUB and the manifest, no audio. The
gateway rebuilds the map from it and leaves the audiobook alone.
"""
import argparse, json, os, shutil, sqlite3, sys, time, zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.database import apply_global_time_offsets   # noqa: E402
from core.fingerprint import koreader_partial_md5      # noqa: E402
from core.tts_worker import inject_chunk_map           # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lib-id", required=True)
    ap.add_argument("--mfa-dir", default="mfa_workspace")
    ap.add_argument("--pack", action="store_true")
    args = ap.parse_args()

    db_path = os.path.join("library", "database.db")
    backup = f"{db_path}.before-rebuild-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(db_path, backup)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    book = conn.execute("SELECT * FROM books WHERE lib_id = ?", (args.lib_id,)).fetchone()
    if book is None:
        sys.exit(f"No book {args.lib_id}")
    book_id = book["book_id"]
    chapter_ids = {r[0] for r in conn.execute("SELECT chapter_id FROM chapters WHERE book_id = ?", (book_id,))}
    before = {r["xpath_string"] + f"#{i}": r["start_time"] for i, r in enumerate(conn.execute(
        """SELECT n.xpath_string, n.start_time FROM sync_nodes n JOIN chapters c USING(chapter_id)
           WHERE c.book_id = ? ORDER BY n.start_time""", (book_id,)))}

    chunks = []
    for f in sorted(os.listdir(args.mfa_dir)):
        if not f.endswith("_nodes.json"):
            continue
        base = f[:-len("_nodes.json")]
        with open(os.path.join(args.mfa_dir, f), encoding="utf-8") as fh:
            sidecar = json.load(fh)
        if sidecar["chapter_id"] in chapter_ids:
            chunks.append((base, sidecar))
    if len(chunks) != len(chapter_ids):
        sys.exit(f"{len(chunks)} chunk sidecars in {args.mfa_dir} for {len(chapter_ids)} chapter rows; "
                 "this is not the workspace this book was built in.")

    cur = conn.cursor()
    cur.execute("""DELETE FROM word_timings WHERE node_id IN (SELECT node_id FROM sync_nodes
                   WHERE chapter_id IN (SELECT chapter_id FROM chapters WHERE book_id = ?))""", (book_id,))
    cur.execute("DELETE FROM sync_nodes WHERE chapter_id IN (SELECT chapter_id FROM chapters WHERE book_id = ?)", (book_id,))
    unaligned = 0
    for base, sidecar in chunks:
        align = os.path.join(args.mfa_dir, "output", base + ".json")
        if not os.path.exists(align):
            unaligned += 1
            continue
        with open(os.path.join(args.mfa_dir, base + ".txt"), encoding="utf-8") as fh:
            text = fh.read().strip()
        with open(align, encoding="utf-8") as fh:
            entries = json.load(fh)["tiers"]["words"]["entries"]
        inject_chunk_map(cur, sidecar["chapter_id"], sidecar["nodes"], text, entries, base)
    conn.commit()
    conn.close()
    apply_global_time_offsets(db_path, book_id)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    book_dict = dict(conn.execute("SELECT * FROM books WHERE book_id = ?", (book_id,)).fetchone())
    book_dict["koreader_md5"] = koreader_partial_md5(book_dict["epub_path"])
    chapters = [dict(r) for r in conn.execute("SELECT * FROM chapters WHERE book_id = ?", (book_id,))]
    nodes = [dict(r) for r in conn.execute(
        "SELECT * FROM sync_nodes WHERE chapter_id IN (SELECT chapter_id FROM chapters WHERE book_id = ?)", (book_id,))]
    words = [dict(r) for r in conn.execute(
        """SELECT * FROM word_timings WHERE node_id IN (SELECT node_id FROM sync_nodes
           WHERE chapter_id IN (SELECT chapter_id FROM chapters WHERE book_id = ?))""", (book_id,))]
    conn.close()

    after = {n["xpath_string"] + f"#{i}": n["start_time"] for i, n in enumerate(sorted(nodes, key=lambda n: n["start_time"]))}
    moved = sorted(abs(after[k] - before[k]) for k in after if k in before)
    total = sum(c["duration"] or 0 for c in chapters)
    print(f"📐 {book_dict.get('title') or args.lib_id}: {len(nodes)} nodes (was {len(before)}), "
          f"{len(words):,} word timings, {unaligned} unaligned chunk(s)")
    if moved:
        print(f"   node starts moved: median {moved[len(moved) // 2]:.2f}s, "
              f"p99 {moved[int(len(moved) * .99)]:.2f}s, max {moved[-1]:.2f}s")
    print(f"   map ends at {max(n['end_time'] for n in nodes):.2f}s; audio is {total:.2f}s")
    print(f"   database backed up to {backup}")

    ebook_dir = os.path.dirname(book_dict["epub_path"])
    manifest = {"book": book_dict, "chapters": chapters, "sync_nodes": nodes, "word_timings": words}
    with open(os.path.join(ebook_dir, "manifest.json"), "w") as mf:
        json.dump(manifest, mf)
    if args.pack:
        folder = os.path.basename(ebook_dir)
        out = f"{folder}_MapPack.zip"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(book_dict["epub_path"], book_dict["epub_path"], compress_type=zipfile.ZIP_STORED)
            z.write(os.path.join(ebook_dir, "manifest.json"), os.path.join(ebook_dir, "manifest.json"))
        print(f"📦 {out} ({os.path.getsize(out) // 1024} KB): map only, no audio.")


if __name__ == "__main__":
    main()
