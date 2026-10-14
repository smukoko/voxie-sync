#!/usr/bin/env python3
"""What would the block filter drop from these books? Read it before trusting it.

Walks each EPUB's spine the way the engine does -- the same leaf blocks, the
same Gutenberg trim, print-artifact rules and chapter-number rewrite -- then
asks JEV about every block and prints what core/block_filter.py would remove.
Nothing is changed anywhere. Every would-be drop is written, with the block
before and after it, to a review file, because the only honest test of a
filter that deletes text is a person reading what it deletes.

    python tools/block_filter_report.py ~/Kobo_Archive/*.epub
"""
import argparse
import asyncio
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.parser import parse_xhtml  # noqa: E402

from dotenv import load_dotenv  # noqa: E402
from lxml import etree, html  # noqa: E402

from core import block_filter  # noqa: E402
from core.junk_provider import build  # noqa: E402
from core.parser import block_text  # noqa: E402
from core.structure import (drop_print_artifacts, leaf_blocks, say_chapter_numbers,  # noqa: E402
                            structural_junk, trim_boilerplate_blocks)


def spine_documents(epub_path):
    """(href, position_in_book, [(element, text)]) per spine document, as the engine sees them."""
    with zipfile.ZipFile(epub_path) as z:
        container = etree.fromstring(z.read("META-INF/container.xml"))
        opf_path = container.xpath("//n:rootfile/@full-path",
                                   namespaces={"n": "urn:oasis:names:tc:opendocument:xmlns:container"})[0]
        opf_tree = etree.fromstring(z.read(opf_path))
        ns = {"opf": "http://www.idpf.org/2007/opf"}
        manifest = {i.get("id"): i.get("href") for i in opf_tree.xpath("//opf:item", namespaces=ns)}
        spine = [manifest[r.get("idref")] for r in opf_tree.xpath("//opf:itemref", namespaces=ns)
                 if r.get("idref") in manifest]
        declared = structural_junk(z, opf_tree, opf_path)
        opf_dir = os.path.dirname(opf_path)
        for index, href in enumerate(spine):
            full = os.path.join(opf_dir, href).replace("\\", "/") if opf_dir else href
            if declared.get(full):
                continue
            try:
                tree = parse_xhtml(z.read(full))
            except Exception:
                continue
            pairs = [(b, block_text(b)) for b in leaf_blocks(tree)]
            pairs = [(b, t) for b, t in pairs if t]
            pairs, _, _ = trim_boilerplate_blocks(pairs)
            pairs, _ = drop_print_artifacts(pairs)
            rewritten = say_chapter_numbers([t for _, t in pairs])
            pairs = [(b, t) for (b, _), t in zip(pairs, rewritten)]
            if sum(len(t.split()) for _, t in pairs) < 20:
                continue
            yield href, index / max(1, len(spine) - 1), pairs


async def report(paths, review_path):
    jev = build("jev")
    out = open(review_path, "w", encoding="utf-8")
    grand = {"blocks": 0, "dropped": 0, "isolated": 0, "words": 0}
    print(f"{'book':40} {'blocks':>7} {'drop':>5} {'words':>6} {'isolated':>8}")
    for path in paths:
        name = os.path.basename(path)[:38]
        b = d = iso = w = 0
        out.write(f"\n{'=' * 78}\n{os.path.basename(path)}\n{'=' * 78}\n")
        for href, rho, pairs in spine_documents(path):
            _, rep = await block_filter.filter_blocks(jev, pairs, rho, href, apply=False)
            b += rep.blocks
            d += len(rep.dropped)
            iso += len(rep.isolated)
            w += rep.dropped_words
            texts = [t for _, t in pairs]
            for label, rows in (("WOULD DROP", rep.dropped), ("KEPT (isolated)", rep.isolated)):
                for i, p, kind, text in rows:
                    out.write(f"\n[{label}] {href} #{i}  p={p:.2f}  {kind}\n")
                    if i > 0:
                        out.write(f"    before: {' '.join(texts[i - 1].split())[:160]}\n")
                    out.write(f"    >>>>>>  {' '.join(text.split())[:400]}\n")
                    if i + 1 < len(texts):
                        out.write(f"    after:  {' '.join(texts[i + 1].split())[:160]}\n")
            if rep.failed:
                out.write(f"\n[FAILED] {href}: {rep.failed}\n")
        print(f"{name:40} {b:7} {d:5} {w:6} {iso:8}")
        for k, v in (("blocks", b), ("dropped", d), ("isolated", iso), ("words", w)):
            grand[k] += v
    out.close()
    print(f"{'TOTAL':40} {grand['blocks']:7} {grand['dropped']:5} {grand['words']:6} {grand['isolated']:8}")
    print(f"\n{jev.summary()}\nreview every line of: {review_path}")
    await jev.aclose()


def main():
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("epubs", nargs="+")
    ap.add_argument("--review", default="block_filter_review.txt")
    a = ap.parse_args()
    asyncio.run(report(a.epubs, a.review))


if __name__ == "__main__":
    main()
