#!/usr/bin/env python3
"""Exercise the chunker, including against real books on disk.

The property that matters most is not the sizing -- it is that no text is lost.
A chunker that silently drops a sentence is indistinguishable, in the finished
audiobook, from the truncation bug this module was written to prevent, and it
would be found the same way: months later, by someone listening.

    python tools/test_chunking.py
"""
import glob
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.parser import parse_xhtml  # noqa: E402

from core.chunking import (  # noqa: E402
    CHUNK_MAX_CHARS, CHUNK_TARGET_CHARS, chunk_text, describe,
    pack_blocks, sentence_spans, split_block,
)

results = []


def check(name, got, want):
    ok = got == want
    results.append(ok)
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else f"   got={got!r} want={want!r}"))


def check_true(name, got):
    check(name, bool(got), True)


print("sentence spans — lossless by construction\n")

for lang, text in [
    ("en", 'Mr. Brom Bones grinned. "I won\'t," she said, "go there." He read at 3.5 miles... Then silence!'),
    ("es", "El Sr. García llegó. ¿Verdad? ¡Sí! Leyó a 3,5 km..."),
    ("fr", "M. Dupont est arrivé. « Je ne peux pas », dit-elle. Puis le silence !"),
    ("nl", "Dhr. de Vries kwam aan. Hij las bijv. om 3,5 uur... Toen stilte!"),
    ("en", "No terminal punctuation at all"),
    ("en", "   "),
]:
    spans = sentence_spans(text, lang)
    rebuilt = "".join(text[a:b] for a, b in spans)
    check(f"{lang}: spans cover the text exactly", rebuilt, text)

print("\nsplitting one paragraph\n")

short = "A short paragraph. It has two sentences."
check("a paragraph under the ceiling is untouched", split_block(short, "en"), [short])

# A long paragraph of many sentences must come back sentence-aligned, under the
# ceiling, and reassemble to the original.
long_para = " ".join(f"This is sentence number {i} and it carries a little weight." for i in range(120))
pieces = split_block(long_para, "en")
check_true("a long paragraph is split", len(pieces) > 1)
check("...every piece is under the ceiling", all(len(p) <= CHUNK_MAX_CHARS for p in pieces), True)
check("...and it reassembles exactly", " ".join(pieces), long_para)
spread = max(len(p) for p in pieces) - min(len(p) for p in pieces)
check_true(f"...pieces are balanced (spread {spread} chars)", spread < CHUNK_MAX_CHARS // 2)
check("...split at sentence ends, never mid-sentence",
      all(p.rstrip().endswith(".") for p in pieces), True)

# The pathological case: no punctuation anywhere, so every segmenter returns one
# sentence. It must still not reach the model oversized.
runon = "word " * 2000
pieces = split_block(runon.strip(), "en")
check("a run-on with no punctuation still fits", all(len(p) <= CHUNK_MAX_CHARS for p in pieces), True)
check("...and keeps every word", " ".join(pieces).split(), runon.split())

print("\npacking blocks into chunks\n")

blocks = [(f"/x/{i}", f"Paragraph {i}. " + ("Sentence here. " * 12).strip()) for i in range(40)]
chunks = pack_blocks(blocks, "en")
check("every chunk is under the hard ceiling",
      all(len(chunk_text(c)) <= CHUNK_MAX_CHARS for c in chunks), True)
check_true("chunks cluster around the target",
           sum(len(chunk_text(c)) for c in chunks) / len(chunks) <= CHUNK_TARGET_CHARS)
rebuilt = " ".join(chunk_text(c) for c in chunks)
check("packing loses no text", rebuilt, " ".join(t for _, t in blocks))

# Paragraph integrity: with paragraphs that comfortably fit, no block should
# ever appear in more than one chunk.
appearances = {}
for i, c in enumerate(chunks):
    for key, _ in c:
        appearances.setdefault(key, set()).add(i)
check("a paragraph that fits is never split across chunks",
      max(len(v) for v in appearances.values()), 1)

# An oversized paragraph is the one case where a block may span chunks.
big = [("/x/big", " ".join(f"Sentence {i} of a very long paragraph." for i in range(200)))]
big_chunks = pack_blocks(big, "en")
check_true("an oversized paragraph is split across chunks", len(big_chunks) > 1)
check("...still under the ceiling",
      all(len(chunk_text(c)) <= CHUNK_MAX_CHARS for c in big_chunks), True)
check("...and loses nothing", " ".join(chunk_text(c) for c in big_chunks), big[0][1])

# Runt tail folding.
tail_blocks = [("/x/1", "A. " * 300), ("/x/2", "Tiny tail.")]
tail_chunks = pack_blocks(tail_blocks, "en")
last = chunk_text(tail_chunks[-1])
check_true("a runt tail is folded back in, or is not a runt", len(last) >= 400 or len(tail_chunks) == 1)

print("\nagainst the real books on disk\n")


def blocks_from_epub(path):
    from lxml import etree, html
    out = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if not name.endswith((".xhtml", ".html", ".htm")):
                continue
            try:
                tree = parse_xhtml(z.read(name))
            except Exception:
                continue
            etree.strip_elements(tree, "script", "style", "sup", "title", with_tail=False)
            tags = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}
            for node in tree.xpath("//*"):
                if node.tag in tags and not any(c.tag in tags for c in node.iterdescendants()):
                    text = " ".join(node.itertext()).strip()
                    if text:
                        out.append((name, " ".join(text.split())))
    return out


candidates = sorted(glob.glob(os.path.expanduser("~/Downloads/pg*.epub")))
candidates = [c for c in candidates if "(" not in os.path.basename(c)]
if not candidates:
    print("  (no pg*.epub in ~/Downloads — skipped)")
else:
    for path in candidates[:4]:
        blocks = blocks_from_epub(path)
        if not blocks:
            continue
        name = os.path.basename(path)
        chunks = pack_blocks(blocks, "en")
        joined_in = " ".join(t for _, t in blocks)
        joined_out = " ".join(chunk_text(c) for c in chunks)
        over = [len(chunk_text(c)) for c in chunks if len(chunk_text(c)) > CHUNK_MAX_CHARS]
        longest_block = max(len(t) for _, t in blocks)
        print(f"  {name}")
        print(f"     longest paragraph {longest_block} chars -> {describe(chunks)}")
        check(f"     {name}: nothing over the ceiling", over, [])
        check(f"     {name}: no text lost", joined_out, joined_in)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
