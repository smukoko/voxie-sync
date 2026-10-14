"""Rebuild a book's audio and sync map from chunks already on disk.

Recovers a run whose narration is finished -- the .pristine chunks are still in
mfa_workspace/ -- but whose finished audio or sync map came out wrong. Re-runs
alignment, re-injects every chunk, re-stitches the chapters and rebuilds the
manifest, without touching RunPod, so a defective book costs no GPU time to fix.

Written for the "A Doll's House" run of 2026-08-23, which lost 18.8 minutes of
narration to chunks MFA had skipped and a further 3.5 to the chunk-label
overflow past 'Z'. Both are fixed upstream now; this puts the finished book
back together from the audio that survived.
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from librerian_engine_v3 import align_with_retry           # noqa: E402
from core.tts_worker import (                              # noqa: E402
    process_mfa_factory, stitch_audio_chapters, probe_duration,
)
from core.database import apply_global_time_offsets        # noqa: E402
from core.fingerprint import koreader_partial_md5           # noqa: E402
from core import library                                     # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lib-id", required=True, help="librarian id, e.g. LBR-c43628449de0")
    ap.add_argument("--author", required=True)
    ap.add_argument("--skip-align", action="store_true",
                    help="reuse the alignments already in mfa_workspace/output")
    args = ap.parse_args()

    db_path = os.path.join("library", "database.db")
    mfa_dir = os.path.abspath("mfa_workspace")
    mfa_output_dir = os.path.join(mfa_dir, "output")

    conn = sqlite3.connect(db_path, timeout=20.0)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT book_id, epub_path FROM books WHERE lib_id = ?",
                       (args.lib_id,)).fetchone()
    if row is None:
        sys.exit(f"No book with lib_id {args.lib_id}")
    book_id, epub_path = row["book_id"], row["epub_path"]
    book_title = os.path.basename(os.path.dirname(epub_path))
    audio_dir = library.book_dir(book_title)
    # The same folder, except for a book still in the pre-one-folder layout.
    ebook_dir = os.path.dirname(epub_path)
    print(f"🔧 Repairing book_id={book_id}  '{book_title}'")

    chunks = sorted(f[:-4] for f in os.listdir(mfa_dir) if f.endswith(".wav"))
    # Prefer the .pristine copy, fall back to the .wav. A provider whose output
    # already sits at a listening level writes no .pristine at all, because
    # there is no MFA boost for it to protect the listener from -- CosyVoice
    # returns -19 LUFS and skips both. Summing only .pristine therefore
    # measured 0.0s of narration for a perfectly good book and declared
    # "MISMATCH -- do not upload", which is exactly the wrong answer to give
    # about 43 minutes of audio that cost a GPU to make.
    def _source(chunk):
        pristine = os.path.join(mfa_dir, chunk + ".pristine")
        return pristine if os.path.exists(pristine) else os.path.join(mfa_dir, chunk + ".wav")

    expected = sum(probe_duration(_source(c)) for c in chunks
                   if os.path.exists(_source(c)))
    print(f"   {len(chunks)} chunks on disk, {expected:.1f}s of narration to account for.")

    # 1. Alignment (with the wider-beam retry for anything MFA skips).
    if not args.skip_align:
        print("\n🧠 Re-running alignment...")
        align_with_retry(mfa_dir, mfa_output_dir)
    aligned = {f[:-5] for f in os.listdir(mfa_output_dir) if f.endswith(".json")}
    print(f"   {len(aligned)}/{len(chunks)} chunks aligned.")

    # 2. Reset the derived rows. The chunk rows themselves stay: the per-chunk
    #    _nodes.json files reference their chapter_id, so re-registering the
    #    book would orphan every node.
    print("\n🧹 Clearing the old sync map...")
    cur = conn.cursor()
    # Words first: they reference node ids, and deleting the nodes alone left
    # their word rows behind, pointing at nothing.
    cur.execute("""DELETE FROM word_timings WHERE node_id IN (SELECT node_id FROM sync_nodes
                   WHERE chapter_id IN (SELECT chapter_id FROM chapters WHERE book_id = ?))""", (book_id,))
    cur.execute("""DELETE FROM sync_nodes WHERE chapter_id IN
                   (SELECT chapter_id FROM chapters WHERE book_id = ?)""", (book_id,))
    print(f"   removed {cur.rowcount} stale sync nodes")
    cur.execute("UPDATE chapters SET duration = NULL WHERE book_id = ?", (book_id,))
    conn.commit()
    conn.close()

    # 3. Rebuild the audio folder from scratch so a stale chapter MP3 or an
    #    unstitched leftover cannot survive into the new Sync Pack.
    print("\n🧹 Clearing the old audio...")
    for name in os.listdir(audio_dir):
        if name.endswith((".mp3", ".wav")):
            os.remove(os.path.join(audio_dir, name))
            print(f"   removed {name}")

    # 4/5/6. Inject, stitch, lay the chunks onto one timeline.
    process_mfa_factory(mfa_dir, audio_dir, db_path, args.lib_id, book_title, args.author)
    stitch_audio_chapters(audio_dir, book_title, args.author, args.lib_id)
    apply_global_time_offsets(db_path, book_id)

    # 7. Manifest, in the shape the gateway's /upload expects.
    conn = sqlite3.connect(db_path, timeout=20.0)
    conn.row_factory = sqlite3.Row

    # A repaired book has audio, so say so. The engine states this before it
    # exports its manifest and this path did not, which left a rebuilt book
    # reading "ebook only" on the Mac while a finished MP3 sat beside it. The
    # gateway derives its own state from the manifest's chapters and so was
    # always right -- which is exactly why the wrong local row stayed invisible.
    conn.execute("UPDATE books SET audio_state = 'ready' WHERE book_id = ?", (book_id,))
    conn.commit()

    book_dict = dict(conn.execute("SELECT * FROM books WHERE book_id = ?", (book_id,)).fetchone())
    chapters = [dict(r) for r in conn.execute(
        "SELECT * FROM chapters WHERE book_id = ?", (book_id,))]
    sync_nodes = [dict(r) for r in conn.execute(
        """SELECT * FROM sync_nodes WHERE chapter_id IN
           (SELECT chapter_id FROM chapters WHERE book_id = ?)""", (book_id,))]
    # Word timings travel with the nodes, exactly as the engine exports them.
    # This manifest used to leave them out, so a repaired book shipped with
    # paragraph-level sync only: Redeeming Love reached the gateway with none
    # of its 152,962.
    word_timings = [dict(r) for r in conn.execute(
        """SELECT * FROM word_timings WHERE node_id IN (SELECT node_id FROM sync_nodes
           WHERE chapter_id IN (SELECT chapter_id FROM chapters WHERE book_id = ?))""", (book_id,))]
    conn.close()
    book_dict["koreader_md5"] = koreader_partial_md5(epub_path)

    manifest_path = os.path.join(ebook_dir, "manifest.json")
    with open(manifest_path, "w") as mf:
        json.dump({"book": book_dict, "chapters": chapters, "sync_nodes": sync_nodes,
                   "word_timings": word_timings}, mf)
    print(f"\n📦 Manifest rebuilt: {manifest_path}")

    # 8. The check the original run never made: the map has to be as long as
    #    the audio, and the audio as long as what was narrated.
    audio_total = sum(probe_duration(os.path.join(audio_dir, f))
                      for f in os.listdir(audio_dir) if f.endswith(".mp3"))
    map_total = sum(c["duration"] or 0.0 for c in chapters)
    # A chapter with no duration is a phantom: a row from an abandoned attempt
    # with no audio and no sidecar. Summing with `or 0.0` let 34 of them ride
    # into a manifest that this very check then called correct -- the totals
    # agreed precisely because the junk rows contributed nothing to them.
    phantoms = [c for c in chapters if c["duration"] is None]
    print("\n📐 Verification")
    print(f"   narrated on disk : {expected:9.1f}s")
    print(f"   in chapter MP3s  : {audio_total:9.1f}s")
    print(f"   sync map spans   : {map_total:9.1f}s")
    print(f"   sync nodes       : {len(sync_nodes)}")
    print(f"   word timings     : {len(word_timings)}")
    print(f"   chapter rows     : {len(chapters)} ({len(chunks)} chunks narrated)")
    if phantoms:
        print(f"   ‼️  {len(phantoms)} chapter row(s) carry no duration — phantoms from an earlier run.")
    ok = (abs(audio_total - expected) < 2.0 and abs(map_total - expected) < 2.0
          and not phantoms)
    print("   ✅ audio and map agree with the narration." if ok
          else "   ‼️  MISMATCH — do not upload this book.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
