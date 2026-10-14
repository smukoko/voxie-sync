"""An EPUB 3 book's table of contents is read from its nav document when it has no NCX.

Twisted (2026-10-07) carries only nav.xhtml: read_toc read NCX files alone, so
every chapter was named "Part N" in Audiobookshelf and chapter boundaries fell
back to file breaks. Offline, on generated EPUBs.
"""
import io
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from core.structure import read_toc  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


CONTAINER = ('<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
             '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
             '</rootfiles></container>')


def book(nav=None, ncx=None, nav_props="nav"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("META-INF/container.xml", CONTAINER)
        items = '<item id="c1" href="text/c1.xhtml" media-type="application/xhtml+xml"/>'
        if nav:
            items += f'<item id="nav" href="nav.xhtml" properties="{nav_props}" media-type="application/xhtml+xml"/>'
            z.writestr("OEBPS/nav.xhtml", nav)
        if ncx:
            z.writestr("OEBPS/toc.ncx", ncx)
        z.writestr("OEBPS/content.opf", '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
                   f'version="3.0"><metadata/><manifest>{items}</manifest><spine><itemref idref="c1"/></spine></package>')
    return zipfile.ZipFile(io.BytesIO(buf.getvalue()))


NAV = '''<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>
<nav epub:type="landmarks"><ol><li><a href="text/c1.xhtml">Begin Reading</a></li></ol></nav>
<nav epub:type="toc"><ol>
  <li><a href="text/c1.xhtml">Author’s Note</a></li>
  <li><a href="text/c2.xhtml">Part One</a>
    <ol><li><a href="text/c2.xhtml#ch1">Chapter
        <span>One</span></a></li>
        <li><a href="text/c2.xhtml#ch2">Chapter Two</a></li></ol></li>
  <li><span>A heading with no link</span></li>
</ol></nav></body></html>'''

lookup, anchors = read_toc(book(nav=NAV))
check("each file is named after its first entry",
      lookup == {"OEBPS/text/c1.xhtml": "Author’s Note", "OEBPS/text/c2.xhtml": "Part One"}, str(lookup))
check("entries inside a file keep their fragment, label and depth",
      anchors.get("OEBPS/text/c2.xhtml") == [("ch1", "Chapter One", 1), ("ch2", "Chapter Two", 1)],
      str(anchors))
check("the landmarks list is not taken for the table of contents", "Begin Reading" not in lookup.values())

NCX = '''<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>
<navPoint id="a"><navLabel><text>From the NCX</text></navLabel><content src="text/c1.xhtml"/></navPoint>
</navMap></ncx>'''
lookup, _ = read_toc(book(nav=NAV, ncx=NCX))
check("a book with both still reads its NCX, as before", lookup == {"OEBPS/text/c1.xhtml": "From the NCX"}, str(lookup))
check("a book with neither has no table of contents", read_toc(book()) == ({}, {}))
lookup, _ = read_toc(book(nav=NAV.replace('epub:type="toc"', ""), nav_props="nav"))
check("an untyped nav is the table of contents, never the landmarks beside it",
      lookup.get("OEBPS/text/c1.xhtml") == "Author’s Note" and "Begin Reading" not in lookup.values(), str(lookup))

# What a damaged or unusual book does: no table of contents, never a crash.
def missing_nav_file():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
                   'version="3.0"><manifest><item id="nav" href="nav.xhtml" properties="nav"/></manifest></package>')
    return zipfile.ZipFile(io.BytesIO(buf.getvalue()))


check("a nav the manifest names but the archive lacks: no TOC, no crash", read_toc(missing_nav_file()) == ({}, {}))
check("a nav document with no <nav> at all: no TOC",
      read_toc(book(nav='<html xmlns="http://www.w3.org/1999/xhtml"><body><p>x</p></body></html>')) == ({}, {}))
only_landmarks = NAV[:NAV.index('<nav epub:type="toc">')] + "</body></html>"
check("only a landmarks list: no TOC (it names no chapters)", read_toc(book(nav=only_landmarks)) == ({}, {}))
lookup, _ = read_toc(book(nav=NAV.replace('<li><a href="text/c1.xhtml">Author’s Note</a></li>',
                                          '<li><a href="#top">Back to top</a></li>')))
check("a link within the nav itself (#fragment only) is no entry", "Back to top" not in lookup.values(), str(lookup))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
