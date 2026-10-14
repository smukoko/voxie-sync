"""Chapters inside one file, picture text, and Gutenberg's repeated title line.

Found narrating two Project Gutenberg books for the launch (2026-10-04):

  - The Canterville Ghost (#14522): its seven chapters share one file and came
    out as one 62-minute chapter; an illustrated Maupassant collection (#10746,
    12 stories in 2 files) would have been 2 chapters named after the first
    story in each.
  - The same edition narrated its 17-entry List of Illustrations, then each
    caption again mid-story, and a "The Project Gutenberg eBook, ..." line that
    sits just after the START marker.

Each rule is also tested on what it must NOT remove.

    python tools/test_chapters_and_pictures.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.parser import parse_xhtml  # noqa: E402
from core.structure import (chapter_segments, chapter_starts, drop_gutenberg_front_matter, drop_gutenberg_title_page,
                            drop_printed_contents,  # noqa: E402
                            drop_picture_text, narratable_blocks, say_chapter_numbers,
                            trim_boilerplate_blocks)

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


def doc(body):
    tree = parse_xhtml(f'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head>'
                       f'<body>{body}</body></html>'.encode())
    return tree, narratable_blocks(tree)


def texts(pairs):
    return [t for _, t in pairs]


# --- chapters inside one file ------------------------------------------------
tree, blocks = doc("""
<h1>The Canterville Ghost</h1><p>Front matter.</p>
<h2><a id="I"></a>I.</h2><p>When Mr. Hiram B. Otis bought Canterville Chase.</p><p>More.</p>
<div id="II"></div><h2>II.</h2><p>The storm raged.</p>
<h2 id="III">III.</h2><p>The next morning.</p>""")
anchors = [("I", "I.", 1), ("II", "II.", 1), ("III", "III.", 1), ("nowhere", "Lost", 1)]
starts = chapter_starts(tree, blocks, anchors)
segments = chapter_segments(blocks, starts, "The Canterville Ghost", min_words=0)
check("three chapters start where the table of contents points",
      [t for t, _ in segments] == ["The Canterville Ghost", "I.", "II.", "III."], [t for t, _ in segments])
check("an anchor inside the heading starts the heading",
      texts(segments[1][1])[0] == "I.", texts(segments[1][1]))
check("an empty anchor before the heading starts the heading after it",
      texts(segments[2][1])[0] == "II.", texts(segments[2][1]))
check("an anchor on the heading itself starts it", texts(segments[3][1])[0] == "III.")
check("no block is lost or repeated", sum(len(b) for _, b in segments) == len(blocks))
check("an anchor that names nothing starts nothing", "Lost" not in [t for t, _ in segments])

# A book with one chapter per file: its anchor points at the top. Unchanged.
tree, blocks = doc('<h2 id="top">Chapter One</h2><p>Text.</p><p>More text.</p>')
segments = chapter_segments(blocks, chapter_starts(tree, blocks, [("top", "Chapter One", 0)]), "Chapter One")
check("one chapter per file stays one chapter", len(segments) == 1 and segments[0][0] == "Chapter One")
segments = chapter_segments(blocks, chapter_starts(tree, blocks, []), "Part 3")
check("no anchors: the document's own title", [t for t, _ in segments] == ["Part 3"])

# An anchor whose chapter was removed (boilerplate) lands on the next kept block
# only if there is one after it.
tree, blocks = doc('<h2 id="a">Story</h2><p>Text.</p><h2 id="lic">LICENCE</h2><p>Terms.</p>')
kept = blocks[:2]
segments = chapter_segments(kept, chapter_starts(tree, kept, [("a", "Story", 0), ("lic", "LICENCE", 0)]), "Story",
                            min_words=0)
check("an anchor after the last kept block starts nothing", [t for t, _ in segments] == ["Story"])

# A file that opens on the end of the previous file's chapter (Gutenberg splits
# by size): the last paragraph of "Un Parricide" was heard as "Le Rendez-vous".
tree, blocks = doc(f"""<p>L'accusé se rassit. Devant cette révélation, l'affaire a été reportée.</p>
<h3 id="r">Le Rendez-vous</h3><p>{" ".join(["word"] * 60)}</p>""")
starts = chapter_starts(tree, blocks, [("r", "Le Rendez-vous", 2)])
segments = chapter_segments(blocks, starts, "Le Rendez-vous", continues="Un Parricide")
check("text above the file's first entry ends the previous chapter",
      [t for t, _ in segments] == ["Un Parricide", "Le Rendez-vous"]
      and texts(segments[0][1]) == [texts(blocks)[0]], [t for t, _ in segments])
check("...without it, folded forward as before",
      [t for t, _ in chapter_segments(blocks, starts, "Le Rendez-vous")] == ["Le Rendez-vous"])
check("...and a file whose first entry is at its top is not split",
      [t for t, _ in chapter_segments(blocks[1:], chapter_starts(tree, blocks[1:], [("r", "Le Rendez-vous", 2)]),
                                      "Le Rendez-vous", continues="Un Parricide")] == ["Le Rendez-vous"])

# Headings that are not chapters, shaped like the two real books' TOCs.
LONG = " ".join(["word"] * 60)
tree, blocks = doc(f"""
<h1 id="t">The Canterville Ghost</h1><h3>By</h3><h2 id="w">WILDE</h2><h4 id="d">1906</h4><p>{" ".join(["imprint"] * 45)}</p>
<h2 id="c1">I.</h2><p>{LONG}</p><h2 id="c2">II.</h2><p>{LONG}</p>""")
anchors = [("t", "The Canterville Ghost", 0), ("w", "WILDE", 1), ("d", "1906", 2), ("c1", "I", 1), ("c2", "II", 1)]
segments = chapter_segments(blocks, chapter_starts(tree, blocks, anchors), "The Canterville Ghost")
check("a title page of several lines is one first chapter, named by its first line",
      [t for t, _ in segments] == ["The Canterville Ghost", "I", "II"], [t for t, _ in segments])
check("folding drops nothing", sum(len(b) for _, b in segments) == len(blocks))
tree, blocks = doc(f"""<h1 id="t">The Canterville Ghost</h1><h3>By</h3><h2>WILDE</h2>
<h2 id="c1">I.</h2><p>{LONG}</p><h2 id="c2">II.</h2><p>{LONG}</p>""")
segments = chapter_segments(blocks, chapter_starts(tree, blocks, [("t", "The Canterville Ghost", 0), ("c1", "I", 0),
                                                                  ("c2", "II", 0)]), "The Canterville Ghost")
check("a credit folded into a numbered chapter leaves it its number", [t for t, _ in segments] == ["I", "II"],
      [t for t, _ in segments])

tree, blocks = doc(f"""
<h2 id="paris">PARIS 1907</h2>
<h3 id="bds">BOULE DE SUIF</h3><p>{LONG}</p>
<h3 id="pain">Le Pain maudit</h3><h4 id="i">I</h4><p>{LONG}</p><h4 id="ii">II</h4><p>{LONG}</p>
<h3 id="sab">Les Sabots</h3><p>{LONG}</p>""")
anchors = [("paris", "PARIS 1907", 1), ("bds", "BOULE DE SUIF", 2), ("pain", "Le Pain maudit", 2),
           ("i", "I", 3), ("ii", "II", 3), ("sab", "Les Sabots", 2)]
segments = chapter_segments(blocks, chapter_starts(tree, blocks, anchors), "Boule de Suif")
check("a story's title names its first section; the parent of all the stories names none",
      [t for t, _ in segments] == ["BOULE DE SUIF", "Le Pain maudit", "II", "Les Sabots"], [t for t, _ in segments])

# Usher: the title heading is removed (said once already) and falls onto the byline.
tree, blocks = doc(f'<h1 id="t">THE FALL OF THE HOUSE OF USHER</h1><h2 id="a">EDGAR ALLAN POE</h2><p>{LONG}</p>')
kept = blocks[1:]
segments = chapter_segments(kept, chapter_starts(tree, kept, [("t", "THE FALL OF THE HOUSE OF USHER", 0),
                                                              ("a", "EDGAR ALLAN POE", 1)]), "x")
check("a removed title still names its file's first chapter, not the byline under it",
      [t for t, _ in segments] == ["THE FALL OF THE HOUSE OF USHER"], [t for t, _ in segments])

# Usher as the engine sees it: title and "By" kept, then the byline's entry.
tree, blocks = doc(f'<h1 id="t">THE FALL OF THE HOUSE OF USHER</h1><h3>By</h3><h2 id="a">EDGAR ALLAN POE</h2><p>{LONG}</p>')
segments = chapter_segments(blocks, chapter_starts(tree, blocks, [("t", "THE FALL OF THE HOUSE OF USHER", 0),
                                                                  ("a", "EDGAR ALLAN POE", 0)]), "x")
check("a byline beside the title does not rename the story",
      [t for t, _ in segments] == ["THE FALL OF THE HOUSE OF USHER"], [t for t, _ in segments])

# --- picture text --------------------------------------------------------------
tree, blocks = doc("""
<h2>LIST OF ILLUSTRATIONS</h2>
<ul><li><a href="#image1">Miss Virginia E. Otis</a></li>
    <li><a href="#image2">"Had once raced old Lord Bilton"</a></li>
    <li><a href="ch.xhtml#image3">"Blood has been spilled"</a></li></ul>
<h2>I.</h2><p>When Mr. Otis bought Canterville Chase.</p>
<div class="figcenter"><img src="a.jpg" alt=""/><p class="caption">MISS VIRGINIA E. OTIS</p></div>
<figure><img src="b.jpg" alt=""/><figcaption>A figure caption</figcaption></figure>
<p>A few weeks after this.</p>""")
kept, counts = drop_picture_text(blocks)
check("the list of illustrations and its heading go",
      "LIST OF ILLUSTRATIONS" not in texts(kept) and "Miss Virginia E. Otis" not in texts(kept), texts(kept))
check("a caption beside its picture goes", "MISS VIRGINIA E. OTIS" not in texts(kept))
check("a <figcaption> goes", "A figure caption" not in texts(kept))
check("the story stays, in order",
      texts(kept) == ["I.", "When Mr. Otis bought Canterville Chase.", "A few weeks after this."], texts(kept))
check("what was cut is counted", counts == {"caption": 2, "illustration-list": 3}, counts)

tree, blocks = doc("""
<h2>Contents</h2>
<ul><li><a href="#c1">Chapter One</a></li><li><a href="#c2">Chapter Two</a></li>
    <li><a href="#c3">Chapter Three</a></li></ul>
<h2>Illustrations</h2>
<ul><li><a href="#i1">One</a></li><li><a href="#i2">Two</a></li></ul>
<h2>Plates</h2>
<ul><li><a href="#p1">One</a> facing page 4</li><li><a href="#p2">Two</a></li><li><a href="#p3">Three</a></li></ul>
<h2>Further reading</h2>
<ul><li><a href="https://a.example">A</a></li><li><a href="https://b.example">B</a></li>
    <li><a href="https://c.example">C</a></li></ul>
<ul><li>Eggs</li><li>Flour</li><li>Sugar</li></ul>
<p class="caption">A paragraph classed caption with no picture near it.</p>""")
kept, _ = drop_picture_text(blocks)
check("NOT a list of contents (its heading names no pictures)",
      "Chapter One" in texts(kept) and "Contents" in texts(kept))
check("NOT a list of two pictures", "Illustrations" in texts(kept))
check("NOT a list where an item says more than its link", "One facing page 4" in texts(kept))
check("NOT a list of web links", "A" in texts(kept))
check("NOT a list without links", "Eggs" in texts(kept))
check("NOT a caption with no picture beside it",
      "A paragraph classed caption with no picture near it." in texts(kept))

# The Worst Journey in the World: the caption is the paragraph after the picture's.
tree, blocks = doc("""
<p class="figcenter"><a href="big.xhtml"><img alt="Midnight—E. A. Wilson, del." src="m.jpg"/></a></p>
<p class="figcenter"><span class="smcap">Midnight</span>—E. A. Wilson, del.</p>
<h1>THE WORST JOURNEY</h1>
<p class="figcenter"><img alt="Frontispiece" src="f.jpg"/></p>
<p>Midnight came, and with it the wind.</p>""")
kept, counts = drop_picture_text(blocks)
check("a paragraph repeating the picture's alt text goes", "Midnight—E. A. Wilson, del." not in texts(kept), texts(kept))
check("NOT a paragraph after a picture that says something else",
      "Midnight came, and with it the wind." in texts(kept) and "THE WORST JOURNEY" in texts(kept))

# What the diff over the library caught (2026-10-04): both were real text.
tree, blocks = doc("""
<div style="text-align: center;"><img alt="Boule de Suif" src="c.jpg"/></div>
<h1>Boule de Suif</h1>
<div class="figcenter"><img alt="'Poor, poor Ghost,' she murmured" src="g.jpg"/></div>
<p>'Poor, poor Ghost,' she murmured</p>""")
kept, _ = drop_picture_text(blocks)
check("NOT a title that repeats its cover picture's alt text", "Boule de Suif" in texts(kept))
check("NOT a line of the story that a caption quotes", "'Poor, poor Ghost,' she murmured" in texts(kept))

# --- Gutenberg's repeated title line ------------------------------------------------
tree, blocks = doc("""
<div>*** START OF THE PROJECT GUTENBERG EBOOK THE CANTERVILLE GHOST ***</div>
<p>The Project Gutenberg eBook, The Canterville Ghost, by Oscar Wilde, Illustrated by Wallace Goldsmith</p>
<h1>The Canterville Ghost</h1>
<p>He said: "The Project Gutenberg eBook of my life is a long one."</p>""")
kept, _, reason = trim_boilerplate_blocks(blocks)
check("the title line just after the START marker goes", texts(kept)[0] == "The Canterville Ghost", texts(kept))
check("the same words later in the book stay", len(kept) == 2)
tree, blocks = doc('<p>The Project Gutenberg eBook, without any marker.</p><p>Text.</p>')
kept, _, _ = trim_boilerplate_blocks(blocks)
check("without a START marker nothing is trimmed", len(kept) == 2)

# --- the source edition's front matter, after Gutenberg's START marker --------
tree, blocks = doc("""
<div>*** START OF THE PROJECT GUTENBERG EBOOK BOULE DE SUIF ***</div>
<h2>LIBRAIRIE OLLENDORFF</h2><p>Collection des Grands Romans.</p><p>A 1 FRANC.</p>
<p>Envoi franco contre 1 fr. 25 par volume.</p>
<h1 id="t">Boule de Suif</h1><p>Pendant plusieurs jours de suite des lambeaux d'armée.</p>""")
kept, _, _ = trim_boilerplate_blocks(blocks)
kept, front = drop_gutenberg_front_matter(tree, kept, [("t", "Boule de Suif", 0)])
check("the publisher's catalogue before the first TOC entry goes",
      texts(kept) == ["Boule de Suif", "Pendant plusieurs jours de suite des lambeaux d'armée."], texts(kept))
check("...all four blocks of it", len(front) == 4, texts(front))
kept2, front2 = drop_gutenberg_front_matter(tree, blocks, [])
check("a file with no TOC entry keeps everything", not front2 and len(kept2) == len(blocks))

# --- the source edition's title page: its credit is said, its imprint is not ---
PROSE = " ".join(["mot"] * 60)
tree, blocks = doc(f"""
<h1 id="t">Boule de Suif</h1><h2 id="o">OEUVRES COMPLETES ILLUSTRÉES DU GUY DE MAUPASSANT</h2>
<p>ÉDITION DE LUXE</p><p>(Voir Catalogue à la fin du volume.)</p>
<h1 id="g">GUY DE MAUPASSANT</h1><h2 id="p">PARIS 1907</h2>
<h3 id="s">BOULE DE SUIF</h3><p>Pendant plusieurs jours {PROSE}</p>""")
anchors = [("t", "Boule de Suif", 0), ("o", "OEUVRES", 1), ("g", "GUY DE MAUPASSANT", 1),
           ("p", "PARIS 1907", 1), ("s", "BOULE DE SUIF", 1)]
kept, imprint = drop_gutenberg_title_page(tree, blocks, anchors, "Boule de Suif", "Guy de Maupassant")
check("the title page keeps the title and the author", texts(kept)[:3] == ["Boule de Suif", "GUY DE MAUPASSANT", "BOULE DE SUIF"],
      texts(kept))
check("...and loses its imprint", texts(imprint) == ["OEUVRES COMPLETES ILLUSTRÉES DU GUY DE MAUPASSANT", "ÉDITION DE LUXE",
                                                     "(Voir Catalogue à la fin du volume.)", "PARIS 1907"], texts(imprint))
check("...and the story is untouched", texts(kept)[-1].startswith("Pendant plusieurs jours"))

tree, blocks = doc(f"""
<h1 id="t">The Canterville Ghost</h1><h3>By</h3><h2 id="w">WILDE</h2>
<h4 id="i">Illustrated by WALLACE GOLDSMITH</h4><h4 id="d">1906</h4><h6>To my wife</h6>
<h2 id="c1">I</h2><p>{PROSE}</p>""")
anchors = [("t", "The Canterville Ghost", 0), ("w", "WILDE", 1), ("i", "Illustrated", 1), ("d", "1906", 1), ("c1", "I", 1)]
kept, imprint = drop_gutenberg_title_page(tree, blocks, anchors, "The Canterville Ghost", "Oscar Wilde")
check("'By' and the surname the page prints stay, and a dedication",
      texts(kept)[:5] == ["The Canterville Ghost", "By", "WILDE", "To my wife", "I"], texts(kept))
check("...the illustrator and the year go", texts(imprint) == ["Illustrated by WALLACE GOLDSMITH", "1906"], texts(imprint))

tree, blocks = doc(f'<h1 id="t">ARSÈNE LUPIN</h1><h3 id="g">Gentleman-Cambrioleur</h3><h4>1907</h4><p>{PROSE}</p>')
kept, imprint = drop_gutenberg_title_page(tree, blocks, [("t", "ARSÈNE LUPIN", 0), ("g", "Gentleman", 1)],
                                          "Arsène Lupin, gentleman-cambrioleur", "Maurice Leblanc")
check("a title printed over two lines is still the title", texts(kept)[:2] == ["ARSÈNE LUPIN", "Gentleman-Cambrioleur"],
      texts(imprint))

# What it must NOT delete.
POEM = "<p>Une ligne du poème ici</p>" * 3
tree, blocks = doc(f"""<h1 id="t">Poèmes</h1><h2 id="a">Le Lac</h2>{POEM}<h2 id="b">Le Soir</h2>{POEM}
<h2 id="c">Le Vallon</h2><p>{PROSE}</p>""")
anchors = [("t", "Poèmes", 0), ("a", "Le Lac", 1), ("b", "Le Soir", 1), ("c", "Le Vallon", 1)]
kept, imprint = drop_gutenberg_title_page(tree, blocks, anchors, "Poèmes", "Lamartine")
check("NOT a collection of short poems (mostly lines, not headings)", not imprint and len(kept) == len(blocks))
tree, blocks = doc(f'<h1 id="t">Walden</h1><p>Paris 1854</p><p>{PROSE}</p><h2 id="c">Economy</h2><p>{PROSE}</p>')
kept, imprint = drop_gutenberg_title_page(tree, blocks, [("t", "Walden", 0), ("c", "Economy", 1)], "Walden", "Thoreau")
check("NOT a first entry with prose under it (a preface)", not imprint)
tree, blocks = doc(f'<p>Above the first entry</p><h1 id="t">Walden</h1><p>Paris</p><h2 id="c">I</h2><p>{PROSE}</p>')
kept, imprint = drop_gutenberg_title_page(tree, blocks, [("t", "Walden", 0), ("c", "I", 1)], "Walden", "Thoreau")
check("NOT when text sits above the first entry (the front-matter rule's job)", not imprint)

# --- the book's printed table of contents ---------------------------------------
labels = ["Boule de Suif", "L'Épave", "Découverte", "Un Parricide", "Bombard", "Divorce", "Une Soirée"]
kept, gone = drop_printed_contents([(None, "Fin de l'histoire."), (None, "TABLE"),
                                    (None, "Boule de Suif L'Épave Découverte Un Parricide Bombard Divorce Une Soirée.")], labels)
check("a printed table of contents and its heading go", texts(kept) == ["Fin de l'histoire."] and len(gone) == 2)
kept, gone = drop_printed_contents([(None, "Il revit Bombard, Boule de Suif et le Divorce dont on parlait tant."),
                                    (None, "Divorce")], labels)
check("NOT prose that names titles, NOT a lone chapter heading", not gone)

# --- chapter numbers --------------------------------------------------------------
check("chapter numbers are said in words", say_chapter_numbers(["II", "XII."]) == ["Two.", "Twelve."])
check("prose is never touched", say_chapter_numbers(["Henry IV entered."]) == ["Henry IV entered."])

print("=" * 60)
print(f"{passed + failed} checks: {passed} passed, {failed} FAILED")
sys.exit(1 if failed else 0)
