"""Pack a chapter's text blocks into chunks for the TTS model.

The old rule was "accumulate whole paragraphs until the running total passes
350 words", and it had two faults that cost real narration.

It counted *after* adding, so a chunk could never be smaller than one
paragraph and had no ceiling at all: a single 800-word paragraph became a
single 800-word chunk. Measured chunk lengths ran to 3,376 characters on
"The Legend of Sleepy Hollow", 4,272 on "Le Fantome de l'Opera" and 5,146 on
"Gevoel en verstand".

And Voxtral stops at 163.84 seconds without saying so. At the slowest rate
observed across a real book (15.1 chars/sec) that ceiling is about 2,470
characters, so those long chunks were being silently cut off mid-text -- 2,125
characters of Sleepy Hollow were never narrated, and the only symptom was MFA
failing to align the four chunks whose audio no longer matched their
transcript. So chunk size is a correctness bound, not only the listening-quality
question SAC-43 was originally filed as.

The rules here exist to keep the performance intact while respecting that bound,
because a chunk boundary is audible: the model sees one chunk at a time, with no
knowledge of what came before, so every boundary resets its intonation.

  1. A paragraph is never split unless it alone would breach the ceiling.
     Paragraph boundaries are where an author already put a pause, so a chunk
     that ends on one costs nothing. Breaking mid-paragraph is the thing that
     flattens delivery, and it now happens only where the alternative is losing
     the words outright.
  2. When a paragraph must be split, it is split at sentence boundaries and
     never inside a sentence -- a half-sentence has no intonation contour to
     speak of, and its other half starts without one.
  3. Those splits are balanced rather than greedy. Filling to the ceiling and
     leaving the remainder gives a full piece and a short tail, and the tail
     reads as an afterthought; two halves of a long paragraph sound like two
     halves. So a paragraph is divided into the fewest pieces that fit and
     those pieces are made even.
  4. A runt chunk at the end of a chapter is folded back into its predecessor
     where that fits. An eight-second chunk is its own generation call with its
     own noise floor, for one or two sentences.

Text is never lost: pieces are sliced from the original string by character
offset and reassemble to it exactly, which `test_chunking.py` asserts over the
real books rather than trusting the segmenter.
"""
import math
import re

from core.languages import DEFAULT_LANGUAGE, normalise

# The listening-test ceiling from SAC-43. Chunks aim for this.
CHUNK_TARGET_CHARS = 1500

# The hard ceiling, and the reason this module exists. Voxtral stops at 163.84s;
# the slowest narration rate measured over a real book was 15.13 chars/sec,
# which puts the cut at ~2,479 characters. 2,300 keeps a margin for a slower
# passage or a slower voice, since overshooting costs narration while
# undershooting costs nothing but an extra request.
CHUNK_MAX_CHARS = 2300

# Below this, a trailing chunk is folded back into the one before it.
CHUNK_MIN_TAIL_CHARS = 400

# pysbd covers every language this project narrates (en, es, fr, nl). Imported
# lazily and cached per language: core/ai_brain.py's import-time model load is
# the anti-pattern this avoids, and a Segmenter is not free to build.
_SEGMENTERS = {}


def _segmenter(language):
    code = normalise(language) or DEFAULT_LANGUAGE
    if code not in _SEGMENTERS:
        import pysbd
        from pysbd.languages import LANGUAGE_CODES
        # An unsupported language still gets sentences, just English-flavoured
        # ones. That is a worse split point, never a lost one -- see the module
        # docstring on why a wrong boundary is survivable and a lost one is not.
        lang = code if code in LANGUAGE_CODES else DEFAULT_LANGUAGE
        _SEGMENTERS[code] = pysbd.Segmenter(language=lang, clean=False, char_span=True)
    return _SEGMENTERS[code]


def sentence_spans(text, language=DEFAULT_LANGUAGE):
    """(start, end) for each sentence, covering `text` with no gaps.

    char_span rather than the segmented strings: the pieces are then sliced from
    the original, so reassembly is exact by construction instead of by trusting
    the segmenter to preserve whitespace. pysbd's Spanish has known quirks (it
    splits "El Sr. Garcia"), and this is what makes them harmless.
    """
    if not text:
        return []
    try:
        spans = [(s.start, s.end) for s in _segmenter(language).segment(text)]
    except Exception:
        # A segmenter failure must not take down a run that has already paid for
        # its GPU. One sentence is a valid answer; the caller falls back to
        # splitting on words if the result is too long.
        spans = []
    if not spans:
        return [(0, len(text))]

    # Close any gap the segmenter left, so concatenation is lossless.
    covered = []
    cursor = 0
    for start, end in spans:
        if start > cursor:
            start = cursor
        if end <= start:
            continue
        covered.append((start, end))
        cursor = end
    if cursor < len(text):
        if covered:
            covered[-1] = (covered[-1][0], len(text))
        else:
            covered = [(0, len(text))]
    return covered


def _split_on_words(text, max_chars):
    """Last resort for a 'sentence' longer than the ceiling.

    A paragraph with no terminal punctuation at all is one sentence to any
    segmenter, and it still must not reach the model oversized -- that is
    precisely the silent truncation this module exists to prevent. Splitting
    mid-sentence is bad; losing the words is worse.
    """
    words = text.split(" ")
    pieces, current = [], ""
    for word in words:
        candidate = word if not current else current + " " + word
        if current and len(candidate) > max_chars:
            pieces.append(current)
            current = word
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces or [text]


def split_block(text, language=DEFAULT_LANGUAGE, max_chars=CHUNK_MAX_CHARS):
    """Split one paragraph into the fewest sentence-aligned pieces that fit.

    Returns [text] unchanged when it already fits, which is the common case and
    the one worth protecting: most paragraphs are never touched.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    spans = sentence_spans(text, language)
    sentences = [text[a:b] for a, b in spans]

    # Balanced, not greedy. Aim for the fewest pieces that can fit, then make
    # them even -- two halves of a long paragraph should sound like two halves,
    # not like a full chunk followed by a stray remark.
    pieces_needed = max(1, math.ceil(len(text) / max_chars))
    target = math.ceil(len(text) / pieces_needed)

    pieces, current = [], ""
    for sentence in sentences:
        if not current:
            current = sentence
            continue
        joined = current + sentence
        # The ceiling is absolute; the target only decides where to break when
        # there is a choice.
        if len(joined) > max_chars or len(current) >= target:
            pieces.append(current)
            current = sentence
        else:
            current = joined
    if current:
        pieces.append(current)

    # A single sentence can still exceed the ceiling.
    final = []
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        if len(piece) <= max_chars:
            final.append(piece)
        else:
            final.extend(p.strip() for p in _split_on_words(piece, max_chars) if p.strip())
    return final


def pack_blocks(blocks, language=DEFAULT_LANGUAGE,
                target_chars=CHUNK_TARGET_CHARS,
                max_chars=CHUNK_MAX_CHARS,
                min_tail_chars=CHUNK_MIN_TAIL_CHARS):
    """Group blocks into chunks.

    `blocks` is a sequence of (key, text); `key` is passed through untouched so
    the caller can carry an xpath or an element with it. Returns a list of
    chunks, each a list of (key, piece_text) in reading order -- a block that had
    to be split appears once per piece, under the same key.

    Chunks break at paragraph boundaries wherever possible: a paragraph that
    would push the running total past the target starts the next chunk instead
    of being torn in half.
    """
    chunks, current, current_len = [], [], 0

    def flush():
        nonlocal current, current_len
        if current:
            chunks.append(current)
            current, current_len = [], 0

    for key, text in blocks:
        for piece in split_block(text, language, max_chars):
            # +1 for the space the caller joins pieces with.
            projected = current_len + (1 if current else 0) + len(piece)
            if current and projected > target_chars:
                flush()
                projected = len(piece)
            current.append((key, piece))
            current_len = projected

    flush()

    # Fold a runt tail back in, when it fits. One or two sentences alone get
    # their own request and their own noise floor for no benefit.
    if len(chunks) >= 2:
        tail = chunks[-1]
        tail_len = sum(len(t) for _, t in tail) + len(tail) - 1
        prev_len = sum(len(t) for _, t in chunks[-2]) + len(chunks[-2]) - 1
        if tail_len < min_tail_chars and prev_len + 1 + tail_len <= max_chars:
            chunks[-2] = chunks[-2] + tail
            chunks.pop()

    return chunks


def chunk_text(chunk):
    """The string sent to the model for one packed chunk."""
    return " ".join(text for _, text in chunk)


def describe(chunks):
    """One line of planning telemetry, printed before a GPU is paid for."""
    if not chunks:
        return "0 chunks"
    lengths = sorted(len(chunk_text(c)) for c in chunks)
    total = sum(lengths)
    median = lengths[len(lengths) // 2]
    return (f"{len(chunks)} chunks, {total:,} characters "
            f"(median {median}, longest {lengths[-1]})")


# A chunk may be halved at most twice, giving four pieces at the smallest.
#
# The retries exist for a transient failure and the requeue for a busy server.
# Neither helps when a chunk is simply too slow to finish, which is what cost
# The Screwtape Letters 042_B_IX and 043_C_IX: twelve identical attempts at the
# same 95-second wall, then quarantine. Halving changes the question instead of
# asking it again.
#
# Bounded because the cause might not be length at all. If some token sequence
# stalls the model, splitting isolates it into one half and the other half is
# saved -- but splitting for ever would turn one bad chunk into an exponential
# retry storm. Two levels is a 4x reduction in what a quarantine costs, from a
# whole chunk to a quarter of one.
MAX_SPLIT_DEPTH = 2


def split_chunk(chapter, language=DEFAULT_LANGUAGE):
    """Halve one queued chunk. Returns [a, b], or [] if it cannot be split.

    Splits between nodes wherever there is more than one, because a node
    boundary is a paragraph end -- a place the author already put a pause, and
    the one cut that costs no prosody at all. Only a chunk that is a single
    paragraph is cut inside one, and then at a sentence boundary.

    The halves are sliced from the original strings by offset rather than
    rebuilt from the segmenter, so reassembly is exact by construction. That is
    the same invariant pack_blocks relies on, and it is what makes a bad
    sentence boundary move a cut instead of dropping a word.
    """
    nodes = chapter.get("nodes") or []
    depth = chapter.get("split_depth", 0)
    if depth >= MAX_SPLIT_DEPTH or not nodes:
        return []

    if len(nodes) > 1:
        # Cut at the node boundary closest to half the characters.
        total = sum(len(n["text"]) for n in nodes)
        running, cut, best = 0, 1, None
        for i, n in enumerate(nodes[:-1], start=1):
            running += len(n["text"])
            distance = abs(running - total / 2)
            if best is None or distance < best:
                best, cut = distance, i
        groups = [nodes[:cut], nodes[cut:]]
    else:
        text = nodes[0]["text"]
        spans = sentence_spans(text, language)
        if len(spans) < 2:
            return []  # one sentence, one paragraph: nothing safe to cut
        total = len(text)
        cut_at, best = None, None
        for _, end in spans[:-1]:
            distance = abs(end - total / 2)
            if best is None or distance < best:
                best, cut_at = distance, end
        if cut_at is None:
            return []
        base = nodes[0]
        groups = [
            [{**base, "text": text[:cut_at].strip()}],
            [{**base, "text": text[cut_at:].strip()}],
        ]
        if not groups[0][0]["text"] or not groups[1][0]["text"]:
            return []

    halves = []
    for group in groups:
        # Offsets are per chunk: each half is its own string, so a node's
        # position has to be measured inside the half it ended up in, not
        # carried over from the chunk it came from.
        rebuilt, cursor = [], 0
        for n in group:
            end = cursor + len(n["text"])
            rebuilt.append({**n, "start_idx": cursor, "end_idx": end,
                            "char_count": len(n["text"])})
            cursor = end + 1
        halves.append({
            **chapter,
            "text": " ".join(n["text"] for n in group),
            "nodes": rebuilt,
            "split_depth": depth + 1,
            # The retry budget starts fresh: these are different, shorter
            # requests, and carrying the exhausted count over would quarantine
            # them without ever trying them.
            "requeue_count": 0,
        })
    return halves
