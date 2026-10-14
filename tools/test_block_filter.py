#!/usr/bin/env python3
"""The block filter deletes text, so the rule for acting on a verdict is tested alone.

Offline: verdicts are fabricated. What is under test is which blocks the rule
removes given those verdicts, and that every failure keeps the text.

Measured against the real model, for the record (tools/block_filter_report.py):
all 5,719 blocks of the eight books in the library -> 0 drops. pg1080 with the
Gutenberg trim switched OFF -> 0 of 33 essay blocks and 48 of 51 licence blocks
(96.6% of its words) dropped.

    python tools/test_block_filter.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import block_filter as B  # noqa: E402
from core.junk_provider import JUNK, KEEP, Verdict  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  ({detail})" if detail else ""))


def V(p):
    return Verdict(JUNK if p >= 0.85 else KEEP, "jev", p, {}, "kind=legal")


def drops(ps, **kw):
    d, iso = B.select_drops([V(p) if p is not None else None for p in ps], **kw)
    return sorted(d), sorted(iso)


# --- the run rule -------------------------------------------------------------
check("confident junk at the START of a document is dropped", drops([0.95, 0.1, 0.1, 0.1])[0] == [0])
check("confident junk at the END is dropped", drops([0.1, 0.1, 0.1, 0.97])[0] == [3])
check("a whole trailing run goes together", drops([0.1, 0.1, 0.93, 0.95, 0.99])[0] == [2, 3, 4])
check("a long run in the middle goes", drops([0.1, 0.95, 0.96, 0.97, 0.1])[0] == [1, 2, 3])
d, iso = drops([0.1, 0.1, 0.96, 0.1, 0.1])
check("ONE confident block mid-chapter is kept -- that is what a false positive looks like",
      d == [] and iso == [2], f"drop={d} isolated={iso}")
d, iso = drops([0.1, 0.96, 0.97, 0.1])
check("two mid-chapter are still kept (below MIN_RUN)", d == [] and iso == [1, 2])
check("below the threshold is never dropped, anywhere", drops([0.89, 0.89, 0.89])[0] == [])
check("a failed classification (None) breaks a run and is never dropped",
      drops([0.95, None, 0.1, 0.1])[0] == [0])
check("the threshold is honoured", drops([0.95, 0.1], threshold=0.99)[0] == [])


# --- filter_blocks: apply vs shadow, and failures -------------------------------
class FakeProvider:
    def __init__(self, ps, boom_at=None):
        self.ps, self.boom_at = ps, boom_at

    async def classify_block(self, text, before="", after="", tag="p", position_in_doc=0.0, position_in_book=0.0):
        i = int(text.split()[1])
        if self.boom_at is not None and i in self.boom_at:
            raise RuntimeError("network")
        return V(self.ps[i])


class El:
    tag = "p"


pairs = [(El(), f"block {i} text") for i in range(5)]
ps = [0.1, 0.1, 0.1, 0.95, 0.98]

kept, rep = asyncio.run(B.filter_blocks(FakeProvider(ps), pairs, 0.9, "ch9.xhtml", apply=True))
check("apply removes the trailing run", [t for _, t in kept] == ["block 0 text", "block 1 text", "block 2 text"])
check("and reports it", len(rep.dropped) == 2 and rep.dropped_words == 6, f"{len(rep.dropped)} blocks, {rep.dropped_words} words")

kept, rep = asyncio.run(B.filter_blocks(FakeProvider(ps), pairs, 0.9, "ch9.xhtml", apply=False))
check("shadow returns every block untouched", kept == pairs)
check("...but still reports what would go", len(rep.dropped) == 2)

kept, rep = asyncio.run(B.filter_blocks(FakeProvider(ps, boom_at={0, 1, 2, 3, 4}), pairs, 0.9, "x", apply=True))
check("a total outage keeps every block", kept == pairs and rep.failed)

kept, rep = asyncio.run(B.filter_blocks(FakeProvider(ps, boom_at={3}), pairs, 0.9, "x", apply=True))
check("one failed block is kept and splits nothing wrongly",
      [t for _, t in kept] == ["block 0 text", "block 1 text", "block 2 text", "block 3 text"],
      str([t for _, t in kept]))

# The opening credit: JEV scored Screwtape's "by" 0.85 as title-page furniture,
# and a run at the start of a document goes -- so the book opened on the author.
credit = [(El(), f"block {i} text") for i in range(6)]
cps = [0.95, 0.95, 0.95, 0.1, 0.1, 0.1]
kept, rep = asyncio.run(B.filter_blocks(FakeProvider(cps), credit, 0.0, "title.xhtml", apply=True))
check("without exemption the opening run is dropped", len(kept) == 3)
kept, rep = asyncio.run(B.filter_blocks(FakeProvider(cps), credit, 0.0, "title.xhtml", apply=True,
                                        exempt={0, 1, 2}))
check("exempt blocks survive whatever their score", kept == credit, str([t for _, t in kept]))
kept, rep = asyncio.run(B.filter_blocks(FakeProvider(cps), credit, 0.0, "title.xhtml", apply=True,
                                        exempt={1}))
check("an exempt block breaks the run it sits in",
      [t for _, t in kept] == [t for _, t in credit[1:]] and [i for i, *_ in rep.isolated] == [2],
      str([t for _, t in kept]))

# A heading belongs to its text: Redeeming Love's "ACKNOWLEDGMENTS" was dropped
# alone at a document's edge while the paragraph under it was kept.
class H1:
    tag = "h1"


headed = [(H1(), "block 0 text"), (El(), "block 1 text"), (El(), "block 2 text")]
kept, rep = asyncio.run(B.filter_blocks(FakeProvider([0.95, 0.1, 0.1]), headed, 0.5, "ack.htm", apply=True))
check("a heading is never dropped while its text is kept", kept == headed, str([t for _, t in kept]))
kept, rep = asyncio.run(B.filter_blocks(FakeProvider([0.1, 0.1, 0.95]), headed[:1] + headed[1:],
                                        0.5, "ack.htm", apply=True))
check("...a paragraph at the edge still goes", [t for _, t in kept] == ["block 0 text", "block 1 text"])
kept, rep = asyncio.run(B.filter_blocks(FakeProvider([0.95, 0.95, 0.95]), headed, 0.5, "ad.htm", apply=True))
check("...and a heading goes with the text it heads", kept == [])

kept, rep = asyncio.run(B.filter_blocks(FakeProvider(ps, boom_at={3}), pairs, 0.9, "x", apply=True))
lines = B.describe(rep, applied=True)
check("the log line names what was dropped", any("dropped 1 of 5" in l for l in lines), str(lines[:1]))

# --- mode ---------------------------------------------------------------------
for value, want in (("", "off"), ("shadow", "shadow"), ("ON", "on"), ("bogus", "off")):
    os.environ["JUNK_BLOCK_FILTER"] = value
    check(f"JUNK_BLOCK_FILTER={value!r} -> {want}", B.mode() == want)
os.environ.pop("JUNK_BLOCK_FILTER", None)

print("\n" + "=" * 60)
passed = sum(1 for r in results if r)
print(f"{len(results)} checks: {passed} passed, {len(results) - passed} FAILED")
sys.exit(0 if passed == len(results) else 1)
