#!/usr/bin/env python3
"""Everything the narrator will be asked to say, for a whole book, to be READ.

EPUBs are messy enough that no set of rules is right until someone has read
every line it produces. This writes the whole book's narration -- every block,
in reading order, exactly as core.narration hands it to the model -- plus every
block a stage removed and why, to reports/<book>_narration.txt, then lists the
patterns most likely to be wrong so the reading starts somewhere. The lint is a
pointer, not a verdict: the review is the reading.

No GPU, no network (unless --jev), seconds per book, so the loop is: run, read,
fix, run again. When the book has been through the engine, every narrated line
is also checked against the sync map it stored.

    python tools/narration_review.py --title "Screwtape"
    python tools/narration_review.py --epub ~/Kobo_Dropzone/new.epub     # before any GPU
"""
import argparse
import collections
import copy
import os
import re
import sqlite3
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lxml import etree  # noqa: E402

from core import library  # noqa: E402
from core.narration import apply_errata, load_errata, narrate_text  # noqa: E402
from core.parser import get_koreader_xpath_lxml, parse_xhtml  # noqa: E402
from core.languages import normalise as normalise_language  # noqa: E402
from core.structure import (_ROMAN_RE, drop_gutenberg_front_matter, drop_gutenberg_title_page, drop_picture_text, drop_printed_contents,  # noqa: E402
                            drop_print_artifacts, drop_repeated_title, read_toc, text_outside_blocks,
                            is_dedication, narratable_blocks, opening_credit, say_chapter_numbers, structural_junk,
                            trim_boilerplate_blocks, trim_credits)

# Each pattern is a question for the reader, not an accusation.
LINT = {
    "roman numeral": lambda t: [w for w in re.findall(r"\b[IVXLCDM]{2,}\b", t) if _ROMAN_RE.match(w)],
    "all-caps word": lambda t: re.findall(r"\b[A-Z]{2,}\b", t),
    "digits": lambda t: re.findall(r"\b\d[\d,.:/-]*\b", t),
    "symbol": lambda t: re.findall(r"[&%$#@*§¶†‡~^_=+<>|\\/]", t),
    "bracket": lambda t: re.findall(r"[\[\]{}]", t),
    "spaced dots": lambda t: re.findall(r"\.\s\.(?:\s\.)*", t),
    "space before punctuation": lambda t: re.findall(r"\w\s+[,.;:!?](?:\s|$)", t),
    "broken hyphenation": lambda t: re.findall(r"\b\w+- \w+", t),
    "ligature": lambda t: re.findall(r"[ﬀ-ﬆ]", t),
    "non-Latin": lambda t: re.findall(r"[^\x00-ɏ‐-‧‰-⁞€]", t),
    "url/email": lambda t: re.findall(r"\bhttps?://\S+|\bwww\.\S+|\S+@\S+\.\w+", t),
    "abbreviation": lambda t: re.findall(r"\b(?:Mr|Mrs|Ms|Dr|St|Rev|Prof|Sr|Jr|Capt|Col|Gen|Lt|Mt|No|Vol|pp?|ch|cf|viz|etc|e\.g|i\.e)\.", t),
    "tiny block": lambda t: [t] if len(t) <= 4 else [],
}


def book_meta(epub):
    """(title, language, author) from the OPF -- the same fields the engine reads."""
    with zipfile.ZipFile(epub) as z:
        container = etree.fromstring(z.read("META-INF/container.xml"))
        opf_path = container.xpath("//n:rootfile/@full-path",
                                   namespaces={"n": "urn:oasis:names:tc:opendocument:xmlns:container"})[0]
        opf = etree.fromstring(z.read(opf_path))
    dc = {"dc": "http://purl.org/dc/elements/1.1/"}
    title = (opf.xpath("//dc:title/text()", namespaces=dc) or [""])[0]
    lang = normalise_language((opf.xpath("//dc:language/text()", namespaces=dc) or ["en"])[0]) or "en"
    author = (opf.xpath("//dc:creator/text()", namespaces=dc) or [""])[0]
    return title, lang, author


UNEXTRACTED = []   # (doc#, text) the engine never extracts, so never narrates


def spine_documents(epub, repeat_state):
    """Per spine document: (doc#, href, position, [(element, literal, removed-reason, text)]).

    Mirrors the engine's order exactly: declared junk, Gutenberg trim, print
    artifacts, chapter numbers, the title said once, trailing credits, and the
    20-word floor.
    """
    title, language, author = book_meta(epub)
    with zipfile.ZipFile(epub) as z:
        container = etree.fromstring(z.read("META-INF/container.xml"))
        opf_path = container.xpath("//n:rootfile/@full-path",
                                   namespaces={"n": "urn:oasis:names:tc:opendocument:xmlns:container"})[0]
        opf = etree.fromstring(z.read(opf_path))
        ns = {"opf": "http://www.idpf.org/2007/opf"}
        manifest = {i.get("id"): i.get("href") for i in opf.xpath("//opf:item", namespaces=ns)}
        spine = [manifest[r.get("idref")] for r in opf.xpath("//opf:itemref", namespaces=ns)
                 if r.get("idref") in manifest]
        declared = structural_junk(z, opf, opf_path)
        toc_lookup, toc_anchors = read_toc(z)
        toc_labels = set(toc_lookup.values()) | {lbl for entries in toc_anchors.values() for _, lbl, _ in entries}
        opf_dir = os.path.dirname(opf_path)
        for index, href in enumerate(spine):
            full = os.path.join(opf_dir, href).replace("\\", "/") if opf_dir else href
            try:
                tree = parse_xhtml(z.read(full))
            except Exception:
                continue
            stray = text_outside_blocks(copy.deepcopy(tree))
            # The engine's own list, loose text included (core.structure.StrayRun).
            leaves = narratable_blocks(tree)
            covered = re.sub(r"\s+", "", "".join(t for _, t in leaves))
            UNEXTRACTED.extend((index + 1, t) for t in stray
                               if len(t.split()) >= 2 and re.sub(r"\s+", "", t) not in covered)
            reasons, says_title = {}, False
            if declared.get(full):
                for b, _ in leaves:
                    reasons[id(b)] = f"document declared junk ({declared[full]})"
                pairs = []
            else:
                pairs, _, boiler_reason = trim_boilerplate_blocks(leaves)
                for b, _ in leaves:
                    if id(b) not in {id(x) for x, _ in pairs}:
                        reasons[id(b)] = f"Gutenberg trim ({boiler_reason})"
                if boiler_reason and "header" in boiler_reason:
                    pairs, front = drop_gutenberg_front_matter(tree, pairs, toc_anchors.get(full, []))
                    for b, _ in front:
                        reasons[id(b)] = "the source edition's front matter (before the first TOC entry)"
                    pairs, imprint = drop_gutenberg_title_page(tree, pairs, toc_anchors.get(full, []),
                                                               title, author)
                    for b, _ in imprint:
                        reasons[id(b)] = "the source edition's title page (its imprint, not the credit)"
                before = {id(b) for b, _ in pairs}
                pairs, _ = drop_print_artifacts(pairs)
                for b in before - {id(x) for x, _ in pairs}:
                    reasons[b] = "print artifact"
                before = {id(b) for b, _ in pairs}
                pairs, _ = drop_picture_text(pairs)
                for b in before - {id(x) for x, _ in pairs}:
                    reasons[b] = "picture caption or list of illustrations"
                pairs, contents = drop_printed_contents(pairs, toc_labels)
                for b, _ in contents:
                    reasons[id(b)] = "the book's printed table of contents"
                pairs = [(b, t) for (b, _), t in zip(pairs, say_chapter_numbers([t for _, t in pairs],
                                                                           language=language))]
                pairs, repeats, says_title = drop_repeated_title(pairs, title, repeat_state)
                for b, _ in repeats:
                    reasons[id(b)] = "the book's title again (said once already)"
                pairs, credits = trim_credits(pairs)
                for b, _ in credits:
                    reasons[id(b)] = "e-text credits"
                if sum(len(t.split()) for _, t in pairs) < 20 and not is_dedication([t for _, t in pairs]):
                    for b, _ in pairs:
                        reasons[id(b)] = "document under 20 words"
                    pairs = []
            text_of = {id(b): t for b, t in pairs}
            if not pairs:
                says_title = False
            yield (index + 1, href, index / max(1, len(spine) - 1), says_title,
                   [(b, lit, reasons.get(id(b)), text_of.get(id(b), lit)) for b, lit in leaves])


def run_plan(title):
    """The document verdicts ai_brain made on this book's last run, from the ghost log."""
    verdicts, capture = {}, False
    try:
        from core.platform_support import ghost_log_path
        for line in open(ghost_log_path(), errors="replace"):
            if "BOOK DETECTED" in line:
                capture = title.lower() in line.lower()
                if capture:
                    verdicts = {}
            m = re.search(r"(✅|🚫)\s+\d+ words\s+(\S+)", line)
            if capture and m:
                verdicts[m.group(2)] = "KEEP" if m.group(1) == "✅" else "JUNK"
    except OSError:
        pass
    return verdicts


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--title", help="a book already in the library (reviews its KEPUB)")
    g.add_argument("--epub", help="any EPUB, e.g. before it is dropped")
    ap.add_argument("--jev", action="store_true",
                    help="judge documents and blocks with JEV as .env configures the engine, "
                         "instead of replaying the last run's verdicts")
    ap.add_argument("--examples", type=int, default=6)
    a = ap.parse_args()

    if a.title:
        found = library.find_kepub(a.title, root=ROOT)
        if not found:
            sys.exit(f"no KEPUB for {a.title!r}")
        epub, name = found[0], os.path.basename(os.path.dirname(found[0]))
    else:
        epub, name = os.path.expanduser(a.epub), os.path.splitext(os.path.basename(a.epub))[0]
    book_title, language, book_author = book_meta(epub)
    errata, errata_used = load_errata(os.path.dirname(epub)), set()

    jev = block_mode = None
    if a.jev:
        import asyncio
        from dotenv import load_dotenv
        load_dotenv(os.path.join(ROOT, ".env"))
        from core import block_filter
        from core.junk_provider import build
        jev, block_mode = build("jev"), block_filter.mode()
        loop = asyncio.new_event_loop()
    verdicts = {} if a.jev else run_plan(a.title or name)

    stored = {}
    if a.title and not a.jev:
        db = sqlite3.connect(os.path.join(ROOT, "library", "database.db"))
        for x, t in db.execute("""SELECT n.xpath_string, n.text_content FROM sync_nodes n
                                  JOIN chapters c ON c.chapter_id = n.chapter_id
                                  JOIN books b ON b.book_id = c.book_id
                                  WHERE b.epub_path LIKE ? AND b.deleted_at IS NULL""", (f"%{name}%",)):
            stored.setdefault(x, []).append(" ".join((t or "").split()))

    lines, hits = [], collections.defaultdict(list)
    narrated = removed = mismatches = checked = partial = 0
    repeats = collections.Counter()
    repeat_state = {}
    for doc, href, rho, says_title, records in spine_documents(epub, repeat_state):
        live = [(el, t) for el, lit, why, t in records if why is None]
        doc_why = None
        if live and a.jev and not is_dedication([t for _, t in live]):
            text = "\n\n".join(t for _, t in live)
            v = loop.run_until_complete(jev.classify(text, len(text.split()), rho, href))
            if v.verdict == "JUNK":
                doc_why = f"document verdict JUNK (JEV p={v.confidence:.2f}, {v.detail})"
            elif block_mode in ("on", "shadow"):
                exempt = {i for i, (_, t) in enumerate(live) if opening_credit(t, book_title, book_author)}
                kept, rep = loop.run_until_complete(
                    block_filter.filter_blocks(jev, live, rho, href, apply=True, exempt=exempt))
                dropped = {id(live[i][0]): (p, kind) for i, p, kind, _ in rep.dropped}
                if dropped:
                    records = [(el, lit, why or (f"block filter{'' if block_mode == 'on' else ' (shadow: WOULD drop)'} "
                                                 f"p={dropped[id(el)][0]:.2f}" if id(el) in dropped else None), t)
                               for el, lit, why, t in records]
                    if block_mode == "shadow":
                        # Shadow removes nothing: show it, keep narrating it.
                        for el, lit, why, t in records:
                            if why and "shadow" in why:
                                lines.append(f"[{doc:>3}] <{el.tag}> ?? {why}: {lit}")
                        records = [(el, lit, None if (why and "shadow" in why) else why, t)
                                   for el, lit, why, t in records]
        elif live and verdicts.get(href) == "JUNK":
            doc_why = "document verdict JUNK (last run)"
        if doc_why:
            records = [(el, lit, why or doc_why, t) for el, lit, why, t in records]
        elif live and says_title:
            repeat_state["title_said"] = True      # committed only for a narrated document

        survivors = [(el, t) for el, lit, why, t in records if why is None]
        following = {id(el): (survivors[i + 1][1] if i + 1 < len(survivors) else None)
                     for i, (el, _) in enumerate(survivors)}
        for el, lit, why, text in records:
            tag = f"[{doc:>3}] <{el.tag}>"
            if why:
                removed += 1
                lines.append(f"{tag} ~~ REMOVED ({why}): {lit}")
                continue
            out = narrate_text(el.tag, text, following.get(id(el)), language)
            if out is None:
                continue
            if errata:
                out = apply_errata(out, errata, errata_used)
            narrated += 1
            repeats[out] += 1
            lines.append(f"{tag} {out}")
            for kind, find in LINT.items():
                for h in find(out):
                    hits[kind].append((doc, h, out))
            if stored:
                got = " ".join(stored.get(get_koreader_xpath_lxml(doc, el), []))
                if got:
                    checked += 1
                    flat = " ".join(out.split())
                    if flat in got:
                        pass
                    elif got in flat:
                        partial += 1
                    else:
                        mismatches += 1
    if jev:
        loop.run_until_complete(jev.aclose())

    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:60]
    path = os.path.join(ROOT, "reports", f"{slug}_narration.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    words = sum(len(l.split()) for l in lines if "~~ REMOVED" not in l and " ?? " not in l)
    print(f"{name} [{language}]: {narrated} narrated blocks (~{words:,} words), {removed} removed"
          + (f"  | JEV: {jev.summary()}, block filter {block_mode}" if jev else ""))
    if errata:
        stale = [w for w, _ in errata if w not in errata_used]
        print(f"errata: {len(errata) - len(stale)} of {len(errata)} applied"
              + (f"; STALE: {stale}" if stale else ""))
    if UNEXTRACTED:
        lost = sum(len(t.split()) for _, t in UNEXTRACTED)
        print(f"NOT NARRATED: {lost} words sit outside every block and are never read "
              f"(e.g. {UNEXTRACTED[0][1][:60]!r})")
        for doc, t in UNEXTRACTED:
            hits["outside every block"].append((doc, t[:30], t))
    if stored:
        print(f"engine check: {checked} lines compared with the sync map, {mismatches} differ, "
              f"{partial} mapped only in part (a chunk went unaligned)")
    for text, n in repeats.most_common():
        if n < 3:
            break
        hits["repeated line"].append((0, f"{n}x", text))
    print("\npoints to start reading from (not verdicts):")
    for kind, rows in sorted(hits.items(), key=lambda kv: -len(kv[1])):
        uniq = collections.Counter(h for _, h, _ in rows)
        print(f"  {kind:26} {len(rows):5}  e.g. {', '.join(repr(h)[:40] for h, _ in uniq.most_common(a.examples))}")
    print(f"\nthe whole narration, to read: reports/{slug}_narration.txt")


if __name__ == "__main__":
    main()
