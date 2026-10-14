"""Kokoro says the words its lexicon lacks the way a reader would.

"By Wilde", the first words of The Canterville Ghost, was heard "By Wilda"
(2026-10-12): Kokoro's lexicon has no "Wilde" and eSpeak NG says wˈIldə.
core/kokoro_words.SAY_LIKE teaches the narrator's lexicon such words
(server/cpu_tts.py, at load); the text and the sync map are untouched, and
the words are part of Kokoro's cache key so the old audio is not served.
Offline: a fake lexicon, then misaki's own G2P with its eSpeak fallback when
both are installed.
"""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from core import kokoro_words  # noqa: E402

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name} {detail}")


g2p = types.SimpleNamespace(lexicon=types.SimpleNamespace(golds={"wild": "wˈIld", "Wilde": None}))
del g2p.lexicon.golds["Wilde"]
kokoro_words.teach(g2p)
check("Wilde is said like wild", g2p.lexicon.golds.get("Wilde") == "wˈIld", f"({g2p.lexicon.golds})")
g2p = types.SimpleNamespace(lexicon=types.SimpleNamespace(golds={"wild": "wˈIld", "Wilde": "own"}))
kokoro_words.teach(g2p)
check("a word the lexicon already knows is not overridden", g2p.lexicon.golds["Wilde"] == "own")
kokoro_words.teach(types.SimpleNamespace())
check("a G2P without a lexicon (another language) is left alone", True)

try:
    from misaki import en, espeak
    real = en.G2P(trf=False, british=False, fallback=espeak.EspeakFallback(british=False))
except Exception as e:      # no misaki or no eSpeak NG here: the fake above is the test
    print(f"SKIP  misaki's own G2P ({str(e)[:60]})")
else:
    before = real("By Wilde...")[0]
    kokoro_words.teach(real)
    after, wild = real("By Wilde...")[0], real("By wild...")[0]
    check("misaki with eSpeak said it wrong before", before != wild, f"({before} vs {wild})")
    check("and says it like 'wild' after, Wilde's too",
          after == wild and real("Wilde's")[0] == real("wild's")[0], f"({after} vs {wild})")

# --- The cache key: a change of words is a change of audio ----------------
from core.tts_providers import KokoroProvider  # noqa: E402
provider = KokoroProvider()
before = provider.cache_identity()
saved = dict(kokoro_words.SAY_LIKE)
kokoro_words.SAY_LIKE["Featherstonehaugh"] = "fanshaw"
check("the words are in Kokoro's cache key: changing them re-renders",
      provider.cache_identity() != before and "words=" in before, f"({before})")
kokoro_words.SAY_LIKE.clear()
kokoro_words.SAY_LIKE.update(saved)
check("and the same words give the same key", provider.cache_identity() == before)

# --- The CPU server teaches them when it loads Kokoro -----------------------
class FakePipeline:
    """kokoro.KPipeline as far as the lexicon goes: English has one, others do not."""

    def __init__(self, lang_code, repo_id=None, device=None):
        self.g2p = types.SimpleNamespace(lexicon=types.SimpleNamespace(golds={"wild": "wˈIld"})) \
            if lang_code in ("a", "b") else types.SimpleNamespace()


real_kokoro = sys.modules.get("kokoro")
sys.modules["kokoro"] = types.SimpleNamespace(KPipeline=FakePipeline)
sys.path.insert(0, os.path.join(ROOT, "server"))
import cpu_tts  # noqa: E402
check("the CPU server's American and British pipelines say Wilde like wild",
      all(cpu_tts.pipeline(c, "cpu").g2p.lexicon.golds.get("Wilde") == "wˈIld" for c in ("a", "b")))
check("another language's pipeline loads untouched", not hasattr(cpu_tts.pipeline("f", "cpu").g2p, "lexicon"))
if real_kokoro is not None:
    sys.modules["kokoro"] = real_kokoro
else:
    del sys.modules["kokoro"]

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
