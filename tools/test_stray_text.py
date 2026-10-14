"""Text outside every leaf block is narrated, and mapped to an element the reader can resolve.

Usher lost 70 words of the story this way (its epigraph, in a <td>, and a
passage of the Mad Trist, bare text after a <div> inside a <pre>), and
narration_review.py could only report it. Also: Calibre's metadata jacket is
declared junk, since the new runs would otherwise add its star rating and
tags to a document the classifier already kept.
"""
import io, os, sys, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lxml import etree  # noqa: E402
from core.parser import get_koreader_xpath_lxml, parse_xhtml  # noqa: E402
from core.structure import StrayRun, narratable_blocks, structural_junk  # noqa: E402
from core.narration import narrate_text  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


def units(body):
    return narratable_blocks(parse_xhtml(f"<html><body>{body}</body></html>".encode()))


def path(el):
    return get_koreader_xpath_lxml(1, el)[len("point(/1/body/DocFragment[1]"):-1]


# The shape of Usher's first document, cut down.
usher = units("""
<pre><div class="book">
<div class="center"><h1>THE FALL OF THE HOUSE OF USHER</h1><h2>EDGAR ALLAN POE</h2>
<table><tr><td><i>Son coeur est un luth suspendu;<br/>Sitot qu'on le touche il resonne.</i></td></tr></table>
</div>
<p>De Beranger.</p>
<p>enwritten—</p>
<div class="center"><table><tr><td>Who entereth herein</td></tr></table></div>
And Ethelred uplifted his mace, and struck upon the head of the dragon.
<p>Here again I paused abruptly.</p>
</div></pre>""")
texts = [t for _, t in usher]
check("the epigraph is narrated", any(t.startswith("Son coeur") for t in texts), f"{texts}")
check("the Mad Trist passage is narrated", any(t.startswith("And Ethelred") for t in texts))
check("in reading order",
      texts.index(next(t for t in texts if t.startswith("Son"))) < texts.index("De Beranger.")
      < texts.index(next(t for t in texts if t.startswith("And Ethelred")))
      < texts.index("Here again I paused abruptly."))
check("a <br> is a space", any("suspendu; Sitot" in t for t in texts))
epigraph = next(u for u, t in usher if t.startswith("Son"))
ethelred = next(u for u, t in usher if t.startswith("And Ethelred"))
check("the epigraph maps to the element holding it and nothing else",
      path(epigraph).endswith("/div[1]/table"), path(epigraph))
check("a bare tail maps to the leaf block just before it",
      path(ethelred).endswith("/div[2]"), path(ethelred))
check("never to a container that holds blocks (the gateway walks up into it)",
      not any(path(u).endswith(("/pre", "/div", "/body")) for u, _ in usher if isinstance(u, StrayRun)))
check("a run is prose to the narration rules, even anchored to a heading",
      epigraph.tag == "p" and narrate_text(epigraph.tag, "EDGAR ALLAN POE").endswith("."))

lead = units("<div>Loose text first.<p>Then a paragraph.</p></div>")
check("loose text before any block maps to the next block",
      [t for _, t in lead] == ["Loose text first.", "Then a paragraph."]
      and path(lead[0][0]) == path(lead[1][0]), path(lead[0][0]))

cells = units("<table><tr><td>Name</td><td>Age</td></tr></table><p>x</p>")
check("table cells are separate runs, never one word", [t for _, t in cells][:2] == ["Name", "Age"],
      f"{[t for _, t in cells]}")

nothing = units("Only loose text, no block at all.")
check("a document with no block at all still narrates, mapped to <body> only",
      [t for _, t in nothing] == ["Only loose text, no block at all."] and path(nothing[0][0]) == "/body")

kepub = units('<div><h2>T</h2><table><tr><td><span class="koboSpan" id="kobo.1.1">One. </span>'
              '<span class="koboSpan" id="kobo.1.2">Two.</span></td></tr></table></div>')
check("a run split into koboSpans maps to the element holding all of it",
      path(kepub[1][0]).endswith("/div/table"), path(kepub[1][0]))

plain = units("<p>One.</p><p>Two <em>three</em>.</p>")
check("an ordinary document is unchanged", [t for _, t in plain] == ["One.", "Two three."]
      and not any(isinstance(u, StrayRun) for u, _ in plain))


# --- Calibre's jacket ---------------------------------------------------------
def book(items, files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    opf = ('<package xmlns="http://www.idpf.org/2007/opf"><manifest>'
           + "".join(f'<item id="{i}" href="{h}" media-type="application/xhtml+xml"/>' for i, h in items)
           + "</manifest></package>")
    return zipfile.ZipFile(buf), etree.fromstring(opf.encode())


jacket = '<html><head><meta name="calibre-content" content="jacket"/></head><body>x</body></html>'
z, opf = book([("calibre_jacket", "jacket.xhtml"), ("c1", "ch1.xhtml")],
              {"jacket.xhtml": jacket, "ch1.xhtml": "<html/>"})
check("Calibre's jacket is declared junk by its manifest id",
      structural_junk(z, opf, "content.opf") == {"jacket.xhtml": "calibre:jacket"})
z, opf = book([("j", "jacket.xhtml")], {"jacket.xhtml": jacket})
check("and by its own meta tag", structural_junk(z, opf, "content.opf").get("jacket.xhtml") == "calibre:jacket")
z, opf = book([("j", "jacket.xhtml")], {"jacket.xhtml": "<html><body>A story called Jacket.</body></html>"})
check("a chapter merely named jacket.xhtml is not", structural_junk(z, opf, "content.opf") == {})

engine = open(os.path.join(ROOT, "librerian_engine_v3.py"), encoding="utf-8").read()
review = open(os.path.join(ROOT, "tools", "narration_review.py"), encoding="utf-8").read()
check("the engine and the review narrate from the same list",
      "block_texts = narratable_blocks(tree)" in engine and "leaves = narratable_blocks(tree)" in review)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
