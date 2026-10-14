"""Structural signals for deciding what belongs in an audiobook.

The semantic classifier in ai_brain.py asks "does this read like prose?" That is
the wrong question for boilerplate that *is* prose. The Project Gutenberg
licence is well-formed English with full sentences and natural flow, so it
passes that test easily -- 46% of the first real audiobook was licence text.

These functions use signals that do not depend on judgement: the literal markers
Gutenberg puts in every one of its files, and the structural metadata publishers
already declare in the OPF and the navigation document. Where those speak, they
are exact; the classifier only has to handle what is left.
"""
import os
import re

# Gutenberg wraps the actual work in these, in every edition it produces. The
# wording varies slightly ("THE"/"THIS", trailing title text), hence the regex.
_GUTENBERG_START = re.compile(
    r'\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*\*\*', re.I | re.S)
# Some editions repeat the title as a line of their own just after the START
# marker: "The Project Gutenberg eBook, The Canterville Ghost, by Oscar Wilde".
# Only directly after the marker is it boilerplate.
_GUTENBERG_TITLE_LINE = re.compile(r"^\s*The Project Gutenberg e-?Book\b", re.I)

_GUTENBERG_END = re.compile(
    r'\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*\*\*', re.I | re.S)

# Section types that should never be narrated. Deliberately conservative: this
# only ever adds JUNK verdicts, and a wrong one silently removes real writing
# from the book. Anything arguable (appendix, preface, introduction, epilogue)
# is left to the classifier.
JUNK_SECTION_TYPES = {
    "cover", "toc", "copyright-page", "copyright", "title-page", "titlepage",
    "index", "glossary", "bibliography", "colophon", "imprint",
    "acknowledgements", "acknowledgments", "loi", "lot",
}


def trim_boilerplate(text):
    """Cut publisher boilerplate out of a document's text.

    Returns (text, removed_word_count, reason).

    This has to work *inside* a document, not just select whole ones. In
    pg1080.epub the essay and the full licence share a single spine file, so no
    document-level verdict could separate them: the file is genuinely half
    narrative and half licence.
    """
    original_words = len(text.split())
    reason = None

    match = _GUTENBERG_START.search(text)
    if match:
        text = text[match.end():]
        reason = "gutenberg-header"

    match = _GUTENBERG_END.search(text)
    if match:
        text = text[:match.start()]
        reason = "gutenberg-licence" if reason is None else "gutenberg-header+licence"

    removed = original_words - len(text.split())
    return text.strip(), removed, reason


def _normalise_href(base_dir, href):
    href = (href or "").split("#")[0]
    if not href:
        return None
    return os.path.join(base_dir, href).replace("\\", "/") if base_dir else href


def structural_junk(zip_file, opf_tree, opf_path):
    """Hrefs the book itself declares as non-narrative.

    Reads four sources, any of which may be absent:
      - the EPUB 2 OPF <guide>, whose reference/@type names the cover, the
        table of contents, the copyright page and so on;
      - the EPUB 3 navigation document's landmarks, same idea via epub:type;
      - epub:type attributes on sections inside the documents themselves;
      - Calibre's generated metadata jacket.

    Publishers declare this. Guessing at it with an embedding model when the
    answer is written down in the file is the mistake this replaces.
    """
    opf_dir = os.path.dirname(opf_path)
    junk = {}

    # --- EPUB 2 <guide> ---
    for ref in opf_tree.xpath('//*[local-name()="guide"]/*[local-name()="reference"]'):
        ref_type = (ref.get("type") or "").lower().strip()
        target = _normalise_href(opf_dir, ref.get("href"))
        if target and ref_type in JUNK_SECTION_TYPES:
            junk[target] = f"guide:{ref_type}"

    # --- EPUB 3 nav landmarks ---
    nav_href = next(
        (item.get("href") for item in opf_tree.xpath('//*[local-name()="item"]')
         if "nav" in (item.get("properties") or "").split()),
        None,
    )
    nav_path = _normalise_href(opf_dir, nav_href)
    if nav_path:
        try:
            from lxml import html as lxml_html
            nav_tree = lxml_html.fromstring(zip_file.read(nav_path), parser=lxml_html.HTMLParser(encoding="utf-8"))
            nav_dir = os.path.dirname(nav_path)
            for link in nav_tree.xpath('//*[local-name()="nav"]//a'):
                epub_type = ""
                for key, value in link.attrib.items():
                    if key.endswith("type"):
                        epub_type = (value or "").lower().strip()
                target = _normalise_href(nav_dir, link.get("href"))
                # Landmarks give qualified names like "frontmatter copyright-page".
                if target and any(t in JUNK_SECTION_TYPES for t in epub_type.split()):
                    junk[target] = f"landmark:{epub_type}"
        except Exception:
            # A malformed navigation document must not stop the book being made.
            pass

    # --- Calibre's metadata jacket ---
    # Generated by Calibre, not written by the author: title, publisher, the
    # library's star rating, its tags and the store blurb. Screwtape's was
    # judged KEEP at 385 words and narrated; it declares itself twice.
    for item in opf_tree.xpath('//*[local-name()="item"]'):
        target = _normalise_href(opf_dir, item.get("href"))
        if not target or target in junk:
            continue
        if item.get("id") == "calibre_jacket":
            junk[target] = "calibre:jacket"
        elif target.endswith("jacket.xhtml"):
            try:
                head = zip_file.read(target)[:4096]
            except KeyError:
                continue
            if re.search(rb'name="calibre-content"\s+content="jacket"', head):
                junk[target] = "calibre:jacket"

    return junk


def document_epub_type(tree):
    """The epub:type declared on a document's own root or body section, if any."""
    for node in tree.xpath('//*[local-name()="section" or local-name()="body" or local-name()="div"]')[:5]:
        for key, value in node.attrib.items():
            if key.endswith("type"):
                declared = (value or "").lower().strip()
                if any(t in JUNK_SECTION_TYPES for t in declared.split()):
                    return declared
    return None


def trim_boilerplate_blocks(block_texts):
    """Drop the blocks that fall outside the Gutenberg markers.

    The text-level trim above is not enough on its own: the engine narrates and
    builds its sync map from the document's leaf *elements*, so boilerplate has
    to be removed from that list or it comes back at chunking time with the
    trimmed text having changed nothing.

    `block_texts` is a list of (element, text) pairs in document order.
    Returns (kept_pairs, removed_word_count, reason).
    """
    start_at = 0
    end_at = len(block_texts)
    reason = None

    for i, (_, text) in enumerate(block_texts):
        if _GUTENBERG_START.search(text):
            # The marker's own block is boilerplate too.
            start_at = i + 1
            reason = "gutenberg-header"
            break
    if reason:
        while start_at < len(block_texts) and (
                not block_texts[start_at][1].strip()
                or _GUTENBERG_TITLE_LINE.match(block_texts[start_at][1])):
            start_at += 1

    for i in range(start_at, len(block_texts)):
        if _GUTENBERG_END.search(block_texts[i][1]):
            end_at = i
            reason = "gutenberg-licence" if reason is None else "gutenberg-header+licence"
            break

    kept = block_texts[start_at:end_at]
    removed = sum(len(t.split()) for _, t in block_texts) - sum(len(t.split()) for _, t in kept)
    return kept, removed, reason


# --- print-production artifacts -------------------------------------------
#
# An EPUB converted from a print PDF carries the typesetter's furniture into
# the text, and the engine narrates leaf blocks, so it gets read aloud. Measured
# on one edition of Redeeming Love, which Calibre produced from a PDF: **49.0%
# of its 12,220 blocks are not prose.** A different edition of the same book is
# completely clean, so this is a property of the FILE, not of the title. Every page boundary contributes
#
#     <p>11</p>                                        the page number
#     <p>Redeem.000 7/25/07 4:38 PM Page 12</p>        the production footer
#     <p>R</p><p>E</p><p>D</p><p>E</p>...              the running head, one
#                                                      letter per block
#
# 5,271 single-character blocks, 478 footers, 253 page numbers -- 22,356
# characters, about 25 minutes of a sixteen-hour book. Worse than the raw
# quantity is where they land: the footer is emitted mid-sentence, so a reader
# hears "Can't I eleven Redeem dot zero zero zero seven twenty-five zero seven
# four thirty-eight P M Page twelve stay? I'll be very quiet."
#
# This is the same lesson as the Gutenberg licence: boilerplate lives INSIDE
# documents, no document-level verdict separates it, and a classifier asked
# "does this read like prose?" is being asked the wrong question -- "12" and
# "R" read like nothing at all.
#
# Each rule below is deliberately narrower than it could be, because this
# module only ever adds JUNK verdicts and a wrong one silently deletes writing.
_FOOTER_DATE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")
_FOOTER_PAGE = re.compile(r"\bPage\s+\d+\b", re.I)
_BARE_NUMBER = re.compile(r"^\d{1,4}$")

# How many bare-number blocks a document needs before they are read as page
# numbers rather than as content. A novel has a dozen numeric chapter headings;
# a PDF conversion has hundreds. The threshold is what keeps "<h2>12</h2>" in a
# normal book and drops "<p>12</p>" in this one.
_PAGE_NUMBER_MIN = 20

# A single character alone in a block is never narration -- but a lone "I" or
# "V" could be a chapter heading, so only RUNS are dropped. Three consecutive
# is the signature of a letter-spaced running head and cannot be a heading.
_LETTER_RUN_MIN = 3


def drop_print_artifacts(block_texts):
    """Remove page numbers, production footers and letter-spaced running heads.

    Takes and returns the same (element, text) pairs as trim_boilerplate_blocks.
    Returns (kept, removed_counts) where removed_counts is a dict by reason, so
    a caller can report what it cut rather than only how much.
    """
    texts = [t.strip() for _, t in block_texts]
    n = len(block_texts)
    drop = [False] * n
    counts = {"footer": 0, "page-number": 0, "letter-run": 0}

    for i, t in enumerate(texts):
        # A date and a page marker in one short block is a production footer.
        # Both together, because a novel may legitimately write either.
        if len(t) <= 120 and _FOOTER_DATE.search(t) and _FOOTER_PAGE.search(t):
            drop[i] = True
            counts["footer"] += 1

    bare = [i for i, t in enumerate(texts) if _BARE_NUMBER.match(t)]
    if len(bare) >= _PAGE_NUMBER_MIN:
        for i in bare:
            if not drop[i]:
                drop[i] = True
                counts["page-number"] += 1

    i = 0
    while i < n:
        if len(texts[i]) == 1 and texts[i].isalnum():
            j = i
            while j < n and len(texts[j]) == 1 and texts[j].isalnum():
                j += 1
            if j - i >= _LETTER_RUN_MIN:
                for k in range(i, j):
                    if not drop[k]:
                        drop[k] = True
                        counts["letter-run"] += 1
            i = j
        else:
            i += 1

    kept = [pair for pair, d in zip(block_texts, drop) if not d]
    return kept, counts


# Phrases that essentially never occur in the body of a book but are unmissable
# in copyright pages, licences and publisher back matter. Used as an alarm on
# text that survived filtering, not as a filter itself -- a novel is allowed to
# mention a publisher, and silently deleting a chapter over one phrase would be
# far worse than narrating a stray paragraph.
BOILERPLATE_PHRASES = (
    "project gutenberg",
    "all rights reserved",
    "no part of this publication may be reproduced",
    "no part of this book may be reproduced",
    "isbn",
    "library of congress cataloging",
    "printed in the united states of america",
    "this ebook is for the use of anyone anywhere",
)


def boilerplate_phrases_in(text):
    """Which boilerplate markers survived into text we are about to narrate."""
    lowered = text.lower()
    return [phrase for phrase in BOILERPLATE_PHRASES if phrase in lowered]


# --- Chapter numbers the model cannot read ---------------------------------
#
# A heading that is a bare Roman numeral is narrated letter by letter, because
# the model has no way to tell "XXV" from an initialism. Measured on The
# Screwtape Letters (2026-09-16) using the book's own word timings: Roman
# numerals took 0.150s per letter against 0.048s for ordinary prose, and "XXV"
# (3 letters) took 0.78s while "WORMWOOD" (8 letters) took 0.35s.
#
# Capitalisation was the other suspect and the same measurement clears it:
# WORMWOOD in caps speaks at exactly the rate of lowercase "patient", and
# SCREWTAPE is *faster* than Screwtape. It is the numeral, not the case -- and
# "xxv" would fail identically.
#
# Rewritten rather than dropped. The numeral is a real chapter cue and a reader
# who hears "Chapter Twenty-Five" has been told something true; deleting it
# would quietly lose the only spoken boundary between letters. This is not
# model-specific sanitising -- a bare numeral standing alone as a block is
# structure, and every TTS has to be told how to say it.
_ROMAN_VALUES = (("M",1000),("CM",900),("D",500),("CD",400),("C",100),("XC",90),
                 ("L",50),("XL",40),("X",10),("IX",9),("V",5),("IV",4),("I",1))
_ROMAN_RE = re.compile(r"^(?=[MDCLXVI])M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$")

_ONES = ("", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight",
         "Nine", "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen",
         "Sixteen", "Seventeen", "Eighteen", "Nineteen")
_TENS = ("", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy",
         "Eighty", "Ninety")


def roman_to_int(token):
    """The integer a Roman numeral spells, or None if it is not one.

    Strict: the regex rejects "IIII" and "VX" so an ordinary word in caps can
    never be mistaken for a numeral. Note "I", "V", "X", "L", "C", "D" and "M"
    are all valid numerals AND English words or initials -- the caller decides
    whether the context is a standalone heading, which is what makes this safe.
    """
    token = token.strip().upper()
    if not token or not _ROMAN_RE.match(token):
        return None
    total, i = 0, 0
    for sym, val in _ROMAN_VALUES:
        while token[i:i + len(sym)] == sym:
            total += val
            i += len(sym)
    return total if i == len(token) else None


def spell_number(n):
    """1 -> 'One', 25 -> 'Twenty-Five'. Chapter numbers, so 1..999 is plenty."""
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")
    hundreds, rest = divmod(n, 100)
    out = f"{_ONES[hundreds]} Hundred"
    return f"{out} {spell_number(rest)}" if rest else out


def say_chapter_numbers(block_texts, label=None, language="en"):
    """Rewrite blocks that are nothing but a chapter number.

    Said as the bare number, "Nine.", unless a label is given: the book printed
    only the numeral, and a label would be a guess -- Screwtape's are letters,
    not chapters.

    Applied at block level and only when the block is *entirely* the numeral, so
    a sentence mentioning "Henry V" is untouched -- it is a block of prose, not
    a standalone heading. Returns a new list; blocks that are not numerals are
    passed through unchanged.
    """
    # Spelled out: the aligner's dictionary has words, not digits.
    speller = spell_number
    out = []
    for text in block_texts:
        stripped = (text or "").strip().rstrip(".").strip()
        value = roman_to_int(stripped) if stripped else None
        if value is None and stripped.isdigit():
            value = int(stripped)
        if value and 0 < value < 1000:
            out.append(f"{label} {speller(value)}." if label else f"{speller(value)}.")
        else:
            out.append(text)
    return out


# Styled capitals, which a TTS reads as an initialism unless they are undone.
# Measured by ear on Redeeming Love: the epigraph "CALIFORNIA, 1850." was
# spelled out letter by letter. The book's headings are set in caps by the
# typesetter -- it is a font choice, not something the author wrote -- so the
# capitals carry no meaning the listener can use, and every one of them is a
# word the model would otherwise refuse to say.
#
# Deliberately conservative: this only ever LOWERS case, never raises it, and
# it keeps anything that might genuinely be spelled. What it cannot decide --
# an isolated shout inside prose, an unfamiliar acronym -- it leaves alone, so
# the residue is a judgement call rather than a silent mistake.
_KEEP_UPPER = {
    "OK", "TV", "DNA", "USA", "US", "UK", "UN", "EU", "FBI", "CIA", "NASA",
    "NATO", "BBC", "CNN", "AM", "PM", "BC", "AD", "CE", "BCE", "CEO", "CFO",
    "USD", "EUR", "GBP", "SOS", "ID", "PC", "TNT", "VIP", "ATM", "GPS", "URL",
    "HTML", "PDF", "DVD", "CD", "FM", "UFO", "IQ", "RIP", "PS",
}

_VOWEL = re.compile(r'[AEIOUY]')

# Common words a book sets in capitals for emphasis ("who'd find ME funny",
# "I HAVE no intention"). Lowered to lowercase mid-sentence; anything else a
# run lowers becomes Title Case, which reads the same and looks like a name.
_COMMON = {
    "A", "AN", "THE", "AND", "OR", "BUT", "OF", "TO", "IN", "ON", "AT", "BY", "FOR",
    "FROM", "WITH", "AS", "IS", "WAS", "ARE", "WERE", "BE", "AM", "DO", "DID", "HAVE",
    "HAS", "HAD", "WILL", "CAN", "MUST", "NOT", "NO", "YES", "SO", "IF", "IT", "ME",
    "MY", "WE", "OUR", "YOU", "YOUR", "HE", "HIM", "HIS", "SHE", "HER", "THEY", "THEM",
    "THIS", "THAT", "WHAT", "WHO", "WHY", "HOW", "ALL", "NOW", "NEVER",
}


# Short words a book sets in capitals that no letter-count rule can tell from
# an acronym: the Bible's small-caps LORD and GOD, and scripture books cited in
# epigraphs ("1 KINGS 19:12", "JOB 1:21", "ACTS 2:38").
_TITLE_WORDS = {"GOD", "LORD", "JOB", "ACTS"}


def _pronounceable(core):
    """Could this capitalised word only be read as a word, never as letters?

    Redeeming Love's read-through found "LORD", "PSALM" and "KINGS" kept in
    capitals by the first version, which asked for two vowels -- a rule that
    protects acronyms, but also protects every one-vowel word in the language.
    Five letters with a vowel is a word; four letters with a vowel and no
    run of three consonants is a word (LORD, JOHN, MARK) while USSR is not.
    """
    if core in _TITLE_WORDS:
        return True
    if not _VOWEL.search(core):
        return False
    if len(core) >= 5:
        return True
    return len(core) == 4 and not re.search(r"[^AEIOUY]{3}", core)


def _must_stay_upper(token):
    """True when a token is plausibly meant to be spelled out."""
    core = re.sub(r"[^A-Za-z]", "", token)
    if len(core) <= 1:
        return True                      # an initial: "F. Scott"
    if re.fullmatch(r"\(?(?:[A-Za-z]\.){2,}[,;:)]?", token):
        return True                      # dotted initials: "T.E.," "B.S.," "U.S.A."
    if core.upper() in _KEEP_UPPER:
        return True
    if not _VOWEL.search(core.upper()):
        return True                      # BBC, TNT, PG -- unsayable as a word
    if _ROMAN_RE.match(core.upper()):
        return True                      # XIV; a real numeral, never "Xiv"
    return False


def _lower_token(token):
    """CALIFORNIA, -> California,   keeping punctuation and position."""
    out, first = [], True
    for ch in token:
        if ch.isalpha():
            out.append(ch if first else ch.lower())
            first = False
        else:
            out.append(ch)
    return "".join(out)


def normalize_caps(text):
    """Undo typographic capitals so the model says the word instead of spelling it.

    Works on RUNS of capitalised words, not only on whole blocks, because
    reading two books line by line found capitals inside ordinary prose far
    more often than whole capitalised blocks: small-caps openings ("I HAVE no
    intention"), emphasis ("who'd find ME funny"), signatures ("(Signed)
    TOADPIPE"), and epigraph attributions ("...a gentleman. SHAKESPEARE.",
    "WILLIAM WALSH.", "ROMANS 7:19.", "BOOK OF COMMON PRAYER.").

    A run of two or more capitalised words, or a whole capitalised block, is
    typography and is lowered word by word. A lone capitalised word is lowered
    only when it cannot be an acronym: a common word, or four letters or more
    with at least two vowels. Anything that might be spelled out -- initials,
    no-vowel acronyms (IRS, PG-13), known acronyms, real roman numerals -- keeps
    its capitals. This only ever lowers case, never raises it.
    """
    if not text:
        return text
    tokens = re.split(r'(\s+)', text)
    words = [i for i, t in enumerate(tokens) if t.strip()]
    if not words:
        return text
    letters = re.sub(r"[^A-Za-z]", "", text)
    whole = bool(letters) and letters.isupper()

    def is_caps(tok):
        core = re.sub(r"[^A-Za-z]", "", tok)
        return len(core) >= 2 and core.isupper() and not _ROMAN_RE.match(core)

    runs, cur = [], []
    for i in words:
        if is_caps(tokens[i]):
            cur.append(i)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)

    for run in runs:
        typographic = whole or len(run) >= 2
        for i in run:
            tok = tokens[i]
            core = re.sub(r"[^A-Za-z]", "", tok)
            if core in ("AM", "PM"):
                prev = next((tokens[j] for j in range(i - 1, -1, -1) if tokens[j].strip()), "")
                if prev[-1:].isdigit():
                    continue                          # "9 AM" is a time
                tokens[i] = tok.lower() if i != words[0] else _lower_token(tok)
                continue
            if _must_stay_upper(tok):
                continue
            if not typographic and not (core in _COMMON or "'" in tok or _pronounceable(core)):
                continue
            tokens[i] = tok.lower() if (core in _COMMON and i != words[0]) else _lower_token(tok)
    return "".join(tokens)

# The block tags the engine narrates from. A leaf block is one of these with no
# other block tag inside it -- the smallest unit the sync map can name.
BLOCK_TAGS = ('p', 'div', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote')


def leaf_blocks(tree):
    """The narratable leaf blocks of a parsed spine document, in reading order.

    Shared by the engine and by the tools that report on what it would do, so
    a report about the engine can never be about a slightly different engine.
    Mutates `tree`: script/style/sup/title are stripped, as the engine always has.
    """
    from lxml import etree
    etree.strip_elements(tree, "script", "style", "sup", "title", with_tail=False)
    return [node for node in tree.xpath('//*')
            if node.tag in BLOCK_TAGS
            and not any(child.tag in BLOCK_TAGS for child in node.iterdescendants())]


# Elements whose edges end a run of loose text: two table cells, or the text
# before and after a <section>, are separate passages, never one sentence.
_RUN_BOUNDARIES = set(BLOCK_TAGS) | {
    "body", "section", "article", "header", "footer", "aside", "nav", "main",
    "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption",
    "ul", "ol", "dl", "dt", "dd", "figure", "figcaption", "pre", "hr"}


class StrayRun:
    """Text that sits in no leaf block, narrated as a paragraph of its own.

    Usher lost 70 words this way: its French epigraph (in a <td> beside the
    title headings) and a passage of the Mad Trist (bare text after a <div>,
    inside a <pre>). leaf_blocks cannot name them, so nothing was ever read.

    `tag` is what the narration rules see ("p": a run is prose, never a
    heading). `anchor` is the real element the sync map names, because the
    reader can only resolve element paths in the KEPUB:

      - the outermost element holding this run and nothing else (the
        epigraph's <table>): exact, and it contains no other mapped text;
      - else the leaf block just before the run, whose node it then shares.
        The gateway resolves a shared path to the earliest node, so a page
        there starts the block's audio, and listening in the run lands the
        reader a few lines early, never late;
      - else the first leaf block after it.

    Never a container that holds blocks: the gateway walks a missing path up
    to the nearest mapped ancestor, so a mapped <pre> wrapping a whole story
    would catch every unmatched page in it.
    """
    tag = "p"

    def __init__(self, text, first_owner):
        self.text = text
        self.first_owner = first_owner
        self.anchor = None


def _norm(text):
    # Without any whitespace: itertext() has no space where a <br> stood.
    return re.sub(r"\s+", "", text)


def narratable_blocks(tree):
    """Everything the narrator reads in one spine document: [(block, text)] in reading order.

    Leaf blocks with block_text(), plus a StrayRun for every run of text no
    leaf block contains. The single list the engine and narration_review.py
    both narrate from. Mutates `tree` as leaf_blocks does.
    """
    from core.parser import block_text
    leaves = leaf_blocks(tree)
    leaf_set = set(leaves)
    body = tree.find(".//body")
    if body is None:
        return [(b, t) for b, t in ((b, block_text(b)) for b in leaves) if t]

    units, pieces = [], []          # pieces: [(text, owner element or None)]

    def flush():
        text = re.sub(r"[ \t]+", " ", "".join(p for p, _ in pieces)).strip()
        if text:
            owner = next((o for p, o in pieces if p.strip()), None)
            units.append(StrayRun(text, owner))
        pieces.clear()

    def visit(el):
        if el in leaf_set:
            flush()
            units.append(el)
            return
        if not isinstance(el.tag, str):
            return
        boundary = el.tag in _RUN_BOUNDARIES
        if boundary:
            flush()
        if el.tag == "br":
            pieces.append((" ", None))
        if el.text:
            pieces.append((el.text, el))      # the run starts at el's start
        for child in el:
            visit(child)
            if child.tail:
                pieces.append((child.tail, None))   # starts mid-container: no element
        if boundary:
            flush()

    visit(body)
    flush()

    def holds_only(el, text):
        return (el is not body and el not in leaf_set
                and not any(d in leaf_set or (isinstance(d.tag, str) and d.tag in BLOCK_TAGS)
                            for d in el.iterdescendants())
                and _norm("".join(el.itertext())) == _norm(text))

    out, last_leaf, waiting = [], None, []
    for unit in units:
        if not isinstance(unit, StrayRun):
            last_leaf = unit
            for run in waiting:
                run.anchor = unit
            waiting = []
            text = block_text(unit)
            if text:
                out.append((unit, text))
            continue
        # In a KEPUB a run of several sentences is several koboSpans, so the
        # element holding exactly the run can be an ancestor of the first one.
        el = unit.first_owner if unit.first_owner is not body else None
        while el is not None and not holds_only(el, unit.text):
            el = el.getparent()
            if el is None or el is body or el in leaf_set or any(
                    isinstance(d.tag, str) and d.tag in BLOCK_TAGS for d in el.iterdescendants()):
                el = None
        if el is not None:
            while el.getparent() is not None and holds_only(el.getparent(), unit.text):
                el = el.getparent()
            unit.anchor = el
        elif last_leaf is not None:
            unit.anchor = last_leaf
        else:
            waiting.append(unit)
        out.append((unit, unit.text))
    # No leaf block in the whole document: the <body> itself, which the
    # gateway's walk-up never reaches, so it can only match exactly.
    for run in waiting:
        run.anchor = body
    return out


def text_outside_blocks(tree):
    """Runs of text in a document that no leaf block contains.

    The narration is built from leaf_blocks only, so text sitting directly in
    a <section>, in a <td>, or beside a child block is never read -- and a
    review of what the narrator says cannot show what was never extracted.
    Usher's French epigraph was lost this way. Returns the stray strings.
    """
    blocks = set(leaf_blocks(tree))
    body = tree.find(".//body")
    if body is None:
        return []
    stray = []
    for el in body.iter():
        if not isinstance(el.tag, str) or el in blocks or any(a in blocks for a in el.iterancestors()):
            continue
        for piece in (el.text, *(c.tail for c in el)):
            if piece and piece.strip():
                stray.append(" ".join(piece.split()))
    return stray


# --- Book-level repeats and trailing credits ---------------------------------
#
# Both found by reading Screwtape's whole narration. Every letter's file opens
# with the book's title as a heading, so "The Screwtape Letters" was read 32
# times -- three of them in a row on the title page. And the scan the EPUB was
# made from signs off with its own credits: "About this e-text: / Version 1.0 /
# Created 6/6/2000 / If you make corrections, please increment the version
# number by 0.1 and repost."

_LEADING_ARTICLE = re.compile(r"^(the|a|an|le|la|les|l|el|los|las|de|het|een|der|die|das)\s+", re.I)


def _title_key(text):
    t = re.sub(r"[^\w\s]", " ", text or "").lower()
    t = re.sub(r"\s+", " ", t).strip()
    return _LEADING_ARTICLE.sub("", t)


def drop_repeated_title(block_texts, book_title, state):
    """Say the book's title once; drop every later block that is only the title.

    `state` is a dict the caller keeps for the whole book, since the repeats
    span documents. This does NOT mark the title as said: it returns whether
    this document would say it, and the caller commits that only once the
    document is actually narrated. Marking it here was a bug found on the
    second read-through -- Screwtape's title.xhtml, three words and skipped for
    being under 20, "said" the title, so the real title page lost it and the
    book opened on "C. S. Lewis." Only blocks that are *entirely* the title go.

    Returns (kept, removed, says_title).
    """
    key = _title_key(book_title)
    if not key:
        return block_texts, [], False
    kept, removed, says = [], [], False
    for el, text in block_texts:
        if _title_key(text) == key:
            if state.get("title_said") or says:
                removed.append((el, text))
                continue
            says = True
        kept.append((el, text))
    return kept, removed, says


def opening_credit(text, book_title, author):
    """True for the blocks with which a book announces itself: its title, "by", its author.

    These are exempt from the block filter. JEV is told a title page is
    furniture, and on Screwtape it scored "by" 0.85 -- but "The Screwtape
    Letters, by C. S. Lewis" is how an audiobook should begin.
    """
    k = _title_key(text)
    return bool(k) and (k == _title_key(book_title) or k == "by" or
                        (bool(author) and k == _title_key(author)))


_DEDICATION = re.compile(r"^\s*(?:to|for|in (?:loving )?memory of|dedicated to)\s+\S", re.I)
_DEDICATION_MAX_WORDS = 40
_DEDICATION_MAX_BLOCKS = 5


def is_dedication(texts):
    """True when a whole document is a short dedication: "To those who hurt and hunger."

    The author's words, and an audiobook reads them. Redeeming Love's dedication
    is its own seven-word document, so the 20-word floor that exists to skip
    covers and bare title pages dropped it before any verdict was asked --
    while Screwtape's, sharing the title page, was narrated. Only a whole
    document this short can qualify, so a chapter that opens "To the reader"
    is never mistaken for one.
    """
    texts = [t for t in texts if t and t.strip()]
    if not texts or len(texts) > _DEDICATION_MAX_BLOCKS:
        return False
    if sum(len(t.split()) for t in texts) > _DEDICATION_MAX_WORDS:
        return False
    return bool(_DEDICATION.match(texts[0]))


_CREDITS = re.compile(
    r"^\s*(?:about this e-?text|this e-?(?:text|book) (?:was|is|has)|scanned(?: and proof-?read)? by|"
    r"proof-?read by|e-?(?:text|book) (?:version|prepared|created)|if you make corrections)",
    re.I)
_CREDITS_MAX_TAIL = 10


def trim_credits(block_texts):
    """Drop a scan's sign-off: a credits line and everything after it in the document.

    Only when the credits start within the last _CREDITS_MAX_TAIL blocks, so a
    matching line early in a document can never take the prose after it.
    Returns (kept, removed).
    """
    for i, (_, text) in enumerate(block_texts):
        if _CREDITS.search(text) and len(block_texts) - i <= _CREDITS_MAX_TAIL:
            return block_texts[:i], block_texts[i:]
    return block_texts, []


# --- chapters inside one file ------------------------------------------------
#
# A chapter used to be a spine document, named after the first table-of-contents
# entry pointing into it. Project Gutenberg files are often a whole book, or
# several stories, in one document: The Canterville Ghost's seven chapters came
# out as one 62-minute chapter, and an illustrated Maupassant collection (12
# stories in 2 files) would have been 2 chapters named after the first story in
# each, so a listener hears "Bombard" under a chapter called "Le Rendez-vous".
# The table of contents already says where each chapter starts: its #fragment.

def chapter_starts(tree, blocks, anchors):
    """{index into `blocks`: label} where a TOC entry starts a chapter in this document.

    `blocks` are the (block, text) pairs about to be chunked, in reading order;
    `anchors` the (fragment, label, depth) entries that point inside this
    document; depth is the entry's nesting in the table of contents. The
    values are (label, depth).
    An anchor inside a kept block starts that block (an <a id> in the heading).
    Otherwise it falls to the first kept block after it: an empty marker before
    the heading, or a heading that was removed (said once already as the book's
    title). A direct hit beats a fall-through, and of several fall-throughs to
    one block the last entry wins: in a Maupassant collection the title page's
    "PARIS 1907" and the story "BOULE DE SUIF" both fell to the story's first
    paragraph, and the first-wins rule called the story "PARIS 1907". An anchor
    that names nothing, or falls past the last kept block, starts nothing.
    """
    order = {el: i for i, el in enumerate(tree.iter())}
    index_of = {id(b): i for i, (b, _) in enumerate(blocks)}

    def position(block):
        el = block if hasattr(block, "iterancestors") else (
            getattr(block, "anchor", None) or getattr(block, "first_owner", None))
        return order.get(el)

    positions = [position(b) for b, _ in blocks]
    direct, fallen, first = {}, {}, None
    for fragment, label, depth in anchors:
        hits = tree.xpath("//*[@id=$f or @name=$f]", f=fragment)
        if not hits:
            continue
        el = hits[0]
        idx = next((index_of[id(a)] for a in [el, *el.iterancestors()] if id(a) in index_of), None)
        if idx is None:
            here = order.get(el, -1)
            idx = next((i for i, p in enumerate(positions) if p is not None and p > here), None)
            if idx is not None:
                fallen[idx] = (label, depth)
        else:
            direct.setdefault(idx, (label, depth))
        if first is None and idx is not None:
            first = (idx, (label, depth))
    starts = {**fallen, **direct}
    # The file's own name, as before chapters were split: its first entry wins
    # the first block. Usher's title heading is removed (the book's title is
    # said once) and its anchor falls onto the byline, which has an entry of
    # its own: the story was called "EDGAR ALLAN POE".
    if first is not None and first[0] == 0:
        starts[0] = first[1]
    return starts


def read_toc(z):
    """({file: first label}, {file: [(fragment, label, depth)]}) from the EPUB's NCX, or its EPUB 3 nav.

    The first: a file's name, its first entry (several entries commonly point
    into one file, and last-wins named every chunk of a Gutenberg edition "THE
    FULL PROJECT GUTENBERG LICENSE"). The second: every entry with a #fragment,
    at every level (Gutenberg nests its TOC by heading level, so a collection's
    stories sit two levels below its title page), minus boilerplate. Shared by
    the engine and tools/narration_review.py.
    """
    from lxml import etree
    from core.parser import get_zip_path
    lookup, anchors = {}, {}
    toc_path = next((n for n in z.namelist() if n.endswith(".ncx")), None)
    if not toc_path:
        # An EPUB 3 book may carry only its navigation document. Twisted did
        # (2026-10-07): with no NCX read, every chapter was named "Part N" and
        # its boundaries fell back to file breaks.
        return _read_nav(z)
    ns = {"ncx": "http://www.daisy.org/z3986/2005/ncx/"}
    tree = etree.fromstring(z.read(toc_path))
    for nav in tree.xpath("//ncx:navPoint", namespaces=ns):
        label = (nav.xpath("./ncx:navLabel/ncx:text/text()", namespaces=ns) or [""])[0].strip()
        src, _, fragment = (nav.xpath("./ncx:content/@src", namespaces=ns) or [""])[0].partition("#")
        full_path = get_zip_path(toc_path, src)
        lookup.setdefault(full_path, label)
        if fragment and not boilerplate_phrases_in(label):
            depth = len(nav.xpath("ancestor::ncx:navPoint", namespaces=ns))
            anchors.setdefault(full_path, []).append((fragment, label, depth))
    return lookup, anchors


def _read_nav(z):
    """read_toc's two maps from an EPUB 3 navigation document (`<nav epub:type="toc">`).

    Found through the OPF manifest's `properties="nav"`; each `<a href>` is an
    entry, its depth the number of `<ol>` lists around it.
    """
    from lxml import etree
    from core.parser import get_zip_path, parse_xhtml
    lookup, anchors = {}, {}
    try:
        container = etree.fromstring(z.read("META-INF/container.xml"))
        opf_path = container.xpath("//*[local-name()='rootfile']/@full-path")[0]
        opf = etree.fromstring(z.read(opf_path))
        href = opf.xpath("//*[local-name()='item'][contains(concat(' ', @properties, ' '), ' nav ')]/@href")
        if not href:
            return lookup, anchors
        nav_path = get_zip_path(opf_path, href[0])
        doc = parse_xhtml(z.read(nav_path))
    except (KeyError, IndexError, etree.XMLSyntaxError):
        return lookup, anchors
    navs = doc.xpath("//nav")
    # The HTML parser keeps "epub:type" as an attribute name with its prefix.
    kind = lambda n: (n.get("epub:type") or n.get("type") or "").split()  # noqa: E731
    # The toc, else a list that does not say it is something else: the
    # landmarks ("Begin Reading") and page list are not chapters.
    toc = next((n for n in navs if "toc" in kind(n)),
               next((n for n in navs if not {"landmarks", "page-list"} & set(kind(n))), None))
    if toc is None:
        return lookup, anchors
    for a in toc.xpath(".//a[@href]"):
        label = " ".join("".join(a.itertext()).split())
        src, _, fragment = a.get("href").partition("#")
        if not src:
            continue
        full_path = get_zip_path(nav_path, src)
        lookup.setdefault(full_path, label)
        if fragment and not boilerplate_phrases_in(label):
            depth = len(a.xpath("ancestor::ol")) - 1
            anchors.setdefault(full_path, []).append((fragment, label, depth))
    return lookup, anchors


def drop_gutenberg_front_matter(tree, blocks, anchors):
    """In a file with Gutenberg's START marker: drop what comes before its first TOC entry.

    That is the source edition's own front matter, which the e-text kept: an
    illustrated Maupassant (#10746) opens on its publisher's catalogue
    ("Collection des Grands Romans. A 1 Franc. ... Envoi franco contre 1 fr.
    25 par volume."), 18 blocks read aloud before the title. Only after a
    START marker, so a commercial book's epigraph above its first entry
    stays. Returns (kept, dropped).
    """
    starts = chapter_starts(tree, blocks, anchors[:1])
    if not starts:
        return blocks, []
    first = min(starts)
    return blocks[first:], blocks[:first]


_TITLE_PAGE_MAX_BLOCKS = 12
_TITLE_PAGE_MAX_WORDS = 150


def drop_gutenberg_title_page(tree, blocks, anchors, book_title, author):
    """In a file with Gutenberg's START marker: say the title page's credit, not its imprint.

    Gutenberg makes every line of the source edition's title page an entry of
    its table of contents, so after drop_gutenberg_front_matter the book still
    opened on it: "Boule de Suif. Oeuvres complètes illustrées du Guy de
    Maupassant. Édition de luxe. (Voir catalogue à la fin du volume.) Guy de
    Maupassant. Paris 1907." (the user heard it, 2026-10-05), and The
    Canterville Ghost on its subtitle, "Illustrated by Wallace Goldsmith. 1906.
    John W. Luce and Company Boston and London." JEV scored only two of those
    lines as junk, too few for the block filter's run rule.

    The title page is everything before the first entry with MIN_CHAPTER_WORDS
    under it. Of it, the opening credit stays (the title, "by", the author or
    the surname the page prints, a dedication); the rest goes. Only when it
    looks like a title page: mostly headings, at most _TITLE_PAGE_MAX_BLOCKS
    blocks and _TITLE_PAGE_MAX_WORDS words, none of them a paragraph's worth,
    so a collection of short poems is never taken for one. Returns (kept,
    dropped).
    """
    starts = sorted(chapter_starts(tree, blocks, anchors))
    if not starts or starts[0] != 0:
        return blocks, []
    first_chapter = None
    for k, start in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(blocks)
        if sum(len(t.split()) for _, t in blocks[start:end]) >= MIN_CHAPTER_WORDS:
            first_chapter = start
            break
    if not first_chapter:
        return blocks, []
    page = blocks[:first_chapter]
    words = [len((t or "").split()) for _, t in page]
    headings = sum(1 for b, _ in page if _local(getattr(b, "tag", "")) in ("h1", "h2", "h3", "h4", "h5", "h6"))
    if (len(page) > _TITLE_PAGE_MAX_BLOCKS or sum(words) > _TITLE_PAGE_MAX_WORDS
            or max(words) >= MIN_CHAPTER_WORDS or headings * 2 < len(page)):
        return blocks, []
    surname = _title_key(author).split()[-1:] if author else []
    # A title printed over two lines: "ARSÈNE LUPIN", then "Gentleman-Cambrioleur".
    title_parts = {_title_key(part) for part in re.split(r"[,:;.()—–]", book_title or "")} - {""}

    def credit(text):
        return (opening_credit(text, book_title, author) or _title_key(text) in title_parts
                or _title_key(text).split() == surname or bool(_DEDICATION.match(text or "")))

    kept = [p for p in page if credit(p[1])]
    return kept + blocks[first_chapter:], [p for p in page if not credit(p[1])]


_CONTENTS_HEADING = re.compile(r"^\W*(?:table(?: des mati[eè]res)?|(?:table of )?contents|sommaire|index)\W*$", re.I)


def drop_printed_contents(blocks, labels):
    """Remove a printed table of contents: a block made of the book's own TOC labels.

    An illustrated Maupassant ends on "TABLE" and one block listing its twelve
    story titles, read aloud after the last story. A block counts when at
    least three labels make up all but three of its words; a heading that
    names a table of contents goes with it. Returns (kept, dropped).
    """
    def squash(text):
        return " " + " ".join(re.sub(r"[^\w']+", " ", text or "").casefold().split()) + " "
    names = sorted({squash(label) for label in labels if len(squash(label).split()) >= 1},
                   key=len, reverse=True)
    drop = set()
    for i, (b, t) in enumerate(blocks):
        rest, found = squash(t), 0
        for name in names:
            if name.strip() and name in rest:
                found += rest.count(name)
                rest = rest.replace(name, " ")
        if found >= 3 and len(rest.split()) <= 3:
            drop.add(i)
            j = i - 1
            if j >= 0 and _CONTENTS_HEADING.match(blocks[j][1] or ""):
                drop.add(j)
    return [p for i, p in enumerate(blocks) if i not in drop], [p for i, p in enumerate(blocks) if i in drop]


# A table-of-contents entry with less than this under it is a line of the title
# page (Gutenberg's generated TOCs list "WILDE", "1906", "PARIS 1907"), not a
# chapter: it joins the chapter before it rather than becoming a file of its own.
MIN_CHAPTER_WORDS = 40


def untitled_document_title(own_title, has_entry, book_has_toc, previous_title):
    """The chapter a whole document belongs to when the table of contents names
    nothing in it: the one before it.

    A table of contents entry runs until the next one, so a file it never
    names is part of the chapter it follows. Twisted's in-story magazine
    article between "Chapter Two" and "Chapter Three" was its own chapter,
    "Part 5", in the audiobook and in Audiobookshelf (user, 2026-10-11: fold
    it into the chapter before). Two cases keep `own_title` (the document's
    first short heading, else "Part N"): a book with no table of contents at
    all, which would otherwise be one chapter, and a document before the first
    chapter, which has nothing to join.
    """
    if has_entry or not book_has_toc or not previous_title:
        return own_title
    return previous_title


_BARE_NUMERAL = re.compile(r"^\s*(?:[IVXLCDM]+|\d+)\.?\s*$", re.I)


def chapter_segments(blocks, starts, title, min_words=MIN_CHAPTER_WORDS, continues=None):
    """[(title, [blocks])]: `blocks` cut where `starts` says a chapter begins.

    The part before the first start keeps the document's own `title`, unless
    `continues` names the chapter the file before this one ended in: a table
    of contents entry runs until the next one, so text above a file's first
    entry is the end of that chapter, whatever its length. Gutenberg splits a
    collection's files by size, and the last paragraph of "Un Parricide" opened
    the next file; it was folded into "Le Rendez-vous" and heard as its start
    (2026-10-05). Same-title runs are stitched into one file, so it joins its
    own chapter. A part under `min_words` is a heading, not a chapter:

      - followed by a nested entry that is only a numeral (a story's title,
        then its section "I"), it joins that section and names it;
      - otherwise (a line of the title page, a heading over the stories) it
        joins the chapter before it, or at the start of the file the next
        one, under that one's own name.

    Nothing is dropped: this only decides where files begin and what they are
    called.
    """
    if continues and starts and min(starts) > 0:
        first = min(starts)
        return [(continues, blocks[:first])] + chapter_segments(
            blocks[first:], {i - first: v for i, v in starts.items()}, title, min_words)
    segments = []
    for i, block in enumerate(blocks):
        if i in starts or not segments:
            label, depth = starts.get(i, (title, 0))
            segments.append([label, depth, []])
        segments[-1][2].append(block)

    def words(seg):
        return sum(len(t.split()) for _, t in seg[2])

    # carry: (blocks, the name it imposes or None, its first heading, that
    # heading's depth), waiting for the next part. A run of title-page lines
    # keeps its first line's name, unless it reaches a real chapter nested
    # under it (a collection's title page, then its first story). Usher's title
    # and "By" are a run beside the byline that follows, which is not a chapter.
    out, carry = [], None
    for n, seg in enumerate(segments):
        if carry:
            # A numbered chapter keeps its number: The Canterville Ghost's
            # title page, once only its credit was left, folded into "I" and
            # renamed it after the book, between nothing and "II".
            child = words(seg) >= min_words and (seg[1] > carry[3] or bool(_BARE_NUMERAL.match(seg[0])))
            seg = [carry[1] or (seg[0] if child else carry[2]), seg[1], carry[0] + seg[2]]
            carry = None
        if words(seg) >= min_words:
            out.append(seg)
            continue
        nxt = segments[n + 1] if n + 1 < len(segments) else None
        if nxt and nxt[1] > seg[1] and _BARE_NUMERAL.match(nxt[0]):
            carry = (seg[2], seg[0], seg[0], seg[1])
        elif out:
            out[-1][2].extend(seg[2])
        elif nxt:
            carry = (seg[2], None, seg[0], seg[1])
        else:
            out.append(seg)
    return [(label, blocks) for label, _, blocks in out]


# --- pictures -------------------------------------------------------------------
#
# An illustrated edition prints a caption under every picture and a list of the
# pictures up front. Both are for the eye: The Canterville Ghost's narration read
# its 17-entry List of Illustrations aloud, then each caption again in the middle
# of the story, right after the sentence it quotes.

_CAPTION_CLASS = re.compile(r"(?:^|[\s_-])(?:fig)?caption(?:$|[\s_-])", re.I)
_PICTURE_TAGS = ("img", "image", "svg")
_FIGURE_CLASS = re.compile(r"fig|caption|illus", re.I)
_LIST_HEADING = re.compile(r"illustrations?|plates|figures|pictures", re.I)


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _has_picture(el):
    return any(_local(d.tag) in _PICTURE_TAGS for d in el.iter())


def _is_caption(el):
    """A caption: in a <figcaption>, or classed caption beside a picture."""
    for depth, a in enumerate([el, *el.iterancestors()]):
        if depth > 2:
            break
        if _local(a.tag) == "figcaption":
            return True
        if _CAPTION_CLASS.search(a.get("class") or ""):
            container = a.getparent()
            for _ in range(2):
                if container is None:
                    break
                if _has_picture(container):
                    return True
                container = container.getparent()
    return False


def _squash(text):
    return re.sub(r"[\W_]+", "", text or "").casefold()


def _repeats_picture(el, text):
    """The block says what the picture beside it already says in its alt/title.

    The Worst Journey in the World prints each of its 156 captions as the
    paragraph after the picture's own (<p class="figcenter">Midnight—E. A.
    Wilson, del.</p>), and the image carries the same words as its alt text.
    """
    words = _squash(text)
    if not words or _local(el.tag) in ("h1", "h2", "h3", "h4", "h5", "h6"):
        return False      # a title beside its cover picture says the same words
    # Laid out as a figure, not merely next to one: a line of the story that a
    # caption quotes word for word stays ("Poor, poor Ghost," in Canterville).
    holder = el.getparent()
    if not (_FIGURE_CLASS.search(el.get("class") or "")
            or (holder is not None and _local(holder.tag) != "body"
                and _FIGURE_CLASS.search(holder.get("class") or ""))):
        return False
    near = [el.getprevious(), el.getparent()]
    for container in near:
        if container is None:
            continue
        for d in container.iter():
            if _local(d.tag) in _PICTURE_TAGS and words in (_squash(d.get("alt")), _squash(d.get("title"))):
                return True
    return False


def _only_internal_link(li):
    """The item is one link into the book and nothing else."""
    links = [a for a in li.iter() if _local(a.tag) == "a" and a.get("href")]
    if len(links) != 1:
        return False
    href = links[0].get("href")
    if re.match(r"[a-z][a-z0-9+.-]*:", href, re.I):
        return False      # http:, mailto: ... a list of sources, not a list of pictures
    text = re.sub(r"\s+", "", "".join(li.itertext()))
    return text == re.sub(r"\s+", "", "".join(links[0].itertext())) and bool(text)


def drop_picture_text(block_texts):
    """Remove picture captions and lists of illustrations. Returns (kept, counts).

    A list counts only when every one of at least three items is a single link
    into the book, and its heading names pictures; the heading goes with it.
    """
    drop, counts = set(), {"caption": 0, "illustration-list": 0}
    for i, (b, t) in enumerate(block_texts):
        # Loose text (a StrayRun, e.g. a bare <figcaption>) by the element it starts in.
        el = b if hasattr(b, "iterancestors") else getattr(b, "first_owner", None)
        if el is not None and (_is_caption(el) or _repeats_picture(el, t)):
            drop.add(i)
            counts["caption"] += 1

    lists = {}
    for i, (b, _) in enumerate(block_texts):
        if not hasattr(b, "iterancestors"):
            continue
        li = next((a for a in [b, *b.iterancestors()] if _local(a.tag) == "li"), None)
        if li is not None and _local(getattr(li.getparent(), "tag", "")) in ("ul", "ol"):
            lists.setdefault(li.getparent(), []).append((i, li))
    for parent, items in lists.items():
        lis = [c for c in parent if _local(c.tag) == "li"]
        if len(lis) < 3 or not all(_only_internal_link(li) for li in lis):
            continue
        first = min(i for i, _ in items)
        heading = first - 1
        while heading in drop:
            heading -= 1
        if heading < 0:
            continue
        hb, ht = block_texts[heading]
        if not (hasattr(hb, "tag") and _local(hb.tag) in ("h1", "h2", "h3", "h4", "h5", "h6")
                and _LIST_HEADING.search(ht)):
            continue
        for i, _ in items:
            drop.add(i)
            counts["illustration-list"] += 1
        drop.add(heading)
    return [p for i, p in enumerate(block_texts) if i not in drop], counts
