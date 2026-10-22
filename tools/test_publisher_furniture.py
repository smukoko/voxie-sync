"""A publisher's sign-up page is recognised by its words, with no classifier (2026-10-22).

Winning's audiobook opened "Thank you for downloading this Simon & Schuster
ebook. Get a FREE ebook when you join our mailing list... click here to sign
up", and closed on the same page again. The classifier that should have cut
them had no model on that install (tools/test_export_keeps_models.py), and the
book declares no landmark for them. Recognising them from their own words is a
fact, not a guess, so it comes first and needs nothing.

Tested on what it must NOT delete as hard as on what it must: a short chapter
in the middle of a book that says "sign up" or "newsletter" is the author's.
Offline: the cartographer runs on a generated EPUB.
"""
import asyncio
import contextlib
import io
import os
import sys
import tempfile
import zipfile
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.update(TTS_PROVIDER="kokoro", NARRATION_DIRECTOR="off", JUNK_BLOCK_FILTER="off")
os.environ.setdefault("RUNPOD_API_KEY", "test-only")

import librerian_engine_v3 as eng  # noqa: E402
from core.structure import publisher_furniture  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


# The two pages as Winning prints them, and its "Also by" page.
FRONT = ["Simon & Schuster Front Sign-up Page",
         "Thank you for downloading this Simon & Schuster ebook.",
         "Get a FREE ebook when you join our mailing list. Plus, get updates on new releases, deals, "
         "recommended reads, and more from Simon & Schuster. Click below to sign up and see terms and "
         "conditions.", "CLICK HERE TO SIGN UP",
         "Already a subscriber? Provide your email again so we can register this ebook and send you "
         "more of what you like to read. You will continue to receive exclusive offers in your inbox."]
BACK = ["Simon & Schuster Back Sign-up Page"] + ["We hope you enjoyed reading this Simon & Schuster ebook."] + FRONT[2:]
ALSO = ["Also by the Author", "ALSO BY TIM S. GROVER",
        "Relentless: From Good to Great to Unstoppable",
        "Jump Attack: The Formula for Explosive Athletic Performance, Jumping Higher, and Training Like the Pros"]

check("the front sign-up page is the publisher's", publisher_furniture(FRONT, 0.0) == "the publisher's sign-up page")
check("the back sign-up page is the publisher's", publisher_furniture(BACK, 1.0) == "the publisher's sign-up page")
check("'Also by the author' is a list of other books", "other books" in publisher_furniture(ALSO, 0.95))

# Must NOT be cut.
TRAP = ["She wrote a newsletter for the village every month, and everyone was meant to sign up for it "
        "at the baker's, but nobody did. The vicar subscribed out of pity."]
check("a short chapter in the middle that says newsletter and sign up is kept",
      publisher_furniture(TRAP, 0.5) == "")
check("...even at an edge, one ordinary cue alone is not enough",
      publisher_furniture(["He decided to sign up for the army the next morning, and told no one."], 0.0) == "")
check("a long front-matter chapter with cues is kept (the rule is for short pages)",
      publisher_furniture(["newsletter sign up " + "word " * 300], 0.0) == "")
check("a dedication is not furniture", publisher_furniture(["For Shari Wenk, who understood."], 0.0) == "")
check("a document that merely starts 'Also' is kept",
      publisher_furniture(["Also, the harbour was quiet that morning, and she was glad of it."], 0.95) == "")
check("nothing at all is not furniture", publisher_furniture([], 0.0) == "")


# --- Through the cartographer, with the classifier down --------------------------------
def doc(title, paras):
    return (title, paras)


DOCS = [
    doc("Sign-up", FRONT),
    doc("The Harbour", ["The morning boat came in late, and the harbour was already full of people waiting "
                        "for news. Anna stood at the end of the wooden pier with her coat pulled tight against "
                        "the cold wind, and watched the grey water for a sail she had waited a year to see."]),
    doc("The Newsletter", TRAP),
    doc("The Letter", ["Inside the parcel there was a letter, a pressed flower and a map drawn in careful blue "
                       "ink. Her brother wrote that he had found work on a farm beyond the hills, and that he "
                       "was well, and that she should follow the river road until a white house came in sight."]),
    doc("Sign-up again", BACK),
]


def make_epub(path):
    n = len(DOCS)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:'
                   'container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/'
                   'oebps-package+xml"/></rootfiles></container>')
        items = "".join(f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>'
                        for i in range(1, n + 1))
        spine = "".join(f'<itemref idref="c{i}"/>' for i in range(1, n + 1))
        # Only the real chapters are in the table of contents, as in a real book.
        nav = "".join(f'<li><a href="c{i}.xhtml">{DOCS[i - 1][0]}</a></li>' for i in (2, 3, 4))
        z.writestr("OEBPS/content.opf",
                   '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" '
                   'version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   '<dc:identifier id="id">urn:uuid:furniture</dc:identifier><dc:title>The Red Door</dc:title>'
                   '<dc:language>en</dc:language></metadata><manifest>'
                   '<item id="nav" href="nav.xhtml" properties="nav" media-type="application/xhtml+xml"/>'
                   f'{items}</manifest><spine>{spine}</spine></package>')
        z.writestr("OEBPS/nav.xhtml",
                   '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" '
                   'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Contents</title></head><body>'
                   f'<nav epub:type="toc"><ol>{nav}</ol></nav></body></html>')
        for i, (title, paras) in enumerate(DOCS, 1):
            body = "".join(f"<p>{p.replace('&', '&amp;')}</p>" for p in paras)
            z.writestr(f"OEBPS/c{i}.xhtml",
                       '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
                       f'<head><title>{title}</title></head><body>{body}</body></html>')


class Filter:
    name = "fake"

    def __init__(self, verdict=None, error=None):
        self.verdict, self.error, self.asked = verdict, error, []

    async def classify(self, text, word_count, position_ratio, identifier=""):
        self.asked.append(identifier)
        if self.error:
            raise self.error
        return SimpleNamespace(verdict=self.verdict)

    async def aclose(self):
        pass


def plan_with(junk):
    work = tempfile.mkdtemp()
    epub = os.path.join(work, "book.kepub.epub")
    make_epub(epub)
    queue, out = asyncio.Queue(), io.StringIO()
    real = eng.get_junk_provider
    eng.get_junk_provider = lambda: junk
    try:
        with contextlib.redirect_stdout(out):
            asyncio.run(eng.cartographer_producer(epub, queue, book_id=1, language="en"))
    finally:
        eng.get_junk_provider = real
    texts = []
    while not queue.empty():
        texts.append(queue.get_nowait()["text"])
    return " ".join(texts), out.getvalue()


for label, junk in (("a classifier that is down", Filter(error=RuntimeError("no model"))),
                    ("a classifier that keeps everything", Filter(verdict="KEEP"))):
    said, log = plan_with(junk)
    check(f"{label}: the sign-up pages are not narrated",
          "mailing list" not in said and "Thank you for downloading" not in said
          and "hope you enjoyed" not in said, said[:120])
    check(f"{label}: the book's own chapters are", "wooden pier" in said and "blue ink" in said)
    check(f"{label}: the short chapter that says newsletter is", "newsletter for the village" in said)
    check(f"{label}: the log says what was cut and why",
          log.count("the publisher's sign-up page") == 2, log[-400:])
    check(f"{label}: the classifier was never asked about the pages",
          not any(h.endswith(("c1.xhtml", "c5.xhtml")) for h in junk.asked), str(junk.asked))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
