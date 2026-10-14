#!/usr/bin/env python3
"""A chapter that opens on footnotes is still a chapter.

ai_brain judged a whole spine document from one 1,500-character window near its
start. Gutenberg's Worst Journey puts each chapter's footnotes at the top of the
next file, and Groq cut h-0, h-1, h-5 and h-6 (~20,000 words each) as notes in
3 runs of 3; Standard Ebooks' chapter 7 (29,340 words) opens on a table of
sledge weights and went the same way (2026-10-06). A long document is now read
at its start, middle and end, and cut only when most of it reads as junk.

Offline: the ML stack and Groq are fakes that answer by what a window holds.
    python tools/test_junk_windows.py
"""
import asyncio
import os
import sys
import types
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  ({detail})" if detail else ""))


NOTES = "[164] Scott's Last Expedition, vol. ii. p. 58. [165] Ibid. p. 441. [166] My own diary. " * 40
PROSE = ("We roped up, and started to worry along under the cliffs, which had now changed "
         "from ice to rock, and rose eight hundred feet above us. ") * 40


def looks_like_notes(text):
    return "Ibid" in text


class FakeClf:
    def load_model(self, path):
        pass

    def predict_proba(self, X):
        p = 0.9 if X[0][0] == 1.0 else 0.02     # grey for notes, keep for prose
        return np.array([[1 - p, p]])


fakes = {
    "groq": types.SimpleNamespace(Groq=lambda **k: object()),
    "xgboost": types.SimpleNamespace(XGBClassifier=FakeClf),
    "joblib": types.SimpleNamespace(load=lambda p: types.SimpleNamespace(transform=lambda a: a)),
    "sentence_transformers": types.SimpleNamespace(SentenceTransformer=lambda name, **kw: types.SimpleNamespace(
        encode=lambda texts, **k: [[1.0 if looks_like_notes(t) else 0.0, 0.0, 0.0] for t in texts])),
}
asked = []


async def fake_groq(snippet, rho, *a, **k):
    asked.append(snippet)
    return "JUNK" if looks_like_notes(snippet) else "KEEP"


with mock.patch.dict(sys.modules, fakes), mock.patch.dict(os.environ, {"GROQ_API_KEY": "x"}):
    import core.ai_brain as brain
    brain._BRAIN = None
    _sleep = asyncio.sleep
    brain.asyncio.sleep = lambda s: _sleep(0)     # the pause between Groq calls
    with mock.patch.object(brain, "ask_groq_fallback", fake_groq):
        def verdict(text):
            asked.clear()
            return asyncio.run(brain.evaluate_text_block(text, len(text.split()), 0.4, "test"))

        chapter = NOTES[:3000] + PROSE * 4          # opens on the previous chapter's notes
        check("a long chapter that opens on footnotes is kept", verdict(chapter) == "KEEP")
        check("it is read at three places", len(brain.windows(chapter)) == 3)
        check("all notes is still cut", verdict(NOTES * 3) == "JUNK", f"Groq asked {len(asked)}x")
        check("one window of prose at the end does not rescue notes",
              verdict(NOTES * 3 + PROSE[:2500]) == "JUNK")
        check("plain prose never reaches Groq", verdict(PROSE * 3) == "KEEP" and not asked)
        short = NOTES[:1400]
        check("a short document is judged as before, from one window",
              brain.windows(short) == [short] and verdict(short) == "JUNK" and len(asked) == 1)
        check("a middle-length one from characters 500-2000 as before",
              brain.windows("x" * 4000) == ["x" * 1500])

check("the local score keeps what the first window kept", brain.local_score([0.01, 0.9, 0.9]) == 0.01)
check("and takes the median of three otherwise", brain.local_score([0.9, 0.02, 0.5]) == 0.5)
check("most windows decide", brain.majority_junk(["JUNK", "KEEP", "KEEP"]) == "KEEP"
      and brain.majority_junk(["JUNK", "JUNK", "KEEP"]) == "JUNK" and brain.majority_junk(["JUNK"]) == "JUNK")

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
