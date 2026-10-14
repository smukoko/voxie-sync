"""A junk filter that fails costs its verdict, never the book (CLAUDE.md).

On 2026-10-07 the local classifier could not load its sentence-transformer:
the Hugging Face hub's HTTP client was closed, and loading the model by name
asks the hub about its files on every run, cached or not. Each chapter's
classify() raised, the cartographer skipped each one as an error, and the
book ended with "Nothing to narrate". Now a chapter the filter cannot judge
is kept and said to be kept, and the model loads from the local cache first.

Offline: the cartographer runs on a generated EPUB with fake classifiers.
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
from core import ai_brain  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


CHAPTERS = [
    ("The Harbour", "The morning boat came in late, and the harbour was already full of people waiting "
                    "for news. Anna stood at the end of the wooden pier with her coat pulled tight against "
                    "the cold wind, and watched the grey water for a sail she had waited a year to see."),
    ("The Letter", "Inside the parcel there was a letter, a pressed flower and a map drawn in careful blue "
                   "ink. Her brother wrote that he had found work on a farm beyond the hills, and that he "
                   "was well, and that she should follow the river road until a white house came in sight."),
]


def make_epub(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:'
                   'container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/'
                   'oebps-package+xml"/></rootfiles></container>')
        items = "".join(f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>'
                        for i in range(1, len(CHAPTERS) + 1))
        spine = "".join(f'<itemref idref="c{i}"/>' for i in range(1, len(CHAPTERS) + 1))
        nav = "".join(f'<li><a href="c{i}.xhtml">{t}</a></li>' for i, (t, _) in enumerate(CHAPTERS, 1))
        z.writestr("OEBPS/content.opf",
                   '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" '
                   'version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   '<dc:identifier id="id">urn:uuid:junk-outage</dc:identifier><dc:title>The Red Door</dc:title>'
                   '<dc:language>en</dc:language></metadata><manifest>'
                   '<item id="nav" href="nav.xhtml" properties="nav" media-type="application/xhtml+xml"/>'
                   f'{items}</manifest><spine>{spine}</spine></package>')
        z.writestr("OEBPS/nav.xhtml",
                   '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" '
                   'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Contents</title></head><body>'
                   f'<nav epub:type="toc"><ol>{nav}</ol></nav></body></html>')
        for i, (title, text) in enumerate(CHAPTERS, 1):
            z.writestr(f"OEBPS/c{i}.xhtml",
                       '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
                       f'<head><title>{title}</title></head><body><h1>{title}</h1><p>{text}</p></body></html>')


class Filter:
    def __init__(self, verdict=None, error=None):
        self.name, self.verdict, self.error, self.asked = "fake", verdict, error, 0

    async def classify(self, text, word_count, position_ratio, identifier=""):
        self.asked += 1
        if self.error:
            raise self.error
        return SimpleNamespace(verdict=self.verdict)

    async def aclose(self):
        pass


def plan_with(junk):
    """(chunk texts, printed output) of the cartographer with this junk filter."""
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


said, log = plan_with(Filter(verdict="KEEP"))
check("(control) a working filter that keeps both chapters narrates them",
      "wooden pier" in said and "blue ink" in said, log[-300:])
said, log = plan_with(Filter(verdict="JUNK"))
check("(control) a working filter that calls both junk narrates neither", "wooden pier" not in said, said[:80])

broken = Filter(error=RuntimeError("Cannot send a request, as the client has been closed."))
said, log = plan_with(broken)
check("a filter that fails on every chapter still leaves the book narrated",
      "wooden pier" in said and "blue ink" in said, log[-400:])
check("each chapter says it was kept without a verdict", log.count("kept without a verdict") == 2)
check("the plan names what was kept unjudged, before anything bills",
      "kept WITHOUT a junk verdict" in log and "c1.xhtml" in log.split("WITHOUT a junk verdict")[1])
check("the filter was asked about every chapter", broken.asked == 2, f"({broken.asked})")

# --- The model loads from the local cache; the network only the first time --------
calls = []


def cached(name, local_files_only=False):
    calls.append(local_files_only)
    return "model"


check("a cached model is loaded with no network at all",
      ai_brain.load_embedder(loader=cached) == "model" and calls == [True], str(calls))
calls.clear()


def first_time(name, local_files_only=False):
    calls.append(local_files_only)
    if local_files_only:
        raise OSError("couldn't find them in the cached files")
    return "downloaded"


check("a model not cached yet is downloaded, once",
      ai_brain.load_embedder(loader=first_time) == "downloaded" and calls == [True, False], str(calls))


def broken_model(name, local_files_only=False):
    raise ValueError("corrupt weights")


try:
    ai_brain.load_embedder(loader=broken_model)
    check("any other failure is not hidden behind a download", False)
except ValueError:
    check("any other failure is not hidden behind a download", True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
