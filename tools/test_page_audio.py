#!/usr/bin/env python3
"""Read -> listen starts where the page starts, not where its paragraph does.

core/page_audio.py measures how far into its paragraph a KOReader position
falls, in the KEPUB the reader reads, and turns that into the narrated word
that far in. Checked here on a small KEPUB built the way kepubify writes them
(koboSpans, inline markup, entities), and on what it must never do: start
after the page's first word, or outside the paragraph.

    python tools/test_page_audio.py
"""
import io
import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import page_audio as PA  # noqa: E402
from core import landing  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  ({detail})" if detail else ""))


CONTAINER = """<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>"""
OPF = """<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">
<manifest><item id="a" href="a.xhtml"/><item id="b" href="text/b.xhtml"/></manifest>
<spine><itemref idref="a"/><itemref idref="b"/></spine></package>"""
# 20 + 21 characters: "One two three four. " and "Five six seven eight."
P2 = ('<p><span class="koboSpan" id="kobo.2.1">One two three four. </span>'
      '<span class="koboSpan" id="kobo.2.2">Five six seven eight.</span></p>')
DOC_B = f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>B</title></head><body><div>
<p><span class="koboSpan" id="kobo.1.1">Short.</span></p>
{P2}
<p><span class="koboSpan" id="kobo.3.1">Caf&eacute; &amp; </span><em><span class="koboSpan" id="kobo.3.2">bar</span></em> end<br/> more</p>
</div></body></html>"""


def kepub(container=CONTAINER):
    path = os.path.join(tempfile.mkdtemp(), "b.kepub.epub")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", OPF)
        z.writestr("OEBPS/a.xhtml", "<html><body><p>a</p></body></html>")
        z.writestr("OEBPS/text/b.xhtml", DOC_B)
    return path


BOOK = kepub()
PARA = "/body/DocFragment[2]/body/div/p[2]"

f = PA.page_fraction(BOOK, PARA + "/span[2]/text().0", PARA)
check("a page starting on the second sentence is 20/41 of the way in", f is not None and abs(f - 20 / 41) < 1e-9, f)
f = PA.page_fraction(BOOK, PARA + "/span[1]/text().8", PARA)
check("an offset inside a text node counts its characters", f is not None and abs(f - 8 / 41) < 1e-9, f)
f = PA.page_fraction(BOOK, PARA + "/span[1]/text().0", PARA)
check("the paragraph's first character is 0", f == 0.0, f)

P3 = "/body/DocFragment[2]/body/div/p[3]"
f = PA.page_fraction(BOOK, P3 + "/em/span[1]/text().0", P3)
check("inline markup and entities: 'Café & ' is 7 characters before 'bar'",
      f is not None and abs(f - 7 / len("Café & bar end more")) < 1e-9, f)
f = PA.page_fraction(BOOK, P3 + "/text()[2].1", P3)
check("text()[2] is the paragraph's second text node",
      f is not None and abs(f - len("Café & bar end ") / len("Café & bar end more")) < 1e-9, f)

check("a fragment the book does not have", PA.page_fraction(BOOK, "/body/DocFragment[9]/body/div/p[2]/span[2]/text().0",
                                                            "/body/DocFragment[9]/body/div/p[2]") is None)
check("an element the page does not have", PA.page_fraction(BOOK, "/body/DocFragment[2]/body/div/p[7]/span[1]/text().0",
                                                            "/body/DocFragment[2]/body/div/p[7]") is None)
check("a position outside the paragraph asked about", PA.page_fraction(BOOK, P3 + "/em/span[1]/text().0", PARA) is None)

# A book declaring entities must not have them expanded (the gateway reads uploads).
BOMB = kepub('<?xml version="1.0"?><!DOCTYPE c [<!ENTITY a "aaaaaaaaaa">'
             '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;"><!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]>'
             '<container><rootfiles><rootfile full-path="OEBPS/content.opf">&c;</rootfile></rootfiles></container>')
f = PA.page_fraction(BOMB, PARA + "/span[2]/text().0", PARA)
check("a book's own entities are never expanded, and the page is still found",
      f is not None and abs(f - 20 / 41) < 1e-9, f)
import re  # noqa: E402
check("no XML parser that expands entities is imported",
      not re.search(r"^\s*(import xml|from xml)", open(PA.__file__).read(), re.M))

# Narrated words for P2: (char_start, char_end, start time), one word a second from 100 s.
TEXT = "One two three four. Five six seven eight."
WORDS, i = [], 0
for n, w in enumerate(TEXT.split()):
    i = TEXT.index(w, i)
    WORDS.append((i, i + len(w), 100.0 + n))
    i += len(w)

check("lands two words before the page's first word ('Five' is word 4)",
      PA.time_in_paragraph([WORDS], 20 / 41) == 102.0, PA.time_in_paragraph([WORDS], 20 / 41))
check("never before the paragraph's first word", PA.time_in_paragraph([WORDS], 0.05) == 100.0)
check("no words, no answer", PA.time_in_paragraph([[]], 0.5) is None)
half = [w for w in WORDS if w[0] < 20]
rest = [(s - 20, e - 20, t) for s, e, t in WORDS if s >= 20]
check("a paragraph split across two chunks is laid end to end",
      PA.time_in_paragraph([half, rest], 20 / 41) == 102.0, PA.time_in_paragraph([half, rest], 20 / 41))

t = PA.page_start_time(BOOK, PARA + "/span[2]/text().0", PARA, (100.0, 108.0), [WORDS])
check("the whole way: page on the second sentence -> 102 s", t == 102.0, t)
t = PA.page_start_time(BOOK, PARA + "/span[2]/text().0", PARA, (100.0, 141.0), [])
check("no word timings: the same share of the paragraph, 4 s early",
      t is not None and abs(t - (100.0 + 41 * 20 / 41 - 4.0)) < 1e-9, t)
t = PA.page_start_time(BOOK, PARA + "/span[1]/text().3", PARA, (100.0, 141.0), [])
check("...and never before the paragraph's start", t == 100.0, t)
check("the page at the paragraph's start keeps it (None)",
      PA.page_start_time(BOOK, PARA + "/span[1]/text().0", PARA, (100.0, 108.0), [WORDS]) is None)
check("a file that is not a book keeps the paragraph's start",
      PA.page_start_time(__file__, PARA + "/span[2]/text().0", PARA, (100.0, 108.0), [WORDS]) is None)

# The map's paragraph for a position, as landing.time_for_position walks up to it.
nodes = [("/body/DocFragment[2]/body/div/p[2]", 100.0, 108.0), ("/body/DocFragment[2]/body/div/p[3]", 108.0, 112.0)]
check("the position's paragraph in the map", landing.node_path_for_position(nodes, PARA + "/span[2]/text().3") == PARA)
check("inline markup walks up to it", landing.node_path_for_position(nodes, P3 + "/em/span[1]/text().0") == P3)
check("a paragraph the map does not have", landing.node_path_for_position(nodes, "/body/DocFragment[2]/body/div/p[9]") is None)

print(f"\n{sum(results)} passed, {len(results) - sum(results)} failed")
sys.exit(0 if all(results) else 1)
