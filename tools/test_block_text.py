#!/usr/bin/env python3
"""The text handed to the model must be the text the author wrote.

Two separate defects made the narrator spell letters aloud, and both were
invisible in every existing check because the audio was the only place they
showed.

1. Inline runs were joined with a SPACE. Publishers fake small caps by leaving
   the first letter full size and wrapping the rest in a span:

       <h1>A N<span class="small">OTE FROM</span> F<span class="small">RANCINE</span>

   so the space-join produced "A N OTE FROM F RANCINE" and the model read the
   orphans as initials. 75 tokens in Redeeming Love were decapitated that way
   -- ESUS, ATTHEW, SALM, HAKESPEARE, ORINTHIANS -- and the same stray space
   landed before punctuation in 19% of A Doll's House's blocks.

2. say_chapter_numbers' rewrite was DISCARDED. The narration loop re-read each
   block's text out of the DOM, so "IX" went to the model as "IX" and was read
   as two letters, 28 times in The Screwtape Letters.

    python tools/test_block_text.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lxml import html  # noqa: E402

from core.parser import block_text  # noqa: E402
from core.structure import say_chapter_numbers  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  ({detail})" if detail else ""))


def text_of(frag):
    return block_text(html.fromstring(frag))


# --- faux small caps: the real markup from Redeeming Love ------------------
got = text_of('<h1 class="chapter1"><a id="page465"></a>A N<span class="small">OTE FROM</span>'
              ' F<span class="small">RANCINE</span> R<span class="small">IVERS</span></h1>')
check("faux small caps do not orphan the capital", got == "A NOTE FROM FRANCINE RIVERS", got)

got = text_of('<p>J<span class="small">ESUS</span>, J<span class="small">OHN</span> 8:7.</p>')
check("a scripture citation survives intact", got == "JESUS, JOHN 8:7.", got)

# --- the stray space before punctuation ------------------------------------
got = text_of('<p>it is not the way I had things <i>planned</i>.</p>')
check("no space is inserted before a full stop", got == "it is not the way I had things planned.", got)

got = text_of('<p>And she lives <i>there</i>, he thought.</p>')
check("no space is inserted before a comma", got == "And she lives there, he thought.", got)

# --- <br> is the one child that IS whitespace ------------------------------
got = text_of('<p>Line one<br/>Line two</p>')
check("<br> still separates two lines", got == "Line one Line two", got)

got = text_of('<p>A<span>B<i>C</i>D</span>E</p>')
check("tails are emitted in document order", got == "ABCDE", got)

# --- and nothing that was already correct changes --------------------------
got = text_of('<p>Plain prose with <em>emphasis</em> in the middle of it.</p>')
check("ordinary emphasis is unaffected",
      got == "Plain prose with emphasis in the middle of it.", got)

# Spaces and tabs collapse here; newlines are deliberately left for
# normalize_text on the narration path, so the planning path keeps exactly the
# text it had before this helper existed.
got = text_of('<p>  ragged   whitespace  collapses  </p>')
check("runs of spaces collapse", got == "ragged whitespace collapses", got)

from core.parser import normalize_text  # noqa: E402
got = normalize_text(text_of('<p>ragged\n   newline</p>'))
check("normalize_text finishes the job for the model",
      got == "ragged newline", got)

# --- the rewrite must reach the model, not just the plan -------------------
# This is the shape of the engine's loop: block_texts is what say_chapter_numbers
# rewrote, and the narration must read from THAT, never back from the DOM.
doc = html.fromstring('<div><h2>IX</h2></div>')
blocks = [(b, block_text(b)) for b in doc.xpath('//h2')]
rewritten = say_chapter_numbers([t for _, t in blocks])
blocks = [(b, new) for (b, _), new in zip(blocks, rewritten)]
narrated = [t for _, t in blocks]
check("a bare roman numeral reaches the model as words", narrated == ["Nine."], str(narrated))
check("re-reading the DOM would have lost it", block_text(doc.xpath('//h2')[0]) == "IX")

# --- typographic capitals, which the model spells out ----------------------
# Heard on the device: Redeeming Love's epigraph "CALIFORNIA, 1850." was read
# letter by letter. The caps are the typesetter's font choice, not the
# author's word, so they carry nothing the listener can use.
from core.structure import normalize_caps  # noqa: E402

for src, want in [
    ("CALIFORNIA, 1850.",           "California, 1850."),
    ("NEW ENGLAND, 1835.",          "New England, 1835."),
    ("A NOTE FROM FRANCINE RIVERS", "A Note from Francine Rivers"),
    ("JESUS, JOHN 8:7.",            "Jesus, John 8:7."),
    ("ACKNOWLEDGMENTS",             "Acknowledgments"),
]:
    got = normalize_caps(src)
    check(f"{src[:28]!r} is said, not spelled", got == want, got)

# ...and what it must NOT touch.
for src in [
    "He said OK to the FBI.",          # acronyms in ordinary prose
    "Ordinary prose, left alone.",
    "The BBC and the CIA.",
]:
    check(f"untouched: {src[:30]!r}", normalize_caps(src) == src, normalize_caps(src))

check("a known acronym survives a caps-only block",
      normalize_caps("THE FBI AND THE CIA") == "The FBI and the CIA",
      normalize_caps("THE FBI AND THE CIA"))
check("a roman numeral is never title-cased into nonsense",
      normalize_caps("ACT III") == "Act III", normalize_caps("ACT III"))
check("an initial stays an initial",
      normalize_caps("F. SCOTT FITZGERALD") == "F. Scott Fitzgerald",
      normalize_caps("F. SCOTT FITZGERALD"))

# Ordering: the Gutenberg markers must still match after normalisation, because
# nothing guarantees which side of the trim a future edit puts this on.
from core.structure import trim_boilerplate_blocks  # noqa: E402
marker = normalize_caps("*** START OF THE PROJECT GUTENBERG EBOOK A DOLL'S HOUSE ***")
pairs = [(object(), marker), (object(), "The real text of the play begins here.")]
kept, _, _ = trim_boilerplate_blocks(pairs)
check("a title-cased Gutenberg marker still trims",
      [t for _, t in kept] == ["The real text of the play begins here."],
      str([t for _, t in kept]))

print("\n" + "=" * 60)
passed = sum(1 for r in results if r)
print(f"{len(results)} checks: {passed} passed, {len(results) - passed} FAILED")
sys.exit(0 if passed == len(results) else 1)
