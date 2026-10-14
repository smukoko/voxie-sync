"""Block-level junk filtering: the half of the problem a document verdict cannot see.

The junk filter judges a whole spine file at once, and that is structurally
unable to catch boilerplate that shares a file with the book. pg1080.epub put
the essay and the entire Project Gutenberg licence in one document -- 46% of a
finished audiobook was licence text -- and no verdict about the document as a
whole could have separated them. Gutenberg's markers rescue that one case;
they will not rescue a commercial EPUB whose copyright page, "also by this
author" list or sample chapter of another book sits in a file with real prose.

Asking about every block was never affordable before. JEV makes it about
$0.00003 a block, so a novel is cents.

What makes it safe is the rule for acting on a verdict, not the verdict. A
block this deletes is gone from the audiobook AND from the sync map, so the
bar is set by where boilerplate actually lives:

  - it clusters at the edges of a document (a copyright page at the front, a
    licence or an advert at the back), or
  - it comes in runs (a licence is dozens of paragraphs, not one).

So a block is dropped only when the model is confident AND it belongs to a
confident run that touches the start or end of its document, or to a run of
at least MIN_RUN blocks anywhere. A single "junk" verdict in the middle of a
chapter -- an epigraph, a letter the author quotes, a line of verse -- is
exactly what a false positive looks like, and it is kept and reported.

Modes, chosen with JUNK_BLOCK_FILTER:
  off     not asked at all (the default until it has been measured)
  shadow  asked, and every would-be drop printed, but nothing removed
  on      drops applied
"""
import asyncio
import os
from dataclasses import dataclass, field

BLOCK_THRESHOLD = float(os.getenv("JEV_BLOCK_THRESHOLD", "0.9"))
MIN_RUN = int(os.getenv("JEV_BLOCK_MIN_RUN", "3"))
CONTEXT_CHARS = 300
HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "header")


def mode():
    m = os.getenv("JUNK_BLOCK_FILTER", "off").strip().lower()
    return m if m in ("off", "shadow", "on") else "off"


@dataclass
class BlockReport:
    identifier: str
    blocks: int = 0
    dropped: list = field(default_factory=list)    # (index, confidence, kind, text)
    isolated: list = field(default_factory=list)   # confident junk kept by the run rule
    failed: str = ""

    @property
    def dropped_words(self):
        return sum(len(t.split()) for _, _, _, t in self.dropped)


def select_drops(verdicts, threshold=BLOCK_THRESHOLD, min_run=MIN_RUN):
    """Indices to drop, and indices of confident junk kept because they stand alone.

    Pure, so the rule can be tested without a model.
    """
    n = len(verdicts)
    confident = [v is not None and v.confidence is not None and v.confidence >= threshold
                 for v in verdicts]
    drop, isolated = set(), set()
    i = 0
    while i < n:
        if not confident[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and confident[j + 1]:
            j += 1
        run = range(i, j + 1)
        if i == 0 or j == n - 1 or len(run) >= min_run:
            drop.update(run)
        else:
            isolated.update(run)
        i = j + 1
    return drop, isolated


async def classify(provider, texts, tags, position_in_book):
    """One verdict per block, concurrently; None where the provider failed."""
    n = len(texts)

    async def one(i):
        try:
            return await provider.classify_block(
                texts[i],
                before=texts[i - 1][-CONTEXT_CHARS:] if i > 0 else "",
                after=texts[i + 1][:CONTEXT_CHARS] if i + 1 < n else "",
                tag=tags[i],
                position_in_doc=i / max(1, n - 1),
                position_in_book=position_in_book,
            )
        except Exception:
            return None

    return await asyncio.gather(*(one(i) for i in range(n)))


async def filter_blocks(provider, block_texts, position_in_book, identifier, apply, exempt=None):
    """Return (kept block_texts, BlockReport).

    `block_texts` is the engine's list of (element, text). `exempt` holds
    indices that must survive whatever their score. With apply=False the
    list comes back untouched and the report says what would have gone. A
    provider failure keeps every block: an outage must cost precision, never
    text.
    """
    report = BlockReport(identifier=identifier, blocks=len(block_texts))
    if not block_texts:
        return block_texts, report
    texts = [t for _, t in block_texts]
    tags = [getattr(el, "tag", "p") or "p" for el, _ in block_texts]
    try:
        verdicts = await classify(provider, texts, tags, position_in_book)
    except Exception as exc:
        report.failed = f"{type(exc).__name__}: {exc}"
        return block_texts, report

    if all(v is None for v in verdicts):
        report.failed = "every block classification failed"
        return block_texts, report

    # Exempt blocks (the book's opening credit) are never dropped and break any
    # run they sit in, exactly as a low score would.
    for i in (exempt or ()):
        if 0 <= i < len(verdicts):
            verdicts[i] = None
    drop, isolated = select_drops(verdicts)
    # A heading belongs to the text under it. Redeeming Love's read-through
    # found "ACKNOWLEDGMENTS" dropped alone at the edge of its document while
    # the paragraph it heads was kept -- the listener heard the text with no
    # title. A heading goes only with the block after it.
    for i in sorted(drop, reverse=True):
        if tags[i] in HEADING_TAGS and i + 1 < len(texts) and i + 1 not in drop:
            drop.discard(i)
            isolated.add(i)
    for i in sorted(drop):
        report.dropped.append((i, verdicts[i].confidence, verdicts[i].detail, texts[i]))
    for i in sorted(isolated):
        report.isolated.append((i, verdicts[i].confidence, verdicts[i].detail, texts[i]))

    if not apply or not drop:
        return block_texts, report
    return [bt for i, bt in enumerate(block_texts) if i not in drop], report


def describe(report, applied):
    """A few lines for the engine log, readable before any GPU is spent."""
    if report.failed:
        return [f"   ⚠️  block filter unavailable on {report.identifier} ({report.failed}); every block kept"]
    lines = []
    if report.dropped:
        verb = "dropped" if applied else "WOULD drop"
        lines.append(f"   🧽 {report.identifier}: block filter {verb} {len(report.dropped)} of "
                     f"{report.blocks} blocks ({report.dropped_words} words)")
        for i, p, kind, text in report.dropped[:3]:
            lines.append(f"        #{i:<4} p={p:.2f} {kind:18} {' '.join(text.split())[:70]!r}")
        if len(report.dropped) > 3:
            lines.append(f"        ... and {len(report.dropped) - 3} more")
    for i, p, kind, text in report.isolated[:2]:
        lines.append(f"   🤔 {report.identifier}: kept isolated block #{i} (p={p:.2f}, {kind}): "
                     f"{' '.join(text.split())[:60]!r}")
    return lines
