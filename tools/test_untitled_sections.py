"""A document the table of contents names nothing in joins the chapter before it.

Twisted (2026-10-11) has four such files (an in-story magazine article between
"Chapter Two" and "Chapter Three", among others), and each became a chapter of
its own, "Part 5", "Part 17", "Part 36", "Part 37", in the audiobook and in
Audiobookshelf. The user's rule: fold them into the chapter before.

Offline.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.structure import untitled_document_title  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


t = untitled_document_title("Part 5", has_entry=False, book_has_toc=True, previous_title="Chapter Two")
check("an unlisted file between two chapters is part of the one before", t == "Chapter Two", repr(t))

t = untitled_document_title("Who Is J. T. LeBeau?", has_entry=False, book_has_toc=True,
                            previous_title="Chapter Two")
check("even when it has a heading of its own: the table of contents decides", t == "Chapter Two", repr(t))

t = untitled_document_title("Chapter Three", has_entry=True, book_has_toc=True, previous_title="Chapter Two")
check("a file the table of contents names keeps its name", t == "Chapter Three", repr(t))

t = untitled_document_title("Part 1", has_entry=False, book_has_toc=True, previous_title=None)
check("before the first chapter there is nothing to join: it keeps its own name", t == "Part 1", repr(t))

t = untitled_document_title("Part 4", has_entry=False, book_has_toc=False, previous_title="Part 3")
check("a book with no table of contents is not folded into one chapter", t == "Part 4", repr(t))

engine = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "librerian_engine_v3.py")).read()
check("the engine names every narrated document through it", "chapter_title = untitled_document_title(" in engine)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
