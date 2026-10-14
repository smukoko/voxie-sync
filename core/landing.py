"""Where a page lands in the audio, and where an audio time lands in the book.

The one statement of the landing rules. The gateway answers with these
functions, and the KOReader plugin carries the same rules in Lua
(`librarian_sync.koplugin/voxie_landing.lua`) so they work with no network, from a
copy of the map kept on the device. `tools/landing_examples.json` holds the
examples both are tested against (`tools/test_landing.py`, and
`tools/test_landing.lua` under LuaJIT), so the two cannot drift.

A map here is positions only, never text:

    nodes     [(path, start, end), ...] sorted by start; a path is KOReader's
              xpointer without offsets, e.g. /body/DocFragment[3]/body/p[10]
    chapters  [(title, start, end), ...] the audiobook's chapters as the
              listener sees them: one per stitched file, as the gateway
              writes them into Audiobookshelf
"""
import hashlib
import json
import re
import zlib

# How far back to aim when handing a listening position to the reader:
# seconds, not words, because words vary in length and seconds do not.
ROLLBACK_SECONDS = 3.0

# A chapter's first paragraph can start a little after the chapter itself
# (silence at the head of the file); it still belongs to that chapter.
CHAPTER_SLACK = 0.5

MAP_VERSION = 1


def kobo_path(stored):
    """`point(/1/body/...)`, as the map is stored, to KOReader's `/body/...`."""
    return stored.replace("point(/1", "", 1).rstrip(")")


def paragraph_path(raw):
    """KOReader's position with its offsets cut: the paragraph it starts in.

    `/body/DocFragment[3]/body/p[10]/span[2]/text().9` becomes `.../p[10]`. A
    KEPUB wraps text in koboSpan elements, and the map knows paragraphs only.
    """
    path = kobo_path(raw or "")
    path = re.sub(r"/span\[\d+\].*", "", path)
    path = re.sub(r"/text\(\).*", "", path)
    return path


def audio_chapters(rows):
    """[(title, start, end)] from the gateway's chunk rows, in order.

    `rows` are (audio_filename, duration) by chapter_index. A chapter is a run
    of consecutive chunks sharing a title (`NNN_xxx_Title.mp3`), grouped the
    way the stitcher groups them into one file.
    """
    chapters, start = [], 0.0
    for filename, duration in rows:
        # Any number of digits: chunk 1,000 is "1000_A_..." (see
        # core/tts_worker.CHUNK_NAME).
        m = re.match(r"^\d+_[^_]+_(.+)\.(?:mp3|wav)$", filename or "")
        title = m.group(1) if m else (filename or "")
        end = start + (duration or 0.0)
        if chapters and chapters[-1][0] == title:
            chapters[-1] = (title, chapters[-1][1], end)
        else:
            chapters.append((title, start, end))
        start = end
    return chapters


def chapter_at(chapters, seconds):
    """(1-based chapter number, seconds into it) for a time in the audiobook."""
    if not chapters:
        return 1, seconds
    for i, (_, start, end) in enumerate(chapters):
        if seconds < end:
            return i + 1, max(0.0, seconds - start)
    return len(chapters), max(0.0, seconds - chapters[-1][1])


def time_for_position(nodes, position):
    """(start, end) of the paragraph a KOReader position is in, or None.

    Earliest wins where one paragraph has several nodes: a paragraph too long
    for one request is split across chunks, and a position without offsets can
    only mean its start. A page that begins inside inline markup arrives as
    `.../p[10]/em/span[1]/text().3`, which cuts to `.../p[10]/em`, a path no
    node has: walk up to the nearest ancestor that is one. 7.4% of Redeeming
    Love's text spans sit in inline markup, and the percentage guess they used
    to fall to was 5-14 minutes off.
    """
    first = {}
    for path, start, end in nodes:
        if path not in first:
            first[path] = (start, end if end is not None else start)
    path = paragraph_path(position)
    found = first.get(path)
    while found is None and path.count("/") > 3:
        path = path.rsplit("/", 1)[0]
        found = first.get(path)
    return found


def audio_time(nodes, chapters, position, percentage):
    """Seconds into the audiobook for a page: its paragraph's start, else the
    same fraction of the whole (a page the map does not know)."""
    found = time_for_position(nodes, position)
    if found is not None:
        return found[0]
    total = chapters[-1][2] if chapters else 0.0
    return total * (percentage or 0.0)


def landing_position(nodes, chapters, seconds, rollback=ROLLBACK_SECONDS):
    """The paragraph to open for an audio time, or None for an empty map.

    Aim `rollback` seconds back, for context, but never across the start of a
    chapter: three seconds before a chapter begins is the end of the previous
    one, which opened the Kobo on Chapter Five's last page for audio in
    Chapter Six.
    """
    if not nodes:
        return None
    floor = chapters[chapter_at(chapters, seconds)[0] - 1][1] if chapters else 0.0
    target = max(seconds - rollback, floor)
    # The last paragraph started by the target time.
    best = None
    for node in nodes:
        if node[1] <= target:
            best = node
        else:
            break
    if best is None:
        return nodes[0][0]
    if best[1] < floor - CHAPTER_SLACK:
        # Still in the previous chapter: the chapter's own first paragraph,
        # if it has started by the time asked for.
        for node in nodes:
            if node[1] >= floor - CHAPTER_SLACK:
                if node[1] <= seconds:
                    best = node
                break
    return best[0]


def build_map(node_rows, chapter_rows):
    """The device's map as a dict: what the plugin decodes. Positions only."""
    chapters = audio_chapters(chapter_rows)
    nodes = sorted(((kobo_path(x), round(s or 0.0, 2), round(e if e is not None else (s or 0.0), 2))
                    for x, s, e in node_rows), key=lambda n: n[1])
    return {"v": MAP_VERSION,
            "chapters": [[t, round(s, 2), round(e, 2)] for t, s, e in chapters],
            "nodes": [list(n) for n in nodes]}


def pack_map(book_map):
    """(compressed bytes, sha256 of them, uncompressed length).

    zlib's own format, which KOReader's `ffi/zlib` reads. The device checks
    the sha256 every time it loads the map, and needs the length to inflate.
    """
    raw = json.dumps(book_map, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    packed = zlib.compress(raw, 9)
    return packed, hashlib.sha256(packed).hexdigest(), len(raw)
