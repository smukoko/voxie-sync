#!/usr/bin/env python3
"""Every decision the pipeline made about a book, block by block, with its text.

For fine-tuning: the literal text of each block as the book has it, what each
stage did to it -- declared junk, Gutenberg trim, print artifacts, the
chapter-number rewrite, capitals, typography -- the document verdicts, JEV's
score for the block, and the text that was finally sent to the narrator.

It walks the same file the engine walked (the KEPUB in the book's library folder) in the
same order with the same functions, and then checks its own work: every block's
computed narration is compared with what the engine actually stored in the
sync map. A report about the engine that disagreed with the engine would be
worse than none, so any mismatch is printed at the top.

The output contains the book's text, so it is written under reports/, which is
gitignored, and never leaves this Mac.

    python tools/decision_report.py --title "Screwtape" --until-chapter 1 --sentences 2
"""
import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from core.parser import parse_xhtml  # noqa: E402

from dotenv import load_dotenv  # noqa: E402
from lxml import etree, html  # noqa: E402

from core import block_filter, library  # noqa: E402
from core.junk_provider import build  # noqa: E402
from core.narration import narrate_text  # noqa: E402
from core.parser import block_text, get_koreader_xpath_lxml  # noqa: E402
from core.structure import (drop_print_artifacts, leaf_blocks, normalize_caps,  # noqa: E402
                            say_chapter_numbers, structural_junk, trim_boilerplate_blocks)

def old_extraction(el):
    return re.sub(r"\s+", " ", " ".join(el.itertext())).strip()


def run_plan(log_path, title_fragment):
    """ai_brain's final per-document verdicts, read from the ghost log of the last run."""
    verdicts, capture = {}, False
    try:
        lines = open(log_path, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return verdicts
    for line in lines:
        if "BOOK DETECTED" in line:
            capture = title_fragment.lower() in line.lower()
            if capture:
                verdicts = {}
        m = re.search(r"(✅|🚫)\s+(\d+) words\s+(\S+)", line)
        if capture and m:
            verdicts[m.group(3)] = "KEEP" if m.group(1) == "✅" else "JUNK"
    return verdicts


async def main():
    load_dotenv(os.path.join(ROOT, ".env"))
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--title", required=True, help="substring of the library folder name")
    ap.add_argument("--until-chapter", type=int, default=1,
                    help="stop after the Nth block the chapter-number rule rewrote")
    ap.add_argument("--sentences", type=int, default=2, help="...plus this many sentences of prose after it")
    ap.add_argument("--no-jev", action="store_true", help="skip the JEV calls")
    a = ap.parse_args()

    kepubs = library.find_kepub(a.title, root=ROOT)
    if not kepubs:
        sys.exit(f"no KEPUB for {a.title!r} under {library.BOOKS}/")
    kepub = kepubs[0]
    folder = os.path.basename(os.path.dirname(kepub))
    plan = run_plan(os.path.expanduser("~/Library/Logs/librasync-ghost.out"), a.title)
    jev = None if a.no_jev else build("jev")

    # What the engine stored, to check this report against.
    db = sqlite3.connect(os.path.join(ROOT, "library", "database.db"))
    db.row_factory = sqlite3.Row
    book = db.execute("SELECT book_id, lib_id FROM books WHERE epub_path LIKE ? AND deleted_at IS NULL",
                      (f"%{folder}%",)).fetchone()
    stored = {}
    for r in db.execute("""SELECT n.xpath_string, n.text_content FROM sync_nodes n
                           JOIN chapters c ON c.chapter_id = n.chapter_id WHERE c.book_id = ?""",
                        (book["book_id"],)):
        stored.setdefault(r["xpath_string"], []).append(" ".join((r["text_content"] or "").split()))

    docs, blocks_out = [], []
    chapters_seen, sentences_left, done = 0, None, False
    with zipfile.ZipFile(kepub) as z:
        container = etree.fromstring(z.read("META-INF/container.xml"))
        opf_path = container.xpath("//n:rootfile/@full-path",
                                   namespaces={"n": "urn:oasis:names:tc:opendocument:xmlns:container"})[0]
        opf = etree.fromstring(z.read(opf_path))
        ns = {"opf": "http://www.idpf.org/2007/opf"}
        manifest = {i.get("id"): i.get("href") for i in opf.xpath("//opf:item", namespaces=ns)}
        spine = [manifest[r.get("idref")] for r in opf.xpath("//opf:itemref", namespaces=ns)
                 if r.get("idref") in manifest]
        declared = structural_junk(z, opf, opf_path)
        opf_dir = os.path.dirname(opf_path)

        for index, href in enumerate(spine):
            frag = index + 1                      # the engine's doc_fragment_counter
            full = os.path.join(opf_dir, href).replace("\\", "/") if opf_dir else href
            rho = index / max(1, len(spine) - 1)
            doc = {"href": href, "frag": frag, "position": round(rho, 3), "declared": declared.get(full)}
            try:
                tree = parse_xhtml(z.read(full))
            except Exception as exc:
                doc["error"] = str(exc)
                docs.append(doc)
                continue
            leaves = leaf_blocks(tree)
            pairs = [(b, block_text(b)) for b in leaves]
            pairs = [(b, t) for b, t in pairs if t]
            before = {id(b) for b, _ in pairs}
            pairs, boiler_words, boiler_reason = trim_boilerplate_blocks(pairs)
            trimmed = before - {id(b) for b, _ in pairs}
            before = {id(b) for b, _ in pairs}
            pairs, artifact_counts = drop_print_artifacts(pairs)
            artifacts = before - {id(b) for b, _ in pairs}
            texts_in = [t for _, t in pairs]
            rewritten = say_chapter_numbers(texts_in)
            wc = sum(len(t.split()) for t in rewritten)
            doc.update(words=wc, blocks=len(pairs), gutenberg_trimmed=len(trimmed),
                       print_artifacts=len(artifacts), ai_brain=plan.get(href, "?"))

            if doc["declared"]:
                doc["decided"] = f"JUNK (declared: {doc['declared']})"
            elif wc < 20:
                doc["decided"] = "skipped (<20 words)"
            else:
                doc["decided"] = doc["ai_brain"]
            if jev and not doc["declared"] and wc >= 20:
                v = await jev.classify("\n\n".join(rewritten), wc, rho, href)
                doc["jev"] = {"verdict": v.verdict, "p": round(v.confidence, 3), "kind": v.detail}
            block_scores = [None] * len(pairs)
            if jev and doc.get("decided") == "KEEP":
                tags = [getattr(b, "tag", "p") for b, _ in pairs]
                block_scores = await block_filter.classify(jev, rewritten, tags, rho)
                drop, iso = block_filter.select_drops(block_scores)
            else:
                drop, iso = set(), set()
            docs.append(doc)

            if done:
                continue
            # Every leaf, including the ones a stage removed, in reading order.
            kept_index = {id(b): i for i, (b, _) in enumerate(pairs)}
            for b in leaves:
                lit = block_text(b)
                if not lit:
                    continue
                rec = {"frag": frag, "href": href, "tag": b.tag, "literal": lit,
                       "xpath": get_koreader_xpath_lxml(frag, b)}
                old = old_extraction(b)
                if old != lit:
                    rec["old_extraction"] = old
                if id(b) in trimmed:
                    rec["removed"] = f"Gutenberg trim ({boiler_reason})"
                elif id(b) in artifacts:
                    rec["removed"] = "print artifact"
                elif doc["declared"]:
                    rec["removed"] = f"document declared junk ({doc['declared']})"
                elif wc < 20:
                    rec["removed"] = "document under 20 words"
                elif doc["decided"] == "JUNK":
                    rec["removed"] = "document verdict JUNK"
                if id(b) in kept_index:
                    i = kept_index[id(b)]
                    if rewritten[i] != texts_in[i]:
                        rec["chapter_rewrite"] = rewritten[i]
                        chapters_seen += 1
                    s = block_scores[i]
                    if s is not None:
                        rec["jev_block"] = {"p": round(s.confidence, 3), "kind": s.detail,
                                            "rule": "drop" if i in drop else "isolated (kept)" if i in iso else "keep"}
                    if "removed" not in rec:
                        capped = normalize_caps(rewritten[i])
                        if capped != rewritten[i]:
                            rec["caps"] = capped
                        rec["narrated"] = narrate_text(b.tag, rewritten[i],
                                                       rewritten[i + 1] if i + 1 < len(rewritten) else None)
                        got = " ".join(stored.get(rec["xpath"], []))
                        rec["engine_stored"] = got
                        rec["matches_engine"] = bool(got) and rec["narrated"] is not None and \
                            " ".join(rec["narrated"].split()) in got
                blocks_out.append(rec)
                # Stop at the requested point: N sentences into the chosen chapter.
                if chapters_seen >= a.until_chapter and sentences_left is None and rec.get("chapter_rewrite"):
                    sentences_left = a.sentences
                elif sentences_left is not None and rec.get("narrated") and not rec.get("chapter_rewrite"):
                    sentences_left -= len(re.findall(r"[.!?](?:[\"'”’])?(?=\s|$)", rec["literal"]))
                    if sentences_left <= 0:
                        done = True
                        break

    if jev:
        await jev.aclose()
    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "_", a.title.lower()).strip("_")
    with open(os.path.join(ROOT, "reports", f"{slug}_decisions.json"), "w", encoding="utf-8") as f:
        json.dump({"book": folder, "lib_id": book["lib_id"], "docs": docs, "blocks": blocks_out,
                   "jev": jev.summary() if jev else None}, f, ensure_ascii=False, indent=1)
    write_markdown(os.path.join(ROOT, "reports", f"{slug}_decisions.md"), folder, book["lib_id"], docs, blocks_out)
    mism = [b for b in blocks_out if "narrated" in b and not b.get("matches_engine")]
    print(f"{folder}: {len(docs)} documents, {len(blocks_out)} blocks reported; "
          f"{len(mism)} block(s) where this report disagrees with the engine's sync map")
    for b in mism[:5]:
        print(f"   MISMATCH {b['xpath']}\n     report: {b['narrated'][:90]!r}\n     engine: {b['engine_stored'][:90]!r}")
    print(f"written: reports/{slug}_decisions.md and .json")


def write_markdown(path, folder, lib_id, docs, blocks):
    L = [f"# {folder} — every decision, with the literal text", "",
         f"Book `{lib_id}`. Generated from your own copy on this Mac; gitignored; not for sharing.", "",
         "## Documents", "", "| # | file | words | declared | ai_brain (as run) | JEV | decided |",
         "| --- | --- | --- | --- | --- | --- | --- |"]
    for d in docs:
        j = d.get("jev")
        js = f"{j['verdict']} p={j['p']} {j['kind']}" if j else "-"
        L.append(f"| {d['frag']} | `{d['href'].split('/')[-1]}` | {d.get('words', '-')} | "
                 f"{d.get('declared') or '-'} | {d.get('ai_brain', '-')} | {js} | {d.get('decided', '-')} |")
    L += ["", "## Blocks, in reading order", ""]
    for n, b in enumerate(blocks, 1):
        L.append(f"### {n}. `<{b['tag']}>` in `{b['href'].split('/')[-1]}`")
        L.append("")
        L.append("**Literal text in the book:**")
        L.append("")
        L.append("> " + b["literal"].replace("\n", "\n> "))
        L.append("")
        if "old_extraction" in b:
            L.append(f"- Old extraction (before 886d48c) read: `{b['old_extraction']}`")
        if "removed" in b:
            L.append(f"- **Removed:** {b['removed']}")
        if "chapter_rewrite" in b:
            L.append(f"- Chapter number rewritten to: `{b['chapter_rewrite']}`")
        if "caps" in b:
            L.append(f"- Capitals lowered to: `{b['caps']}`")
        if "jev_block" in b:
            j = b["jev_block"]
            L.append(f"- JEV block score: p={j['p']} ({j['kind']}) → {j['rule']}")
        if b.get("narrated"):
            L.append(f"- **Sent to the narrator:** `{b['narrated']}`")
            L.append(f"- Matches the engine's sync map: {'yes' if b.get('matches_engine') else 'NO — ' + repr(b.get('engine_stored'))}")
        L.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
