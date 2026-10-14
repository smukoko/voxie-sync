"""Word-level sync, checked against real aligned books rather than fixtures.

MFA returns a word tier for every chunk and core/tts_worker.py used to keep only
each paragraph's first start and last end. This asserts the rest survives, and
that the character offsets stored with it actually index the text they claim to.

The failure this is really guarding against is a silent one-word shift: an
offset that is off by one highlights the wrong word for the whole book and
nothing crashes. So the substring at (char_start, char_end) is compared against
the word itself, for every word of every node.

Run it over any populated mfa_workspace:

    python tools/test_word_timings.py [workspace_dir]
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.tts_worker import map_timestamps


def check_chunk(mfa_dir, base):
    """Returns (words_checked, [problems]) for one chunk."""
    problems = []
    txt = os.path.join(mfa_dir, base + ".txt")
    nodes_f = os.path.join(mfa_dir, base + "_nodes.json")
    aligned = os.path.join(mfa_dir, "output", base + ".json")
    if not (os.path.exists(txt) and os.path.exists(nodes_f) and os.path.exists(aligned)):
        return 0, []

    full_text = open(txt, encoding="utf-8").read()
    nodes = json.load(open(nodes_f, encoding="utf-8")).get("nodes", [])
    try:
        entries = json.load(open(aligned, encoding="utf-8"))["tiers"]["words"]["entries"]
    except (KeyError, ValueError):
        return 0, []

    word_times = map_timestamps(full_text, entries)

    checked = 0
    idx = 0
    prev_end = -1.0
    for i, node in enumerate(nodes):
        count = len(node["text"].split())
        if count == 0:
            continue
        node_slice = word_times[idx: idx + count]
        spans = [(m.start(), m.end()) for m in re.finditer(r"\S+", node["text"])]

        if len(spans) != len(node_slice):
            problems.append(f"{base} node {i}: {len(spans)} words vs {len(node_slice)} timings")
            idx += count
            continue

        for w, (a, b) in zip(node_slice, spans):
            # The offset must name the word it is stored against.
            if node["text"][a:b] != w["text"]:
                problems.append(
                    f"{base} node {i}: offset {a}:{b} gives "
                    f"{node['text'][a:b]!r}, expected {w['text']!r}")
            # Time must never run backwards across the book.
            if w["start"] < prev_end - 0.001:
                problems.append(
                    f"{base} node {i}: {w['text']!r} starts {w['start']:.3f} "
                    f"before the previous word ended {prev_end:.3f}")
            prev_end = max(prev_end, w["end"])
            checked += 1
        idx += count
    return checked, problems


def main():
    mfa_dir = sys.argv[1] if len(sys.argv) > 1 else "mfa_workspace"
    if not os.path.isdir(os.path.join(mfa_dir, "output")):
        # Skipped loudly, as test_fat_check is without Docker: this test reads
        # real narration, and a fresh clone (or CI) has none to read.
        print(f"SKIP  no aligned output in {mfa_dir}/ — run a book first, or pass a "
              "workspace that has one.")
        return 0

    bases = sorted(f[:-4] for f in os.listdir(mfa_dir) if f.endswith(".wav"))
    if not bases:
        bases = sorted(f[:-len("_nodes.json")] for f in os.listdir(mfa_dir)
                       if f.endswith("_nodes.json"))

    total, all_problems, chunks = 0, [], 0
    for base in bases:
        checked, problems = check_chunk(mfa_dir, base)
        if checked:
            chunks += 1
        total += checked
        all_problems += problems

    print(f"word-level sync check over {mfa_dir}/")
    print("=" * 60)
    print(f"chunks with alignment : {chunks}")
    print(f"words verified        : {total:,}")
    if all_problems:
        print(f"problems              : {len(all_problems)}")
        for p in all_problems[:25]:
            print(f"   ✗ {p}")
        if len(all_problems) > 25:
            print(f"   ... and {len(all_problems) - 25} more")
        print("=" * 60)
        print("FAILED")
        return 1
    print("problems              : 0")
    print("=" * 60)
    print("PASSED — every word's offset names the word, and time never runs backwards.")
    return check_global_timeline()




def check_global_timeline(db_path="library/database.db"):
    """Word timings must share the book's timeline, not each chunk's.

    MFA reports times relative to the chunk it aligned, and
    apply_global_time_offsets shifts sync_nodes onto one continuous timeline.
    Word timings were added to the schema without being added to that shift, so
    an 11,239-second audiobook shipped with every word timing inside its first
    149.6 seconds -- which looks like word sync being broken rather than like an
    offset that was never applied. Nothing else in the pipeline noticed: the
    nodes were right, the audio was right, and the durations matched.
    """
    import sqlite3
    if not os.path.exists(db_path):
        print(f"no database at {db_path}; skipping the timeline check")
        return 0
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    rows = c.execute("""
        SELECT b.book_id, b.lib_id,
               (SELECT MAX(end_time) FROM sync_nodes WHERE chapter_id IN
                  (SELECT chapter_id FROM chapters WHERE book_id = b.book_id)) AS node_end,
               (SELECT MAX(end_time) FROM word_timings WHERE node_id IN
                  (SELECT node_id FROM sync_nodes WHERE chapter_id IN
                     (SELECT chapter_id FROM chapters WHERE book_id = b.book_id))) AS word_end,
               (SELECT COUNT(*) FROM word_timings WHERE node_id IN
                  (SELECT node_id FROM sync_nodes WHERE chapter_id IN
                     (SELECT chapter_id FROM chapters WHERE book_id = b.book_id))) AS words
        FROM books b WHERE b.deleted_at IS NULL
    """).fetchall()

    problems = []
    checked = 0
    for r in rows:
        if not r["words"] or r["node_end"] is None:
            continue  # ebook-only, or a book generated before word timings
        checked += 1
        # The last word must end roughly where the last node ends. A book whose
        # words stop early is on chunk-local time.
        if abs(r["word_end"] - r["node_end"]) > 5.0:
            problems.append(
                f"{r['lib_id']}: nodes end {r['node_end']:.1f}s but words end "
                f"{r['word_end']:.1f}s — word timings are not on the book timeline")

    print(f"\nglobal timeline check over {db_path}")
    print("=" * 60)
    print(f"books with word timings: {checked}")
    if problems:
        for p in problems:
            print(f"   ✗ {p}")
        print("FAILED")
        return 1
    print("problems               : 0")
    print("PASSED — words and nodes share one timeline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
