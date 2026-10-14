"""Where in a paragraph a Kobo page starts, as a time in the audiobook.

Read -> listen used to start the audio at the start of the paragraph holding
the top of the page. A page often opens mid-paragraph, so the audio replayed
the end of the previous page: 10-16 s on average (Redeeming Love, Twisted),
minutes after a long paragraph -- "five sentences back" (user, 2026-10-14).

KOReader names the page's first character exactly:

    /body/DocFragment[17]/body/div/div/p[64]/span[3]/text().12

and the map holds every narrated word's time and its place in the paragraph's
narrated text (`word_timings`, positions only: no text is stored). So this
reads the paragraph in the KEPUB the reader is reading, measures how far into
its text the page starts, and starts the audio at the narrated word that far
in, a couple of words early for safety. The narrated text is the paragraph's
own text with the narrator's spoken forms ("Dr." -> "Doctor"), so the fraction
lands within a word or two of the page's first word.

What this cannot know is how far down the page the reader got: the Kobo
reports the page, not the line. Any doubt -- a path the KEPUB does not have, a
paragraph with no word timings -- returns None and the caller keeps the
paragraph's start, the safe side.

Standard library only: the gateway's container has no lxml (requirements-gateway.txt).
Every file of the book, its OPF included, goes through html.parser, which
never expands an entity the book declares: the gateway reads books people
upload, and xml.etree would (bandit B314, the public repo's first scan).
"""
import posixpath
import re
import zipfile
from html.parser import HTMLParser

# Narrated words before the page's first one: covers the spoken-form drift
# between the page's text and what the narrator says. Measured on Redeeming
# Love and Twisted (1,965 page starts): the word found is the page's first
# 93% of the time, one word late in 6 cases, never more.
SAFETY_WORDS = 2
# A map without word timings (some Kokoro books): the same share of the
# paragraph's time, this much early, never before the paragraph's start.
# Speech is not spread evenly: against the real word times of Redeeming Love
# and Twisted (83,810 words) the share lands after the word in 1.8% of cases
# with 2 s to spare, 0.3% with 4 s.
SHARE_SAFETY_SECONDS = 4.0

_STEP = re.compile(r"^([A-Za-z_][\w.-]*)(?:\[(\d+)\])?$")
_TEXT = re.compile(r"^text\(\)(?:\[(\d+)\])?(?:\.(\d+))?$")
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
         "param", "source", "track", "wbr"}


class _Element:
    __slots__ = ("tag", "children", "parent", "attrs")

    def __init__(self, tag, parent=None, attrs=()):
        self.tag, self.children, self.parent = tag, [], parent
        self.attrs = {k.split(":")[-1].lower(): v for k, v in attrs}

    def elements(self, tag=None):
        return [c for c in self.children if isinstance(c, _Element) and (tag is None or c.tag == tag)]

    def text(self):
        return "".join(c if isinstance(c, str) else c.text() for c in self.children)


class _TreeBuilder(HTMLParser):
    """XHTML to a small tree that keeps text nodes in order, entities decoded."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Element("#document")
        self.current = self.root

    def handle_starttag(self, tag, attrs):
        el = _Element(tag.split(":")[-1].lower(), self.current, attrs)
        self.current.children.append(el)
        if el.tag not in _VOID:
            self.current = el

    def handle_startendtag(self, tag, attrs):
        self.current.children.append(_Element(tag.split(":")[-1].lower(), self.current, attrs))

    def handle_endtag(self, tag):
        tag = tag.split(":")[-1].lower()
        node = self.current
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:
            self.current = node.parent

    def handle_data(self, data):
        if self.current.children and isinstance(self.current.children[-1], str):
            self.current.children[-1] += data
        else:
            self.current.children.append(data)


def _parse(data):
    tree = _TreeBuilder()
    tree.feed(data.decode("utf-8", errors="replace"))
    return tree.root


def _spine(zf):
    """The book's documents in reading order: crengine's DocFragment[1..n]."""
    container = _parse(zf.read("META-INF/container.xml"))
    opf_path = next(el.attrs.get("full-path") for el in _iter(container) if el.tag == "rootfile")
    opf = _parse(zf.read(opf_path))
    base = posixpath.dirname(opf_path)
    items = {el.attrs.get("id"): el.attrs.get("href") for el in _iter(opf) if el.tag == "item"}
    hrefs = [items.get(el.attrs.get("idref")) for el in _iter(opf) if el.tag == "itemref"]
    return [posixpath.normpath(posixpath.join(base, h)) for h in hrefs if h]


def _resolve(body, steps):
    """The element at `steps` (["div", "p[10]", ...]) below <body>, or None."""
    el = body
    for step in steps:
        m = _STEP.match(step)
        if not m:
            return None
        same = el.elements(m.group(1).lower())
        index = int(m.group(2) or 1)
        if index > len(same):
            return None
        el = same[index - 1]
    return el


def _chars_before(paragraph, target, text_index, offset):
    """Characters of `paragraph`'s text before the point (target, text_index, offset)."""
    count = 0

    def walk(node):
        nonlocal count
        if node is target:
            seen = 0
            for child in node.children:
                if isinstance(child, str):
                    seen += 1
                    if seen == text_index:
                        count += min(offset, len(child))
                        return True
                    count += len(child)
                else:
                    count += len(child.text())
            return True
        for child in node.children:
            if isinstance(child, str):
                count += len(child)
            elif walk(child):
                return True
        return False

    return count if walk(paragraph) else None


def page_fraction(epub_path, position, paragraph_path):
    """How far into its paragraph's text a KOReader position falls, 0..1, or None.

    `paragraph_path` is the map's path for that paragraph (KOReader's form,
    `/body/DocFragment[n]/body/...`), an ancestor of `position`.
    """
    if not position.startswith(paragraph_path):
        return None
    m = re.match(r"^/body/DocFragment\[(\d+)\]/body((?:/[^/]+)*)$", position)
    p = re.match(r"^/body/DocFragment\[(\d+)\]/body((?:/[^/]+)*)$", paragraph_path)
    if not m or not p or m.group(1) != p.group(1):
        return None
    with zipfile.ZipFile(epub_path) as zf:
        spine = _spine(zf)
        index = int(m.group(1))
        if not 1 <= index <= len(spine):
            return None
        root = _parse(zf.read(spine[index - 1]))
    bodies = [el for el in _iter(root) if el.tag == "body"]
    if not bodies:
        return None
    steps = [s for s in m.group(2).split("/") if s]
    text_index, offset = 1, 0
    if steps and steps[-1].startswith("text()"):
        t = _TEXT.match(steps.pop())
        if not t:
            return None
        text_index, offset = int(t.group(1) or 1), int(t.group(2) or 0)
    paragraph = _resolve(bodies[0], [s for s in p.group(2).split("/") if s])
    target = _resolve(bodies[0], steps)
    if paragraph is None or target is None:
        return None
    total = len(paragraph.text())
    before = _chars_before(paragraph, target, text_index, offset)
    if not total or before is None:
        return None
    return min(1.0, before / total)


def _iter(node):
    for child in node.children:
        if isinstance(child, _Element):
            yield child
            yield from _iter(child)


def time_in_paragraph(nodes_words, fraction, safety_words=SAFETY_WORDS):
    """The start time of the narrated word `fraction` of the way through a paragraph.

    `nodes_words`: for each node of the paragraph in narration order (a long
    paragraph is split across chunks), its words as (char_start, char_end,
    start_time). Each node's offsets start at 0, so they are laid end to end.
    None when there are no words to go by.
    """
    laid, shift = [], 0
    for words in nodes_words:
        end = 0
        for char_start, char_end, start_time in words:
            if char_start is None or start_time is None:
                continue
            laid.append((char_start + shift, start_time))
            end = max(end, char_end or char_start)
        shift += end
    if not laid or shift <= 0:
        return None
    target = fraction * shift
    at = 0
    for i, (start, _) in enumerate(laid):
        if start > target:
            break
        at = i
    return laid[max(0, at - safety_words)][1]


def page_start_time(epub_path, position, paragraph_path, span, nodes_words):
    """Seconds where the page starts inside its paragraph, or None (keep the paragraph's start).

    `span` is the paragraph's (start, end) in the audio; `nodes_words` its
    words per node, as for time_in_paragraph.
    """
    try:
        fraction = page_fraction(epub_path, position, paragraph_path)
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, StopIteration):
        return None
    if not fraction:
        return None
    start, end = span
    found = time_in_paragraph(nodes_words, fraction)
    if found is None and end and end > start:
        found = start + fraction * (end - start) - SHARE_SAFETY_SECONDS
    if found is None:
        return None
    return max(start, min(found, end if end else found))
